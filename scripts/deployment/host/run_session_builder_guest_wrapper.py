"""Guest-only result wrapper for the frozen session config builder."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


RUNTIME_CONFIG = Path(r"C:\PMAI\data\runtime-session-1.json")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_builder(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("pmai_frozen_session_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("BUILDER_IMPORT_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--contact-index", type=int, action="append", required=True)
    parser.add_argument(
        "--migrate-header-digest-index", type=int, action="append", default=[]
    )
    parser.add_argument(
        "--adopt-latest-inbound-index", type=int, action="append", default=[]
    )
    parser.add_argument("--isolated-recovery-generation")
    parser.add_argument("--visual-label", action="append", default=[])
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {
        "schema": "pmai-session-builder-guest-wrapper-v1",
        "release_id": args.release_id,
        "succeeded": False,
    }
    exit_code = 2
    try:
        if re.fullmatch(r"r\d{8}-\d{2}", args.release_id) is None:
            raise ValueError("RELEASE_ID_INVALID")
        indices = sorted(set(args.contact_index))
        header_upgrades = sorted(set(args.migrate_header_digest_index))
        adoption_indices = sorted(set(args.adopt_latest_inbound_index))
        if any(not 1 <= index <= 9999 for index in indices):
            raise ValueError("CONTACT_INDEX_INVALID")
        if set(header_upgrades) - set(indices):
            raise ValueError("HEADER_UPGRADE_SCOPE_INVALID")
        if set(adoption_indices) - set(indices):
            raise ValueError("ADOPTION_SCOPE_INVALID")
        if args.isolated_recovery_generation is not None:
            from uuid import UUID

            generation_id = str(UUID(args.isolated_recovery_generation))
            if generation_id != args.isolated_recovery_generation:
                raise ValueError("ISOLATED_GENERATION_ID_NOT_CANONICAL")
        visual_indices: set[int] = set()
        for item in args.visual_label:
            index_text, separator, label = item.partition("=")
            if separator != "=" or not index_text.isdigit() or not label.strip():
                raise ValueError("VISUAL_LABEL_INVALID")
            index = int(index_text)
            if index in visual_indices:
                raise ValueError("VISUAL_LABEL_DUPLICATE")
            visual_indices.add(index)
        if visual_indices and visual_indices != set(indices):
            raise ValueError("VISUAL_LABEL_SCOPE_INVALID")
        script = (
            Path("C:/PMAI/app/releases")
            / args.release_id
            / "build_session_observed_runtime_guest.py"
        )
        builder = load_builder(script)
        report["previous_config_sha256"] = _sha256(RUNTIME_CONFIG)
        builder_args: list[str] = []
        if args.isolated_recovery_generation is not None:
            for index in indices:
                builder_args.extend(("--contact-index", str(index)))
        else:
            # Nonisolated contact-index retains its existing refresh meaning.
            if 2 in indices:
                builder_args.append("--include-contact-2")
            for index in indices:
                if index > 2:
                    builder_args.extend(("--additional-contact-index", str(index)))
        for index in indices:
            builder_args.extend(("--refresh-session-index", str(index)))
        for index in header_upgrades:
            builder_args.extend(("--migrate-header-digest-index", str(index)))
        for index in adoption_indices:
            builder_args.extend(("--adopt-latest-inbound-index", str(index)))
        if args.isolated_recovery_generation is not None:
            builder_args.extend((
                "--isolated-recovery-generation",
                args.isolated_recovery_generation,
            ))
        for visual_label in args.visual_label:
            builder_args.extend(("--visual-label", visual_label))
        exit_code = int(builder.main(builder_args))
        report.update({
            "contact_indices": indices,
            "builder_exit_code": exit_code,
            "adopt_latest_inbound_indices": adoption_indices,
            "isolated_recovery_generation": args.isolated_recovery_generation,
            "succeeded": exit_code == 0,
            "result_config_sha256": _sha256(RUNTIME_CONFIG),
        })
    except BaseException as exc:
        report.update({
            "error_type": type(exc).__name__,
            "error_code": str(exc)[:512],
            "result_config_sha256": _sha256(RUNTIME_CONFIG),
        })
        exit_code = 2
    finally:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.result.with_suffix(".tmp.json")
        temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        temporary.replace(args.result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
