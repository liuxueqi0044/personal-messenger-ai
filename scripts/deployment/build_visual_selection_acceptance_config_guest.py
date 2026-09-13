"""Build a validated, attempt-local visual-selection config without migrations."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from run_vm_runtime import _capability, load_config, validate_config
except ModuleNotFoundError:  # repository-root test/import path
    from scripts.run_vm_runtime import _capability, load_config, validate_config


_LABEL = re.compile(r"([1-9][0-9]{0,3})=(.+)", re.DOTALL)
_BINDING = re.compile(r"session-contact-([1-9][0-9]{0,3})")


def _exclusive_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("exclusive write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_labels(values: list[str]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for value in values:
        match = _LABEL.fullmatch(value)
        if match is None:
            raise ValueError("visual label must use INDEX=LABEL")
        index = int(match.group(1))
        label = match.group(2)
        if (
            index in labels
            or not label.strip()
            or len(label) > 96
            or any(ord(character) < 32 for character in label)
        ):
            raise ValueError("visual label must be a unique INDEX=LABEL entry")
        labels[index] = label
    if not labels:
        raise ValueError("visual labels are required")
    return labels


def build(
    *, source_config: Path, output_config: Path, labels: dict[int, str]
) -> dict[str, Any]:
    config = load_config(source_config)
    pack, bindings, _evidence = validate_config(
        config, api_key="offline-visual-acceptance-config"
    )
    _capability(config, pack)
    expected: dict[int, str] = {}
    for binding in bindings:
        match = _BINDING.fullmatch(binding.binding_id)
        if match is None:
            raise ValueError("visual acceptance requires numbered session bindings")
        expected[int(match.group(1))] = binding.binding_id
    if set(labels) != set(expected):
        raise ValueError("visual labels must cover configured bindings one-to-one")
    candidate = dict(config)
    candidate["visual_selection"] = {
        "model": "deepseek-v4-flash-vision-exp",
        "labels": {
            expected[index]: labels[index] for index in sorted(expected)
        },
        "min_confidence": 0.98,
        "timeout_seconds": 8,
    }
    validated_pack, _bindings, _evidence = validate_config(
        candidate, api_key="offline-visual-acceptance-config"
    )
    _capability(candidate, validated_pack)
    if output_config.parent.resolve() != Path(str(config["data_dir"])).resolve():
        raise ValueError("visual acceptance config must stay in the runtime data directory")
    output_config.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        candidate,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        _exclusive_write(output_config, payload)
    except FileExistsError as exc:
        raise RuntimeError("visual acceptance config already exists") from exc
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--output-config", type=Path, required=True)
    parser.add_argument("--visual-label", action="append", default=[])
    args = parser.parse_args(argv)
    labels = _parse_labels(args.visual_label)
    build(
        source_config=args.source_config,
        output_config=args.output_config,
        labels=labels,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
