#!/usr/bin/env python3
"""샌드박스 실행 — 백엔드 두 종.

    DockerBackend      격리 경계다. 사내 에디션의 유일한 허용 백엔드.
    SubprocessBackend  격리 경계가 **아니다.** 자원 가드일 뿐이다.

두 번째를 두는 이유와 그 위험
    개인 노트북에 도커가 없는 경우가 흔하고, 도커 설치를 강제하면 개인판이
    안 쓰인다. 그런데 서브프로세스는 **네트워크를 막지 못하고 파일시스템을
    격리하지 못한다.** 루트 없이 네임스페이스를 만들 수 없기 때문이다.

    그래서 이것을 보안 경계라고 부르지 않는다. `is_isolation_boundary = False`
    이고, 사내 프로파일은 이 백엔드를 거부한다. 이 구분을 흐리는 순간
    docs/SECURITY.md 전체가 무의미해진다.

외부 의존성 없음 (Python 3.11 표준 라이브러리).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from harness.sandbox.policy import SandboxPolicy


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    backend: str
    timed_out: bool = False
    error: str = ""          # 백엔드 자체가 실패한 경우 (도커 없음 등)

    @property
    def available(self) -> bool:
        """실행이 시도되기는 했는가. False면 코드 문제가 아니라 환경 문제다."""
        return not self.error

    def feedback(self, limit: int = 1500) -> str:
        """자가 수정 루프에 넘길 진단 텍스트.

        stderr 전체를 넘기면 컨텍스트를 다 먹는다. 파이썬 트레이스백은
        **끝부분에 실제 예외**가 있으므로 뒤에서 자른다.
        """
        if self.timed_out:
            return f"시간 초과({self.backend}). 무한 루프이거나 입력이 너무 크다."
        err = self.stderr.strip()
        if len(err) > limit:
            err = "…(앞부분 생략)\n" + err[-limit:]
        return err or self.stdout.strip()[-limit:] or f"종료 코드 {self.exit_code}, 출력 없음"


class Backend(Protocol):
    name: str
    is_isolation_boundary: bool

    def available(self) -> bool: ...
    def run(self, code: str, policy: SandboxPolicy, mount_src: str | None) -> RunResult: ...


# ─────────────────────────────────────────────────────────────
class DockerBackend:
    """격리 경계. `harness/sandbox/policy.py`가 만든 인자를 그대로 쓴다."""

    name = "docker"
    is_isolation_boundary = True

    def available(self) -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            # 클라이언트만 있고 데몬이 없는 경우가 흔하다. 실제로 물어본다.
            r = subprocess.run(["docker", "info"], capture_output=True,
                               text=True, timeout=10)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def run(self, code: str, policy: SandboxPolicy,
            mount_src: str | None = None) -> RunResult:
        if not self.available():
            return RunResult(False, -1, "", "", self.name,
                             error="도커 데몬에 연결할 수 없다")
        problems = policy.preflight()
        if problems:
            return RunResult(False, -1, "", "", self.name,
                             error=f"샌드박스 정책 위반: {problems}")

        # 코드는 stdin으로 넘긴다. 볼륨으로 넘기면 쓰기 가능한 마운트가 필요하고,
        # 명령줄로 넘기면 프로세스 목록에 사내 코드가 남는다.
        argv = policy.docker_args(mount_src) + [
            "timeout", "-s", "KILL", str(policy.timeout_sec), "python3", "-"]
        try:
            proc = subprocess.run(argv, input=code, capture_output=True, text=True,
                                  timeout=policy.timeout_sec + 15)
        except subprocess.TimeoutExpired:
            return RunResult(False, -1, "", "", self.name, timed_out=True)
        except OSError as e:
            return RunResult(False, -1, "", "", self.name, error=str(e))

        # `timeout` 은 SIGKILL 시 137을 반환한다.
        timed_out = proc.returncode in (124, 137)
        return RunResult(proc.returncode == 0, proc.returncode,
                         proc.stdout, proc.stderr, self.name, timed_out)


# ─────────────────────────────────────────────────────────────
class SubprocessBackend:
    """자원 가드. **격리 경계가 아니다.**

    막는 것: CPU 시간, 주소공간, 파일 크기, 프로세스 수, 실행 시간.
    못 막는 것: **네트워크 접근, 파일시스템 읽기, 환경변수 노출.**

    개인 노트북에서 도커 없이 시작할 수 있게 두는 것이고,
    사내 데이터를 다루는 환경에서는 쓰지 않는다.
    """

    name = "subprocess"
    is_isolation_boundary = False

    def available(self) -> bool:
        return True

    def run(self, code: str, policy: SandboxPolicy,
            mount_src: str | None = None) -> RunResult:
        try:
            import resource
        except ImportError:  # Windows
            resource = None

        mem_bytes = _parse_size(policy.memory)

        def limits():  # 자식 프로세스에서만 돈다
            os.setsid()  # 프로세스 그룹 분리 — 타임아웃 시 통째로 죽인다
            if resource is None:
                return
            resource.setrlimit(resource.RLIMIT_CPU, (policy.timeout_sec, policy.timeout_sec))
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            resource.setrlimit(resource.RLIMIT_FSIZE, (policy.fsize_bytes, policy.fsize_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (policy.pids_limit, policy.pids_limit))
            resource.setrlimit(resource.RLIMIT_NOFILE, (policy.nofile, policy.nofile))

        with tempfile.TemporaryDirectory(prefix="vv-sbx-") as td:
            script = Path(td) / "gen.py"
            script.write_text(code, encoding="utf-8")
            # 환경을 비운다. 토큰이 든 환경변수가 생성 코드에 노출되지 않게.
            env = {"PATH": "/usr/bin:/bin", "HOME": td, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONIOENCODING": "utf-8"}
            try:
                proc = subprocess.run(
                    [sys.executable, str(script)], capture_output=True, text=True,
                    timeout=policy.timeout_sec, cwd=td, env=env,
                    preexec_fn=limits if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired:
                return RunResult(False, -1, "", "", self.name, timed_out=True)
            except OSError as e:
                return RunResult(False, -1, "", "", self.name, error=str(e))

        # RLIMIT_CPU 가 벽시계 타임아웃보다 먼저 걸리면 SIGXCPU 로 죽는다.
        # 사용자에게는 둘 다 "너무 오래 돌았다"이므로 같게 보고한다.
        # 이것을 구분하지 않으면 무한 루프가 일반 실패로 보여 자가 수정 루프가
        # 고칠 수 없는 코드를 3회 다시 돌린다.
        import signal
        killed = proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
        return RunResult(proc.returncode == 0, proc.returncode,
                         proc.stdout, proc.stderr, self.name, timed_out=killed)


def _parse_size(s: str) -> int:
    s = s.strip().lower()
    mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


# ─────────────────────────────────────────────────────────────
def select_backend(profile, prefer: str | None = None) -> Backend:
    """프로파일이 허용하는 백엔드를 고른다.

    사내 프로파일은 격리 경계가 아닌 백엔드를 쓸 수 없다. 도커가 없으면
    실행 자체를 포기하는 것이 맞다 — 격리 없이 사내 데이터로 AI 생성 코드를
    돌리는 것보다 안 돌리는 게 낫다.
    """
    docker, sub = DockerBackend(), SubprocessBackend()
    if prefer == "docker":
        return docker
    if prefer == "subprocess":
        if profile.edition == "enterprise":
            raise PermissionError(
                "사내 에디션은 subprocess 백엔드를 쓸 수 없다 — 격리 경계가 아니다")
        return sub
    if docker.available():
        return docker
    if profile.edition == "enterprise":
        raise PermissionError(
            "도커를 쓸 수 없다. 사내 에디션은 격리 없이 실행하지 않는다.")
    return sub


if __name__ == "__main__":
    from harness.policy.engine import Profile

    prof = Profile.load("profiles/personal.json")
    be = select_backend(prof)
    print(f"백엔드: {be.name}  (격리 경계: {be.is_isolation_boundary})")
    r = be.run("print(sum(range(10)))", SandboxPolicy())
    print(f"ok={r.ok} out={r.stdout.strip()!r}")
    r = be.run("import sys; print('boom', file=sys.stderr); sys.exit(3)", SandboxPolicy())
    print(f"ok={r.ok} code={r.exit_code} feedback={r.feedback()!r}")
