"""Build a durable guest-local runtime config from explicit observed contacts."""
from __future__ import annotations

import argparse
import copy
import ctypes
import functools
import getpass
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
from messenger_ai.runtime.config_publication import ConfigPublication, atomic_bytes
from messenger_ai.runtime.session_revision import (
    SCHEMA as SESSION_BINDING_SCHEMA, revision_snapshot_path,
    session_revision_number, validate_session_revision,
)

OUTPUT = Path(r"C:\PMAI\data\runtime-session-1.json")
ACCOUNT_ID = "qq-default-account"
RUNTIME_ROOT = Path(r"C:\PMAI\data\runtime")
DATA_ROOT = RUNTIME_ROOT / ACCOUNT_ID
BOOTSTRAP_ROOT = Path(r"C:\PMAI\data")
BRIDGE_DB = DATA_ROOT / "qq-vm-bridge.sqlite3"
CURSOR_DB = BRIDGE_DB.with_suffix(".cursor.sqlite3")
RUNTIME_DB = DATA_ROOT / "runtime.sqlite3"
SOURCE_RULES = Path(r"C:\PMAI\data\rules.sqlite3")
SELECTOR_PACK = Path(__file__).resolve().parent / "selector-pack-session-1.json"
MAX_CONTACT_INDEX = 9999
ISOLATED_GENERATION_SCHEMA = "pmai-isolated-runtime-generation-v1"
ISOLATED_GENERATION_MANIFEST_SCHEMA = "pmai-isolated-runtime-generation-manifest-v1"


