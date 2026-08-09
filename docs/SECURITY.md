# 보안 설계

이 시스템의 위험은 "AI가 틀린 답을 한다"가 아니다. **AI가 사내 데이터를 밖으로
내보내거나, 사내 엔드포인트에서 코드를 실행하는 것**이다.

---

## 1. 위험 표면 넷

모든 통제가 이 넷 중 하나에 붙는다. 표면을 넷으로 고정한 이유는, 새 기능이
추가될 때 "이건 어느 표면인가"를 물으면 통제 누락이 드러나기 때문이다.

| 표면 | 무엇인가 | 최악의 경우 |
|---|---|---|
| `egress` | 데이터가 프로세스 밖으로 나감 | s2p 설계 데이터가 클라우드 API로 |
| `exec` | 명령이 호스트에서 실행됨 | AI 생성 PowerShell이 사내 PC에서 실행 |
| `capture` | 화면이 캡처됨 | EMC 설계 화면 + 옆에 열린 메신저가 함께 |
| `store` | 데이터가 원장에 기록됨 | 토큰·주민번호가 영구 저장 |

전부 `harness/policy/engine.py`를 통과한다. 우회 경로를 만들지 않는다.

---

## 2. 판정은 3단이다

`allow` / `confirm` / `deny`. **기본값이 `confirm`인 것이 핵심이다.**

```json
"exec_default": "confirm"
```

전부 `deny`로 두면 아무도 안 쓰고, 안 쓰이는 통제는 우회된다. 사용자가 명령
전문을 보고 승인하는 것이 실질적으로 가장 강한 통제다.

| 판정 | 동작 |
|---|---|
| `allow` | 즉시 실행. 원장 기록 |
| `confirm` | **전문을 UI에 띄우고** 사용자 승인 대기 |
| `deny` | 실행 불가. 원장에 차단 사유 기록 |
| `redact` | 마스킹 후 진행 |

---

## 3. `exec` — 가장 위험한 표면

PowerShell은 정의상 **호스트에서** 돈다. 그것이 존재 이유다(파일 정리, 장비 제어,
배포). 따라서 샌드박스가 방어선이 될 수 없고, 이 시스템에서 유일하게 격리 없이
실행되는 컴포넌트다.

원본 명세에는 "PowerShell Guard"라는 단어만 있고 정책이 없었다
([`AUDIT.md`](AUDIT.md) §E).

### 금지 패턴 (양 프로파일 공통 최소치)

```
Invoke-Expression, iex, -EncodedCommand, Set-ExecutionPolicy,
New-Object Net.WebClient, Invoke-WebRequest, curl, wget, Start-Process,
Get-Credential, ConvertFrom-SecureString,
Remove-Item -Recurse -Force, rm -rf, reg add, schtasks
```

`-EncodedCommand`와 `Invoke-Expression`이 특히 중요하다. 둘 다 **정적 검사를
무력화**한다 — 허용 목록을 통과한 문자열이 실행 시점에 임의 코드가 된다.

시험: `test_powershell_dangerous_commands_blocked`

### 허용은 읽기 전용만

```
Get-ChildItem, Get-Content, Get-Location, Get-Process, Get-Date,
Test-Path, Measure-Object, git status|log|diff|show|branch
```

쓰기·삭제·네트워크는 허용 목록에 넣지 않는다. 필요하면 `confirm`으로 간다.

---

## 4. `capture` — 보호 대상 그 자체

논리가 단순하다.

> 명세는 "EMC 설계 데이터 등 기업 핵심 기술 보호"를 핵심 제약으로 든다.
> 그런데 EMC 설계 툴 화면 캡처는 **바로 그 핵심 기술 자체**다.

원본 명세는 캡처를 기능으로만 다뤘다([`AUDIT.md`](AUDIT.md) §F).

### 통제 4단

1. **앱 차단 목록** — 메신저·메일·비밀번호 관리자·원격 데스크톱.
   사내판은 여기에 더해 **허용 목록 필수**(목록에 없으면 캡처 불가).

   메신저 차단은 **양 에디션 공통**이다. 사내 기밀과 무관하게, 대화 캡처는
   상대방의 발언을 그 사람 동의 없이 모델로 보내는 행위다. 개인판에서도
   기본 차단하고, 필요하면 사용자가 직접 프로파일에서 푼다.
2. **ROI 기본** — 드래그한 영역만. 전체 화면은 명시적 선택으로만.
   의도치 않은 창이 함께 담기는 것을 막는 가장 효과적인 수단이다.
3. **스크리닝 필수** — OCR → 비밀 스캔 → 탐지 시 마스킹.
   `screened` 플래그 없는 캡처는 저장·전송 모두 불가.
4. **캡처도 이벤트다** — 무엇을 언제 캡처했는지 원장에 남는다.

3번은 Ainative `kb/schema.md`의 에셋 규칙을 그대로 가져온 것이다. 사유도 같다 —
"사내 캡처에는 토큰·개인정보가 실제로 자주 들어 있다".

---

## 5. `store` — 원장 보호

이벤트 원장에는 음성 명령 원문, 화면 캡처, CLI 히스토리, AI 응답 전문이 들어간다.
즉 **영업비밀 + 개인정보의 혼합 저장소**다.

