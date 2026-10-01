from pathlib import Path

import pytest

from messenger_ai.runtime.config_publication import (
    ConfigPublication, assert_data_publication_complete, assert_publication_complete, atomic_bytes, publication_marker,
)


def test_interrupted_publication_blocks_start_and_recovers_exact_bytes(tmp_path: Path):
    output = tmp_path / "runtime.json"
    root = tmp_path / "data"
    root.mkdir()
    registry = root / "registered-session-scope.json"
    manifest = root / "generation-manifest.json"
    output.write_bytes(b"old published config")
    registry.write_bytes(b"old identity")
    transaction = ConfigPublication(output, [registry, manifest])
    transaction.__enter__()  # Simulate process death without __exit__.
    atomic_bytes(registry, b"new identity")
    atomic_bytes(manifest, b"new manifest")
    atomic_bytes(output, b"new published config")
    orphan = root / (".registered-session-scope.json." + "a" * 32 + ".tmp")
    orphan.write_bytes(b"partial next write")
    unrelated = root / "operator-notes.tmp"
    unrelated.write_bytes(b"preserve")
    with pytest.raises(RuntimeError, match="publication incomplete"):
        assert_publication_complete(output)
    with pytest.raises(RuntimeError, match="publication incomplete"):
        assert_data_publication_complete(root)
    assert ConfigPublication.recover(output, runtime_root=root)
    assert output.read_bytes() == b"old published config"
    assert registry.read_bytes() == b"old identity"
    assert not manifest.exists()
    assert not orphan.exists()
    assert unrelated.read_bytes() == b"preserve"
    assert_publication_complete(output)
    assert_data_publication_complete(root)
    assert not ConfigPublication.recover(output, runtime_root=root)


def test_failed_rollback_retains_startup_fence(tmp_path: Path, monkeypatch):
    output = tmp_path / "runtime.json"
    output.write_bytes(b"before")
    transaction = ConfigPublication(output, [])
    def fail(_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(transaction, "_rollback", fail)
    with pytest.raises(OSError):
        with transaction:
            output.write_bytes(b"after")
            raise RuntimeError("publish failed")
    assert publication_marker(output).exists()
    ConfigPublication.recover(output, runtime_root=tmp_path)
    assert output.read_bytes() == b"before"


def test_recovery_validates_all_targets_before_restoring(tmp_path: Path):
    import json
    output = tmp_path / "runtime.json"
    root = tmp_path / "data"
    root.mkdir()
    output.write_bytes(b"before")
    ConfigPublication(output, []).__enter__()
    output.write_bytes(b"after")
    marker = publication_marker(output)
    payload = json.loads(marker.read_text())
    payload["before"].append({"path": str(tmp_path / "outside.json"), "bytes": None})
    marker.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="recovery target"):
        ConfigPublication.recover(output, runtime_root=root)
    assert output.read_bytes() == b"after"
    assert marker.exists()
