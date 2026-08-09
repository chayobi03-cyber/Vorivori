# CLAUDE.md

Claude Code(및 다른 AI 어시스턴트)가 이 저장소에서 작업할 때의 규칙.

## 프로젝트 개요

**Vorivori**는 엔지니어의 음성·화면·CLI·문서·일정·실행 로그를 하나의 업무
메모리로 묶는 **업무 실행 하네스**다. 사내판(망분리)과 개인판(사외)을
하나의 코어 + 두 프로파일로 운영한다.

자매 저장소 [`Ainative`](https://github.com/chayobi03-cyber/Ainative)가
**지식**을 다루고, 이 저장소가 **실행**을 다룬다. 두 저장소는 같은 엔지니어링
규약을 공유한다 — 그 규약을 깨지 말 것.

현재 Phase 0(하네스 코어). GPU·도커·LLM·네트워크 없이 전부 돈다.

## 기술 스택

- **Python 3.11** — **표준 라이브러리만.** `harness/`와 `tools/` 전체가 외부
  의존성 0. 폐쇄망 CI에서 그대로 돌아야 하므로 **이 제약을 깨지 말 것.**
  YAML 파서, requests, pydantic 등을 추가하고 싶어지면 먼저 멈출 것.
- **Bash** — `ci/gate.sh`, `tests/run_checks.sh`
- 패키지 매니저·빌드 시스템 없음. 실행 가능한 스크립트가 전부다.

## 개발 워크플로우

실제로 실행해 성공을 확인한 명령만 기록한다.

| 목적 | 명령 |
|---|---|
| 일상 사용 (개인판) | `python3 tools/vv.py --help` |
| CI 게이트 (진입점) | `./ci/gate.sh` |
| 품질 게이트 | `./tests/run_checks.sh` |
| 단위시험 | `python3 -m unittest discover -s tests -v` |
| Phase 0 왕복 시연 | `python3 tools/demo.py` |
| 개인 프로파일로 시연 | `python3 tools/demo.py --profile profiles/personal.json` |
| 라우터 채점 | `python3 tools/gepa_run.py --eval` |
| GEPA 최적화 | `python3 tools/gepa_run.py --budget 60 --seed 2` |
| 정본 승격 | `python3 tools/gepa_run.py --budget 60 --promote` |
| 반사 프롬프트 추출 | `python3 tools/gepa_run.py --dump-prompt` |
| 정책 엔진 점검 | `python3 harness/policy/engine.py profiles/enterprise.json` |
| 샌드박스 정책 확인 | `python3 harness/sandbox/policy.py` |
| 프로바이더 라우팅 확인 | `python3 harness/providers/registry.py` |

## 규칙

### 행동을 정하는 것은 아티팩트에 둔다

`artifacts/*.json`이 최적화 대상이고, `harness/*/`는 그것을 **해석만** 한다.

라우팅 규칙·프롬프트·재시도 정책을 파이썬 소스에 하드코딩하지 말 것.
하드코딩하는 순간 GEPA 루프가 손댈 대상이 사라진다 — 원본 명세 §7.2가
정확히 그 실수를 했고, 그것이 이 저장소가 존재하는 이유 중 하나다.

**엔진은 최적화 대상이 아니다.** 엔진을 바꾸면 과거 점수와 비교할 수 없어진다.

### 아티팩트를 고쳤으면 반드시 채점한다

```bash
python3 tools/gepa_run.py --eval
```

점수를 확인하지 않은 수정은 개선인지 퇴행인지 알 수 없다. 게이트 G-05가
하한선을 지키지만, 하한선 위에서의 퇴행은 잡지 못한다.

### 태스크뱅크를 최적화 루프가 고치게 하지 않는다

시험 문제를 채점자가 고치는 셈이다. 태스크뱅크는 **사람이** 추가한다.
holdout은 특히 손대지 않는다.

### 정책 엔진을 우회하지 않는다

`egress` · `exec` · `capture` · `store` 네 표면은 **전부** `PolicyEngine`을
통과한다. "이번만 직접 호출" 같은 예외를 만들지 말 것. 예외가 생기는 순간
[`docs/SECURITY.md`](docs/SECURITY.md) 전체가 무의미해진다.

새 기능을 추가할 때 "이건 어느 표면인가"를 먼저 물을 것. 넷 중 아무것도
아니라면 정말 그런지 다시 확인할 것.

### 사내 프로파일의 불변식을 느슨하게 하지 않는다

`Profile.validate()`가 강제하는 것들 — `egress_default: deny`, 캡처 스크리닝,
클라우드 프로바이더 금지. 시험이 실패한다고 이 검사를 지우지 말 것.
검사가 옳고 시험이 틀렸을 가능성을 먼저 볼 것.

### 이벤트는 추가 전용이다

DB 트리거가 UPDATE/DELETE를 막는다. 정정은 `corrects` 필드로 **새 이벤트**를
쓴다. 트리거를 우회하는 코드를 쓰지 말 것.

새 `event_type`을 쓰려면 `EVENT_TYPES`에 먼저 등록한다. 오타로 생긴 유령
유형이 원장을 조용히 갈라놓는다.

### 생성물을 직접 고치지 않는다

`vault/notes/*.md`는 이벤트 원장에서 생성된 뷰다. 내용을 바꾸려면 원장을
바꾸거나 생성 로직을 고친다. Ainative가 `kb/chunks/`를 다루는 방식과 같다.

### 측정하지 않은 것을 검증했다고 쓰지 않는다

이 저장소에서 가장 중요한 서술 규율이다. Ainative에서 가져왔다.

- 태스크뱅크 점수 ≠ 실사용 정확도
- 추정 ≠ 실측 ([`docs/AUDIT.md`](docs/AUDIT.md) §B의 지연 표는 **추정**이다)
- 게이트 통과 = 구조 검증. 내용 검증이 아니다
- "5시드에서 개선됨"과 "개선된다"는 다른 문장이다

문서·요약·커밋 메시지에서 이 구분을 흐리지 말 것.

### 미해결 항목을 조용히 지우지 않는다

[`docs/SECURITY.md`](docs/SECURITY.md) §9의 목록은 정직하게 남긴 것이다.
해결되지 않았는데 목록에서 빼지 말 것. 특히 **녹음 고지 법무 검토**는
Phase 3의 차단 조건이다.

### 문서 작성

- 한 문단 3~5문장.
- 영어 식별자(`PreToolUse`, `Invoke-Expression`, `s2p`)는 번역하지 않고 원문 그대로.
- 표를 쓸 때 결과를 앞 열에 둔다 (한글 폭 정렬 문제).
- 왜 그렇게 했는지를 쓴다. 무엇을 했는지는 코드가 이미 말한다.

## 품질 게이트

`./ci/gate.sh`가 진입점이다. 종료 코드 0이면 통과.

| ID | 검사 | 등급 |
|---|---|---|
| G-01 | 하네스 단위시험 | FAIL |
| G-02 | 프로파일 불변식 | FAIL |
| G-03 | 라우터 아티팩트 유효성 | FAIL |
| G-04 | 태스크뱅크 정합성 | FAIL |
| G-05 | 라우터 성능 하한 (train 0.75 / holdout 0.60) | FAIL |
| G-06 | 샌드박스 정책 preflight | FAIL |
| G-07 | 볼트 경로 분리 | FAIL |
| G-08 | 볼트 비밀 스캔 | FAIL |
| G-09 | 프로바이더 라우팅 (사내는 로컬만) | FAIL |
| — | `OWNERS.yaml` 담당자 기입 | WARN (`REQUIRE_OWNERS=1`로 승격) |

## Git and branching

- Branch from the default branch; never commit directly to it.
- AI-assistant work goes on the branch assigned for the task
  (e.g. `claude/<topic>-<suffix>`). Do not push to a different branch without
  explicit permission.
- Push with `git push -u origin <branch-name>`.
- Open a pull request only when explicitly asked.
- Commit messages: short imperative subject line, body explaining *why* when the
  change is not self-evident.
- Secrets, tokens, and internal hostnames never belong in this file or in commit
  messages.

## Notes for AI assistants

- **실행해 보고 쓴다.** 이 저장소의 모든 수치는 명령을 돌려 얻은 것이다.
  새 수치를 쓸 때도 실행하고 출력을 근거로 삼는다.
- 파일 수·디렉터리 구조를 품질 신호로 읽지 않는다.
- 통과하지 못한 시험을 통과한 것처럼 요약하지 않는다.
- 게이트가 막으면 게이트를 고치기 전에 코드를 먼저 볼 것.
