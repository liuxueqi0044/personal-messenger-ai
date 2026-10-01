from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).parents[2] / "src" / "messenger_ai" / "mcp_gateway"
FORBIDDEN_IMPORT_PARTS = {"adapters", "webui", "llm", "sqlite3", "secrets", "memory"}


def test_optional_sdk_range_matches_fastmcp_api() -> None:
    project = Path(__file__).parents[2] / "pyproject.toml"
    metadata = tomllib.loads(project.read_text(encoding="utf-8"))
    extra = metadata["project"]["optional-dependencies"]["mcp"]
    requirement = next(
        Requirement(item) for item in extra if Requirement(item).name == "mcp"
    )
    # FastMCP first shipped in 1.2; the 2.x SDK removes this import path.
    assert "1.0.0" not in requirement.specifier
    assert "1.1.0" not in requirement.specifier
    assert "1.2.0" in requirement.specifier
    assert "1.99.0" in requirement.specifier
    assert "2.0.0" not in requirement.specifier
    assert "2.2.0" not in requirement.specifier


def test_gateway_has_no_platform_or_storage_dependency_imports() -> None:
    for file in ROOT.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(
                part in name.split(".")
                for name in names
                for part in FORBIDDEN_IMPORT_PARTS
            ), (file, names)
