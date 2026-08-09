# 인수인계 체크리스트

작성 2026-08-09.

이 문서는 "무엇이 있는가"가 아니라 **"무엇을 빠뜨리기 쉬운가"**를 다룬다.
구성과 사용법은 [`README.md`](../README.md), [`CLAUDE.md`](../CLAUDE.md),
[`DAILY_USE.md`](DAILY_USE.md)에 있다.

정리 기준: **빠뜨렸을 때 손실이 큰 순서.**

---

## 0. 이 인수인계가 무엇인지 (그리고 무엇이 아닌지)

| | |
|---|---|
| **이것은** | Phase 0(하네스 코어) + Phase 1(실행 루프) 코드와 설계의 인수인계 |
| **이것은 아니다** | 사내 배포 준비 완료 상태 |

넘기는 것은 **검토·검증받을 물건**이지, 켜면 도는 시스템이 아니다.
§B의 미검증 목록을 먼저 볼 것.

한 문장으로: **구조는 시험됐고 사내 환경에서는 아무것도 검증되지 않았다.**

---

## A. 지금 처리하지 않으면 되돌릴 수 없는 것

### A-1. 이 저장소는 **공개(public)** 다 — 실제 사내 정보를 커밋하지 말 것

`https://github.com/chayobi03-cyber/Vorivori` 는 public이다.
`profiles/enterprise.json`에 들어 있는 값은 **전부 가짜 자리표시자**다.

```json
"endpoint": "http://gpu-node-1.internal:8000/v1",
"pinned_to": ["*.corp.local", "EMC-WS-*"]
```

실제 GPU 노드 주소, 워크스테이션 명명규칙, 내부 도메인을 이 파일에 적어
커밋하면 **그 커밋 자체가 사내 정보 유출**이다. git 히스토리는 지워도 남는다.

**조치**

- [ ] 실값은 `profiles/enterprise.local.json`에 둔다.
      `.gitignore`에 `profiles/*.local.json`이 이미 있다 — 확인만 하면 된다
- [ ] 사내에서 쓸 때는 `VORIVORI_PROFILE=profiles/enterprise.local.json`
- [ ] 저장소를 사내 GitLab으로 미러링할지, public을 유지할지 결정
- [ ] public 유지 시: 사내 정보가 들어갈 수 있는 파일을 목록화해 공유
      (`profiles/`, `taskbank/`, `vault/`, 커밋 메시지)

> `taskbank/router.jsonl`도 주의 대상이다. 실사용 발화를 그대로 넣으면
> 프로젝트명·부품명이 public 저장소에 올라간다. 문형만 남기고 고유명사는 뺀다.

### A-2. 녹음 고지 법무 검토 — **Phase 3 차단 조건**

회의 중 음성이 잡히면 동료의 발화가 동의 없이 이벤트 원장에 들어간다.
개인정보보호법 적용 여부를 이 저장소는 판단할 수 없다.

- [ ] 법무 검토 요청 (다자 발화 녹음·저장·처리)
- [ ] 고지 방식 확정 (물리 표시? 세션 시작 시 안내?)
- [ ] 검토 결과를 [`SECURITY.md`](SECURITY.md) §9에 반영

**검토 전에 Phase 3(음성)을 시작하지 말 것.** 나중에 되돌리려면 이미 쌓인
원장을 전부 폐기해야 한다.

### A-3. 보안 검토를 받지 않았다

아직 아무도 보안 관점에서 보지 않았다. 검토 요청 시 다음을 함께 낼 것.

| 대상 | 문서 |
|---|---|
| 위험 표면 4종과 통제 | [`SECURITY.md`](SECURITY.md) |
| PowerShell `exec` 정책 | [`SECURITY.md`](SECURITY.md) §3, `profiles/*.json` |
| 샌드박스 하드닝 | [`SECURITY.md`](SECURITY.md) §6, `harness/sandbox/policy.py` |
| 미해결 항목 | [`SECURITY.md`](SECURITY.md) §9 |

- [ ] 보안팀 검토 요청
- [ ] **`exec` 표면을 특히 볼 것** — 이 시스템에서 유일하게 호스트에서
      격리 없이 도는 컴포넌트다
- [ ] 검토 의견을 `SECURITY.md`에 반영. 지적을 목록에서 빼지 말 것

### A-4. `OWNERS.yaml`이 비어 있다

담당자 없는 하네스는 6개월이면 썩는다. 게이트가 매번 경고한다.

- [ ] `harness_owner` / `policy_owner` 기입 (같은 사람이어도 되지만 **명시**)
- [ ] CI에 `REQUIRE_OWNERS=1` 설정 → 경고가 차단으로 승격
- [ ] Ainative의 `OWNERS.yaml`도 비어 있다. **함께 정하는 것이 낫다**

---

## B. 검증되지 않은 것 — "된다"고 쓰지 말 것

이 절이 이 문서에서 가장 중요하다. 각 항목은 **왜 아직 검증 못 했는지**와
**어떻게 검증하는지**를 함께 적었다.

