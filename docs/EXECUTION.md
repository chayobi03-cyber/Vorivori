# 실행 루프

원본 명세 §7.1(상태 머신) · §9(실행 하네스)를 구현한 것과, 그 과정에서
바꾼 판단들.

---

## 1. LangGraph를 쓰지 않은 이유

원본은 LangGraph를 든다. 두 가지 때문에 직접 만들었다.

**첫째, 의존성 제약.** 이 저장소는 표준 라이브러리만 쓴다. 폐쇄망 반입 절차를
통과해야 하고, Ainative와 같은 제약을 공유해야 CI가 갈라지지 않는다.

**둘째 — 이쪽이 더 중요하다 —** 정작 필요한 것이 LangGraph에 없다.
§8 이슈3이 짚은 **barge-in 중단 의미론**은 프레임워크가 거저 주지 않는다.
"어떤 노드가 중단 가능한가"는 도메인 지식이고, 우리가 정해야 한다.

직접 만드니 200줄이다. 의존성 하나를 아끼는 것보다, 중단 규칙을 우리가
정할 수 있다는 점이 실질적 이득이었다.

---

## 2. INTERRUPT — 원본이 빠뜨린 노드

§8 이슈3은 barge-in 교착을 정확히 진단했다. 그런데 §7.1의 상태 머신에는
인터럽트가 없다. 여기서 세 가지를 넣었다.

### 2.1 어느 노드에서든 갈 수 있다

```
INTAKE → CLASSIFY → PLAN → EXECUTE → VERIFY → COMMIT → DONE
   │         │        │        │        │        │
   └─────────┴────────┴────────┴────────┴────────┘
                      ↓
                   PARKED  (체크포인트 보존)
```

### 2.2 중단이 아니라 **보류**다

```python
return Outcome(State.PARKED, ctx, interrupted=True, checkpoint=cp)
```

취소하면 사용자가 "아까 하던 거"로 돌아올 수 없다. 체크포인트를 남기고
`PARKED`로 간다. 재개는 `Machine.resume_ctx()`가 한다.

인터럽트로 들어온 새 지시도 함께 싣는다. 사용자가 "아니 그거 말고 이렇게"라고
했을 때 이전 맥락을 버리지 않는다.

```python
ctx = Machine.resume_ctx(checkpoint, interrupt_text="아니 그거 말고")
```

### 2.3 부작용 노드는 중단되지 않는다

```python
Node(State.EXECUTE, execute, interruptible=False),   # 컨테이너가 돌고 있다
Node(State.COMMIT,  commit,  interruptible=False),   # 원장에 쓰는 중이다
```

파일 쓰기·명령 실행·일정 생성이 반쯤 끝난 상태로 멈추면 되돌릴 수 없다.
그런 구간에서는 인터럽트를 **큐에 넣고** 노드를 끝까지 돌린 뒤 처리한다.

인터럽트 확인은 노드 실행 **전**에만 한다. 실행 중간에 끊으면 부작용이
반쯤 남는다.

---

## 3. 샌드박스 백엔드 둘

| | `DockerBackend` | `SubprocessBackend` |
|---|---|---|
| `is_isolation_boundary` | **True** | **False** |
| 네트워크 차단 | ✅ `--network none` | ❌ **못 막는다** |
| 파일시스템 격리 | ✅ read-only + tmpfs | ❌ **못 막는다** |
| CPU·메모리·프로세스 한도 | ✅ | ✅ `RLIMIT_*` |
| 환경변수 차단 | ✅ | ✅ (env 비움) |
| 사내 에디션 | 허용 | **금지** |

### 왜 비격리 백엔드를 두는가

개인 노트북에 도커가 없는 경우가 흔하고, 도커 설치를 강제하면 개인판이
안 쓰인다. 안 쓰이면 이벤트가 안 쌓이고, 이벤트가 안 쌓이면 GEPA 루프가 굶는다.

### 그러면서도 위험하지 않게 하는 방법

**이것을 격리라고 부르지 않는 것.** 플래그 하나가 그 역할을 한다.

```python
class SubprocessBackend:
    is_isolation_boundary = False
```

그리고 사내 에디션은 이 백엔드를 거부한다.

```
$ vv --profile profiles/enterprise.json run ... --backend subprocess
✗ 사내 에디션은 subprocess 백엔드를 쓸 수 없다 — 격리 경계가 아니다
```

도커가 아예 없으면 **실행을 포기한다.**

```
✗ 도커를 쓸 수 없다. 사내 에디션은 격리 없이 실행하지 않는다.
```

격리 없이 사내 데이터로 AI 생성 코드를 돌리는 것보다 안 돌리는 게 낫다.
개인판에서는 경고를 띄우고 진행한다.

```
⚠ 백엔드 'subprocess'는 격리 경계가 아닙니다
  (네트워크·파일시스템을 막지 못합니다). 사내 데이터로 쓰지 마세요.
```

게이트 G-10이 이 구분을 매번 확인한다.

---

## 4. 자가 수정 루프 — 원본 §9.3에 더한 셋

원본:

```python
for attempt in range(3):
    result = run_in_sandbox(code)
    if result.success: return result
    code = ai_fix(code, result.stderr)
```

### 4.1 재시도마다 프로바이더를 승급시킨다

같은 모델에 같은 에러를 다시 주면 **같은 실패가 반복된다.** 다른 관점이
필요하다. `REFLECT`는 `EXECUTE`가 아니라 `PLAN`으로 돌아간다.

