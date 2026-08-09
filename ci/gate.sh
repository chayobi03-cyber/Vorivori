#!/usr/bin/env bash
# CI 진입점. 어떤 CI에서든 이것만 호출한다.
#
# Ainative의 ci/gate.sh 와 같은 계약이다. 두 저장소를 한 파이프라인에서
# 돌릴 때 진입점이 같아야 CI 설정이 갈라지지 않는다.
set -uo pipefail
cd "$(dirname "$0")/.."

./tests/run_checks.sh
rc=$?

# OWNERS 미기입은 경고만 한다. 담당자 없는 하네스는 6개월이면 썩지만,
# 초기 개발을 막을 이유는 없다. REQUIRE_OWNERS=1 로 차단으로 승격한다.
if [ -f OWNERS.yaml ]; then
  if ! grep -qE '^\s{4}name:\s*"[^"]+"' OWNERS.yaml; then
    echo
    echo "  WARN  OWNERS.yaml 담당자 미기입"
    if [ "${REQUIRE_OWNERS:-0}" = "1" ]; then
      echo "        REQUIRE_OWNERS=1 이므로 차단한다."
      rc=1
    fi
  fi
fi

exit "$rc"
