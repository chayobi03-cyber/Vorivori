#!/usr/bin/env python3
"""일상 사용 경로 자기시험 — 일정, 프로바이더 라우팅, CLI.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from harness.policy.engine import PolicyEngine, Profile  # noqa: E402
from harness.providers.registry import ProviderRouter  # noqa: E402
from harness.schedule.ics import KST, Calendar, _parse_duration  # noqa: E402

import vv  # noqa: E402


def _ics(body: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + body + "END:VCALENDAR\n"


def _day(offset: int = 0) -> str:
    return (datetime.now(KST) + timedelta(days=offset)).strftime("%Y%m%d")


class TestIcs(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name) / "c.ics"

    def tearDown(self):
        self.td.cleanup()

    def write(self, body: str) -> Calendar:
        self.p.write_text(_ics(body), encoding="utf-8")
        return Calendar.load(self.p)

    def test_unfolds_continuation_lines(self):
        """RFC 5545는 긴 줄을 CRLF+공백으로 접는다. 펴지 않으면 제목이 깨진다."""
        cal = self.write(
            f"BEGIN:VEVENT\nUID:1\nSUMMARY:아주 긴 제목이 두 줄로 접혀 있는 경\n 우를 확인\n"
            f"DTSTART:{_day()}T100000\nDTEND:{_day()}T110000\nEND:VEVENT\n")
        self.assertEqual(cal.events[0].summary, "아주 긴 제목이 두 줄로 접혀 있는 경우를 확인")

    def test_all_day_event(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:연차\n"
                         f"DTSTART;VALUE=DATE:{_day()}\nEND:VEVENT\n")
        self.assertTrue(cal.events[0].all_day)
        self.assertIn("종일", cal.events[0].fmt())

    def test_missing_dtend_defaults_to_one_hour(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:x\n"
                         f"DTSTART:{_day()}T100000\nEND:VEVENT\n")
        self.assertEqual(cal.events[0].duration, timedelta(hours=1))

    def test_duration_field_used_when_no_dtend(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:x\n"
                         f"DTSTART:{_day()}T100000\nDURATION:PT90M\nEND:VEVENT\n")
        self.assertEqual(cal.events[0].duration, timedelta(minutes=90))

    def test_weekly_byday_excludes_other_weekdays(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:스탠드업\n"
                         f"DTSTART:{_day()}T093000\nDTEND:{_day()}T094500\n"
                         f"RRULE:FREQ=WEEKLY;BYDAY=MO\nEND:VEVENT\n")
        self.assertTrue(cal.events)
        for e in cal.events:
            self.assertEqual(e.start.weekday(), 0, e.start)

    def test_count_limits_recurrence(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:x\n"
                         f"DTSTART:{_day()}T100000\nDTEND:{_day()}T110000\n"
                         f"RRULE:FREQ=DAILY;COUNT=3\nEND:VEVENT\n")
        self.assertEqual(len(cal.events), 3)

    def test_unsupported_rrule_is_reported_not_silently_dropped(self):
        """못 펴는 규칙을 조용히 버리면 '일정 없다'는 잘못된 답이 나간다."""
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:x\n"
                         f"DTSTART:{_day()}T100000\nDTEND:{_day()}T110000\n"
                         f"RRULE:FREQ=YEARLY;BYSETPOS=-1\nEND:VEVENT\n")
        self.assertEqual(len(cal.events), 1)      # 원본은 남는다
        self.assertEqual(cal.unparsed_rrules, 1)  # 그리고 보고된다

    def test_escaped_characters(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:회의\\, 그리고 검토\n"
                         f"DTSTART:{_day()}T100000\nEND:VEVENT\n")
        self.assertEqual(cal.events[0].summary, "회의, 그리고 검토")

    def test_free_blocks_exclude_busy_time(self):
        cal = self.write(f"BEGIN:VEVENT\nUID:1\nSUMMARY:회의\n"
                         f"DTSTART:{_day()}T140000\nDTEND:{_day()}T150000\nEND:VEVENT\n")
        blocks = cal.free_blocks(datetime.now(KST).date(), 9, 18)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][1].hour, 14)
        self.assertEqual(blocks[1][0].hour, 15)

    def test_free_blocks_ignore_short_gaps(self):
        body = ""
        for i, (s, e) in enumerate([("100000", "101500"), ("103000", "110000")]):
            body += (f"BEGIN:VEVENT\nUID:{i}\nSUMMARY:m{i}\n"
                     f"DTSTART:{_day()}T{s}\nDTEND:{_day()}T{e}\nEND:VEVENT\n")
        cal = self.write(body)
        blocks = cal.free_blocks(datetime.now(KST).date(), 10, 12, min_minutes=30)
        # 10:15–10:30 은 15분이라 제외된다
        self.assertTrue(all((e - s) >= timedelta(minutes=30) for s, e in blocks))

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(Calendar.load(Path(self.td.name) / "none.ics").events, [])

    def test_parse_duration_forms(self):
        self.assertEqual(_parse_duration("PT1H30M"), timedelta(hours=1, minutes=30))
        self.assertEqual(_parse_duration("P2D"), timedelta(days=2))


class TestProviderRouting(unittest.TestCase):
    def setUp(self):
        self.ent = PolicyEngine(Profile.load(ROOT / "profiles" / "enterprise.json"))
        self.per = PolicyEngine(Profile.load(ROOT / "profiles" / "personal.json"))

    def test_enterprise_never_selects_cloud(self):
        r = ProviderRouter.load(self.ent)
        for cat in json.loads((ROOT / "artifacts" / "router.rules.json")
                              .read_text())["categories"]:
            c = r.select(cat)
            self.assertTrue(c.ok, cat)
            self.assertEqual(c.transport, "local", f"{cat} → {c.provider}")

    def test_local_preferred_over_cloud_in_personal(self):
        """개인판에서도 로컬로 되는 일은 로컬로. 비용이 아니라 데이터가 안 나가서다."""
        c = ProviderRouter.load(self.per).select("PYTHON_TOOL")
        self.assertEqual(c.transport, "local")

    def test_escalation_changes_provider(self):
        r = ProviderRouter.load(self.per)
        first = r.select("PYTHON_TOOL", attempt=0)
        second = r.select("PYTHON_TOOL", attempt=1)
        self.assertNotEqual(first.provider, second.provider)

    def test_payload_with_secret_blocks_cloud_provider(self):
        r = ProviderRouter.load(self.per)
        c = r.select("DOCUMENTATION", payload="token ghp_" + "a" * 36)
        self.assertNotEqual(c.provider, "claude")

    def test_plan_shows_ladder(self):
        self.assertGreater(len(ProviderRouter.load(self.per).plan("PYTHON_TOOL")), 1)

    def test_artifact_validated(self):
        with self.assertRaises(ValueError):
            ProviderRouter({"category_roles": {"X": []}, "default_roles": ["a"]}, self.per)
        with self.assertRaises(ValueError):
            ProviderRouter({"category_roles": {}, "default_roles": []}, self.per)


class TestCli(unittest.TestCase):
    """CLI는 매일 쓰는 경로다. 깨지면 아무것도 안 쌓인다."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ics = Path(self.td.name) / "c.ics"
        self.ics.write_text(_ics(
            f"BEGIN:VEVENT\nUID:1\nSUMMARY:설계 리뷰\n"
            f"DTSTART:{_day()}T140000\nDTEND:{_day()}T150000\nEND:VEVENT\n"), encoding="utf-8")
        self.env = dict(os.environ)
        os.environ["VORIVORI_HOME"] = self.td.name
        os.environ["VORIVORI_ICS"] = str(self.ics)
        os.environ["VORIVORI_PROFILE"] = str(ROOT / "profiles" / "personal.json")

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.env); self.td.cleanup()

    def run_vv(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = vv.main(list(argv))
        return rc, out.getvalue()

    def test_capture_needs_no_quotes_or_flags(self):
        """포착이 3초를 넘으면 안 쓰인다. 따옴표도 플래그도 없어야 한다."""
        rc, out = self.run_vv("i", "쉴드캔", "접지점", "두", "개로")
        self.assertEqual(rc, 0)
        self.assertIn("쉴드캔 접지점 두 개로", out)

    def test_empty_capture_rejected(self):
        rc, _ = self.run_vv("i", "  ")
        self.assertEqual(rc, 2)

    def test_inbox_lists_capture(self):
        self.run_vv("i", "테스트", "아이디어")
        rc, out = self.run_vv("inbox")
        self.assertEqual(rc, 0)
        self.assertIn("테스트 아이디어", out)

    def test_drop_without_reason_fails_with_guidance(self):
        self.run_vv("i", "버릴", "것")
        rc, out = self.run_vv("list", "inbox")
        iid = next(t for t in out.split() if t.startswith("idea-"))
        rc, out = self.run_vv("triage", iid, "dropped")
        self.assertEqual(rc, 1)
        self.assertIn("reason", out)
        rc, _ = self.run_vv("triage", iid, "dropped", "측정으로", "반증됨")
        self.assertEqual(rc, 0)

    def test_ask_shows_classification_and_provider(self):
        rc, out = self.run_vv("a", "도커", "이미지", "빌드해줘")
        self.assertEqual(rc, 0)
        self.assertIn("INFRA_OPERATION", out)
        self.assertIn("gemini_cli", out)
        self.assertIn("exec", out)  # 위험 표면 고지

    def test_ask_warns_on_low_margin(self):
        """EMC 명사 + 코드 요구가 겹치면 되물어야 한다. 조용히 틀리는 것보다 낫다."""
        rc, out = self.run_vv("a", "터치스톤", "파일", "로드해서", "플롯하는", "코드", "짜줘")
        self.assertIn("격차가 좁습니다", out)

    def test_log_marks_human_origin(self):
        """CLI로 직접 친 값은 사람이 확인한 값이다."""
        rc, out = self.run_vv("log", "100MHz=-42dBm")
        self.assertEqual(rc, 0)
        self.assertIn("origin=human", out)
        rc, out = self.run_vv("note")
        self.assertIn("| human |", out)
        self.assertNotIn("**미확인**", out)

    def test_today_shows_events_and_free_blocks(self):
        rc, out = self.run_vv("today")
        self.assertEqual(rc, 0)
        self.assertIn("설계 리뷰", out)
        self.assertIn("빈 구간", out)

    def test_status_reports_chain_integrity(self):
        self.run_vv("i", "무결성", "확인용")
        rc, out = self.run_vv("status")
        self.assertEqual(rc, 0)
        self.assertIn("검증 통과", out)

    def test_enterprise_profile_refused_on_non_corp_host(self):
        """사내 PC 밖에서 사내 프로파일이 뜨는 것이 가장 현실적인 유출 경로다."""
        rc, out = self.run_vv("--profile", str(ROOT / "profiles" / "enterprise.json"), "status")
        self.assertEqual(rc, 3)
        self.assertIn("고정", out)

    def test_missing_calendar_is_graceful(self):
        os.environ["VORIVORI_ICS"] = str(Path(self.td.name) / "nope.ics")
        rc, out = self.run_vv("today")
        self.assertEqual(rc, 0)
        self.assertIn("일정 파일이 없습니다", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
