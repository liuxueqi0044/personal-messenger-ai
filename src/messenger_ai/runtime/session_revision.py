"""Immutable QQ session metadata revisions over an unchanged business generation.

This module never opens a business database. A revision may replace only the four
process/window/row locators; identities, policies, adoption and data paths remain
exactly those of the immutable generation root.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity


SCHEMA = "pmai-qq-session-binding-v1"
TRUSTED_RUNTIME_ROOT = Path(r"C:\PMAI\data\runtime")
MAX_REVISION = 10000
_HASH = re.compile(r"[0-9a-f]{64}")
_LOCATORS = {
    "process_id", "window_handle", "process_started_at_100ns",
    "selected_row_runtime_id_hash",
}
_SCOPE = (
    "process_id", "window_handle", "process_started_at_100ns",
    "vm_environment_fingerprint", "client_version", "selector_pack_version",
)


@dataclass(frozen=True)
class SessionRevision:
    revision: int
    snapshot_path: Path
    base_config_sha256: str
    base_config: dict[str, Any]


def session_revision_number(config: dict[str, Any]) -> int:
    if "session_binding" not in config:
        return 1
    value = config["session_binding"]
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "revision", "base_config_sha256", "previous_config_sha256"}
        or value.get("schema") != SCHEMA
        or isinstance(value.get("revision"), bool)
        or not isinstance(value.get("revision"), int)
        or not 2 <= value["revision"] <= MAX_REVISION
        or not isinstance(value.get("base_config_sha256"), str)
        or _HASH.fullmatch(value["base_config_sha256"]) is None
        or not isinstance(value.get("previous_config_sha256"), str)
        or _HASH.fullmatch(value["previous_config_sha256"]) is None
    ):
        raise ValueError("session binding revision metadata is invalid")
    return value["revision"]


def revision_snapshot_path(data_dir: Path, revision: int) -> Path:
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= MAX_REVISION:
        raise ValueError("session binding revision is invalid")
    return data_dir / ("runtime-config.json" if revision == 1 else f"runtime-config.session-{revision}.json")


def _regular_path(path: Path, *, required: bool = True) -> None:
    for component in (path, *path.parents):
        if not component.exists() and not component.is_symlink():
            continue
        stat = component.lstat()
        if component.is_symlink() or int(getattr(stat, "st_file_attributes", 0)) & 0x400:
            raise ValueError("session binding path cannot use a reparse point")
    if not path.exists():
        if required:
            raise ValueError("session binding snapshot is missing")
        return
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("session binding snapshot must be a private regular file")


def _read_snapshot(path: Path) -> tuple[bytes, dict[str, Any]]:
    _regular_path(path)
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("session binding snapshot is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != "pmai-v5-runtime-1":
        raise ValueError("session binding snapshot schema is invalid")
    return payload, value


def _stable_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate session coherence and erase only explicitly replaceable locators."""
    value = copy.deepcopy(config)
    value.pop("session_binding", None)
    raw_proofs = value.get("session_observed_evidence")
    bindings = value.get("bindings")
    contacts = value.get("contacts")
    if (
        value.get("identity_mode") != "session_observed_direct"
        or not isinstance(raw_proofs, list) or not raw_proofs
        or not isinstance(bindings, list) or not isinstance(contacts, list)
        or len(raw_proofs) != len(bindings) or len(contacts) != len(bindings)
    ):
        raise ValueError("session binding contact set is invalid")
    if any(
        not isinstance(item, dict)
        or any(type(item.get(field)) is not int or item[field] <= 0
               for field in ("process_id", "window_handle", "process_started_at_100ns"))
        for item in raw_proofs
    ):
        raise ValueError("session binding process locators must be positive integers")
    proofs = [QQSessionObservedDirectIdentity.model_validate(item) for item in raw_proofs]
    by_id = {proof.binding_id: proof for proof in proofs}
    if len(by_id) != len(proofs) or len({proof.selected_row_runtime_id_hash for proof in proofs}) != len(proofs):
        raise ValueError("session binding contacts or row locators are duplicated")
    if any(getattr(proof, field) != getattr(proofs[0], field) for proof in proofs[1:] for field in _SCOPE):
        raise ValueError("session binding evidence is not from one QQ process session")
    pack = value.get("selector_pack")
    if not isinstance(pack, dict) or any(
        (proof.vm_environment_fingerprint, proof.client_version, proof.selector_pack_version)
        != (pack.get("environment_fingerprint"), pack.get("client_version"), pack.get("fixture_suite_version"))
        for proof in proofs
    ):
        raise ValueError("session binding evidence scope does not match selector pack")
    by_binding: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict) or binding.get("binding_id") not in by_id:
            raise ValueError("session binding identity is invalid")
        proof = by_id[binding["binding_id"]]
        if (
            binding["binding_id"] in by_binding
            or binding.get("participant_signature") != proof.participant_signature
            or binding.get("platform_conversation_id") != f"runtime:{proof.selected_row_runtime_id_hash}"
        ):
            raise ValueError("session binding stable identity or locator does not match evidence")
        by_binding[binding["binding_id"]] = binding
    seen_contacts: set[str] = set()
    for contact in contacts:
        binding = contact.get("binding") if isinstance(contact, dict) else None
        if not isinstance(binding, dict) or binding != by_binding.get(binding.get("binding_id")):
            raise ValueError("session binding contact projection does not match binding")
        if contact.get("contact_id") != binding.get("contact_id") or contact["contact_id"] in seen_contacts:
            raise ValueError("session binding contact identity is invalid")
        seen_contacts.add(contact["contact_id"])
    for proof in raw_proofs:
        for field in _LOCATORS:
            proof[field] = "<session-locator>"
    for binding in bindings:
        binding["platform_conversation_id"] = "<session-locator>"
    for contact in contacts:
        contact["binding"]["platform_conversation_id"] = "<session-locator>"
    return value


