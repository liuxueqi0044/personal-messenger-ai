"""Production guest entry point; ``--check`` has no worker/API side effects."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from messenger_ai.adapters.qq.models import (
    QQIdentityBinding,
    QQSelectorPack,
    QQSessionObservedDirectIdentity,
)
from messenger_ai.adapters.qq.vm_driver import (
    QQVMDriverBridge,
    QQVMWorkerProcess,
    VisualSelectionConfig,
    WorkerStatus,
)
from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack
from messenger_ai.domain import Platform
from messenger_ai.llm.deepseek import DeepSeekResponsesProvider
from messenger_ai.memory import Contact, IdentityBinding
from messenger_ai.observability import WindowsDPAPISecretStore
from messenger_ai.policy import CapabilitySnapshot
from messenger_ai.runtime.assembly import assemble_runtime
from messenger_ai.runtime.state import RuntimeState, VerifiedSendStorePaths
from messenger_ai.runtime.webui_projection import RuntimeWebUIProjection
from messenger_ai.webui import LiveHubFacade, create_app

GRACEFUL_STOP_TIMEOUT_SECONDS = 10.0


CONTROL_REQUEST_PATH = Path(r"C:\PMAI\data\qq-session-runtime-control-request.json")
CONTROL_RESULT_PATH = Path(r"C:\PMAI\data\qq-session-runtime-control-result.json")
WORKER_STATUS_PATH = Path(r"C:\PMAI\data\qq-session-runtime-worker-status.json")
CONTROL_REQUEST_SCHEMA = "pmai-qq-runtime-control-request-v1"
CONTROL_RESULT_SCHEMA = "pmai-qq-runtime-control-result-v1"
CONTROL_ACTIONS = {"pause", "resume", "graceful_stop"}


class QQRuntimeInstanceOwner:
    """Process-lifetime ownership of the current user's QQ automation lane."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, *, user_scope: str | None = None) -> None:
        if os.name != "nt":
            raise RuntimeError("QQ runtime instance ownership requires Windows")
        identity = user_scope or "\\".join(
            part for part in (os.environ.get("USERDOMAIN"), getpass.getuser()) if part
        )
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
        self.name = f"Local\\PersonalMessengerAI.QQRuntime.{digest}"
        self._handle: int | None = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
        )
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        self._kernel32.ReleaseMutex.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def acquire(self) -> None:
        if self._handle is not None:
            return
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "cannot create QQ runtime ownership mutex")
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            raise RuntimeError("QQ runtime is already running for this Windows user")
        self._handle = int(handle)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        self._kernel32.ReleaseMutex(handle)
        self._kernel32.CloseHandle(handle)


def load_config(
    path: str | Path, *, expected_sha256: str | None = None
) -> dict[str, Any]:
    try:
        payload = Path(path).read_bytes()
        if expected_sha256 is not None:
            expected = expected_sha256.casefold()
            if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                raise ValueError("expected runtime config digest is invalid")
            actual = hashlib.sha256(payload).hexdigest()
            if not hmac.compare_digest(actual, expected):
                raise ValueError("runtime config digest mismatch")
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read runtime config: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "pmai-v5-runtime-1":
        raise ValueError("unsupported or missing runtime config schema")
    return value


SESSION_IDENTITY_MIGRATION_SCHEMA = "pmai-qq-session-identity-migration-v1"
ISOLATED_GENERATION_SCHEMA = "pmai-isolated-runtime-generation-v1"
TRUSTED_RUNTIME_ROOT = Path(r"C:\PMAI\data\runtime")
_ISOLATED_RUNTIME_DATABASES = (
    "authorization.sqlite3",
    "hub.sqlite3",
    "memory.sqlite3",
    "pacing.sqlite3",
    "qq-vm-bridge.cursor.sqlite3",
    "qq-vm-bridge.sqlite3",
    "rules.sqlite3",
    "runtime.sqlite3",
)
_SESSION_IDENTITY_MIGRATION_KEYS = {
    "schema",
    "binding_id",
    "contact_id",
    "account_id",
    "conversation_id",
    "previous_evidence_hash",
    "current_evidence_hash",
}


