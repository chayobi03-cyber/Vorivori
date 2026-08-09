#!/usr/bin/env python3
"""라우터 아티팩트에 GEPA 루프를 돌린다.

    python3 tools/gepa_run.py --eval                 # 현재 정본 채점만
    python3 tools/gepa_run.py --budget 40            # 최적화 실행 (오프라인 프로포저)
    python3 tools/gepa_run.py --budget 40 --promote  # 개선됐으면 정본 교체
    python3 tools/gepa_run.py --dump-prompt          # 반사 프롬프트를 stdout으로

외부 의존성 없음. --cli 옵션을 주지 않으면 네트워크도 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.optimize.gepa import Gepa, GepaConfig, Instance, Result, Candidate  # noqa: E402
from harness.optimize.proposers import (  # noqa: E402
    CliProposer, FileProposer, OfflineRuleProposer, build_reflection_prompt,
)
from harness.router.rules import Router  # noqa: E402

ARTIFACT = ROOT / "artifacts" / "router.rules.json"
TASKBANK = ROOT / "taskbank" / "router.jsonl"


def load_instances(path: Path) -> list[Instance]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(Instance(**json.loads(line)))
    return out


def evaluate(artifact: dict, inst: Instance) -> Result:
    """라우터 채점기.

    점수는 0/0.5/1.0 세 단계다. 이 단계 구분이 GEPA에 주는 신호의 핵심이다.

      1.0  맞혔고 확신도 있다
      0.5  맞혔지만 1·2위 격차가 좁다 — 되묻게 되므로 완전한 성공이 아니다
      0.0  틀렸다

    스칼라만 주지 않고 feedback 문자열을 함께 낸다. 반사 프로포저는
    점수가 아니라 이 문장을 읽고 규칙을 고친다.
    """
    try:
        router = Router(artifact)
    except (ValueError, KeyError) as e:
        # 프로포저가 깨진 아티팩트를 냈다. 0점 + 진단으로 돌려보낸다.
        return Result(inst.id, 0.0, f"아티팩트 무효: {e}", got="<invalid>")

    c = router.classify(inst.input)
    if c.category != inst.expect:
        return Result(
            inst.id, 0.0,
            f"{inst.expect}로 가야 하는데 {c.category}로 갔다. "
            f"걸린 패턴={c.matched}, 점수={c.score}",
            got=c.category,
        )
    if not c.confident:
        return Result(
            inst.id, 0.5,
            f"{inst.expect}로 맞게 갔으나 2위 {c.runner_up}와 격차가 "
            f"{c.margin:.1f}뿐이라 되묻게 된다",
            got=c.category,
        )
    return Result(inst.id, 1.0, "정확", got=c.category)


def score_report(artifact: dict, instances: list[Instance], label: str) -> dict:
    per_split: dict[str, list[Result]] = {}
    wrong: list[Result] = []
    for inst in instances:
        r = evaluate(artifact, inst)
        per_split.setdefault(inst.split, []).append(r)
        if r.score < 1.0:
            wrong.append(r)
    out = {"label": label}
    for split, rs in sorted(per_split.items()):
        out[split] = round(sum(r.score for r in rs) / len(rs), 4)
        out[f"{split}_n"] = len(rs)
    out["imperfect"] = [(r.instance_id, r.score, r.feedback) for r in wrong]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", action="store_true", help="현재 정본 채점만 하고 끝낸다")
    ap.add_argument("--budget", type=int, default=30)
    ap.add_argument("--minibatch", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--promote", action="store_true", help="holdout이 개선되면 정본을 교체한다")
    ap.add_argument("--cli", action="append", default=[],
                    help="LLM 프로포저 추가. 형식: 이름=명령 (예: claude='claude -p')")
    ap.add_argument("--proposals", default=None, help="FileProposer가 읽을 디렉터리")
    ap.add_argument("--dump-prompt", action="store_true", help="반사 프롬프트만 출력")
    ap.add_argument("--json", default=None, help="리포트 JSON 저장 경로")
    args = ap.parse_args()

    seed_artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    instances = load_instances(TASKBANK)

    if args.eval:
        rep = score_report(seed_artifact, instances, ARTIFACT.name)
        print(json.dumps({k: v for k, v in rep.items() if k != "imperfect"},
                         ensure_ascii=False, indent=2))
        if rep["imperfect"]:
            print(f"\n미완 {len(rep['imperfect'])}건:")
            for iid, sc, fb in rep["imperfect"]:
                print(f"  [{sc}] {iid}: {fb}")
        return 0

    if args.dump_prompt:
        cand = Candidate(artifact=seed_artifact)
        by_id = {i.id: i for i in instances}
        fails = [evaluate(seed_artifact, i) for i in instances if i.split == "train"]
        fails = [f for f in fails if f.score < 1.0][: args.minibatch]
        if not fails:
            print("실패 사례 없음 — 반사할 재료가 없다", file=sys.stderr)
            return 1
        print(build_reflection_prompt(cand, fails, by_id))
        return 0

    rng = random.Random(args.seed)
    proposers: list = [OfflineRuleProposer(rng)]
    for spec in args.cli:
        name, _, cmd = spec.partition("=")
        if not cmd:
            print(f"--cli 형식 오류: {spec!r} (이름=명령)", file=sys.stderr)
            return 2
        proposers.append(CliProposer(name, cmd.split()))
    if args.proposals:
        proposers.append(FileProposer(args.proposals))

    gepa = Gepa(evaluate, proposers,
                GepaConfig(budget=args.budget, minibatch=args.minibatch, seed=args.seed))
    best, report = gepa.optimize(seed_artifact, instances)

    print(f"프로포저      : {[p.name for p in proposers]}")
    print(f"제안 시도     : {report.iterations}회 / 풀 {report.pool_size}개")
    print(f"파레토 프런티어: {len(report.frontier)}개 후보")
    print(f"train   {report.seed_train:.3f} → {report.best_train:.3f}")
    print(f"holdout {report.seed_holdout:.3f} → {report.best_holdout:.3f}"
          f"  {'(개선)' if report.promoted else '(개선 없음)'}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"report": report.to_dict(), "best": best.artifact},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"리포트 저장: {args.json}")

    if args.promote:
        if not report.promoted:
            print("holdout 개선이 없어 승격하지 않는다.")
            return 0
        # 승격 전 마지막 관문: 아티팩트가 실제로 로드되는가.
        Router(best.artifact)
        ARTIFACT.write_text(json.dumps(best.artifact, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"정본 교체 완료: {ARTIFACT} (generation {best.artifact.get('generation')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
