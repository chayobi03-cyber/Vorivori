#!/usr/bin/env python3
"""로컬 `.ics` 일정 읽기.

왜 캘린더 API가 아니라 파일부터인가
    캘린더 API 연동은 OAuth·토큰 보관·망분리 예외를 한꺼번에 끌고 온다.
    그런데 실제로 필요한 것의 90%는 "오늘 뭐 있지"이고, 그건 `.ics` 파일
    하나면 된다. Google/Outlook/Apple 모두 `.ics`를 내보낸다.

    Ainative가 `tools/make_calendar.py`로 `.ics`를 뽑는 것과 같은 판단이다.
    표준 포맷을 경계로 두면 양쪽 다 단순해진다.

읽기 전용이다. 일정 생성·삭제는 이 모듈이 하지 않는다 — 되돌릴 수 없는 동작이라
정책 엔진의 승인 게이트를 거쳐야 하고, 그것은 Phase 1 항목이다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

KST = timezone(timedelta(hours=9))

# RFC 5545: 긴 줄은 CRLF + 공백 한 칸으로 접힌다. 먼저 펴야 파싱이 된다.
_FOLD = re.compile(r"\r?\n[ \t]")
# RFC 5545 TEXT 이스케이프: \n \N \, \; \\
# str.replace 로 처리하므로 패턴은 파일에 실제로 들어 있는 2글자 그대로여야 한다.
# (r"\\," 는 3글자라 매칭되지 않는다 — 실제로 이 버그가 있었다.)
_UNESCAPE = ((r"\n", "\n"), (r"\N", "\n"), (r"\,", ","), (r"\;", ";"))
_BSLASH_SENTINEL = "\x00VVBS\x00"


def _unescape(v: str) -> str:
    # 이스케이프된 백슬래시를 먼저 치워야 `\\,` 같은 입력에서 콤마를
    # 잘못 언이스케이프하지 않는다.
    v = v.replace("\\\\", _BSLASH_SENTINEL)
    for a, b in _UNESCAPE:
        v = v.replace(a, b)
    return v.replace(_BSLASH_SENTINEL, "\\")


def _parse_dt(value: str, params: dict[str, str], default_tz: timezone) -> tuple[datetime, bool]:
    """(datetime, 종일여부). 종일 일정은 VALUE=DATE로 온다."""
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=default_tz), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
    # TZID가 있어도 IANA DB 조회 없이는 정확히 못 푼다. 기본 시간대로 둔다.
    return dt.replace(tzinfo=default_tz), False


@dataclass
class Event:
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    uid: str = ""
    recurring: bool = False

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, lo: datetime, hi: datetime) -> bool:
        return self.start < hi and self.end > lo

    def fmt(self, tz: timezone = KST, width: int = 48) -> str:
        # 캘린더 제목은 길다. 터미널 한 줄을 넘기면 목록이 읽히지 않는다.
        title = self.summary if len(self.summary) <= width else self.summary[:width - 1] + "…"
        if self.all_day:
            return f"종일         {title}"
        s = self.start.astimezone(tz).strftime("%H:%M")
        e = self.end.astimezone(tz).strftime("%H:%M")
        loc = f"  @{self.location}" if self.location else ""
        rep = " ↻" if self.recurring else ""
        return f"{s}–{e}  {title}{loc}{rep}"


# ─────────────────────────────────────────────────────────────
# 반복 일정
#
# RRULE 전체 구현은 RFC 5545에서 가장 복잡한 부분이다. 여기서는 실무에서
# 실제로 쓰이는 것만 편다 — FREQ=DAILY/WEEKLY/MONTHLY, INTERVAL, BYDAY,
# COUNT/UNTIL. BYSETPOS, BYMONTHDAY 조합 등은 처리하지 않는다.
#
# 처리하지 못하는 RRULE은 **조용히 무시하지 않고** 원본 일정만 반환한다.
# 조용히 빠뜨리면 "일정 없다"는 잘못된 답이 나간다.
# ─────────────────────────────────────────────────────────────
_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
SUPPORTED_FREQ = {"DAILY", "WEEKLY", "MONTHLY"}


def _expand_rrule(ev: Event, rrule: str, lo: datetime, hi: datetime) -> list[Event]:
    parts = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = parts.get("FREQ", "")
    if freq not in SUPPORTED_FREQ:
        return [ev]  # 못 펴는 규칙은 원본만. 빠뜨리지 않는다.

    interval = int(parts.get("INTERVAL", 1))
    count = int(parts["COUNT"]) if "COUNT" in parts else None
    until = None
    if "UNTIL" in parts:
        u = parts["UNTIL"]
        until, _ = _parse_dt(u, {}, ev.start.tzinfo or KST)

    bydays = [_WEEKDAY[d[-2:]] for d in parts.get("BYDAY", "").split(",")
              if d and d[-2:] in _WEEKDAY]

    out: list[Event] = []
    dur = ev.duration
    cur = ev.start
    emitted = 0
    # 상한: 무한 반복 규칙이 hi를 넘어서도 돌지 않게 한다.
    for _ in range(2000):
        if cur > hi:
            break
        if until and cur > until:
            break
        if count is not None and emitted >= count:
            break

        ok = True
        if freq == "WEEKLY" and bydays:
            ok = cur.weekday() in bydays
        if ok:
            if cur + dur > lo:
                out.append(Event(ev.summary, cur, cur + dur, ev.all_day,
                                 ev.location, ev.uid, recurring=True))
            emitted += 1

        if freq == "DAILY":
            cur += timedelta(days=interval)
        elif freq == "WEEKLY":
            # BYDAY가 있으면 하루씩 전진하며 요일을 거른다.
            cur += timedelta(days=1) if bydays else timedelta(weeks=interval)
        else:  # MONTHLY — 같은 날짜로 다음 달
            y, m = cur.year, cur.month + interval
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
            try:
                cur = cur.replace(year=y, month=m)
            except ValueError:
                break  # 31일이 없는 달 등
    return out


# ─────────────────────────────────────────────────────────────
class Calendar:
    def __init__(self, events: list[Event], unparsed_rrules: int = 0):
        self.events = sorted(events, key=lambda e: e.start)
        self.unparsed_rrules = unparsed_rrules

    @staticmethod
    def load(paths: str | Path | Iterable[str | Path], tz: timezone = KST,
             horizon_days: int = 120) -> "Calendar":
        if isinstance(paths, (str, Path)):
            paths = [paths]
        evs: list[Event] = []
        unparsed = 0
        lo = datetime.now(tz) - timedelta(days=horizon_days)
        hi = datetime.now(tz) + timedelta(days=horizon_days)
        for p in paths:
            p = Path(p)
            if not p.exists():
                continue
            e, u = _parse_ics(p.read_text(encoding="utf-8", errors="replace"), tz, lo, hi)
            evs.extend(e)
            unparsed += u
        return Calendar(evs, unparsed)

    # -- 조회 ----------------------------------------------------
    def on(self, day: date, tz: timezone = KST) -> list[Event]:
        lo = datetime.combine(day, time.min, tzinfo=tz)
        hi = lo + timedelta(days=1)
        return [e for e in self.events if e.overlaps(lo, hi)]

    def today(self, tz: timezone = KST) -> list[Event]:
        return self.on(datetime.now(tz).date(), tz)

    def upcoming(self, hours: int = 24, tz: timezone = KST) -> list[Event]:
        now = datetime.now(tz)
        return [e for e in self.events if now <= e.start <= now + timedelta(hours=hours)]

    def next_event(self, tz: timezone = KST) -> Event | None:
        now = datetime.now(tz)
        future = [e for e in self.events if e.start > now and not e.all_day]
        return future[0] if future else None

    def free_blocks(self, day: date, work_start: int = 9, work_end: int = 18,
                    min_minutes: int = 30, tz: timezone = KST) -> list[tuple[datetime, datetime]]:
        """딥워크에 쓸 수 있는 빈 구간.

        "일정 뭐 있지"보다 "언제 비어 있지"가 실무에서 더 자주 필요하다.
        """
        lo = datetime.combine(day, time(work_start), tzinfo=tz)
        hi = datetime.combine(day, time(work_end), tzinfo=tz)
        busy = sorted((max(e.start.astimezone(tz), lo), min(e.end.astimezone(tz), hi))
                      for e in self.on(day, tz) if not e.all_day and e.overlaps(lo, hi))
        blocks: list[tuple[datetime, datetime]] = []
        cur = lo
        for s, e in busy:
            if s - cur >= timedelta(minutes=min_minutes):
                blocks.append((cur, s))
            cur = max(cur, e)
        if hi - cur >= timedelta(minutes=min_minutes):
            blocks.append((cur, hi))
        return blocks


def _parse_ics(raw: str, tz: timezone, lo: datetime, hi: datetime) -> tuple[list[Event], int]:
    raw = _FOLD.sub("", raw)  # 접힌 줄 펴기
    events: list[Event] = []
    unparsed = 0
    cur: dict = {}
    in_event = False

    for line in raw.splitlines():
        if line.startswith("BEGIN:VEVENT"):
            in_event, cur = True, {}
            continue
        if line.startswith("END:VEVENT"):
            in_event = False
            if "DTSTART" not in cur:
                continue
            start, all_day = cur["DTSTART"]
            if "DTEND" in cur:
                end = cur["DTEND"][0]
            elif "DURATION" in cur:
                end = start + _parse_duration(cur["DURATION"])
            else:
                end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
            ev = Event(cur.get("SUMMARY", "(제목 없음)"), start, end, all_day,
                       cur.get("LOCATION", ""), cur.get("UID", ""))
            if "RRULE" in cur:
                freq = dict(p.split("=", 1) for p in cur["RRULE"].split(";") if "=" in p).get("FREQ", "")
                if freq not in SUPPORTED_FREQ:
                    unparsed += 1
                events.extend(_expand_rrule(ev, cur["RRULE"], lo, hi))
            else:
                events.append(ev)
            continue
        if not in_event or ":" not in line:
            continue

        name_part, _, value = line.partition(":")
        bits = name_part.split(";")
        name = bits[0].upper()
        params = dict(b.split("=", 1) for b in bits[1:] if "=" in b)

        if name in ("DTSTART", "DTEND"):
            try:
                cur[name] = _parse_dt(value.strip(), params, tz)
            except ValueError:
                pass
        elif name in ("SUMMARY", "LOCATION", "UID", "RRULE", "DURATION"):
            cur[name] = _unescape(value.strip())

    return events, unparsed


def _parse_duration(v: str) -> timedelta:
    m = re.match(r"[+-]?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v)
    if not m:
        return timedelta(hours=1)
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=s)


if __name__ == "__main__":
    import sys

    cal = Calendar.load(sys.argv[1:] or [])
    print(f"일정 {len(cal.events)}건")
    for e in cal.today():
        print(" ", e.fmt())
