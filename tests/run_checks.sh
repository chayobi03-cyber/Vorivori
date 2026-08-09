#!/usr/bin/env bash
# 하네스 품질 게이트.
# 네트워크·API 키·도커가 필요 없으므로 폐쇄망 CI에서 그대로 돈다.
# 종료 코드 0 = 통과, 1 = 차단.
#
# Ainative의 tests/run_checks.sh 와 같은 계약을 따른다 — CI는 이것만 호출한다.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
run() {
  local name="$1"; shift
  # printf 폭 지정은 바이트 기준이라 한글이 섞이면 어긋난다. 결과를 앞에 둔다.
  if out=$("$@" 2>&1); then
    echo "  PASS  $name"
  else
    echo "  FAIL  $name"
    echo "$out" | sed 's/^/    /'
    fail=1
  fi
}

echo "=== Vorivori 하네스 게이트 ==="

# G-01 코어 자기시험
run "G-01 하네스 단위시험" python3 -m unittest discover -s tests -q

# G-02 프로파일 불변식.
#      사내 프로파일이 느슨해지는 것은 이 저장소에서 가장 위험한 회귀다.
run "G-02 프로파일 불변식" python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.policy.engine import Profile
for p in sorted(pathlib.Path("profiles").glob("*.json")):
    prof = Profile.load(p)           # validate() 내장
    assert prof.exec_deny, f"{p}: exec_deny가 비었다"
    assert prof.store_redact_secrets, f"{p}: 비밀 마스킹이 꺼져 있다"
ent = Profile.load("profiles/enterprise.json")
assert ent.egress_default == "deny"
assert not [n for n, v in ent.providers.items() if v.get("transport") == "cloud"]
assert ent.pinned_to, "사내 프로파일은 호스트에 고정돼야 한다"
PY

# G-03 라우터 아티팩트가 로드되는가.
#      GEPA가 만든 아티팩트를 승격한 직후 이 검사가 유일한 안전망이다.
run "G-03 라우터 아티팩트 유효성" python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.router.rules import Router
Router.load()
PY

# G-04 태스크뱅크 정합성.
#      라벨 오타 하나가 GEPA를 엉뚱한 방향으로 최적화시킨다.
run "G-04 태스크뱅크 정합성" python3 - <<'PY'
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd()))
art = json.loads(pathlib.Path("artifacts/router.rules.json").read_text())
cats = set(art["categories"])
rows = [json.loads(l) for l in pathlib.Path("taskbank/router.jsonl").read_text().splitlines() if l.strip()]
ids = [r["id"] for r in rows]
assert len(ids) == len(set(ids)), "태스크뱅크 id 중복"
for r in rows:
    assert r["expect"] in cats, f"{r['id']}: 미정의 카테고리 {r['expect']}"
    assert r["split"] in ("train", "holdout"), f"{r['id']}: split 오류"
assert sum(r["split"] == "holdout" for r in rows) >= 8, "holdout이 너무 작다 — 승격 판단을 신뢰할 수 없다"
PY

# G-05 회귀 방지: 정본 아티팩트가 태스크뱅크 하한선 아래로 내려가지 않는가.
#      손으로 규칙을 고치다 조용히 퇴행하는 것을 막는다.
run "G-05 라우터 성능 하한" python3 - <<'PY'
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd()))
sys.path.insert(0, str(pathlib.Path.cwd() / "tools"))
from gepa_run import evaluate, load_instances
art = json.loads(pathlib.Path("artifacts/router.rules.json").read_text())
inst = load_instances(pathlib.Path("taskbank/router.jsonl"))
for split, floor in (("train", 0.75), ("holdout", 0.60)):
    rs = [evaluate(art, i) for i in inst if i.split == split]
    score = sum(r.score for r in rs) / len(rs)
    assert score >= floor, f"{split} {score:.3f} < 하한 {floor}"
    print(f"{split}={score:.3f}")
PY

