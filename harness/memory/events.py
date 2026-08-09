#!/usr/bin/env python3
"""이벤트 원장 — 추가 전용(append-only) 업무 메모리.

설계 근거
    PRD v2 §10.1은 SQLite + PostgreSQL + Qdrant + Markdown 네 저장소를 든다.
    PoC 단계에서 저장소가 넷이면 정합성 유지 비용이 기능 개발 비용을 넘는다.
    여기서는 **SQLite 하나가 정본**이고, Markdown 볼트와 벡터 인덱스는 전부
    이 원장에서 파생되는 뷰다. 파생물이 틀리면 재생성하면 된다.

    Ainative가 `kb/chunks/`를 빌더 출력으로 두고 직접 수정을 금지하는 것과 같은
    구조다. 정본 하나, 파생물 여럿.

불변식
    1. 이벤트는 수정·삭제되지 않는다. 정정은 새 이벤트로 한다(`corrects` 필드).
    2. 저장 전 반드시 PolicyEngine.prepare_for_store()를 통과한다.
    3. 모든 이벤트는 origin을 갖는다 — human이 말한 것과 AI가 만든 것을 섞지 않는다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

# 이벤트 유형 정본. 새 유형을 쓰려면 여기에 먼저 추가한다 —
# 오타로 생긴 유령 유형이 원장을 조용히 갈라놓는 것을 막는다.
EVENT_TYPES = frozenset({
    "voice.command",
    "text.command",
    "screen.capture",
    "cli.exec",
    "ai.request",
    "ai.response",
    "sandbox.result",
    "note.created",
    "idea.captured",
    "idea.promoted",
    "task.created",
    "task.updated",
    "measurement.logged",
    "schedule.query",
    "policy.verdict",
    "gepa.iteration",
})

# origin — 이 값이 없으면 AI가 만든 측정값과 사람이 읽은 측정값이 구분되지 않는다.
# EMC 측정 기록이 보고서로, 보고서가 제품으로 흘러가는 경로에서 이 구분이 전부다.
ORIGINS = frozenset({"human", "ai", "instrument", "system"})

DDL = """
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    project      TEXT,
    event_type   TEXT NOT NULL,
    origin       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    redactions   TEXT NOT NULL DEFAULT '[]',
    corrects     TEXT,
    prev_hash    TEXT,
    hash         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project, created_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 수정·삭제 차단. 애플리케이션 버그로도 과거가 바뀌지 않게 DB 레벨에서 막는다.
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events는 추가 전용이다. 정정은 corrects 필드로 새 이벤트를 쓴다');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events는 추가 전용이다. 보존기간 만료는 tools/retention.py 로 한다');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Event:
    event_type: str
    payload: dict[str, Any]
    origin: str = "human"
    session_id: str = "default"
    project: str | None = None
    corrects: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_now)
    redactions: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(
                f"알 수 없는 event_type: {self.event_type!r}. "
                f"harness/memory/events.py의 EVENT_TYPES에 먼저 등록할 것."
            )
        if self.origin not in ORIGINS:
            raise ValueError(f"origin은 {sorted(ORIGINS)} 중 하나여야 한다: {self.origin!r}")


