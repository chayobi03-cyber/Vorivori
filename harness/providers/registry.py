#!/usr/bin/env python3
"""프로바이더 라우팅 — 어느 AI에게 보낼지 정한다.

원본 §8.2가 고정 파이프라인인 이유와 그 한계
    문서는 "GPT → Claude → Gemini CLI" 순서를 못박는다. 아키텍처 작업에는
    맞지만 "dBm을 와트로 환산" 같은 것도 3단계를 태운다. 작업 유형별 배치가
    실제에 가깝다.

두 축으로 정한다
    role     작업 카테고리가 요구하는 역할 (architect / implement / review / …)
    escalate 싼 모델로 먼저 시도하고, 실패하면 비싼 모델로 올린다

라우팅 규칙은 `artifacts/provider.routing.json`에 있다 — 즉 GEPA 최적화
대상이다. "Claude가 리팩터링을 잘한다"는 인상이지만, 어느 배치가 재시도를
적게 유발했는지는 측정할 수 있다.

**모든 선택은 PolicyEngine을 통과한다.** 사내 프로파일에서 클라우드
프로바이더가 선택되면 여기서 걸린다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "artifacts" / "provider.routing.json"


@dataclass
class Choice:
    provider: str | None
    role: str
    reason: str
    transport: str = ""
    model: str = ""
    blocked: list[tuple[str, str]] = None  # (프로바이더, 차단 사유)

    def __post_init__(self):
        if self.blocked is None:
            self.blocked = []

    @property
    def ok(self) -> bool:
        return self.provider is not None

    def __str__(self) -> str:
        if not self.ok:
            return f"[선택 불가] role={self.role} — {self.reason}"
        tag = "로컬" if self.transport == "local" else "클라우드"
        return f"{self.provider} ({tag}{'/' + self.model if self.model else ''}) — {self.reason}"


class ProviderRouter:
    def __init__(self, artifact: dict[str, Any], policy):
        self.artifact = artifact
        self.policy = policy
        self.category_roles: dict[str, list[str]] = artifact["category_roles"]
        self.default_roles: list[str] = artifact.get("default_roles", ["implement"])
        self.escalation: list[str] = artifact.get("escalation", [])
        self._validate()

    @staticmethod
    def load(policy, path: str | Path = DEFAULT_ARTIFACT) -> "ProviderRouter":
        return ProviderRouter(json.loads(Path(path).read_text(encoding="utf-8")), policy)

    def _validate(self) -> None:
        for cat, roles in self.category_roles.items():
            if not roles:
                raise ValueError(f"category_roles[{cat}]가 비어 있다")
        if not self.default_roles:
            raise ValueError("default_roles가 비어 있다")

    def _candidates(self, role: str) -> list[tuple[str, dict]]:
        """해당 역할을 맡을 수 있는 프로바이더. 로컬 우선으로 정렬한다.

        로컬을 앞에 두는 이유는 비용이 아니라 **데이터가 나가지 않기 때문**이다.
        개인판에서도 로컬로 되는 일을 굳이 클라우드로 보내지 않는다.
        """
        out = [(n, p) for n, p in self.policy.profile.providers.items() if p.get("role") == role]
        return sorted(out, key=lambda np: (np[1].get("transport") != "local", np[0]))

    def select(self, category: str, payload: str = "", attempt: int = 0) -> Choice:
        """카테고리에 맞는 프로바이더를 고른다.

        attempt > 0 이면 승급 사다리를 탄다 — 앞 시도가 실패했다는 뜻이다.
        """
        roles = self.category_roles.get(category, self.default_roles)
        if attempt > 0 and self.escalation:
            idx = min(attempt - 1, len(self.escalation) - 1)
            roles = [self.escalation[idx]] + roles

        blocked: list[tuple[str, str]] = []
        for role in roles:
            for name, prov in self._candidates(role):
                verdict = self.policy.check_provider(name, payload)
                if verdict.allowed:
                    why = f"role={role}" + (f", 시도 {attempt + 1}회차 승급" if attempt else "")
                    return Choice(name, role, why, prov.get("transport", ""),
                                  prov.get("model", ""), blocked)
                blocked.append((name, verdict.reason))

        return Choice(None, roles[0],
                      f"role={roles} 을 맡을 수 있는 프로바이더가 없거나 전부 정책에 막혔다",
                      blocked=blocked)

    def plan(self, category: str) -> list[Choice]:
        """승급 사다리 전체를 미리 보여준다. 사용자가 비용을 예측할 수 있게."""
        seen: set[str] = set()
        out: list[Choice] = []
        for attempt in range(len(self.escalation) + 1):
            c = self.select(category, attempt=attempt)
            if c.ok and c.provider not in seen:
                seen.add(c.provider)
                out.append(c)
        return out


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.policy.engine import PolicyEngine, Profile

    for prof in ("profiles/enterprise.json", "profiles/personal.json"):
        pol = PolicyEngine(Profile.load(prof))
        r = ProviderRouter.load(pol)
        print(f"\n=== {pol.profile.name} ({pol.profile.edition}) ===")
        for cat in ("PYTHON_TOOL", "EMC_ANALYSIS", "DOCUMENTATION", "INFRA_OPERATION"):
            print(f"  {cat:18} {r.select(cat)}")