# G-06 샌드박스 정책 preflight.
#      network=none, 비루트, cap-drop 이 꺼지면 여기서 걸린다.
run "G-06 샌드박스 정책" python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.sandbox.policy import SandboxPolicy
p = SandboxPolicy()
problems = p.preflight()
assert not problems, problems
args = " ".join(p.docker_args())
for flag in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only"):
    assert flag in args, f"누락: {flag}"
PY

# G-07 볼트 분리.
#      사내 볼트와 개인 볼트가 같은 경로를 가리키면 분리가 무의미하다.
run "G-07 볼트 경로 분리" python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.policy.engine import Profile
roots = {p.stem: pathlib.Path(Profile.load(p).vault_root).resolve()
         for p in pathlib.Path("profiles").glob("*.json")}
assert len(set(roots.values())) == len(roots), f"볼트 경로 중복: {roots}"
for name, r in roots.items():
    for other, o in roots.items():
        if name != other:
            assert not str(r).startswith(str(o) + "/"), f"{name} 볼트가 {other} 안에 있다"
PY

# G-08 볼트에 비밀이 커밋되지 않았는가.
#      노트는 사람이 손으로도 고치는 파일이라 스캐너를 한 번 더 건다.
run "G-08 볼트 비밀 스캔" python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.policy.engine import scan_secrets
bad = []
for p in pathlib.Path("vault").rglob("*.md"):
    found = scan_secrets(p.read_text(encoding="utf-8", errors="ignore"))
    if found:
        bad.append((str(p), found))
assert not bad, bad
PY

# G-09 프로바이더 라우팅 아티팩트.
#      사내 프로파일이 어떤 카테고리에서도 클라우드를 고르면 안 된다.
#      라우팅 규칙이 아티팩트라 손으로 고칠 수 있으므로 매번 확인한다.
run "G-09 프로바이더 라우팅" python3 - <<'PY'
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.policy.engine import PolicyEngine, Profile
from harness.providers.registry import ProviderRouter
cats = json.loads(pathlib.Path("artifacts/router.rules.json").read_text())["categories"]
ent = PolicyEngine(Profile.load("profiles/enterprise.json"))
r = ProviderRouter.load(ent)
for c in cats:
    ch = r.select(c)
    assert ch.ok, f"{c}: 사내 프로파일에서 선택 가능한 프로바이더가 없다"
    assert ch.transport == "local", f"{c} → {ch.provider} ({ch.transport}) — 사내에서 클라우드 금지"
# 개인 프로파일도 로드는 돼야 한다
ProviderRouter.load(PolicyEngine(Profile.load("profiles/personal.json")))
PY

# G-10 실행 루프 안전 규칙.
#      샌드박스 백엔드 선택과 재시도 정책이 느슨해지면 격리 없이 사내 데이터로
#      AI 생성 코드가 돈다. 그 회귀를 여기서 막는다.
run "G-10 실행 루프 안전" python3 - <<'PY2'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from harness.execute.loop import RetryPolicy
from harness.execute.sandbox import DockerBackend, SubprocessBackend, select_backend
from harness.policy.engine import Profile

assert DockerBackend().is_isolation_boundary
assert not SubprocessBackend().is_isolation_boundary, \
    "subprocess 를 격리 경계로 표시하면 SECURITY.md 가 무의미해진다"

ent = Profile.load("profiles/enterprise.json")
try:
    select_backend(ent, prefer="subprocess")
    raise AssertionError("사내 에디션이 비격리 백엔드를 받아들였다")
except PermissionError:
    pass

rp = RetryPolicy.load()
assert 1 <= rp.max_attempts <= 5, f"재시도 {rp.max_attempts}회는 과하다"
assert rp.is_fatal("ModuleNotFoundError: No module named 'skrf'"), \
    "설치 불가 오류를 재시도하면 3회를 낭비한다"
assert rp.is_fatal("OSError: [Errno 30] Read-only file system")
assert not rp.is_fatal("ValueError: 잘못된 주파수"), "일반 오류까지 fatal 이면 자가수정이 죽는다"
PY2

echo
if [ "$fail" -eq 0 ]; then
  echo "게이트 통과."
else
  echo "게이트 차단. 위 FAIL 항목을 해결할 것."
fi
exit "$fail"
