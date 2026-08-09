#!/usr/bin/env python3
"""정책 엔진 — 사내/사외 에디션 경계를 코드로 강제한다.

이 파일이 존재하는 이유
    PRD v2는 §14에서 "인터넷 직접 연결 금지"라고 쓰고, §8.1에서는 GPT/Claude/
    Gemini CLI를 프로바이더 풀에 올린다. 두 문장은 양립할 수 없다. 문서로 쓴
    규칙은 지켜지는지 확인할 방법이 없으므로, 경계를 실행 가능한 판정기로 옮긴다.

    egress(외부 송신) · exec(명령 실행) · capture(화면 캡처) 세 가지가 이 시스템의
    위험 표면 전부다. 셋 다 이 엔진을 거치지 않으면 안 된다.

외부 의존성 없음 (Python 3.11 표준 라이브러리). 폐쇄망에서 그대로 돈다.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Decision = Literal["allow", "deny", "confirm", "redact"]

# 프로파일에서 참조할 수 있는 위험 표면
SURFACES = ("egress", "exec", "capture", "store")


class PolicyError(Exception):
    """프로파일이 성립하지 않을 때. 실행을 계속하면 안 되는 상황이다."""


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    surface: str
    subject: str
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "redact")

    def __str__(self) -> str:
        return f"[{self.decision.upper()}] {self.surface}:{self.subject} — {self.reason} ({self.rule})"


# ─────────────────────────────────────────────────────────────
# 크리덴셜·비밀 탐지
#
# 화면 캡처와 CLI 로그에는 토큰이 실제로 자주 들어간다. Ainative가
# kb/schema.md 에서 `screened: true` 를 강제하는 것과 같은 이유다.
# 여기서는 텍스트 표면만 본다 — 이미지 OCR 후 다시 이 함수를 통과시킨다.
# ─────────────────────────────────────────────────────────────
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("jdbc-pass", re.compile(r"(?i)(?:password|passwd|pwd)\s*[=:]\s*\S{6,}")),
    # 주민등록번호 — 개인정보보호법 대상. 개인 볼트에서도 저장하지 않는다.
    ("kr-rrn", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")),
)


def scan_secrets(text: str) -> list[str]:
    """탐지된 비밀의 종류 목록. 값 자체는 반환하지 않는다(로그 오염 방지)."""
    return [name for name, pat in SECRET_PATTERNS if pat.search(text)]


def redact(text: str) -> tuple[str, list[str]]:
    """탐지된 비밀을 자리표시자로 치환한다. (치환된 텍스트, 탐지 종류)"""
    found: list[str] = []
    out = text
    for name, pat in SECRET_PATTERNS:
        if pat.search(out):
            found.append(name)
            out = pat.sub(f"<<REDACTED:{name}>>", out)
    return out, found


# ─────────────────────────────────────────────────────────────
# 프로파일
# ─────────────────────────────────────────────────────────────
@dataclass
class Profile:
    """에디션 하나의 전체 정책. profiles/*.json 이 정본이다."""

    name: str
    edition: str  # "enterprise" | "personal"
    vault_root: str
    egress_default: Decision
    egress_allow: list[str] = field(default_factory=list)
    egress_deny: list[str] = field(default_factory=list)
    providers: dict[str, dict] = field(default_factory=dict)
    exec_default: Decision = "confirm"
    exec_allow: list[str] = field(default_factory=list)
    exec_deny: list[str] = field(default_factory=list)
    capture_require_screening: bool = True
    capture_allow_apps: list[str] = field(default_factory=list)
    capture_deny_apps: list[str] = field(default_factory=list)
    store_redact_secrets: bool = True
    store_retention_days: int = 0  # 0 = 무기한
    approvals: dict[str, str] = field(default_factory=dict)
    # 프로파일을 세션마다 갈아끼우지 못하게 하는 고정 장치.
    # 사내 PC에서 personal 프로파일이 뜨는 것이 가장 큰 유출 경로다.
    pinned_to: list[str] = field(default_factory=list)

    @staticmethod
    def load(path: str | Path) -> "Profile":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f for f in Profile.__dataclass_fields__}
        unknown = set(raw) - known - {"$comment"}
        if unknown:
            raise PolicyError(f"{path}: 알 수 없는 필드 {sorted(unknown)}")
        raw.pop("$comment", None)
        prof = Profile(**raw)
        prof.validate()
        return prof

    def validate(self) -> None:
        if self.edition not in ("enterprise", "personal"):
            raise PolicyError(f"edition은 enterprise|personal 이어야 한다: {self.edition!r}")
        if self.edition == "enterprise":
            # 사내 에디션의 불변식. 이 조건이 깨지면 §14 망분리 전제가 무너진다.
            if self.egress_default != "deny":
                raise PolicyError("enterprise 프로파일의 egress_default는 deny 여야 한다")
            if not self.capture_require_screening:
                raise PolicyError("enterprise 프로파일은 캡처 스크리닝을 끌 수 없다")
            cloud = [n for n, p in self.providers.items() if p.get("transport") == "cloud"]
            if cloud:
                raise PolicyError(f"enterprise 프로파일에 클라우드 프로바이더 금지: {cloud}")


# ─────────────────────────────────────────────────────────────
# 엔진
# ─────────────────────────────────────────────────────────────
class PolicyEngine:
    def __init__(self, profile: Profile, hostname: str | None = None):
        self.profile = profile
        if profile.pinned_to and hostname is not None:
            if not any(fnmatch.fnmatch(hostname, pat) for pat in profile.pinned_to):
                raise PolicyError(
                    f"프로파일 {profile.name!r}은 {profile.pinned_to} 에만 고정돼 있다. "
                    f"현재 호스트: {hostname!r}"
                )

    # -- egress -------------------------------------------------
    def check_egress(self, host: str, payload: str = "") -> Verdict:
        """외부 송신 판정. host는 도메인 또는 프로바이더 이름."""
        for pat in self.profile.egress_deny:
            if fnmatch.fnmatch(host, pat):
                return Verdict("deny", "egress", host, f"egress_deny:{pat}", "명시적 차단 목록")
        for pat in self.profile.egress_allow:
            if fnmatch.fnmatch(host, pat):
                leaked = scan_secrets(payload) if payload else []
                if leaked:
                    return Verdict(
                        "deny", "egress", host, "secret-scan",
                        f"페이로드에 비밀 포함: {leaked}",
                    )
                return Verdict("allow", "egress", host, f"egress_allow:{pat}", "허용 목록")
        return Verdict(
            self.profile.egress_default, "egress", host, "egress_default",
            f"허용 목록에 없음 (기본값 {self.profile.egress_default})",
        )

    def check_provider(self, name: str, payload: str = "") -> Verdict:
        prov = self.profile.providers.get(name)
        if prov is None:
            return Verdict("deny", "egress", name, "provider-registry", "등록되지 않은 프로바이더")
        if prov.get("transport") == "local":
            return Verdict("allow", "egress", name, "provider:local", "로컬 추론 — 망 이탈 없음")
        return self.check_egress(prov.get("endpoint", name), payload)

    # -- exec ---------------------------------------------------
    def check_exec(self, command: str) -> Verdict:
        """명령 실행 판정.

        PowerShell 훅은 이 시스템에서 가장 위험한 컴포넌트다. 파이썬은 컨테이너
        안에서 돌지만 PowerShell은 정의상 호스트에서 돈다 — 그것이 존재 이유다.
        따라서 샌드박스가 아니라 이 판정기가 유일한 방어선이다.
        """
        norm = " ".join(command.split())
        for pat in self.profile.exec_deny:
            if re.search(pat, norm, re.IGNORECASE):
                return Verdict("deny", "exec", norm[:80], f"exec_deny:{pat}", "금지 패턴 일치")
        for pat in self.profile.exec_allow:
            if re.match(pat, norm, re.IGNORECASE):
                return Verdict("allow", "exec", norm[:80], f"exec_allow:{pat}", "허용 패턴 일치")
        return Verdict(
            self.profile.exec_default, "exec", norm[:80], "exec_default",
            f"허용 목록에 없음 (기본값 {self.profile.exec_default})",
        )

    # -- capture ------------------------------------------------
    def check_capture(self, app: str, ocr_text: str = "", screened: bool = False) -> Verdict:
        """화면 캡처 판정.

        원본 PRD가 통째로 빠뜨린 표면이다. EMC 설계 툴 화면 캡처는 문서가
        보호하겠다고 선언한 바로 그 핵심 기술을 담고 있다.
        """
        for pat in self.profile.capture_deny_apps:
            if fnmatch.fnmatch(app, pat):
                return Verdict("deny", "capture", app, f"capture_deny:{pat}", "캡처 금지 앱")
        if self.profile.capture_allow_apps:
            if not any(fnmatch.fnmatch(app, p) for p in self.profile.capture_allow_apps):
                return Verdict("deny", "capture", app, "capture_allow", "허용 앱 목록에 없음")
        if self.profile.capture_require_screening and not screened:
            return Verdict("confirm", "capture", app, "require_screening", "스크리닝 미완료")
        leaked = scan_secrets(ocr_text) if ocr_text else []
        if leaked:
            return Verdict("redact", "capture", app, "secret-scan", f"화면에 비밀 노출: {leaked}")
        return Verdict("allow", "capture", app, "capture_default", "통과")

    # -- store --------------------------------------------------
    def prepare_for_store(self, text: str) -> tuple[str, list[str]]:
        """이벤트 저장 직전 훅. 비밀은 원장에 들어가기 전에 지운다."""
        if not self.profile.store_redact_secrets:
            return text, []
        return redact(text)

    def requires_approval(self, action: str) -> str | None:
        return self.profile.approvals.get(action)


def load_engine(profile_path: str | Path, hostname: str | None = None) -> PolicyEngine:
    return PolicyEngine(Profile.load(profile_path), hostname=hostname)


if __name__ == "__main__":  # 간이 점검
    import sys

    prof = sys.argv[1] if len(sys.argv) > 1 else "profiles/enterprise.json"
    eng = load_engine(prof)
    print(f"profile: {eng.profile.name} ({eng.profile.edition})")
    for v in (
        eng.check_provider("claude"),
        eng.check_provider("qwen_local"),
        eng.check_exec("Get-ChildItem C:\\work"),
        eng.check_exec("Invoke-Expression $payload"),
        eng.check_capture("Notepad.exe", screened=True),
    ):
        print(" ", v)
