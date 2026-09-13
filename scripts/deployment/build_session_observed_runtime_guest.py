"""Build a durable guest-local runtime config from explicit observed contacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity

OUTPUT = Path(r"C:\PMAI\data\runtime-session-1.json")
DATA_ROOT = Path(r"C:\PMAI\data\runtime\qq-default-account")
BOOTSTRAP_ROOT = Path(r"C:\PMAI\data")
BRIDGE_DB = DATA_ROOT / "qq-vm-bridge.sqlite3"
CURSOR_DB = BRIDGE_DB.with_suffix(".cursor.sqlite3")
RUNTIME_DB = DATA_ROOT / "runtime.sqlite3"
SOURCE_RULES = Path(r"C:\PMAI\data\rules.sqlite3")
SELECTOR_PACK = Path(__file__).resolve().parent / "selector-pack-session-1.json"
ACCOUNT_ID = "qq-default-account"
MAX_CONTACT_INDEX = 9999


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temporary, path)


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
    try:
        proof = QQSessionObservedDirectIdentity.model_validate(value["evidence"])
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
        raise RuntimeError("durable account has no contact-1 registration; explicit rebind required")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
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
    args = parser.parse_args(argv)
    additional = set(args.additional_contact_index)
    adopt = set(args.adopt_latest_inbound_index)
    refresh = set(args.refresh_session_index)
    header_upgrade = set(args.migrate_header_digest_index)
    for index in additional | adopt | refresh | header_upgrade:
        if not 1 <= index <= MAX_CONTACT_INDEX:
            parser.error(f"contact index must be between 1 and {MAX_CONTACT_INDEX}")
    if adopt - additional:
        parser.error("latest inbound adoption requires the same explicit additional contact index")
    if additional & {1, 2}:
        parser.error("contact 1/2 use the existing compatibility options")
    selected_indices = {1} | additional | _existing_registration_indices() | _existing_runtime_indices()
    if args.include_contact_2:
        selected_indices.add(2)
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
        if _bootstrap_consumed(item.conversation_id):
            continue
        if item.index in adoptions:
            bootstrap_last_inbound_once.append(item.conversation_id)
            bootstrap_last_inbound_provenance[item.conversation_id] = adoptions[item.index]
        elif item.index in {1, 2}:
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
    for path, registration, migration in pending_registrations:
        _atomic_json(path, registration)
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

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    target_rules = DATA_ROOT / "rules.sqlite3"
    if not target_rules.exists():
        shutil.copy2(SOURCE_RULES, target_rules)

    config = {
        "schema": "pmai-v5-runtime-1",
        "data_dir": str(DATA_ROOT),
        "secret_vault": r"C:\PMAI\secrets",
        "model": "deepseek-v4-flash",
        "worker_timeout_seconds": 45,
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
    _atomic_json(OUTPUT, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
