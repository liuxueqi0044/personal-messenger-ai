"""PENDING: schema sketch only; not a production configuration builder.

No real producer currently supplies the required account/profile/session
evidence or authorization_scope. Do not use this module for production.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, tempfile
from pathlib import Path
from typing import Any

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
SCHEMA = "pmai-qq-bootstrap-evidence-v1"

def _obj(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict): raise ValueError(f"{name}_must_be_object")
    return value

def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"{name}_required")
    return value

def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if not HEX64.fullmatch(result): raise ValueError(f"{name}_must_be_sha256")
    return result.lower()

def build(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema") != SCHEMA: raise ValueError("unsupported_evidence_schema")
    scope = _obj(payload.get("account_scope"), "account_scope")
    account_id = _text(scope.get("account_id"), "account_id")
    identity_hmac = _hash(scope.get("identity_hmac"), "identity_hmac")
    scope_version = _text(scope.get("scope_version"), "scope_version")
    selector = _obj(payload.get("selector_pack"), "selector_pack")
    capability = _obj(payload.get("capability"), "capability")
    if selector.get("client_version") != capability.get("client_version"):
        raise ValueError("selector_capability_client_version_mismatch")
    if selector.get("environment_fingerprint") != capability.get("environment_fingerprint"):
        raise ValueError("selector_capability_environment_mismatch")
    if capability.get("healthy") is not True or capability.get("execution_mode") != "guest_foreground":
        raise ValueError("capability_not_guest_foreground_healthy")
    if capability.get("send_background") != "unsupported" or capability.get("verify_background") != "unsupported":
        raise ValueError("background_capability_must_be_unsupported")
    raw = payload.get("bindings")
    if not isinstance(raw, list) or not raw: raise ValueError("bindings_required")
    contacts: list[dict[str, Any]] = []; bindings: list[dict[str, Any]] = []
    seen_contact: set[str] = set(); seen_binding: set[str] = set(); seen_conversation: set[str] = set()
    for item in raw:
        row = _obj(item, "binding")
        contact_id = _text(row.get("contact_id"), "contact_id")
        if contact_id in seen_contact: raise ValueError("duplicate_contact_id")
        if row.get("account_id") != account_id: raise ValueError("binding_account_scope_mismatch")
        if row.get("conversation_type") != "direct": raise ValueError("only_direct_bindings_allowed")
        evidence = _obj(row.get("friendship_evidence"), "friendship_evidence")
        if evidence.get("source") != "qq_guest_profile_probe" or evidence.get("status") != "verified":
            raise ValueError("friendship_evidence_source_or_status_invalid")
        profile_digest = _hash(evidence.get("profile_digest"), "profile_digest")
        evidence_hmac = _hash(evidence.get("evidence_hmac"), "evidence_hmac")
        if row.get("friendship_verified") is not None: raise ValueError("friendship_verified_must_be_derived")
        signature = _text(row.get("participant_signature"), "participant_signature")
        if signature.startswith("uncertified:"): raise ValueError("uncertified_participant_signature")
        binding_id = _text(row.get("binding_id"), "binding_id")
        hub_id = _text(row.get("hub_conversation_id"), "hub_conversation_id")
        platform_id = _text(row.get("platform_conversation_id"), "platform_conversation_id")
        for value, seen, code in ((binding_id, seen_binding, "duplicate_binding_id"), (hub_id, seen_conversation, "duplicate_conversation_id")):
            if value in seen: raise ValueError(code)
            seen.add(value)
        rule = _obj(row.get("rulepack"), "rulepack")
        if rule.get("status") != "active": raise ValueError("rulepack_not_active")
        rule_id = _text(rule.get("rulepack_id"), "rulepack_id"); version = _text(rule.get("version"), "rulepack_version"); source_hash = _hash(rule.get("source_hash"), "rulepack_source_hash")
        binding = {"hub_conversation_id": hub_id, "contact_id": contact_id, "account_id": account_id, "platform_conversation_id": platform_id, "participant_signature": signature, "binding_id": binding_id, "conversation_type": "direct", "friendship_verified": True}
        bindings.append(binding)
        contacts.append({"contact_id": contact_id, "rulepack_status": "active", "rulepack_id": rule_id, "rulepack_version": version, "rulepack_source_hash": source_hash, "binding": binding})
        seen_contact.add(contact_id)
    return {"schema": "pmai-v5-runtime-1", "data_dir": _text(payload.get("data_dir"), "data_dir"), "secret_vault": _text(payload.get("secret_vault"), "secret_vault"), "identity_hmac": identity_hmac, "identity_scope_version": scope_version, "contacts": contacts, "bindings": bindings, "selector_pack": selector, "capability": capability}

def main() -> int:
    print(json.dumps({"succeeded": False, "error_code": "BOOTSTRAP_PENDING_REAL_EVIDENCE_SCHEMA"})); return 3
    parser = argparse.ArgumentParser(); parser.add_argument("--input", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        value = build(json.loads(Path(args.input).read_text(encoding="utf-8")))
        target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle: json.dump(value, handle, sort_keys=True, indent=2); handle.write("\n")
        os.replace(temp_name, target); return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"succeeded": False, "error_code": str(exc) if isinstance(exc, ValueError) else type(exc).__name__.upper()})); return 2

if __name__ == "__main__": raise SystemExit(main())
