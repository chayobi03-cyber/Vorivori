#!/usr/bin/env python3
"""자가 수정 실행 루프 — 노드 정의와 재시도 전략.

원본 §9.3의 루프는 이렇다.

    for attempt in range(3):
        result = run_in_sandbox(code)
        if result.success: return result
        code = ai_fix(code, result.stderr)
    raise ExecutionFailed()

세 가지를 더한다.

1. **재시도마다 프로바이더를 승급시킨다.** 같은 모델에 같은 에러를 다시 주면
   같은 실패가 반복된다. 다른 관점이 필요하다.
2. **재시도 전략이 아티팩트에 있다.** `artifacts/retry.policy.json`.
   즉 GEPA 최적화 대상이다 — 원본 §19가 "Retry Strategy"를 최적화 대상으로
   들었는데, 파이썬 for문 안에 있으면 손댈 수 없다.
3. **재시도해도 소용없는 실패를 구분한다.** `ModuleNotFoundError`는 코드를
   고쳐서 풀리지 않는다(`network:none`이라 설치가 불가능하다). 이런 것은
   즉시 중단하고 사람에게 알린다 — 3회 태우는 것이 낭비다.

Fixer는 주입된다. 기본 Fixer는 LLM 없이 동작하므로 폐쇄망·CI에서 루프 전체가
돈다. 실제 LLM 연결은 `CliFixer`가 담당한다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from harness.execute.machine import Ctx, Machine, Node, State
from harness.execute.sandbox import Backend, RunResult
from harness.sandbox.policy import SandboxPolicy

DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "artifacts" / "retry.policy.json"


class Fixer(Protocol):
    name: str

    def fix(self, code: str, error: str, ctx: Ctx) -> str | None: ...


# ─────────────────────────────────────────────────────────────
class NullFixer:
    """수정하지 않는다. 루프 구조를 시험하기 위한 기준선."""
    name = "null"

    def fix(self, code: str, error: str, ctx: Ctx) -> str | None:
        return None


FIX_PROMPT = """\
아래 파이썬 코드가 샌드박스에서 실패했다. 고쳐라.

## 코드
```python
{code}
```

## 에러
```
{error}
```

## 제약
- 샌드박스는 network=none 이다. 패키지 설치·네트워크 접근은 **불가능**하다.
- 사용 가능한 패키지: {packages}
- 파일 쓰기는 /tmp 와 /work 에서만 된다.

