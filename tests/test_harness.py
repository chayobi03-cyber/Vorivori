#!/usr/bin/env python3
"""하네스 코어 자기시험. 외부 의존성·네트워크 없이 돈다 (unittest, 표준 라이브러리).

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.memory.events import Event, EventStore, build_context  # noqa: E402
from harness.memory.vault import Vault  # noqa: E402
from harness.optimize.gepa import (  # noqa: E402
    Candidate, Gepa, GepaConfig, Instance, Result, pareto_frontier,
    prune_dominated, sample_parent,
)
from harness.optimize.proposers import OfflineRuleProposer, _extract_json  # noqa: E402
from harness.policy.engine import PolicyEngine, PolicyError, Profile, redact, scan_secrets  # noqa: E402
from harness.router.rules import Router, normalize  # noqa: E402
from harness.sandbox.policy import SandboxPolicy  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from gepa_run import evaluate, load_instances  # noqa: E402


# ─────────────────────────────────────────────────────────────
class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.ent = Profile.load(ROOT / "profiles" / "enterprise.json")
        self.per = Profile.load(ROOT / "profiles" / "personal.json")

    def test_enterprise_rejects_cloud_provider(self):
        """사내 프로파일에 클라우드 프로바이더를 넣으면 로드 자체가 실패해야 한다."""
        bad = json.loads((ROOT / "profiles" / "enterprise.json").read_text())
        bad["providers"]["gpt"] = {"transport": "cloud", "endpoint": "api.openai.com"}
        with self.assertRaises(PolicyError):
            Profile(**{k: v for k, v in bad.items() if k != "$comment"}).validate()

    def test_enterprise_cannot_open_egress(self):
        bad = json.loads((ROOT / "profiles" / "enterprise.json").read_text())
        bad.pop("$comment", None)
        bad["egress_default"] = "allow"
        with self.assertRaises(PolicyError):
            Profile(**bad).validate()

    def test_enterprise_denies_external_hosts(self):
        eng = PolicyEngine(self.ent)
        self.assertEqual(eng.check_egress("api.anthropic.com").decision, "deny")
        self.assertEqual(eng.check_egress("gpu-node-1.internal").decision, "allow")

    def test_powershell_dangerous_commands_blocked(self):
        eng = PolicyEngine(self.ent)
        for cmd in ("Invoke-Expression $p", "powershell -EncodedCommand ZQBj",
                    "New-Object Net.WebClient", "Set-ExecutionPolicy Bypass",
                    "Remove-Item C:\\ -Recurse -Force", "Get-Credential"):
            self.assertEqual(eng.check_exec(cmd).decision, "deny", cmd)

    def test_unlisted_command_needs_confirmation_not_denial(self):
        """모르는 명령은 막지 말고 되묻는다. 전부 막으면 아무도 안 쓴다."""
        eng = PolicyEngine(self.ent)
        self.assertEqual(eng.check_exec("Get-Something-Unusual").decision, "confirm")

    def test_capture_denies_messenger_and_password_manager(self):
        eng = PolicyEngine(self.ent)
        self.assertEqual(eng.check_capture("KakaoTalk.exe", screened=True).decision, "deny")
        self.assertEqual(eng.check_capture("1Password.exe", screened=True).decision, "deny")

    def test_capture_requires_screening(self):
        eng = PolicyEngine(self.ent)
        self.assertEqual(eng.check_capture("Code.exe", screened=False).decision, "confirm")
        self.assertEqual(eng.check_capture("Code.exe", screened=True).decision, "allow")

    def test_egress_blocks_payload_carrying_secrets(self):
        eng = PolicyEngine(self.per)
        v = eng.check_egress("api.anthropic.com", payload="token: ghp_" + "a" * 36)
        self.assertEqual(v.decision, "deny")

    def test_secret_scanner_catches_common_shapes(self):
        for s in ("sk-ant-" + "a" * 20, "AKIA" + "B" * 16, "ghp_" + "c" * 36,
                  "-----BEGIN PRIVATE KEY-----", "password = hunter2xyz",
                  "901010-1234567"):
            self.assertTrue(scan_secrets(s), s)

    def test_redact_removes_value(self):
        out, found = redact("key=ghp_" + "d" * 36)
        self.assertIn("github-token", found)
        self.assertNotIn("d" * 36, out)

    def test_profile_pinning_blocks_wrong_host(self):
        """사내 PC에서 개인 프로파일이 뜨는 것이 가장 현실적인 유출 경로다."""
        with self.assertRaises(PolicyError):
            PolicyEngine(self.ent, hostname="my-home-laptop")
        PolicyEngine(self.ent, hostname="EMC-WS-07")  # 통과해야 함


# ─────────────────────────────────────────────────────────────
class TestEventStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.td.name) / "e.db")

    def tearDown(self):
        self.store.close(); self.td.cleanup()

    def test_append_only_update_blocked(self):
        self.store.log("voice.command", {"text": "hi"})
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("UPDATE events SET payload='{}'")

    def test_append_only_delete_blocked(self):
        self.store.log("voice.command", {"text": "hi"})
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("DELETE FROM events")

    def test_hash_chain_survives_same_second_writes(self):
        """같은 초에 여러 건이 들어와도 체인이 깨지지 않아야 한다."""
        for i in range(50):
            self.store.log("cli.exec", {"n": i}, origin="system")
        ok, msg = self.store.verify_chain()
        self.assertTrue(ok, msg)

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(ValueError):
            self.store.log("voice.comand", {})  # 오타

    def test_unknown_origin_rejected(self):
        with self.assertRaises(ValueError):
            self.store.append(Event("voice.command", {}, origin="robot"))

    def test_policy_redacts_before_store(self):
        eng = PolicyEngine(Profile.load(ROOT / "profiles" / "personal.json"))
        store = EventStore(Path(self.td.name) / "r.db", policy=eng)
        ev = store.log("cli.exec", {"cmd": "export TOKEN=ghp_" + "e" * 36}, origin="system")
        self.assertIn("github-token", ev.redactions)
        self.assertNotIn("e" * 36, json.dumps(ev.payload))
        store.close()

    def test_context_builder_respects_budget(self):
        for i in range(30):
            self.store.log("sandbox.result", {"out": "x" * 5000}, origin="system",
                           session_id="s")
        ctx = build_context(self.store, "s", limit=20, total_budget=2000)
        self.assertLessEqual(len(ctx), 2400)  # 예산 + 마지막 줄 여유
        self.assertIn("생략", ctx)


# ─────────────────────────────────────────────────────────────
class TestVault(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.td.name) / "e.db")
        self.vault = Vault(Path(self.td.name) / "vault", store=self.store)

    def tearDown(self):
        self.store.close(); self.td.cleanup()

    def test_capture_requires_no_classification(self):
        """포착은 한 줄이면 끝나야 한다. 분류를 요구하면 안 쓰인다."""
        idea = self.vault.capture("페라이트 코어 위치 바꿔보기")
        self.assertEqual(idea.state, "inbox")
        self.assertIsNone(idea.project)
        self.assertTrue(self.vault.get(idea.id))

    def test_drop_requires_reason(self):
        idea = self.vault.capture("버릴 아이디어")
        with self.assertRaises(ValueError):
            self.vault.transition(idea.id, "dropped")
        self.vault.transition(idea.id, "dropped", reason="측정으로 반증됨")

    def test_park_requires_reason(self):
        idea = self.vault.capture("나중에 볼 것")
        with self.assertRaises(ValueError):
            self.vault.transition(idea.id, "parked")

    def test_roundtrip_preserves_fields(self):
        idea = self.vault.capture("제목", body="본문", project="EMC", tags=["filter", "poc"])
        got = self.vault.get(idea.id)
        self.assertEqual(got.title, "제목")
        self.assertEqual(got.body, "본문")
        self.assertEqual(got.tags, ["filter", "poc"])
        self.assertEqual(got.project, "EMC")

    def test_capture_logs_event(self):
        self.vault.capture("이벤트 연결 확인")
        self.assertEqual(len(self.store.recent(types=["idea.captured"])), 1)

    def test_daily_note_separates_ai_and_human_measurements(self):
        self.store.log("measurement.logged",
                       {"label": "100MHz", "value": "-42dBm", "confirmed_by": "engineer-a"},
                       origin="human")
        self.store.log("measurement.logged", {"label": "200MHz", "value": "-38dBm"},
                       origin="ai")
        p = self.vault.daily_note(self.store, day=__import__("harness.memory.vault",
                                  fromlist=["today"]).today())
        text = p.read_text(encoding="utf-8")
        self.assertIn("| ai |", text)          # AI가 받아적은 값
        self.assertIn("| human |", text)       # 사람이 읽은 값
        self.assertIn("미확인", text)           # 확인 안 된 AI 값에 경고
        self.assertIn("-42dBm", text)

    def test_review_note_surfaces_stale_inbox(self):
        i = self.vault.capture("오래된 것")
        i.updated_at = "2020-01-01"
        self.vault._write_idea(i)
        self.vault._rebuild_index()
        self.assertTrue(self.vault.stale(14))
        self.assertIn("방치된", self.vault.review_note().read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────
class TestRouter(unittest.TestCase):
    def test_normalize_absorbs_korean_stt_spacing(self):
        """STT는 '에스 파라미터'와 '에스파라미터'를 오간다. 둘이 같아야 한다."""
        self.assertEqual(normalize("에스 파라미터"), normalize("에스파라미터"))
        self.assertEqual(normalize("S-Parameter"), normalize("sparameter"))

    def test_artifact_loads_and_validates(self):
        Router.load()

    def test_invalid_artifact_rejected(self):
        with self.assertRaises(ValueError):
            Router({"categories": ["A"], "default": "A",
                    "rules": [{"category": "NOPE", "any": ["x"]}]})
        with self.assertRaises(ValueError):
            Router({"categories": ["A"], "default": "A",
                    "rules": [{"category": "A", "any": []}]})

    def test_fallback_is_confident_not_ambiguous(self):
        """아무것도 안 걸린 것은 애매한 게 아니라 '해당 없음'이라는 확정 판단이다."""
        c = Router.load().classify("zzz 아무 의미 없는 소리")
        self.assertTrue(c.fallback)
        self.assertTrue(c.confident)

    def test_none_patterns_suppress_rule(self):
        r = Router({"categories": ["A", "B"], "default": "B", "min_margin": 0.5,
                    "rules": [{"category": "A", "any": ["코드"], "none": ["말고"], "weight": 2}]})
        self.assertEqual(r.classify("코드 짜줘").category, "A")
        self.assertEqual(r.classify("코드 말고 설명").category, "B")


# ─────────────────────────────────────────────────────────────
class TestPareto(unittest.TestCase):
    def test_frontier_keeps_specialist_that_loses_on_average(self):
        """평균은 낮지만 한 인스턴스를 유일하게 맞히는 후보를 살려야 한다.

        이것이 GEPA가 '최고점 후보만 변이'와 다른 유일한 지점이다.
        """
        scores = {
            "generalist": {"i1": 0.9, "i2": 0.9, "i3": 0.0},
            "specialist": {"i1": 0.1, "i2": 0.1, "i3": 1.0},
        }
        wins = pareto_frontier(scores)
        self.assertEqual(wins["generalist"], {"i1", "i2"})
        self.assertEqual(wins["specialist"], {"i3"})
        self.assertEqual(set(prune_dominated(wins)), {"generalist", "specialist"})

    def test_dominated_candidate_pruned(self):
        wins = {"a": {"i1", "i2"}, "b": {"i1"}}
        self.assertEqual(set(prune_dominated(wins)), {"a"})

    def test_duplicate_win_sets_collapse(self):
        wins = {"a": {"i1"}, "b": {"i1"}}
        self.assertEqual(len(prune_dominated(wins)), 1)

    def test_zero_score_candidate_excluded_from_frontier(self):
        scores = {"a": {"i1": 1.0}, "b": {"i1": 0.0}}
        self.assertNotIn("b", pareto_frontier(scores))

    def test_sample_parent_weights_by_wins(self):
        rng = random.Random(0)
        wins = {"big": set(f"i{i}" for i in range(20)), "small": {"i99"}}
        picks = [sample_parent(wins, rng) for _ in range(200)]
        self.assertGreater(picks.count("big"), picks.count("small"))
        self.assertGreater(picks.count("small"), 0)  # 소수파도 뽑혀야 한다


# ─────────────────────────────────────────────────────────────
class TestGepaLoop(unittest.TestCase):
    def setUp(self):
        self.instances = load_instances(ROOT / "taskbank" / "router.jsonl")
        self.seed = json.loads((ROOT / "artifacts" / "router.rules.json").read_text())

    def test_taskbank_has_both_splits(self):
        splits = {i.split for i in self.instances}
        self.assertEqual(splits, {"train", "holdout"})

    def test_taskbank_labels_are_valid_categories(self):
        cats = set(self.seed["categories"])
        for i in self.instances:
            self.assertIn(i.expect, cats, i.id)

    def test_taskbank_ids_unique(self):
        ids = [i.id for i in self.instances]
        self.assertEqual(len(ids), len(set(ids)))

    def test_evaluator_gives_textual_feedback_not_just_score(self):
        """스칼라만 주면 반사할 재료가 없다. GEPA의 전제다."""
        bad = Instance("x", "도커 배포해줘", "PYTHON_TOOL")
        r = evaluate(self.seed, bad)
        self.assertEqual(r.score, 0.0)
        self.assertIn("INFRA_OPERATION", r.feedback)
        self.assertTrue(len(r.feedback) > 20)

    def test_broken_artifact_scores_zero_without_crashing(self):
        """프로포저는 깨진 JSON을 낸다. 루프가 죽으면 안 된다."""
        r = evaluate({"categories": ["A"], "default": "ZZZ", "rules": []},
                     Instance("x", "hi", "A"))
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.got, "<invalid>")

    def test_optimize_improves_holdout_reproducibly(self):
        gepa = Gepa(evaluate, [OfflineRuleProposer(random.Random(3))],
                    GepaConfig(budget=40, minibatch=8, seed=3))
        best, rep = gepa.optimize(self.seed, self.instances)
        self.assertGreater(rep.best_train, rep.seed_train)
        self.assertGreaterEqual(rep.best_holdout, rep.seed_holdout)
        Router(best.artifact)  # 산출물이 실제로 로드돼야 한다

    def test_optimized_artifact_records_lineage(self):
        gepa = Gepa(evaluate, [OfflineRuleProposer(random.Random(1))],
                    GepaConfig(budget=20, seed=1))
        best, _ = gepa.optimize(self.seed, self.instances)
        if best.generation > 0:
            self.assertTrue(best.artifact["lineage"])
            self.assertNotEqual(best.artifact["proposer"], "human-seed")


class TestProposerParsing(unittest.TestCase):
    def test_extract_json_from_fenced_llm_output(self):
        out = _extract_json('설명입니다\n```json\n{"a": 1}\n```\n끝')
        self.assertEqual(out, {"a": 1})

    def test_extract_json_handles_braces_in_strings(self):
        out = _extract_json('{"note": "중괄호 } 포함", "n": 2}')
        self.assertEqual(out["n"], 2)

    def test_extract_json_returns_none_on_garbage(self):
        self.assertIsNone(_extract_json("아무 JSON도 없음"))


# ─────────────────────────────────────────────────────────────
class TestSandbox(unittest.TestCase):
    def test_default_policy_passes_preflight(self):
        self.assertEqual(SandboxPolicy().preflight(), [])

    def test_network_enabled_policy_rejected(self):
        self.assertTrue(SandboxPolicy(network="bridge").preflight())

    def test_root_user_rejected(self):
        self.assertTrue(SandboxPolicy(user="0:0").preflight())

    def test_swap_escape_rejected(self):
        self.assertTrue(SandboxPolicy(memory="2g", memory_swap="8g").preflight())

    def test_hardening_flags_present(self):
        args = " ".join(SandboxPolicy().docker_args())
        for flag in ("--cap-drop ALL", "--security-opt no-new-privileges",
                     "--pids-limit", "--read-only", "--network none", "--user 65534"):
            self.assertIn(flag, args, flag)

    def test_input_mount_is_readonly(self):
        args = SandboxPolicy().docker_args(mount_src="/srv/emc")
        self.assertIn("/srv/emc:/data:ro", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