### B-1. 도커 실행 경로가 검증되지 않았다 — **사내판에 치명적**

`DockerBackend`는 **사내 에디션이 유일하게 허용하는 백엔드**다.
그런데 개발 환경에 도커 데몬이 없어 실제 컨테이너 실행을 확인하지 못했다.

| 검증된 것 | 검증 안 된 것 |
|---|---|
| `docker run` 인자 구성 | 실제 컨테이너 기동 |
| 정책 preflight (`--network none` 등) | stdin으로 코드 전달 |
| 데몬 없을 때의 실패 처리 | 타임아웃 시 SIGKILL 동작 |
| 사내 프로파일의 백엔드 거부 | 읽기 전용 루트에서 파이썬 기동 |

**즉 사내 에디션은 지금 실행 기능이 동작하는지 알 수 없다.**

- [ ] 도커 있는 장비에서 §D-2 순서대로 확인
- [ ] 확인 전까지 "샌드박스 실행이 된다"고 보고하지 말 것

### B-2. `py-emc` 이미지가 빌드되지 않았다

`infra/docker/Dockerfile.py-emc`는 작성만 됐다. `scikit-rf` 설치가
`python:3.11-slim`에서 깨끗하게 되는지 확인되지 않았다.

- [ ] 인터넷 되는 곳에서 빌드
- [ ] 이미지 크기 확인 (반입 매체 용량 계획에 필요)
- [ ] `--user 65534` + `--read-only`에서 matplotlib이 뜨는지 확인
      (폰트 캐시 때문에 자주 깨지는 지점이다)

### B-3. 코드 **생성**이 없다 — 루프의 마지막 조각

`vv run`은 `--code`/`--file`로 코드를 받는다. 모델이 코드를 만드는 부분은
비어 있다(`codegen` 자리).

즉 현재 검증된 것은 **루프 구조**이지 **자동화**가 아니다.

- [ ] Ollama 또는 소형 vLLM 연결 (`harness/execute/loop.py`의 `codegen`)
- [ ] 연결 후 **샌드박스 1회 통과율**을 측정 — 이후 모든 개선의 기준선이다

### B-4. 성능 수치의 성격

| 수치 | 성격 |
|---|---|
| 라우터 train 0.972 / holdout 0.750~0.833 | **태스크뱅크 48건 위의 값.** 실사용 정확도 아님 |
| [`AUDIT.md`](AUDIT.md) §B 지연 표 | **추정.** 실측 아님. Phase 3에서 교체 |
| [`AUDIT.md`](AUDIT.md) §C VRAM 계산 | **개략 계산.** 실제 서빙으로 확인 필요 |
| 게이트 10/10 | **구조 검증.** 내용·실사용 검증 아님 |

- [ ] 보고서·품의서에 인용할 때 이 구분을 유지할 것
- [ ] 특히 §C의 VRAM 근거로 하드웨어 사양을 확정하기 전에 실측할 것

### B-5. 태스크뱅크가 작다 (48건)

작은 평가셋 위의 점수는 흔들린다. holdout 12건이면 1건 차이가 8%p다.

- [ ] 실사용 발화 200건 목표 (개인판을 매일 쓰면 자연히 쌓인다)
- [ ] 추가는 **사람이** 한다. GEPA가 태스크뱅크를 고치게 하지 말 것

---

## C. 사내 도입 시 결정해야 하는 것

전부 `profiles/enterprise.local.json`에 들어간다. 기본값은 **추측**이다.

| 항목 | 현재 값 | 결정 필요 |
|---|---|---|
| `pinned_to` | `*.corp.local`, `EMC-WS-*` | 실제 워크스테이션 명명규칙 |
| `providers[].endpoint` | `gpu-node-1.internal` | 실제 GPU 노드 |
| 모델 | Qwen2.5-Coder-32B / Qwen2-VL-7B | 반입 가능 모델 확인 |
| `exec_allow` | 읽기 전용 5종 | **실제 업무 명령을 보고 다시 짤 것** |
| `exec_deny` | 위험 패턴 14종 | 보안팀 검토 후 확정 |
| `capture_allow_apps` | VSCode·터미널·PDF | 실제 쓰는 EMC 툴 추가 |
| `store_retention_days` | 180 | 사규·법무 확인 |
| `approvals` | user / admin / security | 실제 승인자 지정 |

**`exec_allow`가 가장 중요하다.** 너무 좁으면 매번 승인 창이 떠서 아무도 안
쓰고, 안 쓰이는 통제는 우회된다. 실제 쓰는 명령을 2주 관찰한 뒤 정하는 것을
권한다.

- [ ] 위 표 전부 채우기
- [ ] 채운 뒤 `./ci/gate.sh` 재실행 (G-02가 불변식을 다시 검사한다)

---

## D. 반입·설치 절차

### D-1. 코드 반입 — 파일 복사로 끝난다

표준 라이브러리만 쓰므로 pip도 인터넷도 필요 없다.

