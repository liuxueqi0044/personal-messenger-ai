"""Guest-only, read-only N0 inspection of the current QQ navigation surface.

It never selects a contact, invokes a profile, focuses, types, reads message
content, or issues an active-chat lease.  Names are classified locally and only
opaque hashes leave the process.  A profile HMAC here is enrollment evidence,
not a binding: production must independently associate it with the current chat.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def inspect_surface(*, release_root: Path, vault: Path, initialize_key: bool = False) -> dict:
    if os.environ.get("COMPUTERNAME") != "PMAI-QQVM" or os.environ.get("USERNAME", "").lower() != "qqbot":
        raise RuntimeError("CERTIFIED_GUEST_REQUIRED")
    if not release_root.is_relative_to(Path(r"C:\PMAI\app\releases")):
        raise RuntimeError("RELEASE_SCOPE_INVALID")
    sys.path.insert(0, str(release_root))
    from messenger_ai.observability import WindowsDPAPISecretStore
    store = WindowsDPAPISecretStore(vault)
    key = store.get_or_create_hmac_key("qq.identity.hmac") if initialize_key else store.get_secret("qq.identity.hmac")
    if initialize_key:
        return {"schema": "qq-navigation-surface-n0-v1", "succeeded": True, "key_initialized": True}
    from qq_window_metadata_guest import _collect_once
    import uiautomation as auto
    metadata = _collect_once()
    windows = [row for row in metadata["windows"] if row.get("is_visible") and row.get("class_name") == "Chrome_WidgetWin_1"]
    # Read-only investigation may see both chat shell and a transient card.
    # Choosing the largest window is deliberately never an identity assertion.
    if not windows:
        return {"schema": "qq-navigation-surface-n0-v1", "succeeded": False,
            "error_code": "QQ_UI_SURFACE_UNAVAILABLE", "metadata": metadata}
    window = max(windows, key=lambda row: (row["rect"]["right"]-row["rect"]["left"])*(row["rect"]["bottom"]-row["rect"]["top"]))
    root = auto.ControlFromHandle(window["hwnd"])
    queue = [(auto.ControlFromHandle(row["hwnd"]), None, 0) for row in windows]
    nodes: list[dict] = []
    profiles: list[dict] = []
    profile_labels: dict[int, list[tuple[int, str, list[int]]]] = {}
    read_errors: list[dict] = []
    deadline = time.monotonic() + 30
    while queue:
        if len(nodes) >= 2000 or time.monotonic() >= deadline:
            raise RuntimeError("SURFACE_READ_BOUND_EXCEEDED")
        node, parent, depth = queue.pop(0)
        try:
            control_type = str(node.ControlTypeName)
            class_name = str(node.ClassName)
            rect = node.BoundingRectangle
        except Exception as exc:
            read_errors.append({"parent": parent, "depth": depth, "error_type": type(exc).__name__})
            continue
        index = len(nodes)
        # Never request Name on the chat tree. Only the two exact normal-UI
        # profile identifier Text controls are relevant to this N0 probe.
        is_uid_child = (parent is not None and nodes[parent]["class"] == "buddy-profile__header-uid"
                        and control_type == "TextControl")
        name = str(node.Name or "") if is_uid_child else ""
        rectangle = [int(getattr(rect, field)) for field in ("left", "top", "right", "bottom")]
        if is_uid_child and not node.IsOffscreen:
            profile_labels.setdefault(parent, []).append((index, name.strip(), rectangle))
        name_kind = "opaque"
        match = re.fullmatch(r"(?:QQ号|QQ ID|QQ|账号)\s*[:：]?\s*([0-9]{5,12})", name.strip())
        if match and not node.IsOffscreen:
            name_kind = "explicit_qq_id"
            profiles.append({"index": index, "profile_id_hmac": hmac.new(key, match[1].encode("ascii"), hashlib.sha256).hexdigest()})
        elif name.strip() in {"QQ号", "QQ", "QQ ID", "账号"}:
            name_kind = "qq_id_label"
        nodes.append({"index": index, "parent": parent, "depth": depth,
            "type": control_type, "class": class_name,
            "automation_id": str(node.AutomationId),
            "runtime_hash": hashlib.sha256(repr(tuple(node.GetRuntimeId() or ())).encode()).hexdigest(),
            "name_hash": hashlib.sha256(name.encode()).hexdigest(), "name_length": len(name), "name_kind": name_kind,
            "rect": rectangle,
            "offscreen": bool(node.IsOffscreen)})
        name = ""
        queue.extend((child, index, depth + 1) for child in node.GetChildren())
    for group in profile_labels.values():
        labels = [entry for entry in group if entry[1].rstrip(":：").strip() in {"QQ", "QQ号", "QQ ID", "账号"}]
        values = [entry for entry in group if re.fullmatch(r"[0-9]{5,12}", entry[1])]
        if len(labels) == 1 and len(values) == 1:
            label_rect, value_rect = labels[0][2], values[0][2]
            # QQ text glyphs may overlap by one physical pixel at the boundary.
            if value_rect[0] >= label_rect[2] - 1 and abs(value_rect[1] - label_rect[1]) <= 2:
                profiles.append({"index": values[0][0], "profile_id_hmac": hmac.new(key, values[0][1].encode("ascii"), hashlib.sha256).hexdigest()})
    profile_labels.clear()
    key = b""
    return {"schema": "qq-navigation-surface-n0-v1", "succeeded": True,
        "captured_at": datetime.now(UTC).isoformat(), "window": window, "metadata": metadata,
        "nodes": nodes, "read_errors": read_errors, "explicit_profile_candidates": profiles,
        "privacy": {"raw_names_emitted": False, "raw_qq_id_emitted": False,
                    "ui_actions_performed": False, "binding_created": False, "lease_issued": False}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--initialize-key", action="store_true")
    args = parser.parse_args()
    if args.report.parent != Path(r"C:\PMAI\data"):
        raise RuntimeError("REPORT_SCOPE_INVALID")
    try:
        result = inspect_surface(release_root=args.release_root, vault=args.vault, initialize_key=args.initialize_key)
    except Exception as exc:
        import traceback
        result = {"schema": "qq-navigation-surface-n0-v1", "succeeded": False, "error_type": type(exc).__name__,
            "error_code": str(exc) if type(exc) is RuntimeError and re.fullmatch(r"[A-Z_]+", str(exc)) else None,
            "diagnostic": [{"file": Path(row.filename).name, "function": row.name, "line": row.lineno} for row in traceback.extract_tb(exc.__traceback__)[-4:]]}
    args.report.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["succeeded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
