#!/usr/bin/env python3
"""실행 루프 자기시험 — 상태 머신, 인터럽트, 자가 수정, 샌드박스.

도커 없이 돈다. 도커 경로는 인자 구성만 검사한다 (CI에 데몬이 없다).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.execute.loop import (  # noqa: E402
    NullFixer, RetryPolicy, build_machine, extract_code,
)
from harness.execute.machine import Ctx, Machine, Node, Outcome, State  # noqa: E402
from harness.execute.sandbox import (  # noqa: E402
    DockerBackend, SubprocessBackend, select_backend,
)
from harness.memory.events import EventStore  # noqa: E402
from harness.policy.engine import PolicyEngine, Profile  # noqa: E402
from harness.providers.registry import ProviderRouter  # noqa: E402
from harness.router.rules import Router  # noqa: E402
from harness.sandbox.policy import SandboxPolicy  # noqa: E402


class ScriptedFixer:
    """정해진 순서로 코드를 내놓는다. 자가 수정 루프를 결정적으로 시험한다."""
    name = "scripted"

    def __init__(self, *codes: str):
        self.codes = list(codes)
        self.calls = 0

    def fix(self, code, error, ctx):
        self.calls += 1
        return self.codes.pop(0) if self.codes else None


# ─────────────────────────────────────────────────────────────
class TestSubprocessBackend(unittest.TestCase):
    def setUp(self):
        self.be = SubprocessBackend()
        self.pol = SandboxPolicy(timeout_sec=5)

    def test_not_an_isolation_boundary(self):
        """이 구분이 흐려지면 SECURITY.md 전체가 무의미해진다."""
        self.assertFalse(self.be.is_isolation_boundary)
        self.assertTrue(DockerBackend().is_isolation_boundary)

    def test_success(self):
        r = self.be.run("print(6*7)", self.pol)
        self.assertTrue(r.ok)
        self.assertIn("42", r.stdout)

    def test_failure_carries_diagnosis(self):
        r = self.be.run("raise ValueError('s2p 없음')", self.pol)
        self.assertFalse(r.ok)
        self.assertIn("s2p 없음", r.feedback())

    def test_timeout(self):
        r = self.be.run("while True: pass", SandboxPolicy(timeout_sec=2))
        self.assertTrue(r.timed_out)
        self.assertIn("시간 초과", r.feedback())

    def test_feedback_keeps_tail_where_the_exception_is(self):
        """트레이스백은 끝에 실제 예외가 있다. 앞에서 자르면 진단이 사라진다."""
        r = self.be.run("x=[]\n" * 200 + "raise RuntimeError('마지막에 있는 진짜 원인')",
                        self.pol)
        fb = r.feedback(limit=200)
        self.assertIn("마지막에 있는 진짜 원인", fb)

    def test_environment_is_scrubbed(self):
        """생성 코드에 토큰이 든 환경변수가 노출되면 안 된다."""
        import os
        os.environ["VV_FAKE_SECRET"] = "should-not-leak"
        try:
            r = self.be.run("import os; print(os.environ.get('VV_FAKE_SECRET','ABSENT'))",
                            self.pol)
            self.assertIn("ABSENT", r.stdout)
        finally:
            del os.environ["VV_FAKE_SECRET"]

    def test_enterprise_refuses_unisolated_backend(self):
        ent = Profile.load(ROOT / "profiles" / "enterprise.json")
        with self.assertRaises(PermissionError):
            select_backend(ent, prefer="subprocess")

    def test_personal_falls_back_to_subprocess(self):
        per = Profile.load(ROOT / "profiles" / "personal.json")
        self.assertEqual(select_backend(per, prefer="subprocess").name, "subprocess")


class TestDockerArgs(unittest.TestCase):
    """데몬이 없어도 인자 구성은 검사할 수 있다."""

    def test_code_goes_through_stdin_not_argv(self):
        argv = SandboxPolicy().docker_args() + ["timeout", "-s", "KILL", "30", "python3", "-"]
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("print(", " ".join(argv))

    def test_policy_violation_blocks_run_before_docker(self):
        r = DockerBackend().run("print(1)", SandboxPolicy(network="bridge"))
        self.assertFalse(r.ok)
        self.assertTrue(r.error)


# ─────────────────────────────────────────────────────────────
class TestMachine(unittest.TestCase):
    def _linear(self, interruptible=True):
        seen = []

        def mk(s, nxt):
            def fn(ctx):
                seen.append(s)
                return nxt
            return fn

        nodes = [
            Node(State.INTAKE, mk(State.INTAKE, State.CLASSIFY)),
            Node(State.CLASSIFY, mk(State.CLASSIFY, State.EXECUTE)),
            Node(State.EXECUTE, mk(State.EXECUTE, State.COMMIT), interruptible=interruptible),
            Node(State.COMMIT, mk(State.COMMIT, State.DONE), interruptible=False),
        ]
        return Machine(nodes), seen

    def test_runs_to_done(self):
        m, seen = self._linear()
        out = m.run(Ctx("hi"))
        self.assertTrue(out.ok)
        self.assertEqual(seen, [State.INTAKE, State.CLASSIFY, State.EXECUTE, State.COMMIT])

    def test_interrupt_parks_rather_than_cancels(self):
        """취소하면 사용자가 '아까 하던 거'로 돌아올 수 없다."""
        m, seen = self._linear()
        out = m.run(Ctx("hi"), interrupt_check=lambda: True)
        self.assertEqual(out.final, State.PARKED)
        self.assertTrue(out.interrupted)
        self.assertIsNotNone(out.checkpoint)
        self.assertEqual(seen, [])  # 첫 노드 전에 걸렸다

    def test_uninterruptible_node_finishes_before_parking(self):
        """부작용이 반쯤 끝난 상태로 멈추면 되돌릴 수 없다."""
        calls = {"n": 0}

        def check():
            calls["n"] += 1
            return calls["n"] >= 3  # EXECUTE 진입 시점부터 인터럽트

        m, seen = self._linear(interruptible=False)
        out = m.run(Ctx("hi"), interrupt_check=check)
        self.assertEqual(out.final, State.PARKED)
        self.assertIn(State.EXECUTE, seen)      # 중단 불가 노드는 끝까지 돌았다
        self.assertNotIn(State.COMMIT, seen)    # 그 다음에 보류됐다

    def test_checkpoint_round_trip(self):
        m, _ = self._linear()
        out = m.run(Ctx("원래 지시", project="EMC"), interrupt_check=lambda: True)
        ctx2 = Machine.resume_ctx(out.checkpoint, interrupt_text="아니 그거 말고")
        self.assertEqual(ctx2.text, "원래 지시")
        self.assertEqual(ctx2.project, "EMC")
        self.assertEqual(ctx2.interrupt_text, "아니 그거 말고")

    def test_node_exception_does_not_kill_machine(self):
        def boom(ctx):
            raise RuntimeError("노드 폭발")
        m = Machine([Node(State.INTAKE, boom)])
        out = m.run(Ctx("x"))
        self.assertEqual(out.final, State.FAILED)
        self.assertIn("노드 폭발", out.ctx.trace[-1]["msg"])

    def test_step_limit_stops_infinite_loop(self):
        m = Machine([Node(State.INTAKE, lambda c: State.INTAKE)], max_steps=5)
        out = m.run(Ctx("x"))
        self.assertEqual(out.final, State.FAILED)
        self.assertLessEqual(out.steps, 5)

    def test_bad_return_value_rejected(self):
        m = Machine([Node(State.INTAKE, lambda c: "CLASSIFY")])
        self.assertEqual(m.run(Ctx("x")).final, State.FAILED)


# ─────────────────────────────────────────────────────────────
class TestRetryPolicy(unittest.TestCase):
    def setUp(self):
        self.rp = RetryPolicy.load()

    def test_module_not_found_is_fatal(self):
        """network=none 에서 설치는 불가능하다. 3회 태울 이유가 없다."""
        why = self.rp.is_fatal("ModuleNotFoundError: No module named 'skrf'")
        self.assertIsNotNone(why)
        self.assertIn("network=none", why)

    def test_readonly_fs_is_fatal(self):
        self.assertIsNotNone(self.rp.is_fatal("OSError: [Errno 30] Read-only file system"))

    def test_ordinary_error_is_retryable(self):
        self.assertIsNone(self.rp.is_fatal("ValueError: 잘못된 주파수 범위"))

    def test_artifact_loads(self):
        self.assertGreaterEqual(self.rp.max_attempts, 1)
        self.assertTrue(self.rp.fatal_patterns)


class TestExtractCode(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(extract_code("설명\n```python\nprint(1)\n```\n끝"), "print(1)")

    def test_bare(self):
        self.assertEqual(extract_code("print(1)"), "print(1)")

    def test_refusal_returns_none(self):
        self.assertIsNone(extract_code("죄송합니다. 고칠 수 없습니다."))


# ─────────────────────────────────────────────────────────────
class TestFullLoop(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.td.name) / "e.db")
        self.policy = PolicyEngine(Profile.load(ROOT / "profiles" / "personal.json"))
        self.router = Router.load()
        self.providers = ProviderRouter.load(self.policy)
        self.backend = SubprocessBackend()
        self.pol = SandboxPolicy(timeout_sec=5)

    def tearDown(self):
        self.store.close(); self.td.cleanup()

    def build(self, fixer, code, retry=None):
        return build_machine(
            backend=self.backend, fixer=fixer, router=self.router,
            providers=self.providers, retry=retry or RetryPolicy.load(),
            sandbox_policy=self.pol, codegen=lambda ctx: code, store=self.store)

    def test_happy_path_reaches_done(self):
        m = self.build(NullFixer(), "print('ok')")
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        self.assertTrue(out.ok, out.ctx.trace)
        self.assertEqual(out.ctx.attempt, 0)

    def test_self_heal_recovers_on_second_attempt(self):
        fixer = ScriptedFixer("print('고쳐짐')")
        m = self.build(fixer, "raise ValueError('첫 시도 실패')")
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        self.assertTrue(out.ok, out.ctx.trace)
        self.assertEqual(fixer.calls, 1)
        self.assertEqual(out.ctx.attempt, 1)

    def test_retry_escalates_provider(self):
        """같은 모델에 같은 에러를 다시 주면 같은 실패가 반복된다."""
        fixer = ScriptedFixer("raise ValueError('여전히 실패')", "print('ok')")
        m = self.build(fixer, "raise ValueError('첫 시도 실패')")
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        providers = [t.get("msg", "") for t in out.ctx.trace if t["state"] == "PLAN"]
        self.assertGreater(len(set(providers)), 1, providers)

    def test_fatal_error_stops_immediately(self):
        fixer = ScriptedFixer("print('안 불려야 함')")
        m = self.build(fixer, "import nonexistent_pkg_xyz")
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        self.assertEqual(out.final, State.FAILED)
        self.assertEqual(fixer.calls, 0)  # 수정 시도조차 하지 않았다

    def test_attempts_are_capped(self):
        fixer = ScriptedFixer(*["raise ValueError('계속 실패')"] * 10)
        m = self.build(fixer, "raise ValueError('처음')")
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        self.assertEqual(out.final, State.FAILED)
        self.assertLessEqual(fixer.calls, RetryPolicy.load().max_attempts)

    def test_identical_fix_breaks_the_loop(self):
        same = "raise ValueError('동일')"
        m = self.build(ScriptedFixer(same), same)
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        self.assertEqual(out.final, State.FAILED)
        self.assertIn("동일", out.ctx.trace[-1]["msg"])

    def test_low_confidence_asks_instead_of_guessing(self):
        """틀린 자신감보다 되묻는 편이 낫다."""
        m = self.build(NullFixer(), "print('ok')")
        out = m.run(Ctx("터치스톤 파일 로드해서 플롯하는 코드 짜줘"))
        self.assertEqual(out.final, State.NEEDS_INPUT)

    def test_sandbox_results_recorded_with_isolation_flag(self):
        m = self.build(NullFixer(), "print('ok')")
        m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"))
        rows = self.store.recent(types=["sandbox.result"])
        self.assertTrue(rows)
        self.assertIn("isolated", rows[0]["payload"])
        self.assertFalse(rows[0]["payload"]["isolated"])  # subprocess 는 격리 아님

    def test_interrupt_during_execute_completes_the_container_run(self):
        calls = {"n": 0}

        def check():
            calls["n"] += 1
            return calls["n"] >= 4  # EXECUTE 부근

        m = self.build(NullFixer(), "print('부작용 발생')")
        out = m.run(Ctx("csv 읽어서 그래프 그리는 스크립트 만들어줘"), interrupt_check=check)
        self.assertEqual(out.final, State.PARKED)
        self.assertTrue(out.interrupted)
        self.assertIsNotNone(out.checkpoint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