| 통제 | 구현 |
|---|---|
| 저장 전 마스킹 | `prepare_for_store()` — 크리덴셜·주민번호 패턴 치환 |
| 추가 전용 | **DB 트리거**로 UPDATE/DELETE 차단. 앱 버그로도 과거가 안 바뀐다 |
| 무결성 | 해시 체인. `verify_chain()`으로 사후 수정 탐지 |
| 정정 | 삭제가 아니라 `corrects` 필드로 새 이벤트 |
| 보존 | `store_retention_days` — 사내 180일 |
| 출처 | `origin` 필수 (`human`/`ai`/`instrument`/`system`) |

### 탐지 패턴

```
sk-ant-*, sk-*, AIza*, ghp_/gho_/ghu_/ghs_/ghr_*, AKIA*, xox[baprs]-*,
BEGIN PRIVATE KEY, Authorization: Bearer *, password=*,
주민등록번호 (\d{6}-[1-4]\d{6})
```

탐지된 **값 자체는 로그에 남기지 않는다.** 종류만 기록한다 — 로그가 두 번째
유출 경로가 되면 안 된다.

```python
ev.redactions      # ['github-token']
ev.payload['cmd']  # 'export GH_TOKEN=<<REDACTED:github-token>>'
```

게이트 G-08이 볼트 마크다운 파일도 스캔한다. 노트는 사람이 손으로도 고치는
파일이기 때문이다.

---

## 6. 샌드박스

원본 §9.1의 정책은 출발점으로 좋았으나 실제 탈출 경로들이 빠져 있었다.

| 플래그 | 막는 것 | 원본 |
|---|---|---|
| `--network none` | 사내망 스캔, 데이터 유출 | 있음 |
| `--read-only` | 루트 파일시스템 변조 | 있음 |
| `--memory` + `--memory-swap` **동일** | 스왑으로 메모리 제한 우회 | swap 누락 |
| `--pids-limit` | fork 폭탄 | 누락 |
| `--cap-drop ALL` | 기본 capability 악용 | 누락 |
| `--security-opt no-new-privileges` | setuid 권한 상승 | 누락 |
| `--user 65534:65534` | 컨테이너 내 root | 누락 |
| `--ulimit fsize` | 디스크 채우기 | 누락 |
| `tmpfs ...,noexec,nosuid` | 쓰기 가능 경로를 실행 경로로 | 누락 |
| `--volume src:/data:**ro**` | 원본 s2p 변조 | 누락 |

`SandboxPolicy.preflight()`가 이 중 핵심을 검사하고, 게이트 G-06이 CI에서
재확인한다.

### `network: none`의 부작용

**pip이 안 된다.** 이미지에 패키지를 미리 구워야 한다.

```python
BAKED_PACKAGES = ("numpy", "scipy", "pandas", "matplotlib",
                  "scikit-rf", "pytest", "flake8")
```

원본 명세가 이 모순을 다루지 않아, 그대로 착수하면 첫 실행에서 막힌다.

---

## 7. 에디션 경계

[`EDITIONS.md`](EDITIONS.md) 참조. 보안 관점 요약:

- 사내 프로파일은 `egress_default != "deny"`이거나 클라우드 프로바이더가 있으면
  **로드 자체가 실패**한다.
- 프로파일은 호스트에 고정된다(`pinned_to`). 사내 PC에서 개인 프로파일이 뜨는
  것이 **가장 현실적인 유출 경로**이므로 이것을 막는다.
- 프로파일 전환 UI는 만들지 않는다. 전환이 쉬우면 실수로 전환된다.
- 볼트 경로는 겹치거나 중첩될 수 없다 (게이트 G-07).

---

## 8. 외부 배포 브릿지

망분리 환경에서 외부 배포(Netlify 등)가 필요할 때.

```
사내 샌드박스에서 빌드
      ↓
아티팩트 서명
      ↓
보안 검토 (사람)
      ↓
CI/CD 프록시 (망연계)
      ↓
외부 배포
```

**하네스가 직접 외부로 배포하지 않는다.** 사내판의 `egress_deny`에
`api.netlify.com`이 들어 있고, 데모에서 이를 확인할 수 있다.

```
▸ "도커 이미지 다시 빌드해서 올려줘"
  → 배포 판정: [DENY] egress:api.netlify.com — 명시적 차단 목록
```

---

## 9. 아직 해결되지 않은 것

정직하게 남긴다.

| 항목 | 상태 |
|---|---|
| **녹음 고지 / 개인정보 동의** | **미해결. 법무 검토 필요.** 다자 발화 환경에서 개인정보보호법 적용 여부. Phase 3 **차단 조건** |
| 원장 암호화 (at-rest) | 미구현. SQLite 파일이 평문이다 |
| 다중 사용자 접근통제 | 미구현. 현재 단일 사용자 전제 |
| OCR 후 비밀 스캔 | 인터페이스만 있고 OCR 미연결 (Phase 4) |
| 프로포저 출력의 프롬프트 인젝션 | 부분 대응. 아티팩트는 `Router()` 검증을 통과해야 채택되지만, 규칙 텍스트 자체는 신뢰되지 않는 입력이다 |
| 개인 → 사내 태스크뱅크 비식별화 | 미구현. 그때까지 **손으로 작성** |
| 사내 CA / 프록시 환경 | 미검증 |

마지막 줄이 중요하다. **이 목록이 비어 있다고 쓰지 않는다.**
