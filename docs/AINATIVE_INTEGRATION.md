# Ainative 연계

대상: [`chayobi03-cyber/Ainative`](https://github.com/chayobi03-cyber/Ainative) v3.0

---

## 1. 두 저장소의 관계

겹치지 않는다. **역할이 직교한다.**

| | Ainative | Vorivori |
|---|---|---|
| 다루는 것 | 지식 (무엇을 아는가) | 실행 (무엇을 하는가) |
| 산출물 | RAG 청크 100개 + 위키 22종 | 이벤트 원장 + 아티팩트 + 노트 |
| 검증 대상 | 청크 품질 (T-01~T-13) | 하네스 동작 (G-01~G-08) |
| 시간축 | 분기 재검토 | 세션 단위 |
| 변화 속도 | 느림 (지식은 천천히 낡는다) | 빠름 (매 세션 이벤트) |

한 문장으로: **Ainative는 Vorivori가 참조하는 기억이고, Vorivori는 Ainative가
갱신되는 경로다.**

---

## 2. 같은 엔지니어링 방언

이것이 실질적으로 가장 중요한 연계다. Vorivori는 Ainative의 규약을 그대로 따랐다.

| 규약 | Ainative | Vorivori |
|---|---|---|
| 의존성 | Python 3.11 표준 라이브러리만 | 동일 |
| CI 진입점 | `ci/gate.sh` | 동일 |
| 게이트 스크립트 | `tests/run_checks.sh` (`run()` 헬퍼) | 동일 패턴 |
| 담당자 | `OWNERS.yaml`, 미기입 시 경고 | 동일 |
| 차단/경고 구분 | FAIL / WARN | 동일 |
| 생성물 직접 수정 금지 | `kb/chunks/`는 빌더 출력 | `vault/notes/`는 원장 파생 |
| 정정 원장 | `kb/corrections.yaml` | 이벤트 `corrects` 필드 |
| 검증 언어 규율 | "측정하지 않은 것을 검증했다고 쓰지 않는다" | 동일 |

**얻는 것**: 두 저장소를 한 CI 파이프라인에서 돌릴 때 설정이 갈라지지 않는다.
그리고 한쪽에서 일하던 사람이 다른 쪽에서 바로 일할 수 있다.

```yaml
# 사내 CI에서
- run: ./ci/gate.sh          # ainative/
- run: ./ci/gate.sh          # vorivori/
```

---

## 3. Ainative에서 가져온 설계 판단 세 가지

### 3.1 에셋 스크리닝 → 화면 캡처 정책

Ainative `kb/schema.md`:

> **`screened: false`인 에셋은 빌드가 거부한다.** 사내 문서 이미지에는 콘솔
> 캡처·대시보드 스크린샷 형태로 토큰과 개인정보가 실제로 자주 들어 있다.

원본 PRD는 화면 캡처의 유출 위험을 통째로 빠뜨렸다([`AUDIT.md`](AUDIT.md) §F).
Ainative가 **이미 같은 문제를 풀어놨으므로** 규칙을 그대로 가져왔다.

```python
if self.profile.capture_require_screening and not screened:
    return Verdict("confirm", "capture", app, "require_screening", "스크리닝 미완료")
```

### 3.2 `confidence` 라벨 → `origin` 필드

Ainative는 자동 생성물에 생성 시점 라벨을 붙인다(`verified` / `mixed` /
`auto-merged` / `draft`). 사람이 검증한 것과 자동 생성된 것을 섞지 않기 위해서다.

Vorivori의 `origin`(`human` / `ai` / `instrument` / `system`)이 같은 역할이다.
적용 지점은 다르다 — Ainative는 청크, Vorivori는 **측정값**. 후자는
[`AUDIT.md`](AUDIT.md) §K에서 보듯 보고서를 거쳐 제품으로 흘러간다.

### 3.3 "구조는 검증됐고 내용은 검증되지 않았다"

Ainative README의 이 한 줄이 이 저장소의 서술 규율이 됐다.

Vorivori 현재 상태로 옮기면: **하네스 코어는 시험됐고, 실사용은 검증되지
않았다.** GEPA 점수 개선은 태스크뱅크 48건 위에서의 수치이며 실사용 정확도가
아니다. README와 커밋 메시지에서 이 구분을 흐리지 않는다.

---

## 4. 실제 데이터 연계

### 4.1 Ainative KB → Vorivori RAG (Phase 2)

Ainative는 이미 색인 준비가 끝나 있다. Vorivori가 새로 만들 것이 없다.

| 필요한 것 | Ainative가 제공하는 것 |
|---|---|
| 색인 대상 정의 | `kb/ingestion.yaml` (include/exclude 확정) |
| 청크 메타데이터 | `kb/manifest.jsonl` (100건) |
| 임베딩 입력 텍스트 | `kb/embeddings.jsonl` (조립 완료) |
| 적재 어댑터 | `tools/upload_vectors.py` (6종, dry-run 통과) |
| 품질 보증 | `tests/run_checks.sh` 8/8 |

**연계 규칙 — Ainative가 이미 문서화한 것을 지킨다.**

1. **`kb/chunks/*.md`만 색인한다.** `docs/`·`sources/`·`authored/`·`_inputs/`를
   같이 넣으면 같은 내용이 두 번 색인돼 순위가 조용히 망가진다
   (Ainative가 v2.3에서 실제로 겪은 결함).
2. **`confidence` 필터를 실제로 건다.** 사실 확인이 필요한 질의에는
   `strict`(= `verified`만). Ainative는 이 필터를 "retrieval_owner의 핵심 책무"로
   못박아 뒀다.
3. **`retrieval_questions`를 색인에 넣지 않는다.** 골든셋이 이 질문에서 나왔으므로
   같이 색인하면 정답을 색인해 두고 찾는 셈이 된다(실측 Recall@10 = 1.000, 순수 누출).

### 4.2 Vorivori → Ainative (Phase 3 이후)

역방향. 실행에서 얻은 지식이 KB로 돌아간다.

```
Vorivori 이벤트 원장
   │  반복 실패 패턴, 해결된 트러블슈팅, 확정된 운영 절차
   ▼
후보 노트 (vault/work/notes/)
   │  사람이 선별 — 자동 반영하지 않는다
   ▼
Ainative kb/authored/   (confidence: draft)
   │  tools/build_v3.py 재빌드
   ▼
kb/chunks/  →  게이트 통과 후 색인
```

**자동화하지 않는다.** Ainative의 `confidence: draft`는 "신규 집필, 사내 실정
미반영"이라는 뜻이고, 사람 검토를 전제한다. 실행 로그에서 자동 생성한 청크를
`verified`로 올리는 순간 KB 신뢰도가 무너진다.

### 4.3 EMC 수치 데이터는 KB에 넣지 않는다

원본 PRD §12.1이 정확히 지적한 것이고, Ainative의 색인 정책과도 맞는다.

| KB로 (Qdrant) | 샌드박스로 (Tool-calling) |
|---|---|
| 설계 가이드라인, 규격 해설 | `.s2p` / `.s4p` 원본 |
| 부품 선정 기준 | 복소 임피던스 배열 |
| 트러블슈팅 절차 | 계측기 CSV raw 로그 |
| 운영 규약 | 시계열 측정 데이터 |

수치는 임베딩하지 말고 `scikit-rf` / `pandas`로 다룬다. 그래서
`harness/sandbox/policy.py`의 `BAKED_PACKAGES`에 `scikit-rf`가 들어 있다
(`network: none`이므로 실행 시점 설치가 불가능하다).

---

## 5. 겹치는 관심사와 그 정리

두 저장소가 모두 다루는 주제들. **어느 쪽이 정본인지 정해 둔다.**

| 주제 | 정본 | 다른 쪽 |
|---|---|---|
| MCP 보안·게이트웨이 | Ainative `kb/chunks/v3-mcp-*` | Vorivori는 참조만 |
| 모델 라우팅·비용 | Ainative `v3-model-routing`, `v3-cost-visibility` | Vorivori 프로파일이 이 지식을 구현 |
| 샌드박스·dry-run 확인 | Ainative `v3-dry-run-confirm` | Vorivori `policy/engine.py`가 구현 |
| 평가 방법론 | Ainative `v3-eval-methodology` | Vorivori 태스크뱅크가 사례 |
| 프롬프트 템플릿 | Ainative `v3-prompt-templates` | Vorivori `artifacts/`가 실물 |
| 회귀 게이트 | Ainative `v3-regression-gate` | Vorivori `ci/gate.sh`가 실물 |

패턴이 보인다. **Ainative가 "이렇게 해야 한다"를 쓰고, Vorivori가 그것을
실행 가능한 형태로 구현한다.** 그래서 Vorivori는 Ainative KB의 첫 번째
실증 사례이기도 하다 — 문서가 실제로 구현 가능한지 확인하는 경로.

이 관계가 유용한 이유: Ainative 청크가 틀렸거나 모호하면 Vorivori를 구현하다가
드러난다. 그때 `kb/corrections.yaml`에 1차 출처와 함께 등록한다.

---

## 6. 지금 할 것 / 나중에 할 것

**지금 (Phase 0~1)**

- [x] 게이트 계약 통일 (`ci/gate.sh`)
- [x] 의존성 제약 통일 (표준 라이브러리만)
- [x] `OWNERS.yaml` 도입
- [ ] Ainative `OWNERS.yaml` 담당자 기입 — Vorivori 담당자와 함께 정하는 것이 낫다
- [ ] 두 저장소를 한 CI 파이프라인에 등록

**나중에 (Phase 2 이후)**

- [ ] Qdrant 적재 (Ainative `upload_vectors.py` 실적재는 아직 미검증)
- [ ] Vorivori RAG가 `ingestion.yaml` 규칙을 따르는지 게이트에 추가
- [ ] `confidence: strict` 필터가 실제로 걸려 있는지 확인하는 시험
- [ ] Vorivori 노트 → `kb/authored/` 후보 추출 도구

> Ainative README가 밝힌 대로 벡터 DB **실적재는 아직 검증되지 않았다**
> (dry-run만 통과). Vorivori RAG 연동은 그 검증 이후로 잡는다.
