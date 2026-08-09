#!/usr/bin/env python3
"""vv — Vorivori 일상 진입점.

이 파일이 존재하는 이유
    Phase 0에는 시연(`demo.py`)만 있고 실제로 쓸 경로가 없었다. 아이디어 하나
    붙잡는 데 파이썬을 import 해야 한다면 아무도 안 쓴다. 안 쓰이면 이벤트가
    안 쌓이고, 이벤트가 안 쌓이면 GEPA 루프가 굶는다.

    그래서 **포착은 3초 안에 끝나야 한다**는 것이 이 CLI의 유일한 설계 목표다.

        vv i 쉴드캔 접지점 두 개로 늘려보기

    따옴표도 플래그도 없다.

설정 (환경변수)
    VORIVORI_HOME     기본 ~/.vorivori
    VORIVORI_PROFILE  기본 profiles/personal.json
    VORIVORI_ICS      `.ics` 경로들 (: 또는 , 구분). 없으면 $HOME/calendars/*.ics

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.memory.events import EventStore, build_context  # noqa: E402
from harness.memory.vault import STATES, Vault, today  # noqa: E402
from harness.policy.engine import PolicyEngine, PolicyError, Profile  # noqa: E402
from harness.providers.registry import ProviderRouter  # noqa: E402
from harness.router.rules import Router  # noqa: E402
from harness.schedule.ics import KST, Calendar  # noqa: E402


# ─────────────────────────────────────────────────────────────
class Ctx:
    """열려 있는 모든 것. 명령마다 필요한 것만 늦게 만든다."""

    def __init__(self, profile_path: str | None = None):
        self.home = Path(os.environ.get("VORIVORI_HOME", Path.home() / ".vorivori"))
        self.home.mkdir(parents=True, exist_ok=True)
        pp = profile_path or os.environ.get(
            "VORIVORI_PROFILE", str(ROOT / "profiles" / "personal.json"))
        self.profile = Profile.load(pp)
        # 호스트 고정은 실제 호스트명으로 검사한다. 사내 프로파일을 개인 PC에서
        # 띄우려 하면 여기서 막힌다 — 그것이 이 검사의 목적이다.
        self.policy = PolicyEngine(self.profile, hostname=os.uname().nodename)
        self._store = self._vault = self._router = self._provider = self._cal = None

    @property
    def store(self) -> EventStore:
        if self._store is None:
            self._store = EventStore(self.home / "events.db", policy=self.policy)
        return self._store

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            # 프로파일의 vault_root는 상대경로다. HOME 아래로 붙인다.
            self._vault = Vault(self.home / Path(self.profile.vault_root).name,
                                store=self.store)
        return self._vault

    @property
    def router(self) -> Router:
        if self._router is None:
            self._router = Router.load()
        return self._router

    @property
    def providers(self) -> ProviderRouter:
        if self._provider is None:
            self._provider = ProviderRouter.load(self.policy)
        return self._provider

    @property
    def calendar(self) -> Calendar:
        if self._cal is None:
            raw = os.environ.get("VORIVORI_ICS", "")
            paths = [p for p in raw.replace(",", ":").split(":") if p.strip()]
            if not paths:
                paths = [str(p) for p in sorted((self.home / "calendars").glob("*.ics"))]
            self._cal = Calendar.load(paths)
        return self._cal


def _w() -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, 100)


def _hr(title: str = "") -> None:
    w = _w()
    print(f"── {title} " + "─" * max(0, w - len(title) - 4) if title else "─" * w)


# ─────────────────────────────────────────────────────────────
# 명령
# ─────────────────────────────────────────────────────────────
def cmd_idea(ctx: Ctx, args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("무엇을 적을지 알려주세요.  예: vv i 쉴드캔 접지점 두 개로", file=sys.stderr)
        return 2
    idea = ctx.vault.capture(text, project=args.project, tags=args.tag or [])
    print(f"✓ {idea.id}  {idea.title}")
    return 0


def cmd_list(ctx: Ctx, args) -> int:
    state = args.state
    items = ctx.vault.list(state=state, project=args.project)
    if not items:
        print(f"({state or '전체'} 없음)")
        return 0
    _hr(f"{state or '전체'} {len(items)}건")
    for i in items:
        tags = f"  [{' '.join(i.tags)}]" if i.tags else ""
        why = f"  — {i.reason}" if i.reason else ""
        proj = f"  ({i.project})" if i.project else ""
        print(f"{i.id}  {i.state:8} {i.title}{proj}{tags}{why}")
    return 0


def cmd_triage(ctx: Ctx, args) -> int:
    try:
        idea = ctx.vault.transition(args.id, args.state, reason=" ".join(args.reason or []))
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ {idea.id} → {idea.state}")
    return 0


def cmd_review(ctx: Ctx, args) -> int:
    stale = ctx.vault.stale(args.days)
    counts = {s: len(ctx.vault.list(state=s)) for s in STATES}
    _hr("아이디어 현황")
    print("  " + "  ".join(f"{s} {counts[s]}" for s in STATES if counts[s]))
    if stale:
        print()
        _hr(f"{args.days}일 이상 방치된 inbox — 여기부터 처리")
        for i in stale:
            print(f"  {i.id}  ({i.created_at})  {i.title}")
        print("\n  이 목록이 길어지는 것이 메모북이 죽어가는 신호다.")
    path = ctx.vault.review_note()
    print(f"\n리뷰 노트: {path}")
    return 0


def cmd_today(ctx: Ctx, args) -> int:
    cal = ctx.calendar
    if not cal.events:
        print("일정 파일이 없습니다.")
        print(f"  VORIVORI_ICS 로 지정하거나 {ctx.home / 'calendars'}/*.ics 에 두세요.")
        return 0
    day = datetime.now(KST).date() + timedelta(days=args.offset)
    evs = cal.on(day)
    _hr(f"{day} 일정 {len(evs)}건")
    for e in evs:
        print("  " + e.fmt())
    if not evs:
        print("  (없음)")

    blocks = cal.free_blocks(day, min_minutes=args.min_block)
    if blocks:
        print()
        _hr(f"빈 구간 ({args.min_block}분 이상)")
        for s, e in blocks:
            mins = int((e - s).total_seconds() // 60)
            bar = "█" * min(30, mins // 20)
            print(f"  {s.strftime('%H:%M')}–{e.strftime('%H:%M')}  {mins:4}분  {bar}")
    if cal.unparsed_rrules:
        print(f"\n  주의: 해석하지 못한 반복 규칙 {cal.unparsed_rrules}건. "
              f"해당 일정은 최초 1회만 표시됩니다.")
    ctx.store.log("schedule.query", {"day": str(day), "count": len(evs)}, origin="system")
    return 0


def cmd_next(ctx: Ctx, args) -> int:
    e = ctx.calendar.next_event()
    if e is None:
        print("남은 일정 없음.")
        return 0
    left = e.start - datetime.now(KST)
    mins = int(left.total_seconds() // 60)
    when = f"{mins}분 뒤" if mins < 120 else f"{mins // 60}시간 {mins % 60}분 뒤"
    print(f"{e.fmt()}   ({when})")
    return 0


def cmd_ask(ctx: Ctx, args) -> int:
    """분류하고 어디로 갈지 보여준다. 실제 모델 호출은 Phase 1."""
    text = " ".join(args.text).strip()
    if not text:
        return 2
    ctx.store.log("text.command", {"text": text})
    c = ctx.router.classify(text)

    _hr("분류")
    print(f"  {c.category}   점수 {c.score}  격차 {c.margin}")
    if c.matched:
        print(f"  걸린 패턴: {', '.join(c.matched)}")
    if not c.confident:
        print(f"  ⚠ 2위 {c.runner_up}와 격차가 좁습니다. 실제 실행 전에 되묻습니다.")

    print()
    _hr("프로바이더")
    choice = ctx.providers.select(c.category, payload=text)
    print(f"  {choice}")
    for name, why in choice.blocked:
        print(f"    ✗ {name}: {why}")
    ladder = ctx.providers.plan(c.category)
    if len(ladder) > 1:
        print("  승급 사다리: " + " → ".join(x.provider for x in ladder))

    # 카테고리별로 어떤 위험 표면을 건드리는지 미리 보인다.
    surfaces = {
        "INFRA_OPERATION": ("exec", "명령 실행"),
        "EMC_ANALYSIS": ("capture", "화면 캡처 가능성"),
    }
    if c.category in surfaces:
        surf, label = surfaces[c.category]
        print()
        _hr("위험 표면")
        print(f"  이 작업은 {surf} 표면을 건드립니다 ({label}). 실행 시 정책 판정을 거칩니다.")
    return 0


def cmd_log(ctx: Ctx, args) -> int:
    """측정값 기록. origin은 human이다 — 사람이 직접 친 것이므로."""
    text = " ".join(args.text).strip()
    label, _, value = text.partition("=")
    if not value:
        parts = text.rsplit(" ", 1)
        label, value = (parts[0], parts[1]) if len(parts) == 2 else (text, "")
    ctx.store.log("measurement.logged",
                  {"label": label.strip(), "value": value.strip(),
                   "confirmed_by": os.environ.get("USER", "cli-user")},
                  origin="human", project=args.project)
    print(f"✓ 기록: {label.strip()} = {value.strip()}  (origin=human, 확인됨)")
    return 0


def cmd_note(ctx: Ctx, args) -> int:
    path = ctx.vault.daily_note(ctx.store, day=args.day or today(), project=args.project)
    print(f"작업 노트: {path}\n")
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_status(ctx: Ctx, args) -> int:
    p = ctx.profile
    _hr("프로파일")
    print(f"  {p.name} ({p.edition})   egress 기본값: {p.egress_default}")
    print(f"  HOME: {ctx.home}")
    local = [n for n, v in p.providers.items() if v.get("transport") == "local"]
    cloud = [n for n, v in p.providers.items() if v.get("transport") == "cloud"]
    print(f"  프로바이더: 로컬 {local or '없음'} / 클라우드 {cloud or '없음'}")

    print()
    _hr("메모북")
    counts = {s: len(ctx.vault.list(state=s)) for s in STATES}
    live = {s: n for s, n in counts.items() if n}
    print("  " + ("  ".join(f"{s} {n}" for s, n in live.items()) if live else "(비어 있음)"))
    st = ctx.vault.stale(14)
    if st:
        print(f"  ⚠ 14일 이상 방치된 inbox {len(st)}건 — `vv review`")

    print()
    _hr("원장")
    ok, msg = ctx.store.verify_chain()
    print(f"  이벤트 {ctx.store.count()}건   무결성: {msg if ok else '⚠ ' + msg}")

    cal = ctx.calendar
    if cal.events:
        print()
        _hr("일정")
        nxt = cal.next_event()
        print(f"  오늘 {len(cal.today())}건" + (f"   다음: {nxt.fmt()}" if nxt else ""))
    return 0 if ok else 1


def cmd_context(ctx: Ctx, args) -> int:
    print(build_context(ctx.store, args.session, limit=args.limit))
    return 0


# ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="vv", description="Vorivori 업무 하네스",
        epilog="포착이 가장 짧아야 합니다:  vv i 쉴드캔 접지점 두 개로")
    ap.add_argument("--profile", help="프로파일 경로 (기본: VORIVORI_PROFILE 또는 personal)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, *aliases):
        p = sub.add_parser(name, aliases=list(aliases), help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("idea", cmd_idea, "아이디어 포착", "i")
    p.add_argument("text", nargs="+")
    p.add_argument("--project", "-p")
    p.add_argument("--tag", "-t", action="append")

    p = add("list", cmd_list, "아이디어 목록", "ls")
    p.add_argument("state", nargs="?", choices=STATES)
    p.add_argument("--project", "-p")

    p = add("inbox", lambda c, a: cmd_list(c, argparse.Namespace(state="inbox", project=None)),
            "미분류 아이디어")

    p = add("triage", cmd_triage, "아이디어 상태 전이", "t")
    p.add_argument("id")
    p.add_argument("state", choices=STATES)
    p.add_argument("reason", nargs="*", help="dropped/parked에는 필수")

    p = add("review", cmd_review, "주간 리뷰")
    p.add_argument("--days", type=int, default=14)

    p = add("today", cmd_today, "오늘 일정과 빈 구간")
    p.add_argument("--offset", "-o", type=int, default=0, help="+1 = 내일")
    p.add_argument("--min-block", type=int, default=30)

    add("next", cmd_next, "다음 일정")

    p = add("ask", cmd_ask, "분류하고 어느 AI로 갈지 확인", "a")
    p.add_argument("text", nargs="+")

    p = add("log", cmd_log, "측정값 기록")
    p.add_argument("text", nargs="+")
    p.add_argument("--project", "-p")

    p = add("note", cmd_note, "일일 작업 노트 생성")
    p.add_argument("--day")
    p.add_argument("--project", "-p")

    add("status", cmd_status, "전체 현황", "st")

    p = add("context", cmd_context, "AI에 넘길 컨텍스트 미리보기")
    p.add_argument("--session", default="default")
    p.add_argument("--limit", type=int, default=20)

    args = ap.parse_args(argv)
    try:
        ctx = Ctx(args.profile)
    except PolicyError as e:
        print(f"✗ 프로파일 오류: {e}", file=sys.stderr)
        return 3
    return args.fn(ctx, args)


if __name__ == "__main__":
    raise SystemExit(main())
