# Vorivori

**Audio-Vision Work Harness** — 엔지니어의 음성·화면·CLI·문서·일정·실행 로그를
하나의 업무 메모리로 묶고, 여러 AI를 협업 도구로 라우팅하며, 샌드박스 검증과
GEPA 반사 개선 루프로 결과 품질을 올리는 업무 실행 하네스.

사내판(망분리)과 개인판(사외)을 **하나의 코어 + 두 프로파일**로 운영한다.

## 현재 상태

| 항목 | 값 |
|---|---|
| 단계 | Phase 0 완료 · Phase 1 실행 루프 진행 중 |
| 게이트 | G-01~G-10 **10/10 통과** |
| 단위시험 | 113건 통과 |
| 라우터 태스크뱅크 | 48건 (train 36 / holdout 12) |
| 라우터 정확도 | train 0.778 → **0.972**, holdout 0.667 → **0.750~0.833** (GEPA 5시드) |
| 의존성 | Python 3.11 표준 라이브러리만. 설치할 것 없음 |
| GPU / 네트워크 | **불필요** (도커는 격리 실행 시에만) |

**코어는 시험됐고 실사용은 검증되지 않았다.** 위 정확도는 태스크뱅크 48건 위의
수치이며 실사용 정확도가 아니다. 이 구분을 문서·커밋·요약에서 흐리지 않는다.

## 빠른 시작

```bash
# 매일 쓰는 진입점 (개인판)
alias vv='python3 ~/Vorivori/tools/vv.py'
vv i 쉴드캔 접지점 두 개로 늘려보기   # 아이디어 포착 — 따옴표도 플래그도 없다
vv today                              # 오늘 일정과 빈 구간
vv a 도커 이미지 빌드해줘              # 분류 + 어느 AI로 갈지
vv review                             # 주간 리뷰 + 방치 감지
vv run s2p 분석 --file analyze.py     # 샌드박스 실행 + 자가 수정 루프

# 품질 게이트 (외부 의존성 없음, 폐쇄망 가능)
./ci/gate.sh

# Phase 0 왕복 시연 — 지시 → 분류 → 정책 → 실행계획 → 원장 → 노트
python3 tools/demo.py
python3 tools/demo.py --profile profiles/personal.json

# 라우터 아티팩트 채점
python3 tools/gepa_run.py --eval

# GEPA 최적화 (LLM 없이도 동작)
python3 tools/gepa_run.py --budget 60 --seed 2
python3 tools/gepa_run.py --budget 60 --promote      # 개선 시 정본 교체

# 반사 프롬프트를 뽑아 외부 AI에 붙여넣기 (수동 협업 루프)
python3 tools/gepa_run.py --dump-prompt

# 단위시험
python3 -m unittest discover -s tests -v

# 사내 전달용 패키지 (게이트 실패 시 만들지 않음)
./tools/make_handover.sh
```

## 설계 원칙

### 1. 하네스가 모델보다 먼저다

같은 모델로 더 나은 결과를 얻는 경로를 먼저 소진한다. 하드웨어는 그 경로가
고갈됐음을 **숫자로 보인 뒤**의 선택지다. Phase 0 전체가 GPU 없이 도는 것은
이 원칙의 이행이다.

### 2. 행동을 정하는 것은 전부 텍스트 아티팩트에 산다

파이썬 소스에는 아티팩트를 *해석하는* 엔진만 둔다. 이 규칙이 지켜져야
개선 루프가 손댈 대상이 존재한다.

```
artifacts/router.rules.json   ← GEPA가 진화시킨다
harness/router/rules.py       ← 해석만 한다. 최적화 대상 아님
```

### 3. 경계는 문서가 아니라 판정기다

`egress` · `exec` · `capture` · `store` 네 위험 표면은 전부
`harness/policy/engine.py`를 통과한다. 사내 프로파일을 느슨하게 고치면
**로드 자체가 실패한다.**

```python
if self.edition == "enterprise":
    if self.egress_default != "deny":
        raise PolicyError("enterprise 프로파일의 egress_default는 deny 여야 한다")
```

### 4. 정본은 하나, 나머지는 파생

이벤트 원장(SQLite)이 정본이다. Markdown 노트·벡터 인덱스는 전부 파생 뷰이며
재생성 가능하다. 파생물을 직접 고치지 않는다.

### 5. 측정하지 않은 것을 검증했다고 쓰지 않는다

Ainative에서 가져온 규율이다. 태스크뱅크 점수와 실사용 정확도는 다른 것이고,
추정과 실측은 다른 것이다.

## 구성