class _RuntimeBuildFence:
    """Hold the runtime ownership mutex while configuration is published."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        self._handle: int | None = None
        self._kernel32: Any | None = None

    def acquire(self) -> None:
        if os.name != "nt":
            return
        identity = "\\".join(
            part for part in (os.environ.get("USERDOMAIN"), getpass.getuser()) if part
        )
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
        name = f"Local\\PersonalMessengerAI.QQRuntime.{digest}"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
        )
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "cannot create QQ runtime build fence")
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise RuntimeError("QQ runtime must be stopped before configuration build")
        self._kernel32 = kernel32
        self._handle = int(handle)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None or self._kernel32 is None:
            return
        self._kernel32.ReleaseMutex(handle)
        self._kernel32.CloseHandle(handle)


def _current_runtime_data_root() -> Path:
    if not OUTPUT.is_file():
        return RUNTIME_ROOT / ACCOUNT_ID
    try:
        config = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("previous runtime config is unreadable") from exc
    if not isinstance(config, dict) or config.get("schema") != "pmai-v5-runtime-1":
        raise RuntimeError("previous runtime config is invalid")
    data_dir = config.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir:
        raise RuntimeError("previous runtime data directory is invalid")
    candidate = Path(data_dir)
    generation = config.get("runtime_generation")
    if generation is None:
        expected = RUNTIME_ROOT / ACCOUNT_ID
    else:
        if not isinstance(generation, dict):
            raise RuntimeError("previous runtime generation is invalid")
        try:
            generation_id = str(UUID(str(generation.get("generation_id", ""))))
        except ValueError as exc:
            raise RuntimeError("previous runtime generation is invalid") from exc
        expected = _isolated_generation_root(generation_id)
    if (
        not candidate.is_absolute()
        or ".." in candidate.parts
        or os.path.normcase(os.path.abspath(str(candidate)))
        != os.path.normcase(os.path.abspath(str(expected)))
    ):
        raise RuntimeError("previous runtime data directory is invalid")
    return candidate


def _previous_runtime_is_paused() -> None:
    """Require the currently published generation to retain a fail-closed fence."""

    database = _current_runtime_data_root() / "runtime.sqlite3"
    if not database.is_file():
        return
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT paused FROM runtime_global_control WHERE singleton=1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("previous runtime pause state is unreadable") from exc
    if row is None or type(row[0]) is not int or row[0] != 1:
        raise RuntimeError("previous runtime must be globally paused before isolated recovery")


def _has_reparse_component(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError as exc:
        raise RuntimeError("isolated generation path is unavailable") from exc
    return path.is_symlink() or bool(attributes & 0x400)


def _validate_isolated_generation_root(path: Path) -> None:
    """Check existing paths without creating a generation during argument validation."""

    existing_chain = tuple(
        component
        for component in reversed((path, *path.parents))
        if component.exists() or component.is_symlink()
    )
    for component in existing_chain:
        if _has_reparse_component(component):
            raise RuntimeError("isolated generation path cannot use a reparse point")
    if not path.exists():
        return
    for entry in path.iterdir():
        if _has_reparse_component(entry):
            raise RuntimeError("isolated generation contents cannot use a reparse point")
        try:
            stat = os.stat(entry, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("isolated generation contents are unavailable") from exc
        if entry.is_file() and int(getattr(stat, "st_nlink", 1)) != 1:
            raise RuntimeError("isolated generation files must not be hard linked")
        if entry.is_dir():
            raise RuntimeError("isolated generation data root must remain flat")


def _prepare_isolated_generation_root(path: Path) -> None:
    """Create and verify one private, flat generation directory before writes."""

    _validate_isolated_generation_root(path)
    path.mkdir(parents=True, exist_ok=True)
    _validate_isolated_generation_root(path)


def _validate_explicit_generation_selection(path: Path, indices: set[int]) -> None:
    """Reject a changed explicit retry scope before any registration writes."""

    manifest_path = path / "generation-manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contacts = manifest["contacts"]
        if not isinstance(contacts, list):
            raise ValueError("invalid manifest contacts")
        binding_ids = [item["binding_id"] for item in contacts]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("isolated generation manifest is invalid") from exc
    if binding_ids != [f"session-contact-{index}" for index in sorted(indices)]:
        raise RuntimeError("isolated generation contact selection changed")


def _validate_frozen_candidate(config_path: Path, expected_sha256: str) -> None:
    """Validate a frozen release candidate before publishing the global pointer."""

    runner = Path(__file__).resolve().with_name("run_vm_runtime.py")
    if not runner.is_file():
        # Source-tree unit tests load this deployment module before release
        # flattening.  Every installable release contains the sibling runner.
        return
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--config",
            str(config_path),
            "--check",
            "--expected-config-sha256",
            expected_sha256,
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("isolated generation candidate validation failed")


def _fence_isolated_build(function):
    @functools.wraps(function)
    def wrapped(argv: list[str] | None = None) -> int:
        arguments = list(sys.argv[1:] if argv is None else argv)
        isolated = any(
            argument == "--isolated-recovery-generation"
            or argument.startswith("--isolated-recovery-generation=")
            for argument in arguments
        )
        fence = _RuntimeBuildFence()
        fence.acquire()
        try:
            ConfigPublication.recover(OUTPUT, runtime_root=RUNTIME_ROOT)
            if isolated:
                _previous_runtime_is_paused()
            return function(argv)
        finally:
            fence.close()

    return wrapped


@dataclass(frozen=True)
class ContactRegistration:
    index: int
    bootstrap: Path
    registration: Path

    @property
    def contact_id(self) -> str:
        return f"session-contact-{self.index}"

    @property
    def conversation_id(self) -> str:
        return f"qq-session-conversation-{self.index}"

    @property
    def binding_id(self) -> str:
        return self.contact_id


CONTACTS = (
    ContactRegistration(
        1,
        BOOTSTRAP_ROOT / "qq-session-observed-bootstrap-1.json",
        DATA_ROOT / "registered-session-scope.json",
    ),
    ContactRegistration(
        2,
        BOOTSTRAP_ROOT / "qq-session-observed-bootstrap-2.json",
        DATA_ROOT / "registered-session-scope-2.json",
    ),
)


def _use_data_root(path: Path) -> None:
    """Retarget all durable paths before contact discovery or validation."""

    global DATA_ROOT, BRIDGE_DB, CURSOR_DB, RUNTIME_DB, CONTACTS
    DATA_ROOT = path
    BRIDGE_DB = DATA_ROOT / "qq-vm-bridge.sqlite3"
    CURSOR_DB = BRIDGE_DB.with_suffix(".cursor.sqlite3")
    RUNTIME_DB = DATA_ROOT / "runtime.sqlite3"
    CONTACTS = tuple(
        ContactRegistration(
            index,
            BOOTSTRAP_ROOT / f"qq-session-observed-bootstrap-{index}.json",
            DATA_ROOT
            / (
                "registered-session-scope.json"
                if index == 1
                else f"registered-session-scope-{index}.json"
            ),
        )
        for index in (1, 2)
    )


def _isolated_generation_root(generation_id: str) -> Path:
    try:
        canonical = str(UUID(generation_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("isolated recovery generation must be a UUID") from exc
    return RUNTIME_ROOT / "recovery-generations" / canonical / ACCOUNT_ID


def _isolated_generation_manifest(
    generation_id: str,
    selected: tuple[ContactRegistration, ...],
    evidences: list[dict[str, Any]],
    signatures: list[str],
    bootstrap_reports: list[dict[str, Any]],
    adoptions: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    entries = []
    for item, evidence, signature, bootstrap in zip(
        selected, evidences, signatures, bootstrap_reports, strict=True
    ):
        try:
            bootstrap_run_id = str(UUID(str(bootstrap["run_id"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"isolated generation bootstrap audit invalid for {item.binding_id}"
            ) from exc
        entries.append({
            "binding_id": item.binding_id,
            "account_id": ACCOUNT_ID,
            "contact_id": item.contact_id,
            "conversation_id": item.conversation_id,
            "platform_conversation_id": (
                "runtime:" + evidence["selected_row_runtime_id_hash"]
            ),
            "bootstrap_run_id": bootstrap_run_id,
            "evidence_sha256": hashlib.sha256(
                json.dumps(
                    evidence, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
            "participant_evidence_sha256": _memory_evidence_hash(signature),
            "initial_adoption_sha256": (
                hashlib.sha256(
                    json.dumps(
                        adoptions[item.index], sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                if item.index in adoptions
                else None
            ),
        })
    return {
        "schema": ISOLATED_GENERATION_MANIFEST_SCHEMA,
        "generation_id": generation_id,
        "mode": "isolated_identity_recovery",
        "account_id": ACCOUNT_ID,
        "contacts": entries,
    }


def _contact_registration(index: int) -> ContactRegistration:
    if isinstance(index, bool) or not 1 <= index <= MAX_CONTACT_INDEX:
        raise ValueError(f"contact index must be between 1 and {MAX_CONTACT_INDEX}")
    legacy = next((item for item in CONTACTS if item.index == index), None)
    if legacy is not None:
        return legacy
    return ContactRegistration(
        index,
        BOOTSTRAP_ROOT / f"qq-session-observed-bootstrap-{index}.json",
        DATA_ROOT / f"registered-session-scope-{index}.json",
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, json.dumps(value, sort_keys=True, indent=2).encode("utf-8"))


def _atomic_bytes(path: Path, value: bytes) -> None:
    atomic_bytes(path, value)


def _validate_rules_database(path: Path, contact_ids: tuple[str, ...]) -> None:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError("RulePack database integrity check failed")
            active = connection.execute(
                "SELECT version,payload_json,report_json FROM m7_rulepack_sources "
                "WHERE status='active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if (
                active is None
                or not isinstance(active[0], str)
                or not active[0]
                or not contact_ids
            ):
                raise RuntimeError("active RulePack is unavailable")
            json.loads(active[1])
            json.loads(active[2])
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("RulePack database integrity check failed") from exc


def _install_rules_database(source: Path, target: Path, contact_ids: tuple[str, ...]) -> None:
    """Create a verified SQLite snapshot and never trust existence after interruption."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _validate_rules_database(target, contact_ids)
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as source_db:
            with closing(sqlite3.connect(temporary)) as target_db:
                source_db.backup(target_db)
        _validate_rules_database(temporary, contact_ids)
        os.replace(temporary, target)
        _validate_rules_database(target, contact_ids)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_successful_bootstrap(
    item: ContactRegistration,
) -> tuple[dict[str, Any], str, str | None, dict[str, Any]]:
    if not item.bootstrap.is_file():
        raise RuntimeError(f"fresh bootstrap required for {item.binding_id}")
    value = json.loads(item.bootstrap.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pmai-qq-session-observed-bootstrap-v1"
        or value.get("succeeded") is not True
    ):
        raise RuntimeError(f"fresh bootstrap required for {item.binding_id}")
    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, dict) or any(
        type(raw_evidence.get(field)) is not int or raw_evidence[field] <= 0
        for field in ("process_id", "window_handle", "process_started_at_100ns")
    ):
        raise RuntimeError(f"invalid bootstrap process locators for {item.binding_id}")
    try:
        proof = QQSessionObservedDirectIdentity.model_validate(raw_evidence)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid bootstrap evidence for {item.binding_id}") from exc
    if proof.binding_id != item.binding_id:
        raise RuntimeError(f"bootstrap binding mismatch for {item.binding_id}")
    signature = proof.participant_signature
    legacy_signature = getattr(proof, "legacy_participant_signature", None)
    if legacy_signature is not None and not isinstance(legacy_signature, str):
        raise RuntimeError(f"invalid legacy participant signature for {item.binding_id}")
    if value.get("participant_signature") != signature:
        raise RuntimeError(f"bootstrap signature mismatch for {item.binding_id}")
    return proof.model_dump(mode="json"), signature, legacy_signature, value


