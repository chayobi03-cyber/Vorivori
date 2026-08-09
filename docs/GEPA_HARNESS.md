# GEPA 하네스 엔지니어링

원본 명세 §19를 실행 가능한 설계로 바꾼 문서.

---

## 1. GEPA를 한 문장으로

> 실행 트레이스를 **자연어 피드백**으로 되먹여 텍스트 아티팩트를 진화시키되,
> 후보를 평균 점수가 아니라 **인스턴스별 파레토 프런티어**로 관리해
> 서로 다른 전략이 공존하도록 한다.

두 요소가 각각 일을 한다.

**Reflective (반사)** — 학습 신호가 스칼라가 아니라 문장이다. `score=0.0`은
무엇을 고칠지 알려주지 않지만, "PYTHON_TOOL로 가야 하는데 '임피던스'에 걸려
EMC_ANALYSIS로 갔다"는 바로 고칠 수 있다. 언어가 스칼라보다 정보 밀도가 높다는
것이 GEPA의 전제다.

**Pareto (파레토)** — 최고점 후보만 계속 변이시키면 지역 최적점에 갇힌다.
인스턴스 단위로 승자를 보면, 평균은 낮아도 **어떤 항목을 유일하게 맞히는** 후보를
살려둘 수 있다. 그 다양성이 다음 세대의 재료가 된다.

두 번째를 빼면 GEPA가 아니라 그냥 "프롬프트 다시 써달라고 하기"다.

---

## 2. 하네스 엔지니어링으로 옮기면

GEPA는 보통 프롬프트 최적화 기법으로 소개된다. 이 프로젝트에서는 그것을
**하네스 전체의 설계 원칙**으로 승격한다. 규칙은 하나다.

> **행동을 결정하는 것은 전부 버전 관리되는 텍스트 아티팩트에 산다.**
> 파이썬 소스에는 그 아티팩트를 *해석하는* 엔진만 둔다.

이 규칙이 지켜지면 하네스의 모든 부품이 자동으로 최적화 가능해진다. 지켜지지
않으면 (§7.2의 `if "s2p" in text`처럼) 개선 루프가 손댈 것이 없다.

### 최적화 표면 목록

| 아티팩트 | 상태 | 채점 방법 | 비고 |
|---|---|---|---|
| `artifacts/router.rules.json` | **구현됨** | 라벨 정확도 + 확신도 | 참조 구현 |
| `artifacts/prompts/*.md` (에이전트별) | 예정 | 샌드박스 1회 통과율 | Phase 1 |
| `artifacts/retry.policy.json` | 예정 | 성공까지의 시도 수 | Phase 1 |
| `artifacts/context.budget.json` | 예정 | 토큰당 성공률 | Phase 2 |
| `artifacts/output.format.json` | 예정 | 음성/UI 분리 적정성 | Phase 3 |
| `artifacts/vision.trigger.json` | 예정 | 불필요 캡처 비율 | Phase 4 |

엔진(`harness/*/`)은 최적화 대상이 아니다. 엔진을 바꾸면 과거 점수와 비교할 수
없어진다.

---

## 3. GEPA가 성립하기 위한 네 가지

원본 §19가 명사만 나열하고 빠뜨린 것들이다.

### 3.1 태스크뱅크 — 인스턴스별로 채점되는 평가 집합

이것이 없으면 파레토 프런티어를 만들 수 없다. 원본 문서에는 평가 집합이 아예
없다.

`taskbank/router.jsonl` (48건):

```json
{"id":"rt-001","input":"10메가부터 1기가까지 임피던스 매칭 코드 짜줘",
 "expect":"PYTHON_TOOL",
 "note":"EMC 용어가 나오지만 요구는 코드 생성이다. 도메인 명사보다 동사가 의도를 정한다.",
 "split":"train"}
```

**작성 원칙**

1. **실제 발화를 넣는다.** 상상한 문장이 아니라 STT가 뱉을 법한 형태로.
   띄어쓰기 흔들림("에스 파라미터" / "에스파라미터")을 일부러 섞는다.
2. **함정을 의도적으로 넣는다.** rt-001·rt-028·rt-036은 도메인 명사가 의도와
   어긋나는 사례다. 쉬운 것만 모으면 점수는 오르지만 배우는 게 없다.
3. **`note`에 판단 근거를 쓴다.** 이 문장이 반사 프롬프트에 들어가 프로포저의
   진단 재료가 된다. 라벨만 있으면 프로포저는 *왜* 틀렸는지 모른다.
4. **holdout을 먼저 떼고 손대지 않는다.** train으로 고르면 과적합을 고른다.

> 48건은 작다. Phase 1 완료 시점 목표는 실사용 발화 200건이다. 다만 **작은
> 태스크뱅크라도 없는 것보다 압도적으로 낫다** — 지금은 규칙을 고칠 때 개선인지
> 퇴행인지 알 수 있다.

