from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "messenger_ai" / "mcp_gateway"
FORBIDDEN_IMPORT_PARTS = {"adapters", "webui", "llm", "sqlite3", "secrets", "memory"}


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