```
1회차  qwen_local  (implement)
2회차  claude      (review)
3회차  gpt         (architect)
```

### 4.2 재시도 전략이 아티팩트에 있다

`artifacts/retry.policy.json`. 원본 §19가 "Retry Strategy"를 GEPA 최적화
대상으로 들었는데, 파이썬 `for`문 안에 있으면 손댈 수 없다.

### 4.3 재시도해도 소용없는 실패를 구분한다

가장 실용적인 추가다.

```json
"fatal": {
  "ModuleNotFoundError|No module named":
    "샌드박스는 network=none 이라 패키지를 설치할 수 없다. 이미지에 미리 구워야 한다.",
  "PermissionError|Read-only file system":
    "읽기 전용 루트에 쓰려 했다. /tmp 또는 /work 를 쓰도록 지시가 필요하다."
}
```

`ModuleNotFoundError`는 코드를 고쳐서 풀리지 않는다. 3회를 태우는 것은
시간과 토큰 낭비이고, 사용자에게는 "3번 시도했지만 실패"라는 쓸모없는 결론만
남는다. 즉시 멈추고 **무엇을 해야 하는지** 알린다.

```
VERIFY    재시도 불가: 샌드박스는 network=none 이라 패키지를 설치할 수 없다.
          이미지(infra/docker/Dockerfile.py-emc)에 미리 구워야 한다.
✗ FAILED   시도 1회   스텝 5
```

### 4.4 같은 수정안이 오면 멈춘다

```python
if fixed.strip() == ctx.code.strip():
    ctx.note(State.REFLECT, "수정안이 원본과 동일 — 중단")
    return State.FAILED
```

같은 코드를 다시 돌리면 같은 결과가 나온다.

---

## 5. 확신 없으면 실행하지 않는다

```
$ vv run 터치스톤 파일 로드해서 플롯하는 코드 짜줘 --code "..."
  CLASSIFY  EMC_ANALYSIS (격차 0.0)
  CLASSIFY  확신 부족 — 2위 PYTHON_TOOL
? NEEDS_INPUT
  분류에 확신이 없습니다. 더 구체적으로 말해주세요.
```

`CLASSIFY`에서 1·2위 격차가 좁으면 `NEEDS_INPUT`으로 끝낸다.
**틀린 자신감보다 되묻는 편이 낫다.** 임계값(`min_margin`)은 라우터
아티팩트에 있으므로 GEPA 최적화 대상이다.

---

## 6. 이미지를 미리 굽는다

`--network none`이면 pip이 동작하지 않는다. 원본 §9.1이 `network: none`을
정하면서 이 결과를 다루지 않아, 그대로 착수하면 첫 실행에서 막힌다.

```dockerfile
RUN pip install --no-cache-dir \
      numpy scipy pandas matplotlib scikit-rf pytest flake8
```

`scikit-rf`가 EMC 작업의 핵심이다 — 터치스톤(`.s2p`/`.s4p`) 파싱이
여기서 일어난다. 원본 §12가 "수치는 임베딩하지 말고 샌드박스에서"라고 한
판단을 실현하려면 이 패키지가 이미지 안에 있어야 한다.

폐쇄망 반입:

```bash
docker save vorivori/py-emc:0.1 | gzip > py-emc-0.1.tar.gz
# (매체 반입 절차 후)
docker load < py-emc-0.1.tar.gz
```

---

## 7. 실행 추적

모든 전이가 기록된다. 실패했을 때 어디서 무엇이 일어났는지 보인다.

```
$ vv run csv 읽어서 그래프 그리는 스크립트 --code "print('결과: 42')"
── 실행 추적 ──────────────────────────────────────────
  INTAKE    입력 20자
  CLASSIFY  PYTHON_TOOL (격차 2.0)
  PLAN      PYTHON_TOOL → qwen_local (implement)
  EXECUTE   subprocess exit=0
  VERIFY    통과
  COMMIT    완료 (시도 1회)

✓ DONE   시도 1회   스텝 6
```

원장에는 `sandbox.result`가 남고, **격리 여부가 함께 기록된다.**

```json
{"ok": true, "backend": "subprocess", "attempt": 0, "isolated": false}
```

나중에 "이 결과가 격리 환경에서 나온 것인가"를 물을 수 있어야 한다.

---

## 8. 지금 되는 것 / 안 되는 것

| | 상태 |
|---|---|
| 상태 머신 + INTERRUPT + 체크포인트 | **구현·시험됨** |
| 자가 수정 루프 (승급·fatal 구분·동일안 감지) | **구현·시험됨** |
| `SubprocessBackend` 실행 | **구현·시험됨** |
| `DockerBackend` 실행 | 구현됨. **이 환경에 도커 데몬이 없어 미검증** |
| `py-emc` 이미지 | Dockerfile만. **빌드 미검증** |
| 코드 **생성** (모델 연결) | 미구현 — `--code`/`--file`로 주입 |
| barge-in 실제 트리거 (VAD) | 미구현. `interrupt_check` 콜백만 |

**도커 경로는 검증되지 않았다.** 인자 구성과 정책 preflight는 시험했지만,
실제 컨테이너 실행은 데몬이 있는 환경에서 확인해야 한다. 그 전까지
"도커 샌드박스가 동작한다"고 쓰지 않는다.

재현:

```bash
python3 -m unittest tests.test_execute -v
python3 tools/vv.py run <지시> --code "print(1)"
```
