from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "promote_visual_selection_config_guest.py"
)
SPEC = importlib.util.spec_from_file_location(
    "promote_visual_selection_config_guest", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _config(tmp_path: Path) -> dict[str, object]:
    return {
        "schema": "pmai-v5-runtime-1",
        "data_dir": str(tmp_path),
        "bindings": [{"binding_id": "session-contact-1"}],
    }


def _arrange(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(_config(tmp_path), indent=2), encoding="utf-8")
    monkeypatch.setattr(MODULE, "load_config", lambda path: json.loads(path.read_text()))
    monkeypatch.setattr(MODULE, "validate_config", lambda config, api_key: None)

    def build(*, source_config, output_config, labels):
        value = json.loads(source_config.read_text())
        value["visual_selection"] = {
            "model": "deepseek-v4-flash-vision-exp",
            "labels": {"session-contact-1": labels[1]},
            "min_confidence": 0.98,
        }
        MODULE._exclusive_write(
            output_config,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        )
        return value

    monkeypatch.setattr(MODULE, "build", build)
    return config_path


def _accepted_digest(config: Path, labels: dict[int, str]) -> str:
    value = json.loads(config.read_text())
    value["visual_selection"] = {
        "model": "deepseek-v4-flash-vision-exp",
        "labels": {"session-contact-1": labels[1]},
        "min_confidence": 0.98,
    }
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_promotion_keeps_exact_backup_and_installs_validated_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    config = _arrange(monkeypatch, tmp_path)
    before = config.read_bytes()
    backup = tmp_path / "backup.json"
    candidate = tmp_path / "candidate.json"

    report = MODULE.promote(
        config_path=config,
        backup_path=backup,
        candidate_path=candidate,
        labels={1: "联系人甲"},
        accepted_config_sha256=_accepted_digest(config, {1: "联系人甲"}),
    )

    installed = json.loads(config.read_text())
    assert backup.read_bytes() == before
    assert installed["visual_selection"]["labels"] == {"session-contact-1": "联系人甲"}
    assert not candidate.exists()
    assert report["before_sha256"] == report["backup_sha256"]
    assert report["after_sha256"] != report["before_sha256"]
    assert report["after_sha256"] == report["accepted_config_sha256"]
    assert report["state"] == "succeeded"
    assert report["committed"] is True
    assert report["rolled_back"] is False
    assert report["semantic_change"] == "visual_selection_only"


def test_promotion_refuses_existing_visual_config(monkeypatch, tmp_path: Path) -> None:
    config = _arrange(monkeypatch, tmp_path)
    value = json.loads(config.read_text())
    value["visual_selection"] = {"already": "configured"}
    config.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(RuntimeError, match="already configured"):
        MODULE.promote(
            config_path=config,
            backup_path=tmp_path / "backup.json",
            candidate_path=tmp_path / "candidate.json",
            labels={1: "联系人甲"},
            accepted_config_sha256="a" * 64,
        )


def test_promotion_refuses_paths_outside_canonical_directory(
    monkeypatch, tmp_path: Path
) -> None:
    config = _arrange(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="backup must stay beside"):
        MODULE.promote(
            config_path=config,
            backup_path=outside / "backup.json",
            candidate_path=tmp_path / "candidate.json",
            labels={1: "联系人甲"},
            accepted_config_sha256="a" * 64,
        )


def test_promotion_refuses_candidate_outside_runtime_data_dir(
    monkeypatch, tmp_path: Path
) -> None:
    config = _arrange(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="candidate must stay"):
        MODULE.promote(
            config_path=config,
            backup_path=tmp_path / "backup.json",
            candidate_path=outside / "candidate.json",
            labels={1: "联系人甲"},
            accepted_config_sha256="a" * 64,
        )


def test_promotion_rejects_unaccepted_candidate_before_backup_or_commit(
    monkeypatch, tmp_path: Path
) -> None:
    config = _arrange(monkeypatch, tmp_path)
    before = config.read_bytes()
    backup = tmp_path / "backup.json"

    with pytest.raises(RuntimeError, match="ACCEPTED_CONFIG_SHA256_MISMATCH"):
        MODULE.promote(
            config_path=config,
            backup_path=backup,
            candidate_path=tmp_path / "candidate.json",
            labels={1: "联系人甲"},
            accepted_config_sha256="a" * 64,
        )

    assert config.read_bytes() == before
    assert not backup.exists()


def test_promotion_checks_source_cas_before_commit(monkeypatch, tmp_path: Path) -> None:
    config = _arrange(monkeypatch, tmp_path)
    accepted = _accepted_digest(config, {1: "联系人甲"})
    build = MODULE.build

    def racing_build(*, source_config, output_config, labels):
        value = build(
            source_config=source_config,
            output_config=output_config,
            labels=labels,
        )
        source_config.write_bytes(source_config.read_bytes() + b" ")
        return value

    monkeypatch.setattr(MODULE, "build", racing_build)

    with pytest.raises(RuntimeError, match="PROMOTION_SOURCE_CHANGED"):
        MODULE.promote(
            config_path=config,
            backup_path=tmp_path / "backup.json",
            candidate_path=tmp_path / "candidate.json",
            labels={1: "联系人甲"},
            accepted_config_sha256=accepted,
        )

    assert config.read_bytes().endswith(b" ")


def test_post_commit_validation_failure_rolls_back_exact_bytes(
    monkeypatch, tmp_path: Path
) -> None:
    config = _arrange(monkeypatch, tmp_path)
    before = config.read_bytes()
    accepted = _accepted_digest(config, {1: "联系人甲"})
    validate_payload = MODULE._validate_payload
    calls = 0

    def fail_post_commit(payload, *, api_key):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("POST_COMMIT_TEST_FAILURE")
        return validate_payload(payload, api_key=api_key)

    monkeypatch.setattr(MODULE, "_validate_payload", fail_post_commit)

    with pytest.raises(MODULE.PromotionFailure) as raised:
        MODULE.promote(
            config_path=config,
            backup_path=tmp_path / "backup.json",
            candidate_path=tmp_path / "candidate.json",
            labels={1: "联系人甲"},
            accepted_config_sha256=accepted,
        )

    assert raised.value.report["state"] == "rolled_back"
    assert raised.value.report["committed"] is True
    assert raised.value.report["rolled_back"] is True
    assert config.read_bytes() == before
    assert (tmp_path / "backup.json").read_bytes() == before
    assert not (tmp_path / "candidate.json").exists()


def test_backup_is_verified_before_canonical_replace(monkeypatch, tmp_path: Path) -> None:
    config = _arrange(monkeypatch, tmp_path)
    before = config.read_bytes()
    backup = tmp_path / "backup.json"
    accepted = _accepted_digest(config, {1: "联系人甲"})
    replace = MODULE.os.replace

    def guarded_replace(source, destination):
        if Path(destination) == config:
            assert backup.read_bytes() == before
        return replace(source, destination)

    monkeypatch.setattr(MODULE.os, "replace", guarded_replace)

    MODULE.promote(
        config_path=config,
        backup_path=backup,
        candidate_path=tmp_path / "candidate.json",
        labels={1: "联系人甲"},
        accepted_config_sha256=accepted,
    )
