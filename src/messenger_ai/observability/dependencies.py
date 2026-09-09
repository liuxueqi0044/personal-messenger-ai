"""Offline dependency and prohibited-technique gate."""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

FORBIDDEN_PACKAGES = frozenset(
    {
        "frida",
        "pymem",
        "pyinjector",
        "winappdbg",
        "itchat",
        "wxpy",
        "wechatferry",
        "wcferry",
        "oicq",
        "ntqq",
        "qqbot",
        "mirai",
        "nonebot-adapter-onebot",
        "pywechat",
        "pyweixin",
    }
)

_SOURCE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PROCESS_MEMORY_READ",
        re.compile(
            r"\b(?:ReadProcessMemory|NtReadVirtualMemory|pymem)\b", re.IGNORECASE
        ),
    ),
    (
        "PROCESS_MEMORY_WRITE",
        re.compile(r"\b(?:WriteProcessMemory|NtWriteVirtualMemory)\b", re.IGNORECASE),
    ),
    (
        "REMOTE_INJECTION",
        re.compile(
            r"\b(?:CreateRemoteThread|VirtualAllocEx|DLL[_ ]?inject)\b", re.IGNORECASE
        ),
    ),
    (
        "GLOBAL_HOOK",
        re.compile(r"\b(?:SetWindowsHookEx|WH_KEYBOARD|WH_MOUSE)\b", re.IGNORECASE),
    ),
    (
        "PLATFORM_DB_DECRYPT",
        re.compile(
            r"(?:MicroMsg\.db|Msg3\.0\.db|decrypt.{0,24}(?:wechat|qq).{0,16}(?:db|database))",
            re.IGNORECASE,
        ),
    ),
    (
        "UNOFFICIAL_PROTOCOL",
        re.compile(
            r"\b(?:OneBot|OICQ|NTQQ|WeChatFerry|wcferry|itchat|wxpy|Mirai)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "FRIDA_RUNTIME",
        re.compile(r"\bfrida(?:[-_. ]server|\.attach|\.spawn)?\b", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class DependencyFinding:
    rule_id: str
    location: str
    line: int
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DependencyAudit:
    passed: bool
    scanned_files: int
    direct_dependencies: tuple[str, ...]
    findings: tuple[DependencyFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "scanned_files": self.scanned_files,
            "direct_dependencies": list(self.direct_dependencies),
            "findings": [item.to_dict() for item in self.findings],
        }


class DependencyGate:
    def audit(self, project_root: str | Path) -> DependencyAudit:
        root = Path(project_root).resolve()
        dependencies = self._read_dependencies(root / "pyproject.toml")
        findings: list[DependencyFinding] = []
        for dependency in dependencies:
            package = self._package_name(dependency)
            if package in FORBIDDEN_PACKAGES:
                findings.append(
                    DependencyFinding(
                        rule_id="FORBIDDEN_DEPENDENCY",
                        location="pyproject.toml",
                        line=0,
                        detail=package,
                    )
                )

        scanned = 0
        source_root = root / "src"
        scanner_file = Path(__file__).resolve()
        for source in source_root.rglob("*.py") if source_root.exists() else ():
            if source.is_symlink() or source.resolve() == scanner_file:
                continue
            scanned += 1
            text = source.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                for rule_id, pattern in _SOURCE_RULES:
                    if pattern.search(line):
                        findings.append(
                            DependencyFinding(
                                rule_id=rule_id,
                                location=source.relative_to(root).as_posix(),
                                line=line_number,
                                detail="prohibited implementation technique",
                            )
                        )
        return DependencyAudit(
            passed=not findings,
            scanned_files=scanned,
            direct_dependencies=dependencies,
            findings=tuple(findings),
        )

    @staticmethod
    def _read_dependencies(pyproject: Path) -> tuple[str, ...]:
        with pyproject.open("rb") as handle:
            document = tomllib.load(handle)
        project = document.get("project", {})
        dependencies = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            dependencies.extend(group)
        return tuple(sorted({str(item) for item in dependencies}))

    @staticmethod
    def _package_name(requirement: str) -> str:
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
        return match.group(1).replace("_", "-").casefold() if match else ""
