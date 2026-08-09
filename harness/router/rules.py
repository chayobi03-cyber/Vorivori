#!/usr/bin/env python3
"""작업 분류기 — 규칙이 코드가 아니라 **텍스트 아티팩트**에 산다.

왜 이렇게 하는가
    PRD v2 §7.2는 분류기를 이렇게 쓴다.

        if "s2p" in text or "smith" in text:
            return "EMC_ANALYSIS"

    두 가지가 잘못됐다.

    1. 한국어 STT 출력에 대해 거의 동작하지 않는다. "S 파라미터"는 STT가
       "에스파라미터" / "s파라미터" / "SP 라미터" 등으로 뱉는다. 공백과
       표기 흔들림을 흡수하지 않으면 매번 빗나간다.
    2. **최적화할 수 없다.** §19가 GEPA로 개선하겠다고 선언한 대상이
       파이썬 소스에 하드코딩돼 있으면, 개선 루프가 손댈 수 있는 것이 없다.

    그래서 규칙을 `artifacts/router.rules.json`으로 뺐다. 사람도 LLM도 고칠 수
    있는 텍스트이고, taskbank로 채점되며, GEPA가 변이시키는 대상이 된다.
    엔진(이 파일)은 규칙을 해석만 한다 — 엔진은 최적화 대상이 아니다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "artifacts" / "router.rules.json"


def normalize(text: str) -> str:
    """매칭용 정규화.

    한국어 음성 인식 출력은 띄어쓰기가 불안정하다. "에스 파라미터"와
    "에스파라미터"가 같은 것으로 취급되도록 공백·하이픈을 지우고,
    전각/합성 문자를 NFKC로 접는다.
    """
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[\s\-_·,]+", "", t)
    return t


@dataclass
class Classification:
    category: str
    score: float
    matched: list[str]
    runner_up: str | None
    margin: float
    _min_margin: float = 0.5
    fallback: bool = False

    @property
    def confident(self) -> bool:
        """1·2위 격차가 좁으면 확신하지 않는다.

        확신이 없을 때 조용히 틀린 에이전트로 넘기는 것보다 되묻는 편이 낫다.
        이 임계값 자체도 아티팩트에 있고 GEPA 최적화 대상이다.

        단, 아무 규칙도 걸리지 않아 기본값으로 떨어진 경우는 애매한 것이 아니라
        '해당 없음'이라는 확정 판단이다. 잡담에 대고 되묻지 않는다.
        """
        if self.fallback:
            return True
        return self.margin >= self._min_margin


class Router:
    def __init__(self, artifact: dict[str, Any]):
        self.artifact = artifact
        self.categories: list[str] = artifact["categories"]
        self.default: str = artifact.get("default", "GENERAL")
        self.min_margin: float = float(artifact.get("min_margin", 0.5))
        self.rules = artifact["rules"]
        self._validate()

    @staticmethod
    def load(path: str | Path = DEFAULT_ARTIFACT) -> "Router":
        return Router(json.loads(Path(path).read_text(encoding="utf-8")))

    def _validate(self) -> None:
        cats = set(self.categories)
        for i, r in enumerate(self.rules):
            if r["category"] not in cats:
                raise ValueError(f"rules[{i}]: 미정의 카테고리 {r['category']!r}")
            if not r.get("any"):
                raise ValueError(f"rules[{i}]: 'any' 패턴이 비어 있다")
        if self.default not in cats:
            raise ValueError(f"default {self.default!r}가 categories에 없다")

    def classify(self, text: str) -> Classification:
        norm = normalize(text)
        scores: dict[str, float] = {c: 0.0 for c in self.categories}
        matched: dict[str, list[str]] = {c: [] for c in self.categories}

        for rule in self.rules:
            cat = rule["category"]
            weight = float(rule.get("weight", 1.0))
            hits = [p for p in rule["any"] if normalize(p) in norm]
            if not hits:
                continue
            blocked = [p for p in rule.get("none", []) if normalize(p) in norm]
            if blocked:
                continue
            scores[cat] += weight * len(hits)
            matched[cat].extend(hits)

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], self.categories.index(kv[0])))
        top, top_score = ranked[0]
        second, second_score = ranked[1] if len(ranked) > 1 else (None, 0.0)

        if top_score == 0.0:
            return Classification(self.default, 0.0, [], None, 0.0, self.min_margin, fallback=True)
        return Classification(
            top, top_score, matched[top], second, top_score - second_score, self.min_margin
        )


if __name__ == "__main__":
    import sys

    r = Router.load()
    samples = sys.argv[1:] or [
        "10메가부터 1기가까지 임피던스 매칭 코드 짜줘",
        "이 에스 파라미터 그래프에서 반사 큰 구간 찾아줘",
        "오늘 오후 일정 뭐 있지",
        "도커 이미지 다시 빌드해서 올려줘",
        "방금 아이디어 하나 떠올랐는데 페라이트 코어 위치 바꾸는 거",
    ]
    for s in samples:
        c = r.classify(s)
        flag = "" if c.confident else "  ← 확신 부족, 되물어야 함"
        print(f"{c.category:18} {c.score:5.1f} (margin {c.margin:.1f}) {s}{flag}")
