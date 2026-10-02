"""Recoverable publication of guest configuration metadata while runtime is stopped.

The write-ahead file is a startup fence, not an execution journal. An interrupted
publication is rolled back on the next build. Never snapshot live business DBs.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from uuid import uuid4


_METADATA_NAME = re.compile(
    r"(?:registered-session-scope(?:-[1-9][0-9]{0,3})?\.json|"
    r"generation-manifest\.json|runtime-config\.json|"
    r"runtime-config\.session-(?:[2-9]|[1-9][0-9]{1,4})\.json|"
    r"previous-runtime-config\.json|rules\.sqlite3)"
)


def publication_marker(config: Path) -> Path:
    return config.with_name(f".{config.name}.publication.json")


def assert_publication_complete(config: Path) -> None:
    if publication_marker(config).exists():
        raise RuntimeError("runtime config publication incomplete; rebuild while stopped")


def assert_data_publication_complete(data_dir: Path) -> None:
    if (data_dir / ".config-publication.json").exists():
        raise RuntimeError("runtime data configuration publication incomplete; rebuild while stopped")


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigPublication:
    """Rollback metadata on failure; retain the fence until rollback completes."""

    def __init__(self, output: Path, paths: list[Path]) -> None:
        self.output = output.resolve()
        self.paths = list(dict.fromkeys([path.resolve() for path in paths] + [self.output]))
        self.marker = publication_marker(self.output)
        self.snapshots: list[tuple[Path, bytes | None]] = []

    def __enter__(self) -> ConfigPublication:
        assert_publication_complete(self.output)
        for path in self.paths:
            if path != self.output and not _METADATA_NAME.fullmatch(path.name):
                raise ValueError("unsupported config publication target")
            if path.name == "rules.sqlite3" and path.exists():
                # Existing rules are validated, never changed by the builder.
                continue
            self.snapshots.append((path, path.read_bytes() if path.exists() else None))
        payload = {
            "schema": "pmai-config-publication-v1",
            "output": str(self.output),
            "before": [
                {"path": str(path), "bytes": base64.b64encode(value).decode("ascii")
                 if value is not None else None}
                for path, value in self.snapshots
            ],
        }
        atomic_bytes(self.marker, json.dumps(payload, sort_keys=True).encode("utf-8"))
        for marker in self._data_markers(self.snapshots, self.output):
            atomic_bytes(marker, json.dumps({"canonical": str(self.output)}).encode("utf-8"))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self._rollback(self.snapshots)
        for marker in self._data_markers(self.snapshots, self.output):
            marker.unlink(missing_ok=True)
        self.marker.unlink()

    @staticmethod
    def _data_markers(snapshots: list[tuple[Path, bytes | None]], output: Path) -> set[Path]:
        return {path.parent / ".config-publication.json" for path, _ in snapshots if path != output}

    @staticmethod
    def _rollback(snapshots: list[tuple[Path, bytes | None]]) -> None:
        for path, value in reversed(snapshots):
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, value)
            # Only abandoned files from these exact publication targets; never
            # remove unrelated files or walk a business data directory tree.
            pattern = re.compile(rf"\.{re.escape(path.name)}\.(?:[0-9a-f]{{32}}|[0-9]{{1,20}})\.tmp")
            for temporary in path.parent.glob(f".{path.name}.*.tmp"):
                if pattern.fullmatch(temporary.name) and temporary.is_file():
                    temporary.unlink()

    @classmethod
    def recover(cls, output: Path, *, runtime_root: Path) -> bool:
        output = output.resolve()
        marker = publication_marker(output)
        if not marker.exists():
            return False
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("schema") != "pmai-config-publication-v1" or payload.get("output") != str(output):
            raise RuntimeError("invalid config publication recovery record")
        snapshots = []
        for item in payload["before"]:
            path = Path(item["path"])
            if path != output and (
                not path.resolve().is_relative_to(runtime_root.resolve())
                or not _METADATA_NAME.fullmatch(path.name)
            ):
                raise RuntimeError("invalid config publication recovery target")
            value = base64.b64decode(item["bytes"], validate=True) if item["bytes"] is not None else None
            if path.name == "rules.sqlite3" and value is not None:
                raise RuntimeError("business database cannot be restored by config publication")
            snapshots.append((path, value))
        if not snapshots or len({path for path, _ in snapshots}) != len(snapshots) or output not in {path for path, _ in snapshots}:
            raise RuntimeError("invalid config publication recovery record")
        cls._rollback(snapshots)
        for data_marker in cls._data_markers(snapshots, output):
            data_marker.unlink(missing_ok=True)
        marker.unlink()
        return True