```bash
unzip vorivori-handover-<date>.zip
cd vorivori-*/
./ci/gate.sh          # 사내 환경에서 먼저 돌려볼 것
```

- [ ] 사내 Python 3.11 이상 확인 (`python3 --version`)
- [ ] 게이트 10/10 통과 확인. **여기서 깨지면 그다음은 의미가 없다**
- [ ] `python3 tools/demo.py` 로 왕복 확인

### D-2. 도커 이미지 반입 (§B-1·B-2 검증 포함)

```bash
# 인터넷 되는 곳에서
docker build -f infra/docker/Dockerfile.py-emc -t vorivori/py-emc:0.1 .
docker save vorivori/py-emc:0.1 | gzip > py-emc-0.1.tar.gz
sha256sum py-emc-0.1.tar.gz          # 반입 신청서에 기재

# 사내에서 (매체 반입 절차 후)
docker load < py-emc-0.1.tar.gz
python3 tools/vv.py --profile profiles/enterprise.local.json \
        run 도커 확인 --backend docker --code "import skrf; print(skrf.__version__)"
```

마지막 명령이 버전을 출력하면 §B-1·B-2가 해소된다.

- [ ] 매체 반입 절차 확인 (SHA256 기재 필요할 수 있음)
- [ ] 위 확인 결과를 이 문서에 기록

### D-3. 사내 CI 등록

`ci/gate.sh`가 유일한 진입점이다. Ainative와 계약이 같다.

```yaml
- run: ./ci/gate.sh
```

- [ ] 사내 CI에 등록
- [ ] `REQUIRE_OWNERS=1` 설정 (§A-4 완료 후)
- [ ] Ainative와 같은 파이프라인에 넣을지 결정

---

## E. 빠뜨리기 쉬운 규칙

인수받은 사람이 가장 자주 어기는 것들. 전부 [`CLAUDE.md`](../CLAUDE.md)에 있다.

### E-1. 게이트가 막으면 게이트를 고치지 말 것

특히 G-02(프로파일 불변식)와 G-10(격리 경계). 이 둘이 막는다는 것은
**사내 에디션의 안전 조건이 깨졌다**는 뜻이다. 검사가 옳고 코드가 틀렸을
가능성을 먼저 볼 것.

### E-2. `is_isolation_boundary`를 `True`로 바꾸지 말 것

`SubprocessBackend`가 `False`인 것은 네트워크도 파일시스템도 막지 못하기
때문이다. 이 플래그 하나를 뒤집으면 사내 에디션이 비격리 실행을 허용하게 된다.

도커를 못 쓰면 **실행을 포기하는 것이 맞다.**

### E-3. 아티팩트를 고쳤으면 채점할 것

```bash
python3 tools/gepa_run.py --eval
```

G-05가 하한선을 지키지만 하한선 위의 퇴행은 못 잡는다.

### E-4. 생성물을 직접 고치지 말 것

`vault/notes/*.md`는 이벤트 원장에서 생성된 뷰다. 고쳐도 다음 생성에서
되돌아간다.

### E-5. 측정하지 않은 것을 검증했다고 쓰지 말 것

이 저장소의 서술 규율이다. §B-4의 구분을 문서·보고·커밋에서 유지할 것.

---

## F. 30분 안에 파악하려면

```bash
./ci/gate.sh                      # 1) 전부 도는지
python3 tools/demo.py             # 2) 무엇을 하는 물건인지
cat docs/AUDIT.md                 # 3) 원본 PRD에서 무엇을 왜 바꿨는지
cat docs/SECURITY.md              # 4) 위험을 어떻게 다루는지
```

읽는 순서를 하나만 고른다면 [`AUDIT.md`](AUDIT.md)다. 이 저장소의 모든 설계
판단이 거기서 나왔다.

---

## G. 미결 항목 요약

| # | 항목 | 성격 | 차단하는 것 |
|---|---|---|---|
| A-1 | 공개 저장소에 사내 정보 분리 | **즉시** | 반입 전 필수 |
| A-2 | 녹음 고지 법무 검토 | 외부 의존 | **Phase 3 차단** |
| A-3 | 보안 검토 | 외부 의존 | 사내 운영 |
| A-4 | `OWNERS.yaml` 기입 | 즉시 | 장기 유지보수 |
| B-1 | 도커 실행 검증 | 장비 필요 | **사내판 실행 기능 전체** |
| B-2 | `py-emc` 이미지 빌드 | 인터넷 필요 | B-1 |
| B-3 | 코드 생성 모델 연결 | 개발 | 자동화 |
| B-4 | 지연·VRAM 실측 | 장비 필요 | 하드웨어 품의 |
| B-5 | 태스크뱅크 확대 | 사용 시간 | 개선 신뢰도 |
| C | 프로파일 실값 확정 | 사내 협의 | 사내 실행 |

**이 목록을 조용히 줄이지 말 것.** 해결되면 근거와 함께 지운다.