| 경로 | 내용 |
|---|---|
| `harness/policy/` | 정책 엔진 — 4개 위험 표면 판정, 비밀 스캔·마스킹 |
| `harness/memory/` | 이벤트 원장(추가 전용·해시 체인), 아이디어 메모북, 노트 생성 |
| `harness/router/` | 아티팩트 해석 분류기. 한국어 STT 표기 흔들림 흡수 |
| `harness/optimize/` | GEPA 루프 — 파레토 프런티어, 반사 변이, 프로포저 3종 |
| `harness/sandbox/` | 도커 실행 정책 생성기 + preflight |
| `harness/execute/` | 상태 머신(INTERRUPT 포함), 샌드박스 백엔드, 자가 수정 루프 |
| `harness/schedule/` | 로컬 `.ics` 읽기 — 일정, 빈 구간 |
| `harness/providers/` | 프로바이더 라우팅 (역할 기반 + 승급 사다리) |
| `artifacts/` | **최적화 대상 텍스트 아티팩트** |
| `taskbank/` | GEPA 평가 인스턴스 (train / holdout) |
| `profiles/` | 사내·개인 프로파일 (정본) |
| `vault/` | work / personal 볼트 — 절대 교차하지 않음 |
| `tools/` | `vv` CLI · 시연 · GEPA 실행기 |
| `ci/gate.sh` | CI 진입점 — 어떤 CI에서든 이것만 호출 |

## 문서

| 문서 | 용도 |
|---|---|
| [`docs/HANDOVER.md`](docs/HANDOVER.md) | **인수인계 체크리스트** — 빠뜨리기 쉬운 것, 미검증 목록 |
| [`docs/DAILY_USE.md`](docs/DAILY_USE.md) | **매일 쓰기** — `vv` 명령, 일정, 리뷰 습관 |
| [`docs/AUDIT.md`](docs/AUDIT.md) | **원본 PRD v2.0 상세 점검** — 14개 항목, 심각도별 |
| [`docs/GEPA_HARNESS.md`](docs/GEPA_HARNESS.md) | GEPA 하네스 엔지니어링 설계와 실측 |
| [`docs/EXECUTION.md`](docs/EXECUTION.md) | 실행 루프 — INTERRUPT, 샌드박스 백엔드, 자가 수정 |
| [`docs/EDITIONS.md`](docs/EDITIONS.md) | 사내판 / 사외판 분리 |
| [`docs/WORK_ASSISTANT.md`](docs/WORK_ASSISTANT.md) | 아이디어 메모북 · 진행사항 · 일정 |
| [`docs/AINATIVE_INTEGRATION.md`](docs/AINATIVE_INTEGRATION.md) | Ainative 연계 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | 위험 표면과 통제 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 단계별 계획 (조달 리드타임 반영) |
| [`CLAUDE.md`](CLAUDE.md) | AI 어시스턴트 작업 규칙 |

## 원본 명세에서 바뀐 것 (요약)

전체는 [`docs/AUDIT.md`](docs/AUDIT.md).

| # | 원본 | 이 저장소 |
|---|---|---|
| A | GPU H100×2부터 조달 | Phase 0을 GPU 없이. 측정치가 조달 근거 |
| B | "응답 지연 50ms~500ms" | 구간별 예산으로 분리. `t_ack` < 150ms 신설 |
| C | 70B + 72B VLM 동시 서빙 | 32B 코더 + **7B** VLM |
| D | 망분리 선언 + 클라우드 프로바이더 병기 | 프로파일이 코드로 강제. 위반 시 로드 실패 |
| E | "PowerShell Guard" (정책 없음) | allow / confirm / deny 3단 판정 + 금지 패턴 |
| F | 화면 캡처 유출 위험 누락 | 앱 차단목록 + ROI + 스크리닝 필수 |
| H | `if "s2p" in text` 하드코딩 | 텍스트 아티팩트 + 태스크뱅크 채점 |
| I | GEPA가 명사 나열 | 태스크뱅크·텍스트 채점기·파레토·승격 게이트 구현 |
| K | AI 측정값과 사람 측정값 미구분 | `origin` 필수 + 노트에 미확인 경고 |
| M | 아이디어 포착 경로 없음 | 메모북 상태 기계 + 방치 감지 |
| N | barge-in이 상태 머신에 없음 | INTERRUPT 일급 전이 + 중단 불가 구간 + 체크포인트 |

## Ainative와의 관계

[`chayobi03-cyber/Ainative`](https://github.com/chayobi03-cyber/Ainative)는
**지식**(무엇을 아는가), Vorivori는 **실행**(무엇을 하는가)을 다룬다.
같은 엔지니어링 규약(표준 라이브러리 전용, `ci/gate.sh`, `OWNERS.yaml`,
FAIL/WARN 구분)을 공유하므로 한 CI 파이프라인에서 함께 돈다.

상세: [`docs/AINATIVE_INTEGRATION.md`](docs/AINATIVE_INTEGRATION.md)

## 다음 단계

1. 개인판을 매일 사용해 실사용 발화 수집 → 태스크뱅크 200건 (`docs/DAILY_USE.md`)
2. `OWNERS.yaml` 담당자 기입 → CI에 `REQUIRE_OWNERS=1`
3. Phase 1 잔여: 모델 연결(코드 생성) + `py-emc` 이미지 빌드 + 도커 경로 실측
4. Phase 3 착수 전 **녹음 고지 법무 검토** (차단 조건)
