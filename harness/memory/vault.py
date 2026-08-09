#!/usr/bin/env python3
"""Markdown 볼트 — 아이디어 메모북과 진행사항 정리.

원본 PRD가 빠뜨린 것
    §10.3에 "노트 자동 생성"이 있지만, 그것은 **세션이 끝난 뒤** 요약을 만드는
    기능이다. 정작 필요한 것은 반대 방향이다 — 아이디어는 세션 밖에서, 장비를
    만지다가, 회의 중에, 퇴근길에 떠오른다. 그때 붙잡을 경로가 없으면
    메모북은 존재하지 않는 것과 같다.

    그래서 두 축으로 나눈다.

      포착(capture)  : 세션·프로젝트 없이도 한 줄로 들어온다. 분류는 나중에.
      정리(review)   : 포착된 것을 주기적으로 훑어 승격하거나 버린다.

    아이디어가 죽는 지점은 포착이 아니라 정리다. 그래서 상태 기계를 둔다.

        inbox → triaged → active → done
                   └──→ parked ──┘   (되살릴 수 있게 남긴다)
                   └──→ dropped      (이유를 적어야 버려진다)

    이유 없이 버려진 아이디어는 6개월 뒤 같은 형태로 다시 떠오른다.

볼트 파일은 **사람이 읽는 뷰**다. 정본은 이벤트 원장이며, 볼트는 재생성 가능하다.
Ainative가 kb/chunks/를 빌더 출력으로 두는 것과 같은 규칙이다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

STATES = ("inbox", "triaged", "active", "parked", "done", "dropped")
TERMINAL = ("done", "dropped")

KST = timezone(timedelta(hours=9))


def today(tz: timezone = KST) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


def _slug(text: str, maxlen: int = 40) -> str:
    t = unicodedata.normalize("NFKC", text).strip().lower()
    t = re.sub(r"[^\w가-힣\s-]", "", t)
    t = re.sub(r"[\s_]+", "-", t).strip("-")
    return t[:maxlen] or "untitled"


@dataclass
class Idea:
    id: str
    title: str
    body: str = ""
    state: str = "inbox"
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=today)
    updated_at: str = field(default_factory=today)
    # 왜 버렸는지 / 왜 미뤘는지. dropped·parked에서 필수다.
    reason: str = ""
    # 아이디어가 실제 작업으로 이어졌을 때의 흔적.
    linked_events: list[str] = field(default_factory=list)
    linked_notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"state는 {STATES} 중 하나여야 한다: {self.state!r}")
        if self.state in ("dropped", "parked") and not self.reason.strip():
            raise ValueError(
                f"{self.state} 상태에는 reason이 필요하다. "
                f"이유 없이 버린 아이디어는 6개월 뒤 같은 형태로 다시 떠오른다."
            )


class Vault:
    """work / personal 볼트 하나를 다룬다.

    프로파일의 vault_root가 곧 이 경로다. 사내 볼트와 개인 볼트는 절대
    같은 디렉터리를 쓰지 않으며, 교차 참조도 만들지 않는다.
    """

    def __init__(self, root: str | Path, store=None):
        self.root = Path(root)
        self.ideas_dir = self.root / "ideas"
        self.notes_dir = self.root / "notes"
        self.index_path = self.root / "ideas" / "_index.jsonl"
        for d in (self.ideas_dir, self.notes_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.store = store

    # -- 포착 ----------------------------------------------------
    def capture(self, title: str, body: str = "", project: str | None = None,
                tags: Iterable[str] = (), session_id: str = "capture") -> Idea:
        """한 줄 포착. 분류를 요구하지 않는 것이 핵심이다.

        분류를 강제하면 포착이 느려지고, 느린 포착은 안 쓰이게 된다.
        """
        idx = self._load_index()
        seq = len(idx) + 1
        idea = Idea(
            id=f"idea-{today().replace('-', '')}-{seq:03d}",
            title=title.strip(),
            body=body.strip(),
            project=project,
            tags=list(tags),
        )
        idea.validate()
        self._write_idea(idea)
        self._append_index(idea)
        if self.store is not None:
            ev = self.store.log(
                "idea.captured",
                {"idea_id": idea.id, "title": idea.title, "project": project},
                session_id=session_id, project=project,
            )
            idea.linked_events.append(ev.id)
            self._write_idea(idea)
        return idea

    # -- 상태 전이 ------------------------------------------------
    def transition(self, idea_id: str, state: str, reason: str = "",
                   session_id: str = "review") -> Idea:
        idea = self.get(idea_id)
        if idea is None:
            raise KeyError(f"없는 아이디어: {idea_id}")
        if idea.state in TERMINAL and state not in TERMINAL:
            # done/dropped에서 되살리는 것은 허용한다. 다만 흔적을 남긴다.
            idea.body += f"\n\n> {today()}: {idea.state} 에서 {state} 로 되살림."
        idea.state = state
        idea.reason = reason or idea.reason
        idea.updated_at = today()
        idea.validate()
        self._write_idea(idea)
        self._rebuild_index()
        if self.store is not None:
            self.store.log("idea.promoted",
                           {"idea_id": idea_id, "state": state, "reason": reason},
                           session_id=session_id, project=idea.project)
        return idea

    # -- 조회 ----------------------------------------------------
    def get(self, idea_id: str) -> Idea | None:
        for rec in self._load_index():
            if rec["id"] == idea_id:
                return self._read_idea(Path(rec["path"]))
        return None

    def list(self, state: str | None = None, project: str | None = None) -> list[Idea]:
        out = []
        for rec in self._load_index():
            if state and rec.get("state") != state:
                continue
            if project and rec.get("project") != project:
                continue
            idea = self._read_idea(Path(rec["path"]))
            if idea:
                out.append(idea)
        return out

    def stale(self, days: int = 14) -> list[Idea]:
        """오래 방치된 inbox 항목.

        이 목록이 길어지는 것이 메모북이 죽어가는 유일한 신호다.
        주간 리뷰에서 이것부터 본다.
        """
        cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        return [i for i in self.list(state="inbox") if i.updated_at < cutoff]

    # -- 진행사항 노트 --------------------------------------------
    def daily_note(self, store, day: str | None = None, project: str | None = None) -> Path:
        """하루치 이벤트를 사람이 읽는 노트로 만든다.

        AI가 만든 것과 사람이 만든 것을 섹션으로 갈라 쓴다. 원장의 origin
        필드가 여기서 눈에 보이는 값이 된다 — 나중에 이 노트가 보고서 초안이
        될 때, 어느 숫자를 사람이 확인했는지가 그 자리에서 드러나야 한다.
        """
        day = day or today()
        start = f"{day}T00:00:00+00:00"
        end = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00+00:00")
        events = store.between(start, end, project=project)

        by_type: dict[str, list[dict]] = {}
        for e in events:
            by_type.setdefault(e["event_type"], []).append(e)

        L: list[str] = [f"# {day} 작업 노트" + (f" — {project}" if project else ""), ""]
        L.append(f"이벤트 {len(events)}건. 이 파일은 이벤트 원장에서 생성된 뷰다 — 직접 고치지 말 것.")
        L.append("")

        cmds = by_type.get("voice.command", []) + by_type.get("text.command", [])
        if cmds:
            L += ["## 지시", ""]
            L += [f"- {c['payload'].get('text', '')}" for c in cmds] + [""]

        meas = by_type.get("measurement.logged", [])
        if meas:
            L += ["## 측정 기록", "",
                  "| 시각 | 항목 | 값 | 출처 | 확인 |", "|---|---|---|---|---|"]
            for m in meas:
                p = m["payload"]
                confirmed = "✅" if p.get("confirmed_by") else "**미확인**"
                L.append(f"| {m['created_at'][11:16]} | {p.get('label', '')} | "
                         f"{p.get('value', '')} | {m['origin']} | {confirmed} |")
            L += ["",
                  "> `origin: ai` 이고 미확인인 값은 보고서에 그대로 옮기지 않는다.", ""]

        sandbox = by_type.get("sandbox.result", [])
        if sandbox:
            ok = sum(1 for s in sandbox if s["payload"].get("ok"))
            L += ["## 실행 검증", "", f"- 샌드박스 {len(sandbox)}회 중 {ok}회 성공", ""]

        ideas = by_type.get("idea.captured", [])
        if ideas:
            L += ["## 포착한 아이디어", ""]
            L += [f"- `{i['payload']['idea_id']}` {i['payload']['title']}" for i in ideas] + [""]

        pol = [e for e in by_type.get("policy.verdict", []) if e["payload"].get("decision") == "deny"]
        if pol:
            L += ["## 정책 차단", ""]
            L += [f"- {p['payload'].get('surface')}: {p['payload'].get('reason')}" for p in pol] + [""]

        redacted = [e for e in events if e["redactions"]]
        if redacted:
            L += ["## 비밀 마스킹", "",
                  f"- {len(redacted)}건의 이벤트에서 크리덴셜 패턴이 탐지돼 저장 전 치환됐다.", ""]

        path = self.notes_dir / f"{day}{'-' + _slug(project) if project else ''}.md"
        path.write_text("\n".join(L), encoding="utf-8")
        return path

    def review_note(self) -> Path:
        """주간 리뷰용 아이디어 현황."""
        L = [f"# 아이디어 리뷰 — {today()}", ""]
        counts = {s: len(self.list(state=s)) for s in STATES}
        L += ["| 상태 | 건수 |", "|---|---|"]
        L += [f"| {s} | {counts[s]} |" for s in STATES] + [""]

        st = self.stale(14)
        if st:
            L += ["## 14일 이상 방치된 inbox", "",
                  "이 목록이 길어지는 것이 메모북이 죽어가는 신호다. 여기부터 처리한다.", ""]
            L += [f"- `{i.id}` ({i.created_at}) {i.title}" for i in st] + [""]

        for state in ("active", "triaged", "inbox", "parked"):
            items = self.list(state=state)
            if items:
                L += [f"## {state} ({len(items)})", ""]
                for i in items:
                    tag = f" `{' '.join(i.tags)}`" if i.tags else ""
                    why = f" — {i.reason}" if i.reason else ""
                    L.append(f"- `{i.id}` {i.title}{tag}{why}")
                L.append("")

        path = self.notes_dir / f"review-{today()}.md"
        path.write_text("\n".join(L), encoding="utf-8")
        return path

    # -- 내부 ----------------------------------------------------
    def _idea_path(self, idea: Idea) -> Path:
        return self.ideas_dir / f"{idea.id}-{_slug(idea.title)}.md"

    def _write_idea(self, idea: Idea) -> Path:
        meta = {k: v for k, v in idea.__dict__.items() if k not in ("title", "body")}
        fm = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
        path = self._idea_path(idea)
        path.write_text(f"---\n{fm}\n---\n\n# {idea.title}\n\n{idea.body}\n", encoding="utf-8")
        return path

    def _read_idea(self, path: Path) -> Idea | None:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return None
        _, fm, body = raw.split("---", 2)
        meta: dict[str, Any] = {}
        for line in fm.strip().splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            try:
                meta[k.strip()] = json.loads(v.strip())
            except json.JSONDecodeError:
                meta[k.strip()] = v.strip()
        title = ""
        rest = []
        for ln in body.strip().splitlines():
            if ln.startswith("# ") and not title:
                title = ln[2:].strip()
            else:
                rest.append(ln)
        return Idea(title=title, body="\n".join(rest).strip(), **meta)

    def _load_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        return [json.loads(l) for l in self.index_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _append_index(self, idea: Idea) -> None:
        rec = {"id": idea.id, "state": idea.state, "project": idea.project,
               "title": idea.title, "path": str(self._idea_path(idea))}
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _rebuild_index(self) -> None:
        recs = []
        for p in sorted(self.ideas_dir.glob("idea-*.md")):
            idea = self._read_idea(p)
            if idea:
                recs.append({"id": idea.id, "state": idea.state, "project": idea.project,
                             "title": idea.title, "path": str(p)})
        self.index_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + ("\n" if recs else ""),
            encoding="utf-8")