def _adoption_provenance(
    item: ContactRegistration, evidence: dict[str, Any], signature: str,
    bootstrap: dict[str, Any], *, refresh: bool = False,
) -> dict[str, Any]:
    reader = bootstrap.get("reader")
    bubbles = reader.get("bubbles") if isinstance(reader, dict) else None
    if reader is None or reader.get("succeeded") is not True or not isinstance(bubbles, list) or not bubbles:
        raise RuntimeError(f"bootstrap reader evidence missing for {item.binding_id}")
    last = bubbles[-1]
    digest = last.get("text_sha256") if isinstance(last, dict) else None
    bubble_count = reader.get("bubble_count")
    last_ordinal = last.get("ordinal") if isinstance(last, dict) else None
    if (
        not isinstance(last, dict) or last.get("direction") != "inbound"
        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(bubble_count, bool) or not isinstance(bubble_count, int)
        or bubble_count != len(bubbles) or bubble_count < 1
        or isinstance(last_ordinal, bool) or not isinstance(last_ordinal, int)
        or last_ordinal != bubble_count - 1
    ):
        raise RuntimeError(f"bootstrap last inbound proof missing for {item.binding_id}")
    try:
        run_id = str(UUID(str(bootstrap["run_id"])))
        captured_at = datetime.fromisoformat(
            str(bootstrap["completed_at"]).replace("Z", "+00:00")
        )
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"bootstrap adoption audit metadata invalid for {item.binding_id}") from exc
    candidate = _validate_adoption_provenance(item, evidence, signature, {
        "schema": "pmai-qq-bootstrap-last-inbound-adoption-v1",
        "bootstrap_run_id": run_id,
        "captured_at": captured_at.isoformat(),
        "text_sha256": digest,
        "bubble_count": bubble_count,
        "last_ordinal": last_ordinal,
        "last_direction": "inbound",
        "binding_id": item.binding_id,
        "participant_signature": signature,
        "process_id": evidence["process_id"],
        "window_handle": evidence["window_handle"],
        "process_started_at_100ns": evidence["process_started_at_100ns"],
        "selected_row_runtime_id_hash": evidence["selected_row_runtime_id_hash"],
        "client_version": evidence["client_version"],
        "selector_pack_version": evidence["selector_pack_version"],
    })
    if item.registration.exists():
        existing = json.loads(item.registration.read_text(encoding="utf-8"))
        persisted = existing.get("initial_adoption")
        if not isinstance(persisted, dict):
            raise RuntimeError(f"existing registration has no adoption proof for {item.binding_id}")
        allowed_signatures = (signature,)
        if refresh:
            allowed_signatures = _refresh_allowed_adoption_signatures(
                item, existing, signature
            )
        persisted = _validate_adoption_provenance(
            item, evidence, signature, persisted,
            compare_session_scope=not refresh,
            allowed_signatures=allowed_signatures,
        )
        if refresh:
            return persisted
        if persisted != candidate:
            raise RuntimeError(f"adoption evidence changed for {item.binding_id}; explicit repair required")
        return persisted
    if _runtime_conversation_exists(item.conversation_id):
        raise RuntimeError(f"latest-inbound adoption requires a new runtime identity for {item.binding_id}")
    if _bootstrap_consumed(item.conversation_id):
        raise RuntimeError(f"latest-inbound adoption requires an empty cursor for {item.binding_id}")
    return candidate


