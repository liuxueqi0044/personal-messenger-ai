from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "build_visual_selection_acceptance_config_guest.py"
)
SPEC = importlib.util.spec_from_file_location(
    "build_visual_selection_acceptance_config_guest", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"first\nsecond\n", id="lf"),
        pytest.param(b"first\r\nsecond\r\n", id="crlf"),
        pytest.param(b"first\nsecond\r\nthird\rfourth\n", id="mixed"),
    ],
)
def test_exclusive_write_preserves_newline_bytes(
    tmp_path: Path, payload: bytes
) -> None:
    output = tmp_path / "newlines.bin"

    MODULE._exclusive_write(output, payload)

    assert output.read_bytes() == payload


def test_exclusive_write_retries_short_writes(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "short-writes.bin"
    payload = b"first\nsecond\r\nthird\rfourth\n"
    real_write = MODULE.os.write
    write_sizes = []

    def short_write(descriptor: int, remaining: memoryview) -> int:
        chunk = remaining[:3]
        written = real_write(descriptor, chunk)
        write_sizes.append(written)
        return written

    monkeypatch.setattr(MODULE.os, "write", short_write)

    MODULE._exclusive_write(output, payload)

    assert output.read_bytes() == payload
    assert len(write_sizes) > 1
    assert sum(write_sizes) == len(payload)
    assert max(write_sizes) <= 3


def test_build_adds_only_validated_visual_section(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "runtime.json"
    output = tmp_path / "attempt.json"
    source.write_text(json.dumps({
        "schema": "pmai-v5-runtime-1",
        "data_dir": str(tmp_path),
        "model": "deepseek-v4-flash",
        "bindings": [],
    }), encoding="utf-8")
    bindings = (
        SimpleNamespace(binding_id="session-contact-1"),
        SimpleNamespace(binding_id="session-contact-2"),
        SimpleNamespace(binding_id="session-contact-3"),
    )
    validated = []
    monkeypatch.setattr(MODULE, "load_config", lambda _path: json.loads(source.read_text()))
    monkeypatch.setattr(
        MODULE,
        "validate_config",
        lambda config, api_key: validated.append(config.copy()) or ("pack", bindings, ()),
    )
    monkeypatch.setattr(MODULE, "_capability", lambda *_args: None)

    candidate = MODULE.build(
        source_config=source,
        output_config=output,
        labels={1: "联系人甲", 2: "联系人乙", 3: "联系人丙"},
    )

    assert len(validated) == 2
    assert "visual_selection" not in validated[0]
    assert validated[1]["visual_selection"] == candidate["visual_selection"]
    assert candidate["visual_selection"]["labels"] == {
        "session-contact-1": "联系人甲",
        "session-contact-2": "联系人乙",
        "session-contact-3": "联系人丙",
    }
    assert json.loads(output.read_text(encoding="utf-8")) == candidate


def test_build_rejects_partial_labels_before_writing(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "runtime.json"
    source.write_text("{}", encoding="utf-8")
    bindings = (
        SimpleNamespace(binding_id="session-contact-1"),
        SimpleNamespace(binding_id="session-contact-2"),
    )
    monkeypatch.setattr(MODULE, "load_config", lambda _path: {"data_dir": str(tmp_path)})
    monkeypatch.setattr(
        MODULE, "validate_config", lambda *_args, **_kwargs: ("pack", bindings, ())
    )
    monkeypatch.setattr(MODULE, "_capability", lambda *_args: None)

    with pytest.raises(ValueError, match="one-to-one"):
        MODULE.build(
            source_config=source,
            output_config=tmp_path / "attempt.json",
            labels={2: "联系人乙"},
        )
    assert not (tmp_path / "attempt.json").exists()


def test_parse_labels_rejects_duplicates_and_control_characters() -> None:
    assert MODULE._parse_labels(["1=联系人甲", "2=联系人乙"]) == {1: "联系人甲", 2: "联系人乙"}
    with pytest.raises(ValueError, match="unique"):
        MODULE._parse_labels(["2=联系人乙", "2=另一人"])
    with pytest.raises(ValueError, match="unique"):
        MODULE._parse_labels(["2=联系人乙\n注入"])
