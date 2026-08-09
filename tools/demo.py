#!/usr/bin/env python3
"""Phase 0 왕복 시연 — 지시 → 분류 → 정책 → 실행계획 → 원장 → 노트.

    python3 tools/demo.py                       # 사내 프로파일
    python3 tools/demo.py --profile profiles/personal.json

도커도 GPU도 LLM도 없이 돈다. 이 시연이 도는 것이 Phase 0의 완료 기준이다.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.memory.events import EventStore, build_context  # noqa: E402
from harness.memory.vault import Vault, today  # noqa: E402
from harness.policy.engine import PolicyEngine, Profile  # noqa: E402
from harness.router.rules import Router  # noqa: E402
from harness.sandbox.policy import SandboxPolicy, build_command  # noqa: E402

SCRIPT = [
    ("voice.command", "방금 아이디어 떠올랐는데 쉴드캔 접지점 두 개로 늘려보기"),
    ("voice.command", "현재 100메가 노이즈 마이너스 42dBm 기록해줘"),
    ("voice.command", "터치스톤 파일 로드해서 s21 플롯하는 코드 짜줘"),
    ("voice.command", "이 스미스차트 해석 좀"),
    ("voice.command", "오늘 오후 일정 뭐 있지"),
    ("voice.command", "도커 이미지 다시 빌드해서 올려줘"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=str(ROOT / "profiles" / "enterprise.json"))
    ap.add_argument("--keep", action="store_true", help="임시 볼트를 지우지 않고 경로를 출력")
    args = ap.parse_args()

    profile = Profile.load(args.profile)
    # 데모는 호스트 고정을 우회한다. 실제 실행에서는 hostname을 넘겨 고정을 건다.
    policy = PolicyEngine(profile)
    router = Router.load()
    sandbox = SandboxPolicy()

    tmp = tempfile.mkdtemp(prefix="vorivori-demo-")
    store = EventStore(Path(tmp) / "events.db", policy=policy)
    vault = Vault(Path(tmp) / "vault", store=store)
    session = "demo-001"
    project = "EMC_Filter_Study"

    print(f"프로파일: {profile.name} ({profile.edition})  볼트: {profile.vault_root}")
    print("=" * 78)

    for _, text in SCRIPT:
        store.log("voice.command", {"text": text}, session_id=session, project=project)
        c = router.classify(text)
        mark = "" if c.confident else "  [되물음 필요]"
        print(f"\n▸ \"{text}\"")
        print(f"  분류: {c.category} (점수 {c.score}, 격차 {c.margin}){mark}")

        if c.category == "IDEA_CAPTURE":
            idea = vault.capture(text, project=project, session_id=session)
            print(f"  → 메모북 포착: {idea.id} (state=inbox)")

        elif c.category == "MEASUREMENT_LOG":
            # origin=ai 로 남긴다. 사람이 확인하기 전까지는 미확인 값이다.
            store.log("measurement.logged",
                      {"label": "100MHz", "value": "-42dBm", "unit": "dBm"},
                      origin="ai", session_id=session, project=project)
            print("  → 측정 기록 (origin=ai, 미확인 — 사람 확인 전까지 보고서에 못 옮긴다)")

        elif c.category in ("PYTHON_TOOL", "EMC_ANALYSIS"):
            cmd = build_command("/work/gen.py", sandbox)
            store.log("sandbox.result", {"ok": True, "tests": 3, "cmd": cmd[:120]},
                      origin="system", session_id=session, project=project)
            print(f"  → 샌드박스 실행계획: {cmd[:88]}…")

        elif c.category == "INFRA_OPERATION":
            v = policy.check_egress("api.netlify.com")
            store.log("policy.verdict",
                      {"surface": v.surface, "decision": v.decision, "reason": v.reason},
                      origin="system", session_id=session, project=project)
            print(f"  → 배포 판정: {v}")

        elif c.category == "SCHEDULE_QUERY":
            need = policy.requires_approval("calendar.create")
            store.log("schedule.query", {"range": "today"}, origin="system",
                      session_id=session, project=project)
            print(f"  → 일정 조회 허용. 생성은 승인 필요: {need}")

    # 위험 표면 점검
    print("\n" + "=" * 78)
    print("위험 표면 판정")
    for cmd in ("Get-ChildItem C:\\work", "Invoke-Expression $payload", "git push origin main"):
        print(f"  exec    {policy.check_exec(cmd)}")
    for app in ("Code.exe", "KakaoTalk.exe"):
        print(f"  capture {policy.check_capture(app, screened=True)}")

    # 비밀 마스킹 실증
    ev = store.log("cli.exec", {"cmd": "export GH_TOKEN=ghp_" + "a" * 36},
                   origin="system", session_id=session, project=project)
    print(f"  store   마스킹됨: {ev.redactions} → {ev.payload['cmd'][:44]}…")

    ok, msg = store.verify_chain()
    print(f"\n원장 무결성: {msg}  ({store.count()}건)")

    note = vault.daily_note(store, day=today(), project=project)
    review = vault.review_note()
    print(f"\n생성된 노트:\n  {note}\n  {review}")
    print("\n--- 작업 노트 미리보기 " + "-" * 52)
    print(note.read_text(encoding="utf-8"))

    if args.keep:
        print(f"\n볼트 보존: {tmp}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
