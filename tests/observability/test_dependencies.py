from __future__ import annotations

from datetime import UTC, datetime

from messenger_ai.observability import DependencyGate, generate_sbom


def test_current_project_has_no_forbidden_dependency_or_source_technique():
    root = __import__("pathlib").Path(__file__).parents[2]
    report = DependencyGate().audit(root)
    assert report.passed, report.findings
    assert report.scanned_files > 0


def test_forbidden_dependency_and_memory_injection_source_are_detected(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="bad"\nversion="1"\ndependencies=["frida>=1"]\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "bad.py").write_text(
        "def attack():\n    return ReadProcessMemory(target)\n",
        encoding="utf-8",
    )
    report = DependencyGate().audit(tmp_path)
    assert not report.passed
    assert {item.rule_id for item in report.findings} == {
        "FORBIDDEN_DEPENDENCY",
        "PROCESS_MEMORY_READ",
    }


def test_offline_sbom_records_all_direct_dependencies_and_license_status():
    root = __import__("pathlib").Path(__file__).parents[2]
    sbom = generate_sbom(
        root,
        generated_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    assert sbom["metadata"]["networkAccessUsed"] is False
    names = {item["normalized_name"] for item in sbom["components"]}
    assert {"pydantic", "pyyaml", "openai", "mcp"}.issubset(names)
    assert all("license" in item and item["license"] for item in sbom["components"])