### 3.2 텍스트 피드백을 내는 채점기

```python
def evaluate(artifact, inst) -> Result:
    ...
    if c.category != inst.expect:
        return Result(inst.id, 0.0,
            f"{inst.expect}로 가야 하는데 {c.category}로 갔다. "
            f"걸린 패턴={c.matched}, 점수={c.score}",
            got=c.category)
```

점수는 3단계다. 이 단계 구분 자체가 신호다.

| 점수 | 의미 |
|---|---|
| 1.0 | 맞혔고 확신도 있다 |
| 0.5 | 맞혔지만 1·2위 격차가 좁다 — 되묻게 되므로 완전한 성공이 아니다 |
| 0.0 | 틀렸다 |

0.5를 둔 이유: 이진 채점이면 "간신히 맞힌 규칙"과 "확실히 맞힌 규칙"이 같은
점수를 받는다. 그러면 GEPA가 마진을 넓히는 방향으로 진화할 이유가 없어진다.

그리고 `feedback` 문자열이 이 자료형의 **존재 이유**다. 점수만 반환하면 반사할
재료가 없다.

### 3.3 승격 게이트

GEPA는 자동으로 정본을 바꾸지 않는다.

```
후보 생성 → train 점수 → 파레토 풀 진입
                              ↓
                     holdout으로 최종 선정
                              ↓
              holdout이 시드보다 높은가? ─── 아니오 → 승격 안 함
                              ↓ 예
                     Router(artifact) 로드 검증
                              ↓
                    artifacts/*.json 교체 (커밋)
                              ↓
                    게이트 G-05 회귀 하한 확인
```

`tools/gepa_run.py --promote`가 이 순서를 구현한다. `--promote` 없이는
점수만 보고하고 파일을 건드리지 않는다.

### 3.4 계보 기록

승격된 아티팩트는 자기가 어디서 왔는지 안다.

```json
{ "version": "v4", "generation": 4, "lineage": ["v0","v1","v2","v3"],
  "proposer": "offline-rules",
  "note": "add_negative from rt-028 (기대 PYTHON_TOOL, 실제 EMC_ANALYSIS)" }
```

퇴행이 발견되면 계보를 거슬러 어느 세대에서 들어왔는지 찾을 수 있다.

---

## 4. 파레토 선택 — 구현 상세

가장 자주 틀리는 부분이라 따로 쓴다.

```python
def pareto_frontier(scores):          # {cid: {iid: score}}
    wins = {cid: set() for cid in scores}
    for iid in all_instances:
        best = max(scores[cid].get(iid, 0.0) for cid in scores)
        for cid in scores:
            if scores[cid].get(iid, 0.0) >= best:
                wins[cid].add(iid)     # 공동 1위도 포함
    return {cid: w for cid, w in wins.items() if w}
```

핵심은 `>=`다. 공동 1위를 모두 남겨야 동점 전략이 보존된다.

이어서 **지배 제거**: A가 이기는 인스턴스 집합이 B의 진부분집합이면 A는 기여할
다양성이 없으므로 제거한다. 동일 집합은 하나만 남긴다.

그리고 부모 추출은 **이긴 인스턴스 수에 비례한 가중 추출**이다. 최고점 후보가
자주 뽑히되, 한 항목만 맞히는 소수파도 확률적으로 뽑힌다.

이 동작을 시험으로 고정해 두었다 —
`tests/test_harness.py::TestPareto::test_frontier_keeps_specialist_that_loses_on_average`:

```python
scores = {
    "generalist": {"i1": 0.9, "i2": 0.9, "i3": 0.0},   # 평균 0.60
    "specialist": {"i1": 0.1, "i2": 0.1, "i3": 1.0},   # 평균 0.40
}
# 평균으로 자르면 specialist가 죽는다. 파레토는 둘 다 남긴다.
```

**풀 정리 시에도 프런티어 후보는 지키지 않는다.** 평균으로 잘라내면 파레토를
쓰는 의미가 사라지기 때문이다.

---

## 5. 멀티 AI 협업 = 변이 연산자

원본 문서에서 §8(멀티 AI 협업)과 §19(GEPA)는 서로 관계없는 절이다.
**이 설계에서는 같은 것이다.**

> 프로포저(변이 연산자)가 곧 AI 프로바이더다.
> GPT가 하나, Claude가 하나, 로컬 Qwen이 하나 제안하면
> 세 후보가 같은 파레토 프런티어에서 경쟁한다.

이것이 §8.1의 `strength: architecture / refactoring / cli automation` 같은
**주관적 라벨을 측정값으로 대체**한다. "Claude가 리팩터링을 잘한다"는 인상이지만,
"Claude 제안이 프런티어에 3회 진입, GPT는 1회"는 데이터다.

