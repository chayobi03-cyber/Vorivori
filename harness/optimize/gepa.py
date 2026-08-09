#!/usr/bin/env python3
"""GEPA 루프 — 반사적 아티팩트 진화 (Genetic-Pareto).

PRD v2 §19가 빠뜨린 것
    §19는 "수집 대상"과 "최적화 대상"을 명사로 나열한다. 그런데 GEPA가 성립하려면
    명사가 아니라 다음 네 가지가 있어야 한다.

      1. **태스크뱅크** — 인스턴스별로 채점되는 평가 집합.
         이게 없으면 파레토 프런티어를 만들 수 없고, GEPA는 "LLM한테 프롬프트
         다시 써달라고 하기"로 전락한다. 원본 문서에는 평가 집합이 아예 없다.
      2. **텍스트 피드백을 내는 채점기** — 스칼라 점수만으로는 반사가 안 된다.
         GEPA의 핵심 주장이 "언어가 스칼라 보상보다 풍부한 학습 신호"라는 것이다.
      3. **선언된 최적화 표면** — 고칠 대상이 텍스트 파일이어야 한다.
         §7.2처럼 파이썬 if문에 하드코딩돼 있으면 손댈 수 없다.
      4. **승격 게이트** — 새 후보를 언제 정본으로 올릴지에 대한 규칙.

    이 파일은 그 넷을 구현한다.

파레토 선택을 왜 이렇게까지 하는가
    가장 흔한 오구현은 "제일 점수 높은 후보를 계속 변이시키는" 것이다. 그러면
    한 지역 최적점에 갇힌다. GEPA의 핵심은 **인스턴스 단위**로 승자를 본다는 점이다.
    전체 평균은 낮아도 어떤 인스턴스 하나를 유일하게 맞히는 후보는 살려둔다.
    서로 다른 전략이 프런티어에 공존하고, 그 다양성이 다음 세대의 재료가 된다.

외부 의존성 없음 (Python 3.11 표준 라이브러리). 프로포저는 주입 가능하며,
기본 프로포저는 오프라인으로 동작한다 — 폐쇄망에서 루프 전체가 돈다.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence


# ─────────────────────────────────────────────────────────────
# 자료형
# ─────────────────────────────────────────────────────────────
@dataclass
class Instance:
    """태스크뱅크의 한 항목."""
    id: str
    input: str
    expect: str
    note: str = ""
    split: str = "train"


@dataclass
class Result:
    """한 후보를 한 인스턴스에 돌린 결과.

    feedback이 이 자료형의 존재 이유다. score만 있으면 반사할 재료가 없다.
    """
    instance_id: str
    score: float
    feedback: str
    got: str = ""


@dataclass
class Candidate:
    """진화 대상 아티팩트 하나."""
    artifact: dict[str, Any]
    generation: int = 0
    parent: str | None = None
    proposer: str = "seed"
    cid: str = ""

    def __post_init__(self) -> None:
        if not self.cid:
            self.cid = f"g{self.generation}-{abs(hash(json.dumps(self.artifact, sort_keys=True))) % 100000:05d}"


class Proposer(Protocol):
    """반사 변이 연산자.

    이 시스템에서 프로포저는 곧 **AI 프로바이더**다. GPT가 하나, Claude가 하나,
    로컬 Qwen이 하나 제안하면 세 후보가 같은 프런티어에서 경쟁한다.
    원본 문서에서 따로 놀던 §8(멀티 AI 협업)과 §19(GEPA)가 여기서 하나가 된다.
    """

    name: str

    def propose(self, parent: Candidate, failures: Sequence[Result],
                instances: dict[str, Instance]) -> dict[str, Any] | None:
        ...


# ─────────────────────────────────────────────────────────────
# 파레토 프런티어
# ─────────────────────────────────────────────────────────────
def pareto_frontier(scores: dict[str, dict[str, float]]) -> dict[str, set[str]]:
    """후보별로 '자기가 최고점을 낸 인스턴스 집합'을 구한다.

    scores: {candidate_id: {instance_id: score}}
    반환:   {candidate_id: 그 후보가 (공동)1위인 인스턴스 id 집합}

    이 집합이 빈 후보는 어떤 인스턴스에서도 최고가 아니므로 프런티어 밖이다.
    """
    if not scores:
        return {}
    instances = {iid for per in scores.values() for iid in per}
    wins: dict[str, set[str]] = {cid: set() for cid in scores}
    for iid in instances:
        best = max(scores[cid].get(iid, 0.0) for cid in scores)
        for cid in scores:
            if scores[cid].get(iid, 0.0) >= best:
                wins[cid].add(iid)
    return {cid: w for cid, w in wins.items() if w}


def prune_dominated(wins: dict[str, set[str]]) -> dict[str, set[str]]:
    """지배당한 후보를 제거한다.

    A가 이기는 인스턴스 집합이 B의 진부분집합이면 A는 B에 지배된다 —
    A가 기여할 수 있는 다양성이 없다. 동률(같은 집합)은 하나만 남긴다.
    """
    keep: dict[str, set[str]] = {}
    for cid, w in sorted(wins.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        dominated = any(w <= other and w != other for other in keep.values())
        duplicate = any(w == other for other in keep.values())
        if not dominated and not duplicate:
            keep[cid] = w
    return keep


def sample_parent(wins: dict[str, set[str]], rng: random.Random) -> str:
    """프런티어에서 부모를 뽑는다. 이긴 인스턴스 수에 비례한 가중 추출."""
    cids = sorted(wins)
    weights = [len(wins[c]) for c in cids]
    return rng.choices(cids, weights=weights, k=1)[0]


# ─────────────────────────────────────────────────────────────
# 루프
# ─────────────────────────────────────────────────────────────
@dataclass
class GepaConfig:
    budget: int = 20               # 최대 제안 횟수(롤아웃 예산)
    minibatch: int = 12            # 반사에 쓸 학습 인스턴스 수
    seed: int = 0
    promote_min_gain: float = 0.02 # 정본 승격 최소 개선폭
    max_pool: int = 12


@dataclass
class GepaReport:
    best_cid: str
    best_train: float
    best_holdout: float
    seed_train: float
    seed_holdout: float
    iterations: int
    pool_size: int
    frontier: list[str]
    history: list[dict] = field(default_factory=list)

    @property
    def promoted(self) -> bool:
        return self.best_holdout > self.seed_holdout

    def to_dict(self) -> dict:
        d = asdict(self)
        d["promoted"] = self.promoted
        d["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return d


class Gepa:
    def __init__(self, evaluator: Callable[[dict, Instance], Result],
                 proposers: Sequence[Proposer], config: GepaConfig | None = None):
        self.evaluate = evaluator
        self.proposers = list(proposers)
        self.cfg = config or GepaConfig()
        self.rng = random.Random(self.cfg.seed)

    def _run(self, cand: Candidate, instances: Sequence[Instance]) -> list[Result]:
        return [self.evaluate(cand.artifact, inst) for inst in instances]

    @staticmethod
    def _mean(results: Sequence[Result]) -> float:
        return sum(r.score for r in results) / len(results) if results else 0.0

    def optimize(self, seed_artifact: dict, instances: Sequence[Instance]) -> tuple[Candidate, GepaReport]:
        train = [i for i in instances if i.split == "train"]
        holdout = [i for i in instances if i.split == "holdout"]
        by_id = {i.id: i for i in instances}
        if not train:
            raise ValueError("train 스플릿이 비어 있다")

        seed = Candidate(artifact=seed_artifact, generation=0, proposer="seed")
        pool: dict[str, Candidate] = {seed.cid: seed}
        scores: dict[str, dict[str, float]] = {}
        results_cache: dict[str, list[Result]] = {}

        def record(c: Candidate) -> list[Result]:
            res = self._run(c, train)
            scores[c.cid] = {r.instance_id: r.score for r in res}
            results_cache[c.cid] = res
            return res

        record(seed)
        seed_train = self._mean(results_cache[seed.cid])
        seed_holdout = self._mean(self._run(seed, holdout)) if holdout else 0.0

        history: list[dict] = []

        for step in range(self.cfg.budget):
            wins = prune_dominated(pareto_frontier(scores))
            if not wins:
                break
            parent_cid = sample_parent(wins, self.rng)
            parent = pool[parent_cid]

            # 반사 재료: 이 부모가 틀린 것들. 미니배치로 잘라 넣는다.
            failures = [r for r in results_cache[parent_cid] if r.score < 1.0]
            self.rng.shuffle(failures)
            batch = failures[: self.cfg.minibatch]
            if not batch:
                # 학습셋을 다 맞혔다. 더 짤 즙이 없다.
                history.append({"step": step, "action": "stop", "reason": "train 전건 통과"})
                break

            proposer = self.proposers[step % len(self.proposers)]
            new_artifact = proposer.propose(parent, batch, by_id)
            if new_artifact is None:
                history.append({"step": step, "proposer": proposer.name, "action": "no-proposal"})
                continue

            child = Candidate(
                artifact=new_artifact,
                generation=parent.generation + 1,
                parent=parent_cid,
                proposer=proposer.name,
            )
            if child.cid in pool:
                history.append({"step": step, "proposer": proposer.name, "action": "duplicate"})
                continue

            res = record(child)
            child_train = self._mean(res)
            parent_train = self._mean(results_cache[parent_cid])
            history.append({
                "step": step, "proposer": proposer.name, "parent": parent_cid,
                "child": child.cid, "parent_train": round(parent_train, 4),
                "child_train": round(child_train, 4),
            })

            # GEPA는 평균이 낮아도 프런티어에 기여하면 살린다.
            # 여기서 평균으로 잘라내면 다양성이 사라져 파레토를 쓰는 의미가 없다.
            pool[child.cid] = child
            if len(pool) > self.cfg.max_pool:
                frontier = prune_dominated(pareto_frontier(scores))
                for cid in sorted(pool, key=lambda c: self._mean(results_cache[c])):
                    if cid not in frontier and cid != seed.cid and len(pool) > self.cfg.max_pool:
                        pool.pop(cid); scores.pop(cid, None); results_cache.pop(cid, None)

        # 최종 선정은 holdout으로 한다. train으로 고르면 과적합을 고른다.
        if holdout:
            ranked = sorted(
                pool.values(),
                key=lambda c: (self._mean(self._run(c, holdout)), self._mean(results_cache[c.cid])),
                reverse=True,
            )
        else:
            ranked = sorted(pool.values(), key=lambda c: self._mean(results_cache[c.cid]), reverse=True)
        best = ranked[0]

        report = GepaReport(
            best_cid=best.cid,
            best_train=round(self._mean(results_cache[best.cid]), 4),
            best_holdout=round(self._mean(self._run(best, holdout)), 4) if holdout else 0.0,
            seed_train=round(seed_train, 4),
            seed_holdout=round(seed_holdout, 4),
            iterations=len(history),
            pool_size=len(pool),
            frontier=sorted(prune_dominated(pareto_frontier(scores))),
            history=history,
        )
        return best, report