def _validate_adoption_provenance(
    item: ContactRegistration, evidence: dict[str, Any], signature: str,
    value: dict[str, Any], *, compare_session_scope: bool = True,
    allowed_signatures: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    expected_keys = {
        "schema", "bootstrap_run_id", "captured_at", "text_sha256",
        "bubble_count", "last_ordinal", "last_direction", "binding_id",
        "participant_signature", "process_id", "window_handle",
        "process_started_at_100ns", "selected_row_runtime_id_hash",
        "client_version", "selector_pack_version",
    }
    if set(value) != expected_keys or value.get("schema") != "pmai-qq-bootstrap-last-inbound-adoption-v1":
        raise RuntimeError(f"invalid persisted adoption evidence for {item.binding_id}")
    try:
        run_id = str(UUID(str(value["bootstrap_run_id"])))
        captured_at = datetime.fromisoformat(str(value["captured_at"]).replace("Z", "+00:00"))
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid persisted adoption evidence for {item.binding_id}") from exc
    digest = value.get("text_sha256")
    count, ordinal = value.get("bubble_count"), value.get("last_ordinal")
    accepted_signatures = allowed_signatures or (signature,)
    if (
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(count, bool) or not isinstance(count, int) or count < 1
        or isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != count - 1
        or value.get("last_direction") != "inbound"
        or value.get("binding_id") != item.binding_id
        or value.get("participant_signature") not in accepted_signatures
        or (compare_session_scope and any(value.get(field) != evidence[field] for field in (
            "process_id", "window_handle", "process_started_at_100ns",
            "selected_row_runtime_id_hash", "client_version", "selector_pack_version",
        )))
    ):
        raise RuntimeError(f"invalid persisted adoption evidence for {item.binding_id}")
    return {**value, "bootstrap_run_id": run_id, "captured_at": captured_at.isoformat()}


def _refresh_allowed_adoption_signatures(
    item: ContactRegistration,
    existing: dict[str, Any],
    current_signature: str,
) -> tuple[str, ...]:
    """Accept a persisted adoption across one fully proven signature migration."""

    allowed = {current_signature}
    stored_signature = existing.get("participant_signature")
    if isinstance(stored_signature, str):
        allowed.add(stored_signature)
    adoption = existing.get("initial_adoption")
    adoption_signature = (
        adoption.get("participant_signature")
        if isinstance(adoption, dict)
        else None
    )
    migration = existing.get("session_identity_migration")
    expected_migration_keys = {
        "schema",
        "binding_id",
        "contact_id",
        "account_id",
        "conversation_id",
        "previous_evidence_hash",
        "current_evidence_hash",
    }
    if (
        isinstance(adoption_signature, str)
        and isinstance(stored_signature, str)
        and isinstance(migration, dict)
        and set(migration) == expected_migration_keys
        and migration.get("schema") == "pmai-qq-session-identity-migration-v1"
        and migration.get("binding_id") == item.binding_id
        and migration.get("contact_id") == item.contact_id
        and migration.get("account_id") == ACCOUNT_ID
        and migration.get("conversation_id") == item.conversation_id
        and migration.get("previous_evidence_hash")
        == _memory_evidence_hash(adoption_signature)
        and migration.get("current_evidence_hash")
        == _memory_evidence_hash(stored_signature)
    ):
        allowed.add(adoption_signature)
    return tuple(sorted(allowed))


def _validate_same_process_session(proofs: list[dict[str, Any]]) -> None:
    if len(proofs) < 2:
        return
    session_fields = (
        "process_id",
        "window_handle",
        "process_started_at_100ns",
        "vm_environment_fingerprint",
        "client_version",
        "selector_pack_version",
    )
    first = proofs[0]
    if any(proof[field] != first[field] for proof in proofs[1:] for field in session_fields):
        raise RuntimeError("requested bootstraps are not from the same QQ process session")
    rows = {proof["selected_row_runtime_id_hash"] for proof in proofs}
    if len(rows) != len(proofs):
        raise RuntimeError("requested bootstraps do not identify distinct conversation rows")


def _validate_selector_scope(selector_pack: dict[str, Any], evidence: dict[str, Any]) -> None:
    if (
        selector_pack.get("environment_fingerprint") != evidence["vm_environment_fingerprint"]
        or selector_pack.get("client_version") != evidence["client_version"]
        or selector_pack.get("fixture_suite_version") != evidence["selector_pack_version"]
    ):
        raise RuntimeError("bootstrap selector scope mismatch")


def _binding(
    item: ContactRegistration, evidence: dict[str, Any], signature: str
) -> dict[str, Any]:
    return {
        "hub_conversation_id": item.conversation_id,
        "contact_id": item.contact_id,
        "account_id": ACCOUNT_ID,
        "platform_conversation_id": "runtime:" + evidence["selected_row_runtime_id_hash"],
        "participant_signature": signature,
        "binding_id": item.binding_id,
        "conversation_type": "direct",
        "friendship_verified": False,
        "authorization_scope": "all_direct_including_temporary",
    }


def _registration(
    item: ContactRegistration, evidence: dict[str, Any], signature: str,
    adoption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        "schema": "pmai-qq-session-scope-registration-v1",
        "account_id": ACCOUNT_ID,
        "contact_id": item.contact_id,
        "conversation_id": item.conversation_id,
        "binding_id": item.binding_id,
        "participant_signature": signature,
        "stable_participant_signature": signature,
        "session_evidence": evidence,
    }
    if adoption is not None:
        value["initial_adoption"] = adoption
    return value


def _existing_registration_indices() -> set[int]:
    if not DATA_ROOT.is_dir():
        return set()
    result: set[int] = set()
    for path in DATA_ROOT.glob("registered-session-scope*.json"):
        if path.name == "registered-session-scope.json":
            result.add(1)
            continue
        match = re.fullmatch(r"registered-session-scope-([1-9][0-9]{0,3})\.json", path.name)
        if match is not None:
            result.add(int(match.group(1)))
    return result


def _existing_runtime_indices() -> set[int]:
    if not RUNTIME_DB.is_file():
        return set()
    try:
        with sqlite3.connect(f"file:{RUNTIME_DB}?mode=ro", uri=True) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_conversations'"
            ).fetchone()
            if table is None:
                return set()
            rows = db.execute(
                "SELECT conversation_id,contact_id FROM runtime_conversations WHERE account_id=?",
                (ACCOUNT_ID,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError("durable runtime database cannot be verified") from exc
    result: set[int] = set()
    for conversation_id, contact_id in rows:
        match = re.fullmatch(r"qq-session-conversation-([1-9][0-9]{0,3})", str(conversation_id))
        if match is None or str(contact_id) != f"session-contact-{match.group(1)}":
            raise RuntimeError("durable runtime identity is outside the session contact registry")
        result.add(int(match.group(1)))
    return result


def _registration_identity(value: dict[str, Any]) -> dict[str, Any]:
    """Return only identity-bearing fields; ignore registration audit metadata."""
    try:
        evidence = QQSessionObservedDirectIdentity.model_validate(value["session_evidence"])
        if evidence.binding_id != value["binding_id"]:
            raise ValueError("registration binding does not match its evidence")
        stored_signature = value["participant_signature"]
        accepted_signatures = {
            evidence.participant_signature,
            evidence.legacy_participant_signature,
        }
        if stored_signature not in accepted_signatures:
            raise ValueError("registration signature does not match its evidence")
        return {
            "schema": value["schema"],
            "account_id": value["account_id"],
            "contact_id": value["contact_id"],
            "conversation_id": value["conversation_id"],
            "binding_id": value["binding_id"],
            # Canonicalize legacy registrations before comparing durable
            # identity.  The stored legacy signature is handled separately as
            # migration evidence.
            "participant_signature": evidence.participant_signature,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid durable session registration") from exc


def _runtime_conversation_exists(conversation_id: str) -> bool:
    if not RUNTIME_DB.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{RUNTIME_DB}?mode=ro", uri=True) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_conversations'"
            ).fetchone()
            if table is None:
                return False
            return db.execute(
                "SELECT 1 FROM runtime_conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone() is not None
    except sqlite3.Error as exc:
        raise RuntimeError("durable runtime database cannot be verified") from exc


def _registration_write(
    item: ContactRegistration, registration: dict[str, Any], *, refresh: bool = False,
    legacy_signature: str | None = None,
    allow_header_digest_upgrade: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any] | None] | None:
    if item.registration.is_file():
        existing = json.loads(item.registration.read_text(encoding="utf-8"))
        existing_identity = _registration_identity(existing)
        current_identity = _registration_identity(registration)
        same_durable_coordinates = all(
            existing_identity[key] == current_identity[key]
            for key in existing_identity
            if key != "participant_signature"
        )
        permitted_header_upgrade = (
            refresh
            and allow_header_digest_upgrade
            and same_durable_coordinates
            and not existing.get("stable_participant_signature")
        )
        if existing_identity != current_identity and not permitted_header_upgrade:
            raise RuntimeError(
                f"stable participant identity changed for {item.binding_id}; explicit rebind required"
            )
        stored_signature = existing.get("participant_signature")
        if not isinstance(stored_signature, str):
            raise RuntimeError("invalid durable session registration")
        session_changed = (
            existing.get("session_evidence") != registration.get("session_evidence")
        )
        signature_changed = stored_signature != registration["participant_signature"]
        if not refresh:
            if session_changed or signature_changed:
                raise RuntimeError(
                    f"session evidence changed for {item.binding_id}; explicit refresh required"
                )
            return None
        if not session_changed and not signature_changed:
            return None
        previous_hash = _memory_evidence_hash(stored_signature)
        current_hash = _memory_evidence_hash(registration["participant_signature"])
        updated = dict(existing)
        updated["participant_signature"] = registration["participant_signature"]
        updated["session_evidence"] = registration["session_evidence"]
        updated["stable_participant_signature"] = registration["participant_signature"]
        migration = _session_identity_migration(
            item, existing_identity, current_identity, previous_hash, current_hash
        )
        if migration is not None:
            updated["session_identity_migration"] = migration
        return item.registration, updated, migration
    if item.index == 1 and DATA_ROOT.exists() and any(DATA_ROOT.iterdir()):
        raise RuntimeError(
            "durable account has no contact-1 registration; explicit rebind required"
        )
    if item.index > 1 and _runtime_conversation_exists(item.conversation_id):
        raise RuntimeError(
            f"durable conversation exists without registration for {item.binding_id}; explicit rebind required"
        )
    return item.registration, registration, None


def _memory_evidence_hash(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _session_identity_migration(
    item: ContactRegistration, previous: dict[str, Any], current: dict[str, Any],
    previous_hash: str, current_hash: str,
) -> dict[str, Any] | None:
    if previous_hash == current_hash:
        return None
    return {
        "schema": "pmai-qq-session-identity-migration-v1",
        "binding_id": item.binding_id,
        "contact_id": current["contact_id"],
        "account_id": current["account_id"],
        "conversation_id": item.conversation_id,
        "previous_evidence_hash": previous_hash,
        "current_evidence_hash": current_hash,
    }


def _bootstrap_consumed(conversation_id: str) -> bool:
    """A cursor row, rather than an empty DB file, proves baseline adoption."""
    if not CURSOR_DB.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{CURSOR_DB}?mode=ro", uri=True) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursor_state'"
            ).fetchone()
            if table is None:
                return False
            return db.execute(
                "SELECT 1 FROM cursor_state WHERE conversation_id=?", (conversation_id,)
            ).fetchone() is not None
    except sqlite3.Error as exc:
        raise RuntimeError("message cursor database cannot be verified") from exc


def _parse_visual_labels(values: list[str]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for value in values:
        index_text, separator, label = value.partition("=")
        try:
            index = int(index_text)
        except ValueError as exc:
            raise ValueError("visual label must use INDEX=LABEL") from exc
        if (
            separator != "="
            or not 1 <= index <= MAX_CONTACT_INDEX
            or not label.strip()
            or any(ord(character) < 32 for character in label)
            or len(label) > 96
            or index in labels
        ):
            raise ValueError("visual label must be a unique INDEX=LABEL entry")
        labels[index] = label
    return labels


def _refresh_current_session(*, expected_generation: str, expected_sha256: str) -> int:
    """Publish metadata only; the outer build fence owns the stopped runtime."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise RuntimeError("expected current config digest is invalid")
    try:
        expected_generation = str(UUID(expected_generation))
        previous_bytes = OUTPUT.read_bytes()
        current = json.loads(previous_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("current generation configuration is unavailable") from exc
    if hashlib.sha256(previous_bytes).hexdigest() != expected_sha256:
        raise RuntimeError("current config digest changed before session refresh")
    generation = current.get("runtime_generation") if isinstance(current, dict) else None
    if not isinstance(generation, dict) or generation.get("generation_id") != expected_generation:
        raise RuntimeError("current generation does not match requested session refresh")
    data_root = _current_runtime_data_root()
    _validate_isolated_generation_root(data_root)
    # A session refresh must never become a fresh install of missing business data.
    databases = (
        "runtime.sqlite3", "hub.sqlite3", "memory.sqlite3", "pacing.sqlite3",
        "rules.sqlite3", "authorization.sqlite3", "qq-vm-bridge.sqlite3",
        "qq-vm-bridge.cursor.sqlite3",
    )
    if any(not (data_root / name).is_file() for name in databases):
        raise RuntimeError("session refresh requires all existing business databases")
    _previous_runtime_is_paused()
    previous_revision = session_revision_number(current)
    validated = validate_session_revision(current, trusted_runtime_root=RUNTIME_ROOT)
    previous_snapshot = revision_snapshot_path(data_root, previous_revision)
    if previous_snapshot.read_bytes() != previous_bytes:
        raise RuntimeError("current config does not match its frozen session snapshot")
    base_bytes = (data_root / "runtime-config.json").read_bytes()
    base_hash = validated.base_config_sha256 if validated is not None else hashlib.sha256(base_bytes).hexdigest()
    bindings = current.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise RuntimeError("current generation contact set is invalid")
    _use_data_root(data_root)
    selected = []
    for binding in bindings:
        match = re.fullmatch(r"session-contact-([1-9][0-9]{0,3})", str(binding.get("binding_id", "")))
        if match is None:
            raise RuntimeError("current generation binding is outside the contact registry")
        item = _contact_registration(int(match.group(1)))
        if (binding.get("account_id"), binding.get("contact_id"), binding.get("hub_conversation_id")) != (
            ACCOUNT_ID, item.contact_id, item.conversation_id,
        ):
            raise RuntimeError("current generation business identity is invalid")
        selected.append(item)
    indices = {item.index for item in selected}
    if len(indices) != len(selected) or _existing_registration_indices() != indices or _existing_runtime_indices() != indices:
        raise RuntimeError("session refresh must preserve the complete existing contact set")
    loaded = [_load_successful_bootstrap(item) for item in selected]
    evidences = [entry[0] for entry in loaded]
    _validate_same_process_session(evidences)
    old_proofs = {proof["binding_id"]: proof for proof in current["session_observed_evidence"]}
    pending_registrations = []
    for item, binding, (evidence, signature, _legacy, _report) in zip(selected, bindings, loaded, strict=True):
        if signature != binding.get("participant_signature"):
            raise RuntimeError(f"stable participant identity changed for {item.binding_id}")
        _validate_selector_scope(current["selector_pack"], evidence)
        registered = json.loads(item.registration.read_text(encoding="utf-8"))
        if (
            _registration_identity(registered) != _registration_identity(_registration(item, old_proofs[item.binding_id], signature))
            or registered.get("participant_signature") != signature
            or registered.get("session_evidence") != old_proofs[item.binding_id]
        ):
            raise RuntimeError(f"current registration does not match frozen binding for {item.binding_id}")
        updated = copy.deepcopy(registered)
        updated["session_evidence"] = evidence
        pending_registrations.append((item.registration, updated))
    candidate = copy.deepcopy(current)
    candidate["session_observed_evidence"] = evidences
    locators = {proof["binding_id"]: "runtime:" + proof["selected_row_runtime_id_hash"] for proof in evidences}
    for binding in candidate["bindings"]:
        binding["platform_conversation_id"] = locators[binding["binding_id"]]
    for contact in candidate["contacts"]:
        binding = contact["binding"]
        binding["platform_conversation_id"] = locators[binding["binding_id"]]
    if candidate == current:
        raise RuntimeError("session evidence has not changed")
    candidate["session_binding"] = {
        "schema": SESSION_BINDING_SCHEMA,
        "revision": previous_revision + 1,
        "base_config_sha256": base_hash,
        "previous_config_sha256": expected_sha256,
    }
    revision = validate_session_revision(candidate, trusted_runtime_root=RUNTIME_ROOT, require_snapshot=False)
    assert revision is not None
    snapshot = revision.snapshot_path
    if snapshot.exists():
        raise RuntimeError("next session binding revision already exists")
    candidate_bytes = json.dumps(candidate, sort_keys=True, indent=2).encode("utf-8")
    paths = [path for path, _ in pending_registrations] + [snapshot]
    with ConfigPublication(OUTPUT, paths):
        _atomic_bytes(snapshot, candidate_bytes)
        for path, registration in pending_registrations:
            _atomic_json(path, registration)
        validate_session_revision(candidate, trusted_runtime_root=RUNTIME_ROOT)
        _validate_frozen_candidate(snapshot, hashlib.sha256(candidate_bytes).hexdigest())
        # Recheck the same predecessor after validation; canonical is published last.
        if OUTPUT.read_bytes() != previous_bytes:
            raise RuntimeError("current config changed during session refresh")
        _atomic_bytes(OUTPUT, candidate_bytes)
    return 0


@_fence_isolated_build
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-current-session", action="store_true",
                        help="publish only new QQ session locators within the current paused generation")
    parser.add_argument("--expected-current-generation")
    parser.add_argument("--expected-current-config-sha256")
    parser.add_argument(
        "--contact-index",
        type=int,
        action="append",
        help=(
            "select exactly these contacts for an isolated recovery generation; "
            "repeat for multiple contacts, with no implicit or registered contacts"
        ),
    )
    parser.add_argument(
        "--include-contact-2",
        action="store_true",
        help="include explicitly bootstrapped session-contact-2",
    )
    parser.add_argument(
        "--additional-contact-index",
        type=int,
        action="append",
        default=[],
        help="include one explicitly bootstrapped additional session contact",
    )
    parser.add_argument(
        "--adopt-latest-inbound-index",
        type=int,
        action="append",
        default=[],
        help="for a new additional contact only, adopt its bootstrap-proven final inbound once",
    )
    parser.add_argument(
        "--refresh-session-index",
        type=int,
        action="append",
        default=[],
        help="explicitly replace one contact's current QQ session evidence while preserving durable state",
    )
    parser.add_argument(
        "--migrate-header-digest-index",
        type=int,
        action="append",
        default=[],
        help="one-time explicit upgrade from a legacy header digest for this contact",
    )
    parser.add_argument(
        "--visual-label",
        action="append",
        default=[],
        metavar="INDEX=LABEL",
        help=(
            "enable visual row selection with one exact local display label "
            "for every selected contact"
        ),
    )
    parser.add_argument(
        "--isolated-recovery-generation",
        help=(
            "write a fresh, globally paused runtime generation under a UUID-scoped "
            "data root instead of mutating the current durable account"
        ),
    )
    args = parser.parse_args(argv)
    if args.refresh_current_session:
        if (not args.expected_current_generation or not args.expected_current_config_sha256
                or args.isolated_recovery_generation or args.contact_index
                or args.include_contact_2 or args.additional_contact_index
                or args.adopt_latest_inbound_index or args.refresh_session_index
                or args.migrate_header_digest_index or args.visual_label):
            parser.error("current session refresh requires generation/digest only; contact, adoption and migration options are forbidden")
        return _refresh_current_session(
            expected_generation=args.expected_current_generation,
            expected_sha256=args.expected_current_config_sha256,
        )
    if args.expected_current_generation or args.expected_current_config_sha256:
        parser.error("expected current generation/digest requires --refresh-current-session")
    isolated_generation: str | None = None
    if args.isolated_recovery_generation is not None:
        try:
            isolated_generation = str(UUID(args.isolated_recovery_generation))
            isolated_root = _isolated_generation_root(isolated_generation)
        except ValueError as exc:
            parser.error(str(exc))
    explicit = set(args.contact_index or ())
    additional = set(args.additional_contact_index)
    adopt = set(args.adopt_latest_inbound_index)
    refresh = set(args.refresh_session_index)
    header_upgrade = set(args.migrate_header_digest_index)
    for index in explicit | additional | adopt | refresh | header_upgrade:
        if not 1 <= index <= MAX_CONTACT_INDEX:
            parser.error(f"contact index must be between 1 and {MAX_CONTACT_INDEX}")
    if explicit and isolated_generation is None:
        parser.error("contact-index requires an isolated recovery generation")
    if explicit and (args.include_contact_2 or additional):
        parser.error("contact-index cannot be combined with compatibility contact options")
    if isolated_generation is None and adopt - additional:
        parser.error("latest inbound adoption requires the same explicit additional contact index")
    if additional & {1, 2}:
        parser.error("contact 1/2 use the existing compatibility options")
    if isolated_generation is not None:
        # Retarget in memory for legacy discovery, but do not create the
        # generation directory until every argument scope has been validated.
        _validate_isolated_generation_root(isolated_root)
        _use_data_root(isolated_root)
    if explicit:
        selected_indices = explicit
    else:
        selected_indices = {1} | additional | _existing_registration_indices() | _existing_runtime_indices()
        if args.include_contact_2:
            selected_indices.add(2)
    if isolated_generation is not None and adopt - selected_indices:
        parser.error("latest inbound adoption must reference a selected contact index")
    if refresh - selected_indices:
        parser.error("session refresh requires an existing or explicitly included contact index")
    if refresh and refresh != selected_indices:
        parser.error("QQ process session refresh must include every selected contact index")
    if header_upgrade - refresh:
        parser.error("header digest migration requires the same explicit session refresh index")
    selected = tuple(_contact_registration(index) for index in sorted(selected_indices))
    try:
        visual_labels = _parse_visual_labels(args.visual_label)
    except ValueError as exc:
        parser.error(str(exc))
    if visual_labels and set(visual_labels) != selected_indices:
        parser.error("visual labels must be provided for every selected contact")
    if isolated_generation is not None:
        if explicit:
            _validate_explicit_generation_selection(isolated_root, selected_indices)
        _prepare_isolated_generation_root(isolated_root)

    loaded = [_load_successful_bootstrap(item) for item in selected]
    evidences = [value[0] for value in loaded]
    signatures = [value[1] for value in loaded]
    legacy_signatures = [value[2] for value in loaded]
    bootstrap_reports = [value[3] for value in loaded]
    _validate_same_process_session(evidences)

    if not SELECTOR_PACK.is_file():
        raise RuntimeError("selector pack missing")
    selector_pack = json.loads(SELECTOR_PACK.read_text(encoding="utf-8"))
    for evidence in evidences:
        _validate_selector_scope(selector_pack, evidence)
    if not SOURCE_RULES.is_file():
        raise RuntimeError("active RulePack database missing")

    bindings = [
        _binding(item, evidence, signature)
        for item, evidence, signature in zip(selected, evidences, signatures, strict=True)
    ]
    adoptions: dict[int, dict[str, Any]] = {}
    for item, evidence, signature, bootstrap_report in zip(
        selected, evidences, signatures, bootstrap_reports, strict=True
    ):
        if item.index in adopt:
            adoptions[item.index] = _adoption_provenance(
                item, evidence, signature, bootstrap_report,
                refresh=item.index in refresh,
            )
        elif item.registration.is_file():
            existing_registration = json.loads(item.registration.read_text(encoding="utf-8"))
            persisted = existing_registration.get("initial_adoption")
            if isinstance(persisted, dict):
                allowed_signatures = (signature,)
                if item.index in refresh:
                    allowed_signatures = _refresh_allowed_adoption_signatures(
                        item, existing_registration, signature
                    )
                adoptions[item.index] = _validate_adoption_provenance(
                    item, evidence, signature, persisted,
                    compare_session_scope=item.index not in refresh,
                    allowed_signatures=allowed_signatures,
                )
    bootstrap_last_inbound_once = []
    bootstrap_last_inbound_provenance: dict[str, dict[str, Any]] = {}
    for item in selected:
        if isolated_generation is None and _bootstrap_consumed(item.conversation_id):
            continue
        if item.index in adoptions:
            bootstrap_last_inbound_once.append(item.conversation_id)
            bootstrap_last_inbound_provenance[item.conversation_id] = adoptions[item.index]
        elif isolated_generation is None and item.index in {1, 2}:
            # Preserve the original operator-approved compatibility behavior.
            bootstrap_last_inbound_once.append(item.conversation_id)
    pending_registrations = []
    session_identity_migrations = []
    for item, evidence, signature, legacy_signature in zip(
        selected, evidences, signatures, legacy_signatures, strict=True
    ):
        pending = _registration_write(
            item, _registration(item, evidence, signature, adoptions.get(item.index)),
            refresh=item.index in refresh,
            legacy_signature=legacy_signature,
            allow_header_digest_upgrade=item.index in header_upgrade,
        )
        if pending is not None:
            pending_registrations.append(pending)
    staged_registrations = {path: value for path, value, _ in pending_registrations}
    for path, registration, migration in pending_registrations:
        if migration is not None:
            session_identity_migrations.append(migration)
    # A crash/retry may rebuild the config after the registration was written
    # but before the runtime migrated memory.sqlite3.  Keep the narrow migration
    # envelope in the registration so that retry remains deterministic.
    by_binding = {item["binding_id"]: item for item in session_identity_migrations}
    current_signature_by_binding = {
        item.binding_id: signature
        for item, signature in zip(selected, signatures, strict=True)
    }
    for item in selected:
        registered = staged_registrations.get(item.registration)
        if registered is None:
            if not item.registration.is_file():
                continue
            registered = json.loads(item.registration.read_text(encoding="utf-8"))
        persisted_m = registered.get("session_identity_migration")
        if not isinstance(persisted_m, dict):
            continue
        expected_keys = {
            "schema", "binding_id", "contact_id", "account_id",
            "conversation_id", "previous_evidence_hash", "current_evidence_hash",
        }
        if (
            set(persisted_m) != expected_keys
            or persisted_m.get("schema") != "pmai-qq-session-identity-migration-v1"
            or persisted_m.get("binding_id") != item.binding_id
            or persisted_m.get("contact_id") != item.contact_id
            or persisted_m.get("account_id") != ACCOUNT_ID
            or persisted_m.get("conversation_id") != item.conversation_id
            or re.fullmatch(
                r"[0-9a-f]{64}", str(persisted_m.get("previous_evidence_hash", ""))
            ) is None
            or persisted_m.get("current_evidence_hash")
            != _memory_evidence_hash(current_signature_by_binding[item.binding_id])
        ):
            raise RuntimeError(f"invalid persisted identity migration for {item.binding_id}")
        by_binding[item.binding_id] = persisted_m
    session_identity_migrations = list(by_binding.values())

    generation_manifest: dict[str, Any] | None = None
    generation_manifest_sha256: str | None = None
    if isolated_generation is not None:
        generation_manifest = _isolated_generation_manifest(
            isolated_generation,
            selected,
            evidences,
            signatures,
            bootstrap_reports,
            adoptions,
        )
        manifest_path = DATA_ROOT / "generation-manifest.json"
        manifest_bytes = json.dumps(generation_manifest, sort_keys=True, indent=2).encode("utf-8")
        if manifest_path.is_file():
            try:
                existing_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("isolated generation manifest is invalid") from exc
            if existing_manifest != generation_manifest:
                raise RuntimeError("isolated generation manifest changed")
            manifest_bytes = manifest_path.read_bytes()
        generation_manifest_sha256 = hashlib.sha256(
            manifest_bytes
        ).hexdigest()

    target_rules = DATA_ROOT / "rules.sqlite3"
    contact_ids = tuple(item.contact_id for item in selected)
    _validate_rules_database(target_rules if target_rules.exists() else SOURCE_RULES, contact_ids)

    config = {
        "schema": "pmai-v5-runtime-1",
        "data_dir": str(DATA_ROOT),
        "secret_vault": r"C:\PMAI\secrets",
        "model": "deepseek-v4-flash",
        # Cold QQ Chromium/UIA snapshots on the recovery VM have exceeded 45
        # seconds.  Keep a bounded watchdog, but leave enough time for the
        # worker's pre-write safety reserve and post-write verification.
        "worker_timeout_seconds": 90,
        "prepare_write_reserve_seconds": 20,
        "content_policy_checks_enabled": False,
        "identity_mode": "session_observed_direct",
        "session_observed_evidence": evidences,
        "bootstrap_last_inbound_once": bootstrap_last_inbound_once,
        "bootstrap_last_inbound_provenance": bootstrap_last_inbound_provenance,
        "contacts": [
            {"contact_id": binding["contact_id"], "rulepack_status": "active", "binding": binding}
            for binding in bindings
        ],
        "bindings": bindings,
        "selector_pack": selector_pack,
        "capability": {
            "capability_version": "qq-supervised-session-trial-v1",
            "environment_fingerprint": selector_pack["environment_fingerprint"],
            "send_background": "unsupported",
            "verify_background": "unsupported",
            "send_guest_foreground": "supported",
            "verify_guest_foreground": "supported",
            "healthy": True,
            "client_version": selector_pack["client_version"],
            "execution_mode": "guest_foreground",
            "binding_revision": 1,
        },
    }
    if isolated_generation is not None:
        if generation_manifest is None or generation_manifest_sha256 is None:
            raise RuntimeError("isolated generation manifest missing")
        config["runtime_generation"] = {
            "schema": ISOLATED_GENERATION_SCHEMA,
            "generation_id": isolated_generation,
            "mode": "isolated_identity_recovery",
            "enforce_global_pause": True,
            "manifest_sha256": generation_manifest_sha256,
        }
        config["start_globally_paused"] = True
    if session_identity_migrations:
        config["session_identity_migrations"] = session_identity_migrations
    if visual_labels:
        config["visual_selection"] = {
            "model": "deepseek-v4-flash-vision-exp",
            "labels": {
                _contact_registration(index).binding_id: label
                for index, label in sorted(visual_labels.items())
            },
            "min_confidence": 0.98,
            "timeout_seconds": 8,
        }
    if isolated_generation is not None:
        generation_config = DATA_ROOT / "runtime-config.json"
        if generation_config.is_file():
            try:
                existing_config = json.loads(
                    generation_config.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("isolated generation runtime config is invalid") from exc
            if existing_config != config:
                raise RuntimeError("isolated generation runtime config changed")

    publication_paths = [path for path, _, _ in pending_registrations] + [target_rules]
    if isolated_generation is not None:
        previous_config = DATA_ROOT / "previous-runtime-config.json"
        publication_paths += [manifest_path, generation_config, previous_config]
    with ConfigPublication(OUTPUT, publication_paths):
        _install_rules_database(SOURCE_RULES, target_rules, contact_ids)
        for path, registration, _ in pending_registrations:
            _atomic_json(path, registration)
        if isolated_generation is not None:
            if not manifest_path.exists():
                _atomic_bytes(manifest_path, manifest_bytes)
            if not generation_config.exists():
                _atomic_json(generation_config, config)
            candidate_bytes = generation_config.read_bytes()
            _validate_frozen_candidate(generation_config, hashlib.sha256(candidate_bytes).hexdigest())
            if not previous_config.exists() and OUTPUT.is_file():
                current_bytes = OUTPUT.read_bytes()
                if current_bytes != candidate_bytes:
                    _atomic_bytes(previous_config, current_bytes)
            _atomic_bytes(OUTPUT, candidate_bytes)
        else:
            _atomic_json(OUTPUT, config)
            _validate_frozen_candidate(OUTPUT, hashlib.sha256(OUTPUT.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