def _signature_evidence_hash(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _has_reparse_component(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError as exc:
        raise ValueError("isolated runtime generation path is unavailable") from exc
    return path.is_symlink() or bool(attributes & 0x400)


def _existing_path_chain(path: Path) -> tuple[Path, ...]:
    chain = tuple(reversed((path, *path.parents)))
    return tuple(component for component in chain if component.exists() or component.is_symlink())


def _validate_flat_generation_storage(data_dir: Path) -> None:
    """Reject aliases for every file that the isolated runtime can mutate."""

    for component in _existing_path_chain(TRUSTED_RUNTIME_ROOT):
        if _has_reparse_component(component):
            raise ValueError("isolated runtime trusted path cannot use a reparse point")
    for name in _ISOLATED_RUNTIME_DATABASES:
        database = data_dir / name
        for candidate in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), Path(str(database) + "-journal")):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            if _has_reparse_component(candidate):
                raise ValueError("isolated runtime state cannot use a reparse point")
            try:
                stat = os.stat(candidate, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("isolated runtime state is unavailable") from exc
            if not candidate.is_file() or int(getattr(stat, "st_nlink", 1)) != 1:
                raise ValueError("isolated runtime state must be a private regular file")


def _validated_generation_data_dir(config: dict[str, Any], generation_id: str) -> Path:
    raw = config.get("data_dir")
    if not isinstance(raw, str) or not raw:
        raise ValueError("isolated runtime generation data directory is invalid")
    data_dir = Path(raw)
    expected = (
        TRUSTED_RUNTIME_ROOT
        / "recovery-generations"
        / generation_id
        / "qq-default-account"
    )
    if (
        not data_dir.is_absolute()
        or ".." in data_dir.parts
        or os.path.normcase(os.path.abspath(str(data_dir)))
        != os.path.normcase(os.path.abspath(str(expected)))
    ):
        raise ValueError("isolated runtime generation data directory is invalid")
    for component in (
        TRUSTED_RUNTIME_ROOT,
        TRUSTED_RUNTIME_ROOT / "recovery-generations",
        TRUSTED_RUNTIME_ROOT / "recovery-generations" / generation_id,
        expected,
        expected / "generation-manifest.json",
        expected / "runtime-config.json",
    ):
        if _has_reparse_component(component):
            raise ValueError("isolated runtime generation path cannot use a reparse point")
    if data_dir.resolve(strict=False) != expected.resolve(strict=False):
        raise ValueError("isolated runtime generation path alias is invalid")
    _validate_flat_generation_storage(data_dir)
    return data_dir


def _validated_runtime_generation(
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    raw = config.get("runtime_generation")
    start_paused = config.get("start_globally_paused", False)
    if not isinstance(start_paused, bool):
        raise ValueError("start_globally_paused must be a boolean")
    if raw is None:
        if start_paused:
            raise ValueError("start_globally_paused requires an isolated runtime generation")
        return None
    expected_keys = {
        "schema", "generation_id", "mode", "enforce_global_pause",
        "manifest_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise ValueError("runtime generation fields are invalid")
    try:
        generation_id = str(UUID(str(raw.get("generation_id", ""))))
    except ValueError as exc:
        raise ValueError("runtime generation id is invalid") from exc
    if (
        raw.get("schema") != ISOLATED_GENERATION_SCHEMA
        or raw.get("generation_id") != generation_id
        or raw.get("mode") != "isolated_identity_recovery"
        or raw.get("enforce_global_pause") is not True
        or re.fullmatch(r"[0-9a-f]{64}", str(raw.get("manifest_sha256", "")))
        is None
        or start_paused is not True
    ):
        raise ValueError("isolated runtime generation is invalid")
    data_dir = _validated_generation_data_dir(config, generation_id)
    manifest_path = data_dir / "generation-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("isolated runtime generation manifest is unavailable") from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != raw["manifest_sha256"]:
        raise ValueError("isolated runtime generation manifest digest mismatch")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {
            "schema", "generation_id", "mode", "account_id", "contacts"
        }
        or manifest.get("schema")
        != "pmai-isolated-runtime-generation-manifest-v1"
        or manifest.get("generation_id") != generation_id
        or manifest.get("mode") != "isolated_identity_recovery"
        or manifest.get("account_id") != "qq-default-account"
        or not isinstance(manifest.get("contacts"), list)
        or not manifest["contacts"]
    ):
        raise ValueError("isolated runtime generation manifest is invalid")
    return dict(raw), manifest


def _validate_generation_contacts(
    generation: tuple[dict[str, Any], dict[str, Any]] | None,
    config: dict[str, Any],
    bindings: tuple[QQIdentityBinding, ...],
    evidence: tuple[QQSessionObservedDirectIdentity, ...],
) -> None:
    if generation is None:
        return
    manifest = generation[1]
    entries = manifest["contacts"]
    expected_keys = {
        "binding_id", "account_id", "contact_id", "conversation_id",
        "platform_conversation_id", "bootstrap_run_id", "evidence_sha256",
        "participant_evidence_sha256", "initial_adoption_sha256",
    }
    by_binding: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise ValueError("isolated runtime generation contact manifest is invalid")
        try:
            bootstrap_run_id = str(UUID(str(entry.get("bootstrap_run_id", ""))))
        except ValueError as exc:
            raise ValueError(
                "isolated runtime generation contact manifest is invalid"
            ) from exc
        binding_id = entry.get("binding_id")
        if (
            not isinstance(binding_id, str)
            or not binding_id
            or binding_id in by_binding
            or entry.get("bootstrap_run_id") != bootstrap_run_id
            or re.fullmatch(r"[0-9a-f]{64}", str(entry.get("evidence_sha256", "")))
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(entry.get("participant_evidence_sha256", "")),
            )
            is None
            or (
                entry.get("initial_adoption_sha256") is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(entry.get("initial_adoption_sha256"))
                ) is None
            )
        ):
            raise ValueError("isolated runtime generation contact manifest is invalid")
        by_binding[binding_id] = entry
    binding_by_id = {item.binding_id: item for item in bindings}
    if set(by_binding) != set(binding_by_id) or set(by_binding) != {
        item.binding_id for item in evidence
    }:
        raise ValueError("isolated runtime generation contacts do not match evidence")
    bootstrap = set(config.get("bootstrap_last_inbound_once", []))
    provenance = config.get("bootstrap_last_inbound_provenance", {})
    for proof in evidence:
        entry = by_binding[proof.binding_id]
        binding = binding_by_id[proof.binding_id]
        evidence_hash = hashlib.sha256(
            json.dumps(
                proof.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            entry["evidence_sha256"] != evidence_hash
            or entry["participant_evidence_sha256"]
            != _signature_evidence_hash(proof.participant_signature)
            or entry["account_id"] != binding.account_id
            or entry["contact_id"] != binding.contact_id
            or entry["conversation_id"] != binding.hub_conversation_id
            or entry["platform_conversation_id"]
            != binding.platform_conversation_id
        ):
            raise ValueError("isolated runtime generation evidence digest mismatch")
        conversation_id = binding.hub_conversation_id
        adoption_hash = entry["initial_adoption_sha256"]
        adoption = provenance.get(conversation_id)
        if adoption_hash is None:
            if conversation_id in bootstrap or adoption is not None:
                raise ValueError("isolated runtime generation adoption scope mismatch")
            continue
        if conversation_id not in bootstrap or not isinstance(adoption, dict):
            raise ValueError("isolated runtime generation adoption scope mismatch")
        actual_hash = hashlib.sha256(
            json.dumps(adoption, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            adoption_hash != actual_hash
            or adoption.get("bootstrap_run_id") != entry["bootstrap_run_id"]
        ):
            raise ValueError("isolated runtime generation adoption digest mismatch")


def _validated_session_identity_migrations(
    config: dict[str, Any],
    bindings: tuple[QQIdentityBinding, ...],
    evidence: tuple[QQSessionObservedDirectIdentity, ...],
) -> dict[str, dict[str, str]]:
    """Validate the one-time legacy-signature migration envelope.

    The envelope can authorize only an evidence-hash change for the same durable
    account/contact/conversation/binding tuple.  It never authorizes a rebind.
    """

    raw = config.get("session_identity_migrations", [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("session_identity_migrations must be a list of objects")
    if raw and config.get("identity_mode") != "session_observed_direct":
        raise ValueError("session identity migrations require session_observed_direct")

    binding_by_id = {item.binding_id: item for item in bindings}
    evidence_by_id = {item.binding_id: item for item in evidence}
    migrations: dict[str, dict[str, str]] = {}
    seen_bindings: set[str] = set()
    for item in raw:
        if set(item) != _SESSION_IDENTITY_MIGRATION_KEYS:
            raise ValueError("session identity migration fields are invalid")
        if item.get("schema") != SESSION_IDENTITY_MIGRATION_SCHEMA:
            raise ValueError("session identity migration schema is invalid")
        values = {key: item.get(key) for key in _SESSION_IDENTITY_MIGRATION_KEYS - {"schema"}}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("session identity migration values must be non-empty strings")
        binding_id = str(item["binding_id"])
        binding = binding_by_id.get(binding_id)
        proof = evidence_by_id.get(binding_id)
        if binding is None or proof is None:
            raise ValueError("session identity migration binding is not configured")
        if binding_id in seen_bindings or binding.hub_conversation_id in migrations:
            raise ValueError("session identity migrations must be unique")
        seen_bindings.add(binding_id)
        if (
            item["contact_id"] != binding.contact_id
            or item["account_id"] != binding.account_id
            or item["conversation_id"] != binding.hub_conversation_id
        ):
            raise ValueError("session identity migration cannot change durable identity")
        previous_hash = str(item["previous_evidence_hash"])
        current_hash = str(item["current_evidence_hash"])
        if (
            re.fullmatch(r"[0-9a-f]{64}", previous_hash) is None
            or re.fullmatch(r"[0-9a-f]{64}", current_hash) is None
            or previous_hash == current_hash
        ):
            raise ValueError("session identity migration evidence hashes are invalid")
        if current_hash != _signature_evidence_hash(proof.participant_signature):
            raise ValueError("session identity migration current evidence does not match proof")
        migrations[binding.hub_conversation_id] = {
            key: str(value) for key, value in item.items()
        }
    return migrations


def validate_config(config: dict[str, Any], *, api_key: str | None) -> tuple[QQSelectorPack, tuple[QQIdentityBinding, ...], tuple[QQSessionObservedDirectIdentity, ...]]:
    generation = _validated_runtime_generation(config)
    contacts = config.get("contacts")
    if not config.get("data_dir"):
        raise ValueError("data_dir is required")
    if not isinstance(contacts, list) or not contacts:
        raise ValueError("at least one explicitly configured contact is required")
    if not isinstance(config.get("content_policy_checks_enabled", True), bool):
        raise ValueError("content_policy_checks_enabled must be a boolean")
    if any(item.get("rulepack_status") != "active" for item in contacts):
        raise ValueError("every configured contact must reference an already active RulePack")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required; configure it in the guest secret store")
    raw_pack = config.get("selector_pack")
    if not isinstance(raw_pack, dict):
        raise ValueError("selector_pack is required")
    pack = QQSelectorPack.model_validate(raw_pack)
    validate_guest_selector_pack(pack)
    raw_bindings = config.get("bindings") or [item.get("binding") for item in contacts]
    if not isinstance(raw_bindings, list) or len(raw_bindings) != len(contacts) or any(not isinstance(item, dict) for item in raw_bindings):
        raise ValueError("one explicit verified binding is required for every contact")
    bindings = tuple(QQIdentityBinding.model_validate(item) for item in raw_bindings)
    for binding in bindings:
        if binding.conversation_type != "direct":
            raise ValueError(
                f"production QQ runtime requires direct conversation type for {binding.contact_id}"
            )
        if binding.authorization_scope != "all_direct_including_temporary":
            raise ValueError(
                f"all-direct authorization scope is required for {binding.contact_id}"
            )
        if binding.participant_signature.startswith("uncertified:"):
            raise ValueError(
                f"uncertified participant signature for {binding.contact_id}"
            )
    if len({item.hub_conversation_id for item in bindings}) != len(bindings):
        raise ValueError("binding conversation ids must be unique")
    if len({item.binding_id for item in bindings}) != len(bindings):
        raise ValueError("binding ids must be unique")
    configured_ids = {str(item.get("contact_id", "")) for item in contacts}
    if {item.contact_id for item in bindings} != configured_ids:
        raise ValueError("contacts and bindings must have the same contact ids")
    mode = config.get("identity_mode")
    evidence: tuple[QQSessionObservedDirectIdentity, ...] = ()
    if mode == "session_observed_direct":
        raw = config.get("session_observed_evidence")
        if not isinstance(raw, list) or len(raw) != len(bindings):
            raise ValueError("one session observed identity is required for every binding")
        evidence = tuple(QQSessionObservedDirectIdentity.model_validate(item) for item in raw)
        if len({item.binding_id for item in evidence}) != len(evidence):
            raise ValueError("session observed evidence binding ids must be unique")
        if {item.binding_id for item in evidence} != {item.binding_id for item in bindings}:
            raise ValueError("session observed evidence must match bindings one-to-one")
        by_binding = {item.binding_id: item for item in evidence}
        for binding in bindings:
            item = by_binding[binding.binding_id]
            if item.vm_environment_fingerprint != pack.environment_fingerprint or item.client_version != pack.client_version or item.selector_pack_version != pack.fixture_suite_version:
                raise ValueError("session observed evidence scope does not match selector pack")
            if binding.participant_signature != item.participant_signature:
                raise ValueError("session observed participant signature does not match binding")
            if binding.platform_conversation_id != f"runtime:{item.selected_row_runtime_id_hash}":
                raise ValueError("session observed runtime locator does not match binding")
    elif mode is not None:
        raise ValueError("unsupported identity_mode")
    _validated_worker_timing(config)
    _validated_visual_selection(config, bindings)
    migrations = _validated_session_identity_migrations(config, bindings, evidence)
    bootstrap = config.get("bootstrap_last_inbound_once", [])
    if not isinstance(bootstrap, list) or any(not isinstance(item, str) for item in bootstrap) or len(set(bootstrap)) != len(bootstrap):
        raise ValueError("bootstrap_last_inbound_once must be unique conversation ids")
    if bootstrap and mode != "session_observed_direct":
        raise ValueError("bootstrap_last_inbound_once requires session_observed_direct")
    if not set(bootstrap).issubset({item.hub_conversation_id for item in bindings}):
        raise ValueError("bootstrap_last_inbound_once must reference configured bindings")
    raw_provenance = config.get("bootstrap_last_inbound_provenance", {})
    if not isinstance(raw_provenance, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in raw_provenance.items()
    ):
        raise ValueError("bootstrap_last_inbound_provenance must be an object")
    if not set(raw_provenance).issubset(set(bootstrap)):
        raise ValueError("bootstrap adoption provenance must target configured bootstrap")
    binding_by_conversation = {item.hub_conversation_id: item for item in bindings}
    evidence_by_binding = {item.binding_id: item for item in evidence}
    adoption_keys = {
        "schema", "bootstrap_run_id", "captured_at", "text_sha256",
        "bubble_count", "last_ordinal", "last_direction", "binding_id",
        "participant_signature", "process_id", "window_handle",
        "process_started_at_100ns", "selected_row_runtime_id_hash",
        "client_version", "selector_pack_version",
    }
    for conversation_id, provenance in raw_provenance.items():
        binding = binding_by_conversation[conversation_id]
        proof = evidence_by_binding.get(binding.binding_id)
        migration = migrations.get(conversation_id)
        count, ordinal = provenance.get("bubble_count"), provenance.get("last_ordinal")
        try:
            UUID(str(provenance.get("bootstrap_run_id")))
            captured_at = datetime.fromisoformat(
                str(provenance.get("captured_at", "")).replace("Z", "+00:00")
            )
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("bootstrap adoption audit metadata invalid") from exc
        if (
            set(provenance) != adoption_keys
            or provenance.get("schema") != "pmai-qq-bootstrap-last-inbound-adoption-v1"
            or provenance.get("binding_id") != binding.binding_id
            or proof is None
            or not isinstance(provenance.get("participant_signature"), str)
            or _signature_evidence_hash(str(provenance.get("participant_signature")))
            not in {
                _signature_evidence_hash(binding.participant_signature),
                *(
                    (str(migration["previous_evidence_hash"]),)
                    if migration is not None
                    else ()
                ),
            }
            or isinstance(provenance.get("process_id"), bool)
            or not isinstance(provenance.get("process_id"), int)
            or int(provenance["process_id"]) <= 0
            or isinstance(provenance.get("window_handle"), bool)
            or not isinstance(provenance.get("window_handle"), int)
            or int(provenance["window_handle"]) <= 0
            or isinstance(provenance.get("process_started_at_100ns"), bool)
            or not isinstance(provenance.get("process_started_at_100ns"), int)
            or int(provenance["process_started_at_100ns"]) <= 0
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(provenance.get("selected_row_runtime_id_hash", "")),
            ) is None
            or not isinstance(provenance.get("client_version"), str)
            or not provenance.get("client_version")
            or not isinstance(provenance.get("selector_pack_version"), str)
            or not provenance.get("selector_pack_version")
            or provenance.get("last_direction") != "inbound"
            or not isinstance(provenance.get("text_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(provenance["text_sha256"])) is None
            or isinstance(count, bool) or not isinstance(count, int) or count < 1
            or isinstance(ordinal, bool) or not isinstance(ordinal, int)
            or ordinal != count - 1
        ):
            raise ValueError("bootstrap adoption provenance does not match session binding")
    _validate_generation_contacts(generation, config, bindings, evidence)
    return pack, bindings, evidence


def _validated_visual_selection(
    config: dict[str, Any],
    bindings: tuple[QQIdentityBinding, ...],
) -> VisualSelectionConfig | None:
    raw = config.get("visual_selection")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("visual_selection must be an object")
    try:
        visual = VisualSelectionConfig.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"visual_selection is invalid: {exc}") from exc
    expected = {item.binding_id for item in bindings}
    if set(visual.labels) != expected:
        raise ValueError("visual_selection labels must match bindings one-to-one")
    if config.get("model", "deepseek-v4-flash") == visual.model:
        raise ValueError(
            "the vision model is reserved for contact selection, not reply planning"
        )
    worker_timeout, prepare_reserve = _validated_worker_timing(config)
    if visual.timeout_seconds + prepare_reserve + 4 > worker_timeout:
        raise ValueError(
            "visual_selection timeout must leave the prepare write reserve and "
            "four seconds for worker retirement"
        )
    return visual


def _validated_worker_timing(config: dict[str, Any]) -> tuple[float, float]:
    raw_timeout = config.get("worker_timeout_seconds", 15)
    raw_reserve = config.get("prepare_write_reserve_seconds", 5)
    if (
        isinstance(raw_timeout, bool)
        or not isinstance(raw_timeout, (int, float))
        or isinstance(raw_reserve, bool)
        or not isinstance(raw_reserve, (int, float))
    ):
        raise ValueError("worker timing values must be numbers")
    try:
        worker_timeout = float(raw_timeout)
        prepare_reserve = float(raw_reserve)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("worker timing values must be finite numbers") from exc
    if (
        not math.isfinite(worker_timeout)
        or worker_timeout <= 0
        or worker_timeout > 600
    ):
        raise ValueError("worker timeout must be greater than 0 and no more than 600")
    if (
        not math.isfinite(prepare_reserve)
        or prepare_reserve < 1
        or prepare_reserve > worker_timeout - 4
    ):
        raise ValueError(
            "prepare write reserve must be at least one second and leave four seconds "
            "for worker retirement"
        )
    return worker_timeout, prepare_reserve


def validate_active_rules(config: dict[str, Any]) -> None:
    from messenger_ai.rules.service import AtomicRulePackStore
    path = Path(config["data_dir"]).expanduser() / "rules.sqlite3"
    store = AtomicRulePackStore(str(path))
    try:
        for item in config["contacts"]:
            try:
                store.resolve(str(item["contact_id"]))
            except Exception as exc:
                raise ValueError(f"no active M7 RulePack for {item['contact_id']}") from exc
    finally:
        store.connection.close()


def _capability(config: dict[str, Any], pack: QQSelectorPack) -> CapabilitySnapshot:
    raw = config.get("capability")
    if not isinstance(raw, dict):
        raise ValueError("capability evidence is required")
    capability = CapabilitySnapshot.model_validate(raw)
    if capability.environment_fingerprint != pack.environment_fingerprint:
        raise ValueError("capability evidence does not match selector pack environment")
    if capability.client_version != pack.client_version:
        raise ValueError("capability evidence does not match selector pack client version")
    if capability.execution_mode.value != "guest_foreground":
        raise ValueError("production QQ runtime requires guest_foreground capability evidence")
    if capability.send_background.value != "unsupported" or capability.verify_background.value != "unsupported":
        raise ValueError("background send/verify must remain unsupported for guest runtime")
    if capability.send_guest_foreground.value != "supported" or capability.verify_guest_foreground.value != "supported":
        raise ValueError("guest send/verify evidence is missing")
    if not capability.healthy:
        raise ValueError("guest capability evidence is unhealthy")
    return capability


def build_runtime(config: dict[str, Any], *, api_key: str,
                   authorization_signing_key: bytes, run_id: str | None = None,
                   selection_refresh_retry_enabled: bool = True,
                   recover_persistent_state: bool = True):
    pack, bindings, session_evidence = validate_config(config, api_key=api_key)
    worker_timeout, prepare_write_reserve = _validated_worker_timing(config)
    generation = _validated_runtime_generation(config)
    visual_selection = _validated_visual_selection(config, bindings)
    identity_migrations = _validated_session_identity_migrations(
        config, bindings, session_evidence
    )
    bootstrap = config.get("bootstrap_last_inbound_once", [])
    data_dir = Path(config["data_dir"]).expanduser(); data_dir.mkdir(parents=True, exist_ok=True)
    if generation is not None:
        # The bridge constructor performs its own durable recovery.  Establish
        # the global pause row before constructing it so *all* recovery work is
        # fenced, not only the coordinator recovery performed by assembly.
        pause_state = RuntimeState(
            data_dir / "runtime.sqlite3",
            verified_send_stores=VerifiedSendStorePaths(
                hub=data_dir / "hub.sqlite3",
                pacing=data_dir / "pacing.sqlite3",
            ),
            initially_paused=True,
            initial_pause_reason="isolated_identity_recovery",
        )
        try:
            revision, paused, _reason = pause_state.global_control()
            if not paused and not pause_state.set_global_pause(
                paused=True,
                expected_revision=revision,
                reason="isolated_identity_recovery",
            ):
                raise RuntimeError("isolated runtime generation pause fence changed")
        finally:
            pause_state.close()
    provider = DeepSeekResponsesProvider(api_key=api_key, model=config.get("model", "deepseek-v4-flash"), timeout_seconds=float(config.get("timeout_seconds", 30)))
    worker_kwargs: dict[str, Any] = {}
    if visual_selection is not None:
        worker_kwargs = {
            "visual_selection": visual_selection,
            "visual_api_key": api_key,
        }
    worker = QQVMWorkerProcess(
        pack,
        bindings,
        session_evidence=session_evidence,
        run_id=run_id,
        prepare_write_reserve_seconds=prepare_write_reserve,
        **worker_kwargs,
    )
    app_box: dict[str, Any] = {}
    def text_provider(command):
        app = app_box.get("app")
        row = app.hub.store.connection.execute("SELECT text FROM drafts WHERE draft_id=?", (str(command.draft_id),)).fetchone() if app else None
        return str(row["text"]) if row else ""
    bridge = QQVMDriverBridge(worker=worker, bindings=bindings, text_provider=text_provider,
        sqlite_path=data_dir / "qq-vm-bridge.sqlite3", bootstrap_last_inbound=tuple(bootstrap),
        bootstrap_last_inbound_provenance=dict(config.get("bootstrap_last_inbound_provenance", {})),
        timeout_seconds=worker_timeout,
        selection_refresh_retry_enabled=selection_refresh_retry_enabled,
        recover_persistent_state=recover_persistent_state)
    try:
        capability = _capability(config, pack)
        inherited_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            worker.start()
        finally:
            if inherited_key is not None:
                os.environ["DEEPSEEK_API_KEY"] = inherited_key
        health = bridge.probe_health()
        if health.status is not WorkerStatus.OK:
            reason = health.error_code or health.status.value
            raise RuntimeError(f"QQ worker startup health check failed: {reason}")
        # Runtime state and registered conversations are created only after the
        # concrete QQ child has answered the mandatory health handshake.  This
        # prevents external supervisors from treating stale database rows as a
        # successful startup when UIA initialization failed.
        app = assemble_runtime(
            data_dir=data_dir,
            planner_provider=provider,
            driver=bridge,
            capability=capability,
            authorization_signing_key=authorization_signing_key,
            model_concurrency=int(config.get("model_concurrency", 2)),
            content_policy_checks_enabled=config.get(
                "content_policy_checks_enabled", True
            ),
            recover_persistent_state=recover_persistent_state,
            initially_paused=generation is not None,
            initial_pause_reason="isolated_identity_recovery",
        )
    except Exception:
        _close_unassembled(provider, worker, bridge)
        raise
    try:
        if generation is not None:
            _revision, paused, _reason = app.state.global_control()
            if not paused:
                raise RuntimeError("isolated runtime generation pause fence missing")
        for item in bindings:
            try:
                resolved = app.rules.resolve(item.contact_id)
            except Exception as exc:
                raise ValueError(f"no active M7 RulePack for {item.contact_id}") from exc
            if not resolved.rulepack.version:
                raise ValueError(f"active M7 RulePack is incomplete for {item.contact_id}")
        for binding in bindings:
            state_row = app.state.connection.execute("SELECT account_id,contact_id,binding_revision,conversation_type FROM runtime_conversations WHERE conversation_id=?", (binding.hub_conversation_id,)).fetchone()
            if state_row is not None and (state_row["account_id"], state_row["contact_id"], int(state_row["binding_revision"])) != (binding.account_id, binding.contact_id, 1):
                raise ValueError(f"persisted binding changed for {binding.hub_conversation_id}; human rebind required")
            if state_row is not None and state_row["conversation_type"] != binding.conversation_type:
                raise ValueError(f"persisted conversation type changed for {binding.hub_conversation_id}; re-authentication required")
            contact_row = app.memory.store.connection.execute("SELECT 1 FROM memory_contacts WHERE contact_id=?", (binding.contact_id,)).fetchone()
            if contact_row is None:
                app.memory.create_contact(Contact(contact_id=binding.contact_id, created_at=app.hub.now()))
            signature = next((item.participant_signature for item in session_evidence if item.binding_id == binding.binding_id), binding.participant_signature)
            evidence = hashlib.sha256(signature.encode()).hexdigest()
            memory_row = app.memory.store.connection.execute("SELECT contact_id,evidence_hash FROM memory_bindings WHERE platform=? AND account_id=? AND conversation_id=?", (Platform.QQ.value, binding.account_id, binding.hub_conversation_id)).fetchone()
            if memory_row is not None and memory_row["contact_id"] != binding.contact_id:
                raise ValueError(f"persisted identity changed for {binding.hub_conversation_id}; human rebind required")
            if memory_row is None:
                app.memory.bind_identity(IdentityBinding(contact_id=binding.contact_id, platform=Platform.QQ, account_id=binding.account_id, conversation_id=binding.hub_conversation_id, platform_evidence_hash=evidence, verified_by=("session-observed-bootstrap" if session_evidence else "configured-human-binding"), verified_at=app.hub.now()))
            elif memory_row["evidence_hash"] != evidence:
                migration = identity_migrations.get(binding.hub_conversation_id)
                if (
                    migration is None
                    or memory_row["evidence_hash"] != migration["previous_evidence_hash"]
                    or evidence != migration["current_evidence_hash"]
                ):
                    raise ValueError(f"persisted identity evidence changed for {binding.hub_conversation_id}; human rebind required")
                app.memory.bind_identity(IdentityBinding(
                    contact_id=binding.contact_id,
                    platform=Platform.QQ,
                    account_id=binding.account_id,
                    conversation_id=binding.hub_conversation_id,
                    platform_evidence_hash=evidence,
                    verified_by="session-identity-migration",
                    verified_at=app.hub.now(),
                ), expected_evidence_hash=migration["previous_evidence_hash"])
            if state_row is None:
                app.state.register(account_id=binding.account_id, contact_id=binding.contact_id, conversation_id=binding.hub_conversation_id, binding_revision=1, conversation_type=binding.conversation_type)
        app_box["app"] = app
        return app
    except Exception:
        _shutdown(app)
        raise


def _read_control_request(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != CONTROL_REQUEST_SCHEMA:
        raise ValueError("invalid control request schema")
    request_id = str(UUID(str(raw.get("request_id", ""))))
    target_run_id = str(UUID(str(raw.get("target_run_id", ""))))
    action = raw.get("action")
    if action not in CONTROL_ACTIONS:
        raise ValueError("invalid control action")
    requested_at = datetime.fromisoformat(str(raw.get("requested_at", "")).replace("Z", "+00:00"))
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("control requested_at must include a timezone")
    return {
        "request_id": request_id,
        "target_run_id": target_run_id,
        "action": str(action),
        "requested_at": requested_at.isoformat(),
    }


def _write_control_result(path: Path, *, request: dict[str, str], accepted: bool,
                          state: str, error_code: str | None = None) -> None:
    payload: dict[str, Any] = {
        "schema": CONTROL_RESULT_SCHEMA,
        "request_id": request["request_id"],
        "target_run_id": request["target_run_id"],
        "action": request["action"],
        "accepted": accepted,
        "state": state,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    if error_code is not None:
        payload["error_code"] = error_code
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


async def _watch_runtime_control(
    app,
    *,
    run_id: str,
    stop_event: asyncio.Event,
    request_path: Path = CONTROL_REQUEST_PATH,
    result_path: Path = CONTROL_RESULT_PATH,
    poll_interval: float = .2,
) -> str:
    """Consume run-scoped control requests without relying on desktop focus."""
    last_content: bytes | None = None
    while True:
        try:
            content = await asyncio.to_thread(request_path.read_bytes)
        except FileNotFoundError:
            content = None
        except OSError:
            content = None
        if content is not None and content != last_content:
            last_content = content
            try:
                request = await asyncio.to_thread(_read_control_request, request_path)
            except (OSError, ValueError, json.JSONDecodeError):
                request = None
            if request is not None:
                if request["target_run_id"] != run_id:
                    await asyncio.to_thread(
                        _write_control_result,
                        result_path,
                        request=request,
                        accepted=False,
                        state="rejected",
                        error_code="TARGET_RUN_MISMATCH",
                    )
                else:
                    action = request["action"]
                    pause_fence_token = None
                    pause_fence_handed_off = False
                    try:
                        paused = action != "resume"
                        if paused:
                            pause_fence_token = app.begin_global_pause_from_control()
                            # Acknowledge the in-memory pause fence before a
                            # potentially long UI-bearing tick drains.  This is
                            # deliberately not a claim that durable pause has
                            # completed; the final state is written below.
                            await asyncio.to_thread(
                                _write_control_result,
                                result_path,
                                request=request,
                                accepted=True,
                                state="pausing",
                            )
                        pause_fence_handed_off = paused
                        await app.set_global_pause_from_control(
                            paused=paused,
                            reason=f"runtime_control:{action}:{request['request_id']}",
                            pause_fence_token=pause_fence_token,
                        )
                    except Exception:
                        if pause_fence_token is not None and not pause_fence_handed_off:
                            app.cancel_global_pause_from_control(pause_fence_token)
                        await asyncio.to_thread(
                            _write_control_result,
                            result_path,
                            request=request,
                            accepted=False,
                            state="rejected",
                            error_code="CONTROL_APPLY_FAILED",
                        )
                    else:
                        state = "running" if action == "resume" else (
                            "stopping" if action == "graceful_stop" else "paused"
                        )
                        await asyncio.to_thread(
                            _write_control_result,
                            result_path,
                            request=request,
                            accepted=True,
                            state=state,
                        )
                        if action == "graceful_stop":
                            stop_event.set()
                            return action
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except TimeoutError:
            continue
        return "graceful_stop"


def _bounded_worker_record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "request_id", "kind", "binding_id", "started_at", "completed_at",
        "elapsed_ms", "status", "error_code", "worker_exit_code",
        "parent_terminate_reason", "exception_types", "failed_generation",
        "failed_run_id", "release_id", "released_at", "release_operator_id",
        "release_reason_code",
    }
    bounded = {key: value.get(key) for key in allowed if key in value}
    hresult = value.get("com_hresult")
    if isinstance(hresult, int) and not isinstance(hresult, bool):
        bounded["com_hresult"] = hresult
    raw_frames = value.get("project_frames")
    frames: list[dict[str, object]] = []
    if isinstance(raw_frames, list):
        for item in raw_frames[-8:]:
            if not isinstance(item, dict):
                continue
            function, filename, line = item.get("function"), item.get("file"), item.get("line")
            if (
                isinstance(function, str) and len(function) <= 160
                and isinstance(filename, str) and len(filename) <= 320
                and filename.startswith("messenger_ai/") and ".." not in filename
                and isinstance(line, int) and not isinstance(line, bool)
                and 1 <= line <= 10_000_000
            ):
                frames.append({"function": function, "file": filename, "line": line})
    if frames:
        bounded["project_frames"] = frames
    return bounded


def _bounded_recovery_record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "run_id", "failed_generation", "successor_generation", "conversation_id", "binding_id",
        "binding_revision", "request_id", "status", "health_request_id",
        "health_error_code", "created_at", "completed_at",
    }
    return {key: value.get(key) for key in allowed if key in value}


def _worker_witness_payload(app, *, run_id: str, stopping: bool = False) -> dict[str, object]:
    now = datetime.now(UTC)
    snapshot = app.driver.worker_status_snapshot()
    freshness = float(app.driver.observation_freshness_seconds)
    startup = _bounded_worker_record(snapshot.get("startup_health"))
    last_request = _bounded_worker_record(snapshot.get("last_request"))
    observed = _bounded_worker_record(snapshot.get("last_successful_observe"))
    terminal = _bounded_worker_record(snapshot.get("first_terminal_failure"))
    raw_history = snapshot.get("historical_terminal_failures")
    historical_terminal_failures = [
        record for record in (
            _bounded_worker_record(item)
            for item in (raw_history[-8:] if isinstance(raw_history, list) else [])
        ) if record is not None
    ]
    last_recovery = _bounded_recovery_record(snapshot.get("last_read_only_recovery"))
    worker_generation = snapshot.get("worker_generation")
    if (
        isinstance(worker_generation, bool) or not isinstance(worker_generation, int)
        or worker_generation < 1
    ):
        worker_generation = None
    quarantine_count = snapshot.get("observation_quarantine_count")
    if (
        isinstance(quarantine_count, bool) or not isinstance(quarantine_count, int)
        or quarantine_count < 0
    ):
        quarantine_count = None
    alive = snapshot.get("worker_alive") is True
    run_matches = snapshot.get("run_id") == run_id
    observe_age: float | None = None
    if observed is not None and observed.get("completed_at"):
        try:
            completed = datetime.fromisoformat(
                str(observed["completed_at"]).replace("Z", "+00:00")
            )
            if completed.tzinfo is not None and completed.utcoffset() is not None:
                observe_age = (now - completed).total_seconds()
        except ValueError:
            observe_age = None
    startup_ok = bool(startup and startup.get("kind") == "health" and startup.get("status") == "ok")
    last_request_failed = bool(
        last_request and last_request.get("completed_at")
        and last_request.get("status") not in {"ok", "in_progress"}
    )
    observe_ok = bool(
        observed and observed.get("kind") == "observe"
        and observed.get("status") == "ok" and observe_age is not None
        and 0.0 <= observe_age <= freshness
    )
    reasons: list[str] = []
    if stopping:
        state = "stopping"
        reasons.append("STOPPING")
    elif not run_matches:
        state = "unavailable"
        reasons.append("RUN_ID_MISMATCH")
    elif terminal is not None or not alive:
        state = "unavailable"
        reasons.append(str((terminal or {}).get("error_code") or "WORKER_NOT_ALIVE").upper())
    elif not startup_ok:
        state = "degraded"
        reasons.append("STARTUP_HEALTH_NOT_OK")
    elif last_request_failed:
        state = "degraded"
        reasons.append(str(
            last_request.get("error_code")
            or f"LAST_REQUEST_{last_request.get('status', 'FAILED')}"
        ).upper())
    elif observed is None:
        state = "degraded"
        reasons.append("OBSERVE_NOT_YET_SUCCESSFUL")
    elif not observe_ok:
        state = "degraded"
        reasons.append("OBSERVE_STALE")
    else:
        state = "available"
    return {
        "schema": "pmai-qq-runtime-worker-status-v1",
        "run_id": run_id,
        "written_at": now.isoformat(),
        "state": state,
        "available": state == "available",
        "reason_codes": reasons,
        "observe_freshness_seconds": freshness,
        "observe_age_seconds": observe_age,
        "worker_process_id": snapshot.get("worker_process_id"),
        "worker_alive": alive,
        "worker_exit_code": snapshot.get("worker_exit_code"),
        "parent_terminate_reason": snapshot.get("parent_terminate_reason"),
        "startup_health": startup,
        "last_request": last_request,
        "last_successful_observe": observed,
        "first_terminal_failure": terminal,
        "worker_generation": worker_generation,
        "historical_terminal_failures": historical_terminal_failures,
        "last_read_only_recovery": last_recovery,
        "observation_quarantine_count": quarantine_count,
    }


_TRANSIENT_WINDOWS_FILE_ERRORS = frozenset({5, 32, 33})


def _write_worker_witness(path: Path, payload: dict[str, object]) -> None:
    """Atomically publish status, tolerating only brief Windows share locks."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False)
    for attempt in range(4):
        try:
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(path)
            return
        except OSError as exc:
            transient = (
                os.name == "nt"
                and getattr(exc, "winerror", None) in _TRANSIENT_WINDOWS_FILE_ERRORS
            )
            if not transient or attempt == 3:
                raise
            time.sleep(.05 * (attempt + 1))


def _report_background_task_failure(*, component: str, run_id: str,
                                    exc: BaseException) -> None:
    """Emit a bounded current-run diagnostic without exception messages."""

    frames: list[dict[str, object]] = []
    current_module = Path(__file__).resolve(strict=False)
    for frame in traceback.extract_tb(exc.__traceback__)[-12:]:
        normalized = frame.filename.replace("\\", "/")
        if Path(frame.filename).resolve(strict=False) == current_module:
            filename = "scripts/run_vm_runtime.py"
        elif "/messenger_ai/" in normalized:
            filename = f"messenger_ai/{normalized.split('/messenger_ai/', 1)[1]}"
        else:
            continue
        frames.append({
            "function": frame.name[:160],
            "file": filename[:320],
            "line": frame.lineno,
        })
    payload: dict[str, object] = {
        "schema": "pmai-runtime-background-task-failure-v1",
        "run_id": run_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "component": component,
        "exception_type": type(exc).__name__,
        "project_frames": frames[-8:],
    }
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)
    if isinstance(winerror, int) and not isinstance(winerror, bool):
        payload["winerror"] = winerror
    if isinstance(errno, int) and not isinstance(errno, bool):
        payload["errno"] = errno
    print(json.dumps(payload, ensure_ascii=False), flush=True)


async def _watch_worker_status(app, *, run_id: str, stop_event: asyncio.Event,
                               path: Path = WORKER_STATUS_PATH,
                               poll_interval: float = 1.0) -> None:
    try:
        while not stop_event.is_set():
            payload = _worker_witness_payload(app, run_id=run_id)
            await asyncio.to_thread(_write_worker_witness, path, payload)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                continue
        payload = _worker_witness_payload(app, run_id=run_id, stopping=True)
        await asyncio.to_thread(_write_worker_witness, path, payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _report_background_task_failure(
            component="worker_status_publisher", run_id=run_id, exc=exc
        )
        raise


async def _serve(app, *, host: str, port: int, run_id: str | None = None,
                 control_request_path: Path = CONTROL_REQUEST_PATH,
                 control_result_path: Path = CONTROL_RESULT_PATH,
                 worker_status_path: Path = WORKER_STATUS_PATH) -> None:
    import uvicorn
    projection = RuntimeWebUIProjection(state=app.state, hub=app.hub, pacing=app.pacing, rules=app.rules, driver=app.driver)
    server = uvicorn.Server(uvicorn.Config(create_app(LiveHubFacade(projection)), host=host, port=port, log_level="info"))
    stop_event = asyncio.Event()
    runtime_task = asyncio.create_task(
        app.run_forever(stop_event=stop_event) if run_id is not None else app.run_forever()
    )
    web_task = asyncio.create_task(server.serve())
    control_task = (
        asyncio.create_task(_watch_runtime_control(
            app,
            run_id=run_id,
            stop_event=stop_event,
            request_path=control_request_path,
            result_path=control_result_path,
        ))
        if run_id is not None else None
    )
    worker_status_task = (
        asyncio.create_task(_watch_worker_status(
            app, run_id=run_id, stop_event=stop_event, path=worker_status_path
        ))
        if run_id is not None and callable(
            getattr(getattr(app, "driver", None), "worker_status_snapshot", None)
        ) else None
    )
    active = {runtime_task, web_task}
    if control_task is not None:
        active.add(control_task)
    if worker_status_task is not None:
        active.add(worker_status_task)
    done, pending = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
    try:
        graceful_stop = control_task is not None and control_task in done and control_task.result() == "graceful_stop"
        if graceful_stop:
            server.should_exit = True
            stop_event.set()
            # A verified stop request must not leave the supervisor waiting
            # forever on a runtime or Uvicorn task that ignores its signal.
            # wait_for cancels both children on timeout; the existing finally
            # block then performs the normal witness/task/app cleanup.  Do not
            # catch the timeout (or any child exception): callers must retain
            # evidence that shutdown was not clean.
            await asyncio.wait_for(
                asyncio.gather(runtime_task, web_task),
                timeout=GRACEFUL_STOP_TIMEOUT_SECONDS,
            )
        for task in done:
            if task is not control_task:
                task.result()
    finally:
        if worker_status_task is not None:
            if not stop_event.is_set():
                worker_status_task.cancel()
            await asyncio.gather(worker_status_task, return_exceptions=True)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in tuple(getattr(app, "_planning_tasks", ())):
            task.cancel()
        tasks = tuple(getattr(app, "_planning_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            getattr(app, "_planning_tasks", set()).clear()
        close_app = getattr(app, "aclose", None)
        if callable(close_app):
            await close_app()
        setattr(app, "_closed", True)


def _shutdown(app: Any, *, runner: asyncio.Runner | None = None) -> None:
    """Close on the execution loop when supplied; preserve standalone callers."""
    if getattr(app, "_closed", False):
        return
    close_app = getattr(app, "aclose", None)
    if callable(close_app):
        result = close_app()
        if asyncio.iscoroutine(result):
            (runner.run if runner is not None else asyncio.run)(result)
        setattr(app, "_closed", True)
        return
    bridge = getattr(app, "driver", None)
    worker = getattr(bridge, "_worker", None)
    stop = getattr(worker, "stop", None)
    if callable(stop):
        stop()
    db = getattr(bridge, "_db", None)
    if db is not None:
        db.close()
    provider = getattr(app, "planner_provider", None)
    close_provider = getattr(provider, "aclose", None) or getattr(provider, "close", None)
    if callable(close_provider):
        result = close_provider()
        if asyncio.iscoroutine(result):
            (runner.run if runner is not None else asyncio.run)(result)
    cursor = getattr(bridge, "_cursor", None)
    if cursor is not None and callable(getattr(cursor, "close", None)):
        cursor.close()
    auth_store = getattr(getattr(getattr(app, "due", None), "authorization", None), "_store", None)
    for obj in (auth_store, getattr(app, "state", None), getattr(getattr(app, "hub", None), "store", None), getattr(getattr(app, "memory", None), "store", None), getattr(app, "pacing", None), getattr(app, "rules", None)):
        close = getattr(obj, "close", None)
        if callable(close):
            close()
        elif obj is not None and hasattr(obj, "connection"):
            obj.connection.close()


def _close_unassembled(provider: Any, worker: Any, bridge: Any) -> None:
    """Release construction-time resources when assembly never returns an app."""
    stop = getattr(worker, "stop", None)
    if callable(stop):
        stop()
    close_provider = getattr(provider, "aclose", None) or getattr(provider, "close", None)
    if callable(close_provider):
        result = close_provider()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    close = getattr(bridge, "close", None)
    if callable(close):
        close()
    else:
        db = getattr(bridge, "_db", None)
        if db is not None:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Messenger AI V5 guest runtime")
    parser.add_argument("--config")
    parser.add_argument("--check", action="store_true", help="validate without worker, API client, or WebUI")
    parser.add_argument(
        "--assert-runtime-stopped",
        action="store_true",
        help="acquire and release the runtime owner without reading config or secrets",
    )
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8765)
    parser.add_argument("--run-id", help="supervisor-generated UUID for run-scoped control")
    parser.add_argument(
        "--expected-config-sha256",
        help="require the single config read to match this supervisor/build digest",
    )
    args = parser.parse_args(argv)
    owner: QQRuntimeInstanceOwner | None = None
    try:
        if args.assert_runtime_stopped:
            if args.config or args.check or args.run_id or args.expected_config_sha256:
                raise ValueError("runtime stopped assertion cannot be combined with runtime options")
            owner = QQRuntimeInstanceOwner()
            owner.acquire()
            print("runtime stopped ownership confirmed")
            return 0
        if not args.config:
            raise ValueError("--config is required")
        config = load_config(
            args.config,
            expected_sha256=args.expected_config_sha256,
        )
        vault = config.get("secret_vault")
        if not vault:
            raise ValueError("secret_vault is required; configure DPAPI secrets before startup")
        secrets = WindowsDPAPISecretStore(Path(vault))
        key = secrets.get_secret("deepseek.api_key").decode("utf-8")
        if args.check:
            pack, _, _ = validate_config(config, api_key=key)
            _capability(config, pack)
            validate_active_rules(config)
            print("configuration valid; no QQ login, send, or API call performed"); return 0
        if not args.run_id:
            raise ValueError("--run-id is required for runtime startup")
        run_id = str(UUID(args.run_id))
        owner = QQRuntimeInstanceOwner()
        owner.acquire()
        app = build_runtime(
            config,
            api_key=key,
            authorization_signing_key=secrets.get_or_create_hmac_key("runtime.authorization.signing"),
            run_id=run_id,
        )
        with asyncio.Runner() as runner:
            try:
                runner.run(_serve(app, host=args.web_host, port=args.web_port, run_id=run_id))
                return 0
            finally:
                _shutdown(app, runner=runner)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"blocked: {exc}"); return 2
    finally:
        if owner is not None:
            owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