## 출력
수정된 코드 전체를 파이썬 코드 블록 하나로만 출력하라. 설명 없이.
"""


class CliFixer:
    """외부 AI CLI로 코드를 고친다. 프롬프트는 stdin으로만 넘긴다."""

    def __init__(self, name: str, argv: Sequence[str], timeout: int = 120):
        self.name = name
        self.argv = list(argv)
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.argv[0]) is not None

    def fix(self, code: str, error: str, ctx: Ctx) -> str | None:
        if not self.available():
            return None
        from harness.sandbox.policy import BAKED_PACKAGES
        prompt = FIX_PROMPT.format(code=code, error=error,
                                   packages=", ".join(BAKED_PACKAGES))
        try:
            p = subprocess.run(self.argv, input=prompt, capture_output=True,
                               text=True, timeout=self.timeout, shell=False)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if p.returncode != 0:
            return None
        return extract_code(p.stdout)


def extract_code(text: str) -> str | None:
    m = re.search(r"```(?:python|py)?\s*\n(.+?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 펜스 없이 코드만 온 경우
    t = text.strip()
    return t if t and not t.startswith("죄송") else None


# ─────────────────────────────────────────────────────────────
@dataclass
class RetryPolicy:
    max_attempts: int = 3
    escalate_provider: bool = True
    fatal_patterns: tuple[str, ...] = ()
    fatal_reason: dict[str, str] = None

    @staticmethod
    def load(path: str | Path = DEFAULT_ARTIFACT) -> "RetryPolicy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return RetryPolicy(
            max_attempts=int(raw.get("max_attempts", 3)),
            escalate_provider=bool(raw.get("escalate_provider", True)),
            fatal_patterns=tuple(raw.get("fatal", {}).keys()),
            fatal_reason=dict(raw.get("fatal", {})),
        )

    def is_fatal(self, error: str) -> str | None:
        """재시도해도 소용없는 실패인가. 그렇다면 사람이 읽을 이유를 준다."""
        for pat in self.fatal_patterns:
            if re.search(pat, error, re.IGNORECASE):
                return self.fatal_reason.get(pat, pat)
        return None


# ─────────────────────────────────────────────────────────────
def build_machine(*, backend: Backend, fixer: Fixer, router, providers,
                  retry: RetryPolicy, sandbox_policy: SandboxPolicy | None = None,
                  codegen=None, store=None) -> Machine:
    """§7.1 상태 머신을 조립한다.

    `codegen(ctx) -> str` 은 실제 코드 생성기다. 주입 가능하게 둔 이유는
    LLM 없이 루프 전체를 시험할 수 있어야 하기 때문이다.
    """
    pol = sandbox_policy or SandboxPolicy()

    def intake(ctx: Ctx) -> State:
        ctx.note(State.INTAKE, f"입력 {len(ctx.text)}자")
        if store is not None:
            store.log("text.command", {"text": ctx.text},
                      session_id=ctx.session_id, project=ctx.project)
        return State.CLASSIFY

    def classify(ctx: Ctx) -> State:
        c = router.classify(ctx.text)
        ctx.category, ctx.confident = c.category, c.confident
        ctx.note(State.CLASSIFY, f"{c.category} (격차 {c.margin})")
        if not c.confident:
            # 확신 없이 실행에 들어가지 않는다. 틀린 자신감보다 되묻는 게 낫다.
            ctx.note(State.CLASSIFY, f"확신 부족 — 2위 {c.runner_up}")
            return State.NEEDS_INPUT
        return State.PLAN

    def plan(ctx: Ctx) -> State:
        choice = providers.select(ctx.category, payload=ctx.text, attempt=ctx.attempt)
        if not choice.ok:
            ctx.last_error = choice.reason
            ctx.note(State.PLAN, f"프로바이더 없음: {choice.reason}")
            return State.FAILED
        ctx.provider = choice.provider
        ctx.plan = f"{ctx.category} → {choice.provider} ({choice.role})"
        ctx.note(State.PLAN, ctx.plan)
        return State.EXECUTE

    def execute(ctx: Ctx) -> State:
        """부작용 노드. 컨테이너를 만들고 코드를 돌린다 — 중단하지 않는다."""
        if not ctx.code:
            if codegen is None:
                ctx.last_error = "코드 생성기가 없다"
                return State.FAILED
            ctx.code = codegen(ctx)
        res: RunResult = backend.run(ctx.code, pol)
        ctx.result = res
        ctx.note(State.EXECUTE, f"{backend.name} exit={res.exit_code}",
                 attempt=ctx.attempt)
        if store is not None:
            store.log("sandbox.result",
                      {"ok": res.ok, "exit_code": res.exit_code,
                       "backend": res.backend, "attempt": ctx.attempt,
                       "isolated": backend.is_isolation_boundary},
                      origin="system", session_id=ctx.session_id, project=ctx.project)
        return State.VERIFY

    def verify(ctx: Ctx) -> State:
        res: RunResult = ctx.result
        if not res.available:
            ctx.last_error = res.error
            ctx.note(State.VERIFY, f"실행 불가: {res.error}")
            return State.FAILED
        if res.ok:
            ctx.note(State.VERIFY, "통과")
            return State.COMMIT
        ctx.last_error = res.feedback()
        fatal = retry.is_fatal(ctx.last_error)
        if fatal:
            # 재시도로 풀리지 않는 실패. 3회 태우지 않는다.
            ctx.note(State.VERIFY, f"재시도 불가: {fatal}")
            return State.FAILED
        if ctx.attempt + 1 >= retry.max_attempts:
            ctx.note(State.VERIFY, f"{retry.max_attempts}회 시도 소진")
            return State.FAILED
        return State.REFLECT

    def reflect(ctx: Ctx) -> State:
        ctx.attempt += 1
        fixed = fixer.fix(ctx.code, ctx.last_error, ctx)
        if fixed is None:
            ctx.note(State.REFLECT, f"{fixer.name}가 수정안을 내지 못했다")
            return State.FAILED
        if fixed.strip() == ctx.code.strip():
            # 같은 코드를 다시 돌리면 같은 결과가 나온다. 무한 루프 방지.
            ctx.note(State.REFLECT, "수정안이 원본과 동일 — 중단")
            return State.FAILED
        ctx.code = fixed
        ctx.note(State.REFLECT, f"{fixer.name} 수정 (시도 {ctx.attempt + 1}회차)")
        # PLAN으로 돌아가 프로바이더를 승급시킨다.
        return State.PLAN if retry.escalate_provider else State.EXECUTE

    def commit(ctx: Ctx) -> State:
        """부작용 노드. 결과를 원장에 남긴다 — 중단하지 않는다."""
        ctx.note(State.COMMIT, f"완료 (시도 {ctx.attempt + 1}회)")
        if store is not None:
            store.log("task.updated",
                      {"kind": "committed", "category": ctx.category,
                       "attempts": ctx.attempt + 1, "provider": ctx.provider},
                      origin="system", session_id=ctx.session_id, project=ctx.project)
        return State.DONE

    return Machine([
        Node(State.INTAKE, intake),
        Node(State.CLASSIFY, classify),
        Node(State.PLAN, plan),
        Node(State.EXECUTE, execute, interruptible=False),   # 부작용
        Node(State.VERIFY, verify),
        Node(State.REFLECT, reflect),
        Node(State.COMMIT, commit, interruptible=False),     # 부작용
    ], store=store)
