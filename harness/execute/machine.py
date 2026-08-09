#!/usr/bin/env python3
"""실행 상태 머신 — INTERRUPT를 일급 전이로 갖는다.

왜 LangGraph가 아닌가
    원본 명세는 LangGraph를 든다. 그런데 이 저장소는 표준 라이브러리만 쓴다
    (폐쇄망 반입 절차를 통과해야 하고, Ainative와 같은 제약을 공유한다).

    그리고 정작 필요한 것 — **barge-in 중단 의미론** — 은 LangGraph가 거저
    주지 않는다. 직접 만들면 200줄이고, 중단 규칙을 우리가 정할 수 있다.
    의존성 하나를 아끼는 것보다 이쪽이 중요하다.

원본 §7.1이 빠뜨린 것
    문서의 상태 머신은 이렇다.

        START → INTAKE → CLASSIFY → PLAN → EXECUTE → VERIFY → COMMIT
                                                        └ 실패 → REFLECT → RETRY

    §8 이슈3이 barge-in 교착을 정확히 짚었는데, 정작 상태 머신에는 인터럽트가
    없다. 여기서 두 가지를 넣는다.

    1. **어느 노드에서든 INTERRUPT로 갈 수 있다.**
    2. **부작용 노드는 중단되지 않는다.** 파일 쓰기·명령 실행·일정 생성이
       반쯤 끝난 상태로 멈추면 되돌릴 수 없다. 그런 구간에서는 인터럽트를
       큐에 넣고 구간이 끝난 직후에 처리한다.

    그리고 중단은 **취소가 아니라 보류**다. 취소하면 사용자가 "아까 하던 거"로
    돌아올 수 없다. 체크포인트를 남기고 `parked`로 간다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class State(str, Enum):
    INTAKE = "INTAKE"
    CLASSIFY = "CLASSIFY"
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    REFLECT = "REFLECT"
    COMMIT = "COMMIT"
    # 종료 상태
    DONE = "DONE"
    FAILED = "FAILED"
    PARKED = "PARKED"      # 인터럽트로 보류됨. 재개 가능
    NEEDS_INPUT = "NEEDS_INPUT"  # 되물어야 함


TERMINAL = {State.DONE, State.FAILED, State.PARKED, State.NEEDS_INPUT}


@dataclass
class Ctx:
    """노드 사이를 흐르는 상태. 체크포인트로 직렬화된다."""
    text: str
    session_id: str = "default"
    project: str | None = None
    category: str = ""
    confident: bool = True
    plan: str = ""
    code: str = ""
    attempt: int = 0
    max_attempts: int = 3
    provider: str = ""
    last_error: str = ""
    result: Any = None
    trace: list[dict] = field(default_factory=list)
    # 사용자가 인터럽트하며 넘긴 새 지시
    interrupt_text: str = ""

    def note(self, state: State, msg: str, **extra) -> None:
        self.trace.append({"state": state.value, "msg": msg, "t": round(time.time(), 3), **extra})

    def checkpoint(self) -> dict:
        return {"text": self.text, "session_id": self.session_id, "project": self.project,
                "category": self.category, "plan": self.plan, "code": self.code,
                "attempt": self.attempt, "provider": self.provider,
                "last_error": self.last_error[:500]}


@dataclass
class Node:
    state: State
    fn: Callable[[Ctx], State]
    # 부작용이 있는 노드는 중단되지 않는다. 반쯤 끝난 쓰기는 되돌릴 수 없다.
    interruptible: bool = True


@dataclass
class Outcome:
    final: State
    ctx: Ctx
    interrupted: bool = False
    checkpoint: dict | None = None
    steps: int = 0

    @property
    def ok(self) -> bool:
        return self.final is State.DONE


class Machine:
    """노드 그래프를 돈다.

    `interrupt_check`는 매 전이 직전에 호출된다. 음성 경로에서는 VAD가
    사용자 발화를 감지했는지를 반환하게 된다. 지금은 주입 가능한 콜백이라
    시험에서 결정적으로 재현된다.
    """

    def __init__(self, nodes: list[Node], start: State = State.INTAKE,
                 max_steps: int = 40, store=None):
        self.nodes = {n.state: n for n in nodes}
        self.start = start
        self.max_steps = max_steps
        self.store = store

    def run(self, ctx: Ctx, interrupt_check: Callable[[], bool] | None = None) -> Outcome:
        state = self.start
        pending_interrupt = False
        steps = 0

        while state not in TERMINAL:
            if steps >= self.max_steps:
                ctx.note(state, "최대 스텝 초과 — 루프 의심")
                return Outcome(State.FAILED, ctx, steps=steps)
            steps += 1

            node = self.nodes.get(state)
            if node is None:
                ctx.note(state, "정의되지 않은 노드")
                return Outcome(State.FAILED, ctx, steps=steps)

            # 인터럽트 확인은 노드 실행 **전**에 한다. 실행 중 중단은
            # 부작용을 반쯤 남기므로 하지 않는다.
            if interrupt_check is not None and interrupt_check():
                pending_interrupt = True

            if pending_interrupt:
                if node.interruptible:
                    ctx.note(state, "인터럽트 — 보류(취소 아님)")
                    cp = ctx.checkpoint()
                    self._log(ctx, "parked", state, cp)
                    return Outcome(State.PARKED, ctx, interrupted=True,
                                   checkpoint=cp, steps=steps)
                # 중단 불가 구간. 큐에 두고 이 노드는 끝까지 돌린다.
                ctx.note(state, "인터럽트 접수 — 중단 불가 구간이라 완료 후 처리")

            try:
                nxt = node.fn(ctx)
            except Exception as e:  # 노드가 죽어도 머신은 상태를 남긴다
                ctx.note(state, f"노드 예외: {type(e).__name__}: {e}")
                return Outcome(State.FAILED, ctx, steps=steps)

            if not isinstance(nxt, State):
                ctx.note(state, f"노드가 State를 반환하지 않았다: {nxt!r}")
                return Outcome(State.FAILED, ctx, steps=steps)

            # 중단 불가 구간을 막 빠져나왔다면 여기서 인터럽트를 처리한다.
            if pending_interrupt and not node.interruptible:
                cp = ctx.checkpoint()
                ctx.note(nxt, "중단 불가 구간 종료 — 보류 처리")
                self._log(ctx, "parked", nxt, cp)
                return Outcome(State.PARKED, ctx, interrupted=True,
                               checkpoint=cp, steps=steps)

            state = nxt

        self._log(ctx, "final", state, None)
        return Outcome(state, ctx, steps=steps)

    def _log(self, ctx: Ctx, kind: str, state: State, cp: dict | None) -> None:
        if self.store is None:
            return
        self.store.log("task.updated",
                       {"kind": kind, "state": state.value, "checkpoint": cp,
                        "category": ctx.category, "attempt": ctx.attempt},
                       origin="system", session_id=ctx.session_id, project=ctx.project)

    # -- 재개 ----------------------------------------------------
    @staticmethod
    def resume_ctx(checkpoint: dict, interrupt_text: str = "") -> Ctx:
        """보류된 작업을 되살린다.

        인터럽트로 들어온 새 지시가 있으면 함께 싣는다. 사용자가 "아니 그거
        말고 이렇게"라고 한 경우, 이전 맥락을 버리지 않고 이어간다.
        """
        ctx = Ctx(text=checkpoint["text"], session_id=checkpoint.get("session_id", "default"),
                  project=checkpoint.get("project"))
        ctx.category = checkpoint.get("category", "")
        ctx.plan = checkpoint.get("plan", "")
        ctx.code = checkpoint.get("code", "")
        ctx.attempt = checkpoint.get("attempt", 0)
        ctx.provider = checkpoint.get("provider", "")
        ctx.interrupt_text = interrupt_text
        return ctx
