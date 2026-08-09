#!/usr/bin/env python3
"""샌드박스 실행 정책 — docker run 인자를 생성한다.

원본 PRD §9.1이 빠뜨린 것
    문서의 정책은 이렇다.

        network: none / memory_limit: 2GB / cpu_limit: 2
        timeout: 30s / readonly_rootfs: true / tmpfs: /tmp

    좋은 출발점이지만 다음이 빠져 있고, 빠진 것들이 실제 탈출 경로다.

      --cap-drop ALL          기본 capability 집합은 여전히 넓다
      --security-opt no-new-privileges   setuid 바이너리를 통한 권한 상승 차단
      --pids-limit            fork 폭탄. 메모리 제한만으로는 못 막는다
      --user (비루트)          컨테이너 안 root는 여전히 root다
      --read-only + 명시 tmpfs  /tmp 외에 쓰기 가능한 경로가 남으면 의미가 없다
      ulimit nofile/fsize     디스크를 채워 호스트를 멈추는 경로

    그리고 결정적으로: `network: none`이면 pip이 안 된다. 이미지는 scikit-rf,
    numpy, pandas를 **미리 구워서** 만들어야 한다. 이 모순을 문서가 다루지 않으면
    첫 실행에서 바로 막힌다.

이 모듈은 인자를 만들기만 한다. 실행은 호출자가 한다 — 그래야 dry-run으로
정책을 검사할 수 있고, CI에서 도커 없이도 테스트가 돈다.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SandboxPolicy:
    image: str = "vorivori/py-emc:0.1"
    network: str = "none"
    memory: str = "2g"
    memory_swap: str = "2g"        # swap을 memory와 같게 둬야 실제 제한이 걸린다
    cpus: str = "2"
    pids_limit: int = 128
    timeout_sec: int = 30
    user: str = "65534:65534"      # nobody:nogroup
    workdir: str = "/work"
    tmpfs_size: str = "64m"
    nofile: int = 256
    fsize_bytes: int = 32 * 1024 * 1024
    read_only: bool = True
    drop_all_caps: bool = True
    no_new_privileges: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)

    def docker_args(self, mount_src: str | None = None) -> list[str]:
        """`docker run` 인자 목록. 호출자가 command를 뒤에 붙인다."""
        args = [
            "docker", "run", "--rm",
            "--network", self.network,
            "--memory", self.memory,
            "--memory-swap", self.memory_swap,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--user", self.user,
            "--workdir", self.workdir,
            "--ulimit", f"nofile={self.nofile}:{self.nofile}",
            "--ulimit", f"fsize={self.fsize_bytes}",
        ]
        if self.read_only:
            args += ["--read-only"]
            # read-only 루트에서 파이썬이 돌려면 쓰기 가능한 경로가 최소 하나는 필요하다.
            # noexec/nosuid를 붙여 그 경로가 실행 경로가 되지 않게 한다.
            args += ["--tmpfs", f"/tmp:rw,noexec,nosuid,size={self.tmpfs_size}"]
            args += ["--tmpfs", f"{self.workdir}:rw,noexec,nosuid,size={self.tmpfs_size}"]
        if self.drop_all_caps:
            args += ["--cap-drop", "ALL"]
        if self.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        if mount_src:
            # 입력 데이터는 읽기 전용으로만 넣는다. s2p 원본을 컨테이너가 고치면 안 된다.
            args += ["--volume", f"{mount_src}:/data:ro"]
        for k, v in self.extra_env.items():
            args += ["--env", f"{k}={v}"]
        # 오프라인 강제. 이미지에 없는 패키지는 실행 시점에 받아올 수 없다.
        args += ["--env", "PIP_NO_INDEX=1", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        args.append(self.image)
        return args

    def preflight(self) -> list[str]:
        """정책 자체의 결함을 잡는다. CI에서 이 목록이 비어야 한다."""
        problems: list[str] = []
        if self.network != "none":
            problems.append(
                f"network={self.network!r}: 샌드박스는 network=none 이어야 한다. "
                "AI가 생성한 코드가 사내망을 스캔할 수 있다."
            )
        if self.memory_swap != self.memory:
            problems.append("memory_swap이 memory와 다르면 스왑으로 메모리 제한을 우회한다")
        if self.user.startswith("0:") or self.user == "root":
            problems.append("컨테이너를 root로 돌리지 않는다")
        if not self.read_only:
            problems.append("read_only=False: 루트 파일시스템 쓰기를 허용하면 안 된다")
        if not self.drop_all_caps:
            problems.append("cap-drop ALL 이 아니면 기본 capability가 남는다")
        if self.timeout_sec > 300:
            problems.append(f"timeout {self.timeout_sec}s: GPU 노드를 오래 붙잡는다")
        return problems


# 이미지에 미리 구워야 하는 패키지. network=none 이므로 실행 시점 설치는 불가능하다.
# 이 목록은 infra/docker/Dockerfile.py-emc 와 함께 관리한다.
BAKED_PACKAGES = (
    "numpy", "scipy", "pandas", "matplotlib",
    "scikit-rf",   # 터치스톤(.s2p/.s4p) 파싱 — EMC 작업의 핵심
    "pytest", "flake8",
)


def build_command(code_path: str, policy: SandboxPolicy | None = None,
                  mount_src: str | None = None) -> str:
    """사람이 읽고 검토할 수 있는 한 줄 명령. 감사 로그에 이 문자열을 남긴다."""
    p = policy or SandboxPolicy()
    argv = p.docker_args(mount_src) + ["timeout", str(p.timeout_sec), "python3", code_path]
    return " ".join(shlex.quote(a) for a in argv)


if __name__ == "__main__":
    p = SandboxPolicy()
    problems = p.preflight()
    print("preflight:", "통과" if not problems else problems)
    print()
    print(build_command("/tmp/gen.py", p, mount_src="/srv/emc/measurements"))