def validate_session_revision(
    config: dict[str, Any], *, trusted_runtime_root: Path | None = None,
    require_snapshot: bool = True,
) -> SessionRevision | None:
    """Verify every link to the frozen root; no caller-supplied snapshot paths.

    ``require_snapshot=False`` permits only the last revision to be an in-memory
    candidate. Publication uses that precheck, then revalidates its exact saved
    immutable snapshot before switching the canonical file.
    """
    revision = session_revision_number(config)
    if revision == 1:
        return None
    generation = config.get("runtime_generation")
    if not isinstance(generation, dict):
        raise ValueError("session binding revision requires an existing runtime generation")
    try:
        generation_id = str(UUID(str(generation.get("generation_id", ""))))
    except ValueError as exc:
        raise ValueError("session binding generation is invalid") from exc
    if generation.get("generation_id") != generation_id:
        raise ValueError("session binding generation is invalid")
    root = TRUSTED_RUNTIME_ROOT if trusted_runtime_root is None else Path(trusted_runtime_root)
    expected_dir = root / "recovery-generations" / generation_id / "qq-default-account"
    data_dir = Path(str(config.get("data_dir", "")))
    if (not data_dir.is_absolute() or ".." in data_dir.parts
            or os.path.normcase(os.path.abspath(data_dir)) != os.path.normcase(os.path.abspath(expected_dir))):
        raise ValueError("session binding data directory does not match generation")
    base_bytes, base = _read_snapshot(revision_snapshot_path(data_dir, 1))
    base_hash = hashlib.sha256(base_bytes).hexdigest()
    if "session_binding" in base or base.get("runtime_generation") != generation:
        raise ValueError("session binding generation root changed")
    if (base.get("data_dir") != config.get("data_dir")
            or base.get("start_globally_paused") is not True
            or generation.get("enforce_global_pause") is not True):
        raise ValueError("session binding root pause or data scope changed")
    manifest_path = data_dir / "generation-manifest.json"
    _regular_path(manifest_path)
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != generation.get("manifest_sha256"):
        raise ValueError("session binding generation manifest digest mismatch")
    stable_base = _stable_config(base)
    previous_hash = base_hash
    snapshot_path = revision_snapshot_path(data_dir, revision)
    for number in range(2, revision + 1):
        path = revision_snapshot_path(data_dir, number)
        if number == revision and not require_snapshot:
            _regular_path(path, required=False)
            candidate, payload = config, None
        else:
            payload, candidate = _read_snapshot(path)
        if session_revision_number(candidate) != number:
            raise ValueError("session binding revision chain is not consecutive")
        metadata = candidate["session_binding"]
        if metadata["base_config_sha256"] != base_hash or metadata["previous_config_sha256"] != previous_hash:
            raise ValueError("session binding revision hash chain mismatch")
        if _stable_config(candidate) != stable_base:
            raise ValueError("session binding revision changed immutable business or identity fields")
        if number == revision and candidate != config:
            raise ValueError("session binding config does not match immutable snapshot")
        if payload is not None:
            previous_hash = hashlib.sha256(payload).hexdigest()
    return SessionRevision(revision, snapshot_path, base_hash, base)
