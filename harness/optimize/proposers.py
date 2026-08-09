#!/usr/bin/env python3
"""GEPA 변이 연산자(프로포저).

세 종류를 둔다. 셋 다 같은 인터페이스라 같은 파레토 풀에서 경쟁한다.

    OfflineRuleProposer  LLM 없이 실패 사례를 분석해 규칙을 고친다.
                         폐쇄망·CI에서 루프 전체가 돌게 하는 장치이며,
                         LLM 프로포저의 성능 하한선 역할을 한다.

    CliProposer          `claude` / `gemini` / `codex` 같은 CLI를 호출한다.
                         PRD §8의 멀티 AI 협업이 실제로 붙는 지점이다.
                         프로바이더마다 다른 개선안을 내므로 프런티어가 다양해진다.

    FileProposer         사람이(또는 데스크톱 앱 채팅이) 만든 제안을 디렉터리에서 읽는다.
                         반사 프롬프트를 복사해 GPT 창에 붙여넣고 결과를 저장하는
                         수동 루프. 초기에 가장 자주 쓰게 된다.

프로포저는 신뢰되지 않는 입력을 만든다. 반환된 아티팩트는 항상 Router가
검증한 뒤에만 채택된다 — 검증 실패는 그냥 '제안 없음'으로 처리한다.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from harness.optimize.gepa import Candidate, Instance, Result


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    return re.sub(r"[\s\-_·,]+", "", t)


def _all_patterns(artifact: dict) -> set[str]:
    pats: set[str] = set()
    for r in artifact["rules"]:
        pats.update(_norm(p) for p in r["any"])
        pats.update(_norm(p) for p in r.get("none", []))
    return pats


def _rule_for(artifact: dict, category: str) -> dict | None:
    for r in artifact["rules"]:
        if r["category"] == category:
            return r
    return None


def _bump_version(artifact: dict, proposer: str, parent_version: str) -> dict:
    artifact["generation"] = artifact.get("generation", 0) + 1
    artifact["lineage"] = list(artifact.get("lineage", [])) + [parent_version]
    artifact["version"] = f"v{artifact['generation']}"
    artifact["proposer"] = proposer
    return artifact


# ─────────────────────────────────────────────────────────────
# 1. 오프라인 규칙 프로포저
# ─────────────────────────────────────────────────────────────
class OfflineRuleProposer:
    """실패 사례에서 변별 n-gram을 뽑아 규칙을 고친다.

    LLM을 쓰지 않으므로 창의적인 재구성은 못 한다. 대신 결정적이고, 공짜이고,
    폐쇄망에서 돈다. LLM 프로포저가 이것보다 못하면 LLM을 쓸 이유가 없다 —
    그 비교 기준선을 제공하는 것이 이 클래스의 두 번째 역할이다.
    """

    name = "offline-rules"

    def __init__(self, rng, min_len: int = 2, max_len: int = 6):
        self.rng = rng
        self.min_len = min_len
        self.max_len = max_len

    def _ngrams(self, text: str) -> list[str]:
        n = _norm(text)
        out = []
        for size in range(self.max_len, self.min_len - 1, -1):
            for i in range(len(n) - size + 1):
                out.append(n[i:i + size])
        return out

    def propose(self, parent: Candidate, failures: Sequence[Result],
                instances: dict[str, Instance]) -> dict[str, Any] | None:
        art = copy.deepcopy(parent.artifact)
        known = _all_patterns(art)
        f = self.rng.choice(list(failures))
        inst = instances[f.instance_id]

        op = self.rng.choice(["add_positive", "add_negative", "bump_weight"])

        if op == "add_positive":
            # 기대 카테고리에 없는 변별 n-gram을 하나 추가한다.
            cands = [g for g in self._ngrams(inst.input) if g not in known]
            if not cands:
                return None
            # 너무 짧은 조각은 오탐을 부른다. 중간 길이를 선호한다.
            cands.sort(key=lambda g: (abs(len(g) - 4), g))
            rule = _rule_for(art, inst.expect)
            if rule is None:
                art["rules"].append({"category": inst.expect, "weight": 2.0,
                                     "any": [cands[0]], "none": []})
            else:
                rule["any"] = list(rule["any"]) + [cands[0]]

        elif op == "add_negative":
            # 오답 카테고리가 이 문장에서 이겼다. 오답 규칙에 차단어를 넣는다.
            wrong = _rule_for(art, f.got)
            if wrong is None or f.got == inst.expect:
                return None
            cands = [g for g in self._ngrams(inst.input) if g not in known]
            if not cands:
                return None
            cands.sort(key=lambda g: (abs(len(g) - 4), g))
            wrong["none"] = list(wrong.get("none", [])) + [cands[0]]

        else:  # bump_weight
            rule = _rule_for(art, inst.expect)
            if rule is None:
                return None
            rule["weight"] = round(float(rule.get("weight", 1.0)) + 0.5, 2)

        art["note"] = f"{op} from {inst.id} (기대 {inst.expect}, 실제 {f.got})"
        return _bump_version(art, self.name, parent.artifact.get("version", "v0"))


# ─────────────────────────────────────────────────────────────
# 2. 반사 프롬프트 — LLM 프로포저 공용
# ─────────────────────────────────────────────────────────────
REFLECTION_PROMPT = """\
당신은 음성 명령 분류기의 규칙 파일을 개선하는 역할이다.

