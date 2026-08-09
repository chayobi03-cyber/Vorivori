#!/usr/bin/env bash
# 인수인계 패키지(zip) 생성.
#
# 사내 전달용으로 저장소 스냅샷을 묶고, 받는 쪽이 git 없이도 바로 검증할 수 있게
# START-HERE.md 와 MANIFEST(SHA256)를 함께 넣는다.
#
#   ./tools/make_handover.sh
#   → dist/vorivori-handover-<date>.zip
#
# 게이트가 실패하면 패키지를 만들지 않는다 — 깨진 상태를 인수인계하지 않기 위함.
# Ainative의 tools/make_handover.sh 와 같은 계약이다.
set -uo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date +%Y%m%d)"
NAME="vorivori-handover-${STAMP}"
OUT="dist/${NAME}.zip"
STAGE="$(mktemp -d)/${NAME}"

echo "=== 1/5 품질 게이트 ==="
if ! ./ci/gate.sh; then
  echo >&2
  echo "게이트 실패 — 패키지를 만들지 않습니다." >&2
  echo "./ci/gate.sh 로 원인을 확인하십시오." >&2
  exit 1
fi

echo
echo "=== 2/5 스테이징 ==="
mkdir -p "$STAGE"
# 추적 중인 파일만 복사한다. 볼트 실데이터·원장·캐시가 섞이지 않게 하는
# 가장 확실한 방법이다 (.gitignore 를 그대로 신뢰한다).
git ls-files -z | while IFS= read -r -d '' f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$f" "$STAGE/$f"
done
echo "  파일 $(git ls-files | wc -l | tr -d ' ')개"

# 사내 정보가 섞여 나가지 않는지 마지막 확인. public 저장소 전제이므로
# 추적 파일에는 원래 없어야 하지만, 로컬 오버라이드가 실수로 add 됐을 수 있다.
echo
echo "=== 3/5 반출 전 스캔 ==="
if find "$STAGE" -name "*.local.json" | grep -q .; then
  echo "  중단: *.local.json 이 포함돼 있습니다 (사내 실값일 수 있음)" >&2
  find "$STAGE" -name "*.local.json" >&2
  exit 1
fi
if find "$STAGE" -name "*.db" -o -name "events.db" | grep -q .; then
  echo "  중단: 이벤트 원장(.db)이 포함돼 있습니다" >&2
  exit 1
fi
python3 - "$STAGE" <<'PY' || exit 1
import pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.policy.engine import scan_secrets
root = pathlib.Path(sys.argv[1])
bad, skipped = [], []
for p in root.rglob("*"):
    if not (p.is_file() and p.suffix in (".md", ".json", ".py", ".sh", ".jsonl", ".yaml")):
        continue
    text = p.read_text(encoding="utf-8", errors="ignore")
    rel = str(p.relative_to(root))
    # 스캐너 자신을 시험하는 파일은 가짜 크리덴셜을 의도적으로 담는다.
    # 표식이 있으면 건너뛰되 **건너뛴 사실을 출력한다** — 조용히 넘어가면
    # 예외가 예외인 줄 모르게 되고, 진짜 비밀이 그 뒤에 숨는다.
    if "vorivori:scan-allow" in text:
        skipped.append(rel)
        continue
    found = scan_secrets(text)
    if found:
        bad.append((rel, found))
if bad:
    print("  중단: 크리덴셜 패턴 탐지", bad, file=sys.stderr)
    sys.exit(1)
for s in skipped:
    print(f"  건너뜀(scan-allow 표식): {s}")
print(f"  크리덴셜 패턴 없음 (검사 대상에서 {len(skipped)}개 명시적 제외)")
PY

echo
echo "=== 4/5 START-HERE + MANIFEST ==="
cat > "$STAGE/START-HERE.md" <<EOF
# 여기부터 — Vorivori 인수인계 패키지

생성 ${STAMP} · 커밋 \`$(git rev-parse --short HEAD)\` · 브랜치 \`$(git rev-parse --abbrev-ref HEAD)\`

## 0. 이게 무엇인가

엔지니어의 음성·화면·CLI·문서·일정·실행 로그를 하나의 업무 메모리로 묶는
**업무 실행 하네스**입니다. 사내판(망분리)과 개인판을 하나의 코어 + 두
프로파일로 운영합니다.

**이 패키지는 검토·검증받을 물건이지, 켜면 도는 시스템이 아닙니다.**
미검증 항목은 \`docs/HANDOVER.md\` §B에 있습니다.

## 1. 5분 안에 확인 (설치할 것 없음)

Python 3.11 이상만 있으면 됩니다. pip도 인터넷도 GPU도 필요 없습니다.

\`\`\`bash
./ci/gate.sh              # 품질 게이트 10종. 종료 코드 0이면 통과
python3 tools/demo.py     # 지시 → 분류 → 정책 → 실행계획 → 원장 → 노트
\`\`\`

게이트가 통과하지 않으면 그다음은 의미가 없습니다. 먼저 알려주십시오.

## 2. 30분 안에 파악

| 순서 | 문서 | 왜 |
|---|---|---|
| 1 | \`docs/HANDOVER.md\` | **무엇이 검증 안 됐는지.** 가장 먼저 볼 것 |
| 2 | \`docs/AUDIT.md\` | 원본 PRD에서 무엇을 왜 바꿨는지 (설계 판단의 근원) |
| 3 | \`docs/SECURITY.md\` | 위험 표면 4종과 통제, 미해결 목록 |
| 4 | \`docs/EDITIONS.md\` | 사내판/사외판 분리를 코드로 강제하는 방법 |
| 5 | \`README.md\` | 전체 구성 |

## 3. 반출 전 반드시 볼 것

**이 저장소는 public입니다.** \`profiles/enterprise.json\`의 호스트명·
엔드포인트는 전부 **가짜 자리표시자**입니다. 실제 사내 값은
\`profiles/enterprise.local.json\`에 두십시오 (\`.gitignore\` 처리돼 있습니다).

상세: \`docs/HANDOVER.md\` §A-1

## 4. 지금 상태

| 항목 | 값 |
|---|---|
| 게이트 | G-01~G-10 10/10 통과 |
| 단위시험 | 113건 통과 |
| 라우터 정확도 | train 0.972 / holdout 0.750~0.833 (태스크뱅크 48건 기준) |
| 도커 실행 | **미검증** — 사내판의 유일한 허용 백엔드입니다 |
| 코드 생성(모델 연결) | 미구현 |

**구조는 시험됐고 사내 환경에서는 아무것도 검증되지 않았습니다.**
이 구분을 보고서에서 유지해 주십시오.

## 5. 무결성 확인

\`\`\`bash
sha256sum -c MANIFEST.sha256
\`\`\`
EOF

( cd "$STAGE" && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
echo "  START-HERE.md, MANIFEST.sha256 ($(wc -l < "$STAGE/MANIFEST.sha256" | tr -d ' ')개 항목)"

echo
echo "=== 5/5 압축 ==="
mkdir -p dist
rm -f "$OUT"
( cd "$(dirname "$STAGE")" && zip -qr "$OLDPWD/$OUT" "$(basename "$STAGE")" )
rm -rf "$(dirname "$STAGE")"

echo "  $OUT  ($(du -h "$OUT" | cut -f1))"
echo "  SHA256: $(sha256sum "$OUT" | cut -d' ' -f1)"
echo
echo "완료. 반입 신청서에 위 SHA256을 기재하십시오."