class EventStore:
    """추가 전용 이벤트 원장.

    해시 체인을 건다. 원장을 나중에 손댔는지 `verify_chain()`으로 확인할 수 있다.
    감사 대응용이고, 암호학적 서명은 아니다 — 그 구분은 지킨다.
    """

    def __init__(self, path: str | Path, policy=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    # -- 쓰기 ----------------------------------------------------
    def append(self, event: Event) -> Event:
        event.validate()

        body = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        if self.policy is not None:
            body, found = self.policy.prepare_for_store(body)
            event.redactions = found
            event.payload = json.loads(body)

        # rowid 기준으로 직전 이벤트를 잡는다. created_at은 초 단위라 같은 초에
        # 여러 건이 들어오면 순서가 흔들리고, 체인이 조용히 깨진다.
        prev = self.conn.execute(
            "SELECT hash FROM events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["hash"] if prev else None

        digest = hashlib.sha256(
            f"{prev_hash or ''}|{event.id}|{event.event_type}|{event.origin}|{body}|{event.created_at}".encode()
        ).hexdigest()

        self.conn.execute(
            "INSERT INTO events(id, session_id, project, event_type, origin, payload,"
            " redactions, corrects, prev_hash, hash, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.id, event.session_id, event.project, event.event_type, event.origin,
                body, json.dumps(event.redactions), event.corrects,
                prev_hash, digest, event.created_at,
            ),
        )
        self.conn.commit()
        return event

    def log(self, event_type: str, payload: dict, **kw) -> Event:
        return self.append(Event(event_type=event_type, payload=payload, **kw))

    # -- 읽기 ----------------------------------------------------
    def recent(self, limit: int = 20, session_id: str | None = None,
               project: str | None = None, types: Iterable[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"; args.append(session_id)
        if project:
            sql += " AND project = ?"; args.append(project)
        if types:
            tl = list(types)
            sql += f" AND event_type IN ({','.join('?' * len(tl))})"; args.extend(tl)
        sql += " ORDER BY rowid DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.conn.execute(sql, args)]

    def between(self, start: str, end: str, project: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE created_at >= ? AND created_at < ?"
        args: list[Any] = [start, end]
        if project:
            sql += " AND project = ?"; args.append(project)
        sql += " ORDER BY rowid ASC"
        return [self._row(r) for r in self.conn.execute(sql, args)]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["redactions"] = json.loads(d["redactions"])
        return d

    # -- 무결성 --------------------------------------------------
    def verify_chain(self) -> tuple[bool, str]:
        """해시 체인 검증. (정상 여부, 설명)"""
        prev_hash = None
        n = 0
        for r in self.conn.execute("SELECT * FROM events ORDER BY rowid ASC"):
            expect = hashlib.sha256(
                f"{prev_hash or ''}|{r['id']}|{r['event_type']}|{r['origin']}|{r['payload']}|{r['created_at']}".encode()
            ).hexdigest()
            if r["prev_hash"] != prev_hash:
                return False, f"prev_hash 불일치: event {r['id']}"
            if r["hash"] != expect:
                return False, f"hash 불일치: event {r['id']}"
            prev_hash = r["hash"]
            n += 1
        return True, f"{n}건 검증 통과"

    def close(self) -> None:
        self.conn.close()


# ─────────────────────────────────────────────────────────────
# Context Builder — 최근 이벤트를 AI 입력용으로 압축한다.
#
# PRD v2 §6.2는 "최근 20개 이벤트"라고만 쓴다. 그대로 넣으면 샌드박스 stdout
# 하나가 컨텍스트를 다 먹는다. 유형별 예산을 두고 잘라낸다.
# ─────────────────────────────────────────────────────────────
TYPE_BUDGET = {
    "sandbox.result": 400,
    "ai.response": 600,
    "screen.capture": 200,
    "cli.exec": 200,
}
DEFAULT_BUDGET = 300


def build_context(store: EventStore, session_id: str, limit: int = 20,
                  total_budget: int = 4000) -> str:
    """최근 이벤트를 예산 안에서 요약한 문자열로 만든다."""
    rows = list(reversed(store.recent(limit=limit, session_id=session_id)))
    lines: list[str] = []
    used = 0
    for r in rows:
        budget = TYPE_BUDGET.get(r["event_type"], DEFAULT_BUDGET)
        body = json.dumps(r["payload"], ensure_ascii=False)
        if len(body) > budget:
            body = body[:budget] + f"…(+{len(body) - budget}자 생략)"
        line = f"- [{r['created_at']}] {r['event_type']}({r['origin']}): {body}"
        if used + len(line) > total_budget:
            lines.append(f"- …이전 {len(rows) - len(lines)}건 생략 (컨텍스트 예산 초과)")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        s = EventStore(Path(td) / "e.db")
        s.log("voice.command", {"text": "10MHz 매칭 코드 짜줘"}, session_id="demo")
        s.log("ai.response", {"provider": "qwen_local", "chars": 1200}, origin="ai", session_id="demo")
        s.log("sandbox.result", {"ok": True, "tests": 3}, origin="system", session_id="demo")
        print(build_context(s, "demo"))
        print(s.verify_chain())