```python
proposers = [
    OfflineRuleProposer(rng),                     # LLM 없이. 성능 하한선
    CliProposer("claude",  ["claude", "-p"]),
    CliProposer("gemini",  ["gemini", "-p"]),
    CliProposer("qwen",    ["ollama", "run", "qwen2.5-coder"]),
    FileProposer("proposals/"),                   # 데스크톱 앱에서 수동 투입
]
```

`OfflineRuleProposer`가 중요하다. LLM을 안 쓰므로 창의적 재구성은 못 하지만
결정적이고 공짜이며 폐쇄망에서 돈다. **LLM 프로포저가 이것보다 못하면 LLM을 쓸
이유가 없다** — 그 비교 기준선을 제공하는 것이 두 번째 역할이다.

### 프로파일에 따른 프로포저 구성

| 에디션 | 사용 가능한 프로포저 |
|---|---|
| enterprise | `OfflineRuleProposer`, 로컬 vLLM `CliProposer` |
| personal | 위 + 클라우드 CLI(claude/gemini/gpt) + `FileProposer` |

사내에서 클라우드 CLI를 등록하면 정책 엔진의 `egress` 판정에서 막힌다.
설계 의도다.

### 수동 루프 (데스크톱 앱 환경)

CLI가 없어도 돌릴 수 있다. 초기에 가장 자주 쓰게 되는 경로다.

```bash
python3 tools/gepa_run.py --dump-prompt > /tmp/reflect.txt
# → 내용을 Claude/GPT 데스크톱 앱에 붙여넣고, 받은 JSON을 proposals/ 에 저장
python3 tools/gepa_run.py --proposals proposals/ --budget 20 --promote
```

---

## 6. 실측

`tools/gepa_run.py`, 태스크뱅크 48건(train 36 / holdout 12),
프로포저는 `OfflineRuleProposer` 단독(LLM 없음), budget 60.

| seed | train | holdout |
|---|---|---|
| 시드 아티팩트 | 0.778 | 0.667 |
| 0 | 0.972 | 0.750 |
| 1 | 0.972 | 0.750 |
| 2 | — | 0.833 |
| 3 | — | 0.833 |
| 4 | — | 0.750 |

**읽는 법**

- 5개 시드 전부에서 holdout이 개선됐다. 우연이 아니다.
- **holdout 개선폭이 train보다 작다** (+0.08~0.17 vs +0.19). 과적합이 일부
  일어나고 있다. 숨기지 않는다.
- 이것은 **LLM 없이** 나온 수치다. 무작위 n-gram 변이만으로 여기까지 온다는 것은,
  거꾸로 말하면 시드 규칙(= 원본 문서의 방식)이 그만큼 개선 여지가 컸다는 뜻이다.
- LLM 프로포저를 붙였을 때의 수치는 **아직 측정하지 않았다.** 측정 전까지
  "LLM이 더 낫다"고 쓰지 않는다.

재현:

```bash
python3 tools/gepa_run.py --eval                     # 현재 정본 점수
python3 tools/gepa_run.py --budget 60 --seed 2       # 최적화
python3 tools/gepa_run.py --budget 60 --promote      # 개선 시 정본 교체
```

---

## 7. 무엇을 측정할 것인가 (Phase 1 이후)

§19가 든 "응답 만족도" 같은 항목은 채점 함수로 바꿔야 쓸 수 있다.

| 원본 §19 항목 | 실행 가능한 채점 |
|---|---|
| 실패한 프롬프트 | 샌드박스 **1회** 통과율 (재시도 없이) |
| 샌드박스 에러 로그 | 에러 유형별 분포. 같은 유형 반복 = 프롬프트 결함 |
| 재시도 횟수 | 성공까지의 평균 시도 수 (낮을수록 좋음) |
| 사용자 수정 이력 | AI 산출물이 커밋 전 수정된 라인 비율 |
| 응답 만족도 | 명시적 별점 대신 **되묻기 발생률**과 **재지시율** |

마지막 항목이 실무적으로 중요하다. 사람은 별점을 안 누른다. 그러나 "아니 그거
말고"라고 다시 말하는 것은 자연스럽게 기록된다.

---

## 8. 하지 말아야 할 것

1. **엔진을 최적화 대상에 넣지 않는다.** 엔진이 바뀌면 과거 점수와 비교할 수 없다.
2. **train으로 승격 판단하지 않는다.** 반드시 holdout.
3. **태스크뱅크를 GEPA가 고치게 하지 않는다.** 시험 문제를 채점자가 고치는 셈이다.
4. **프로포저 출력을 검증 없이 채택하지 않는다.** LLM은 깨진 JSON을 낸다.
   `evaluate()`가 무효 아티팩트를 0점 + 진단으로 돌려보내고 루프는 죽지 않는다
   (`test_broken_artifact_scores_zero_without_crashing`).
5. **점수가 오른 것을 "검증됐다"고 쓰지 않는다.** 태스크뱅크에서 오른 것이다.
   실사용 검증은 별개이며, 그 구분을 흐리지 않는다.