## 현재 규칙 (JSON)
{artifact}

## 이 규칙이 틀린 사례
{failures}

## 규칙이 동작하는 방식
- 입력과 패턴은 소문자화 + 공백/하이픈 제거 후 부분문자열로 비교된다.
  따라서 "에스 파라미터"와 "에스파라미터"는 같게 취급된다.
- 규칙의 `any` 중 하나라도 걸리면 그 카테고리에 weight * 걸린개수 만큼 점수가 붙는다.
- `none` 중 하나라도 걸리면 그 규칙은 통째로 무시된다.
- 최고점 카테고리가 선택된다. 1·2위 차가 min_margin 미만이면 되묻는다.

## 지침
1. 위 실패 사례를 **왜** 틀렸는지 먼저 진단하라.
2. 이 도메인에서 의도를 정하는 것은 도메인 명사가 아니라 **동사**인 경우가 많다.
   ("임피던스 매칭 코드 짜줘"는 EMC 분석이 아니라 코드 생성이다.)
3. 과적합하지 마라. 실패 문장 전체를 패턴으로 넣는 것은 금지다.
   다른 문장에도 일반화되는 조각을 넣어라.
4. 기존에 맞히던 사례를 깨뜨리지 마라.

## 출력
개선된 규칙 파일 전체를 JSON으로만 출력하라. 설명·마크다운 펜스 없이 JSON만.
"""


def build_reflection_prompt(parent: Candidate, failures: Sequence[Result],
                            instances: dict[str, Instance]) -> str:
    lines = []
    for f in failures:
        inst = instances[f.instance_id]
        lines.append(
            f'- 입력: "{inst.input}"\n'
            f'  기대: {inst.expect} / 실제: {f.got}\n'
            f'  진단: {f.feedback}'
            + (f'\n  메모: {inst.note}' if inst.note else "")
        )
    return REFLECTION_PROMPT.format(
        artifact=json.dumps(parent.artifact, ensure_ascii=False, indent=2),
        failures="\n".join(lines),
    )


def _extract_json(text: str) -> dict | None:
    """LLM 출력에서 JSON 객체를 건져낸다. 펜스나 잡담이 붙어 나오는 것이 정상이다."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ─────────────────────────────────────────────────────────────
# 3. CLI 프로포저 — 멀티 AI 협업이 붙는 지점
# ─────────────────────────────────────────────────────────────
class CliProposer:
    """외부 AI CLI를 변이 연산자로 쓴다.

    사내 에디션에서는 로컬 vLLM을 가리키는 CLI만 등록한다. 클라우드 CLI를
    등록하면 정책 엔진의 egress 판정에서 막힌다 — 그것이 설계 의도다.

    보안: 프롬프트는 stdin으로만 넘긴다. 명령줄에 넣으면 프로세스 목록과
    셸 히스토리에 사내 문장이 남는다.
    """

    def __init__(self, name: str, argv: Sequence[str], timeout: int = 120):
        self.name = name
        self.argv = list(argv)
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.argv[0]) is not None

    def propose(self, parent: Candidate, failures: Sequence[Result],
                instances: dict[str, Instance]) -> dict[str, Any] | None:
        if not self.available():
            return None
        prompt = build_reflection_prompt(parent, failures, instances)
        try:
            proc = subprocess.run(
                self.argv, input=prompt, capture_output=True, text=True,
                timeout=self.timeout, shell=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        art = _extract_json(proc.stdout)
        if art is None or "rules" not in art or "categories" not in art:
            return None
        return _bump_version(art, self.name, parent.artifact.get("version", "v0"))


# ─────────────────────────────────────────────────────────────
# 4. 파일 프로포저 — 사람/데스크톱 앱 루프
# ─────────────────────────────────────────────────────────────
class FileProposer:
    """디렉터리에 미리 넣어둔 제안을 순서대로 소비한다.

    사용법: tools/gepa_run.py --dump-prompt 로 반사 프롬프트를 뽑아
    GPT/Claude 데스크톱 앱에 붙여넣고, 받은 JSON을 proposals/ 에 저장한다.
    다음 실행에서 이 프로포저가 집어간다.
    """

    name = "file"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self._used: set[Path] = set()

    def propose(self, parent: Candidate, failures: Sequence[Result],
                instances: dict[str, Instance]) -> dict[str, Any] | None:
        if not self.dir.is_dir():
            return None
        for p in sorted(self.dir.glob("*.json")):
            if p in self._used:
                continue
            self._used.add(p)
            try:
                art = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if "rules" in art and "categories" in art:
                return _bump_version(art, f"file:{p.name}", parent.artifact.get("version", "v0"))
        return None
