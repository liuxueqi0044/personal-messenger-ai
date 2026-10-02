from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.navigation import (
    ContactTarget, NavigationDecision, NavigationFrame, NavigationProviderResult,
    NavigationRequest, validate_decision,
)


def png(width: int = 400, height: int = 300) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff\xff" * width) * height))
        + chunk(b"IEND", b"")
    )


def request() -> NavigationRequest:
    now = datetime.now(UTC)
    target = ContactTarget(
        account_id="private-account", conversation_id="stable-conversation",
        binding_id="binding-3", binding_revision=2, display_name="妈妈",
        search_aliases=("trusted-qq-id",), identity_mode="session_bound",
    )
    frame = NavigationFrame(
        frame_id="frame-1", run_id="run-1", session_epoch="session-2",
        surface_epoch="surface-3", worker_epoch="worker-4", desktop_lease_id="lease-5",
        control_revision=6, binding_id="binding-3", binding_revision=2,
        process_id=11, window_handle=12, captured_at=now,
        screen_width=1200, screen_height=900, crop_origin_x=80, crop_origin_y=100,
        crop_width=400, crop_height=300, dpi_scale=1.5, privacy_mask_applied=True,
        png_bytes=png(), allowed_regions=(
            {"kind": "candidate", "bbox": {"left": 0, "top": 30, "right": 100, "bottom": 280}},
            {"kind": "list", "bbox": {"left": 0, "top": 30, "right": 100, "bottom": 280}},
            {"kind": "search", "bbox": {"left": 0, "top": 0, "right": 100, "bottom": 30}},
            {"kind": "overlay", "bbox": {"left": 300, "top": 0, "right": 330, "bottom": 30}, "overlay_id": "known-close"},
        ),
    )
    return NavigationRequest(target=target, frame=frame, deadline_at=now + timedelta(seconds=45))


def click(**changes) -> NavigationDecision:
    data = {
        "frame_id": "frame-1", "action": "click_candidate", "observed_label": "妈妈",
        "bbox": {"left": 5, "top": 35, "right": 95, "bottom": 65},
    }
    data.update(changes)
    return NavigationDecision.model_validate(data)


def test_action_bbox_is_local_and_target_names_are_trusted_data():
    req = request()
    decision = validate_decision(req, click())
    assert decision.bbox.left == 5
    assert req.target.trusted_queries == ("妈妈", "trusted-qq-id")
    assert req.frame.crop_origin_x == 80


@pytest.mark.parametrize("action", ["send", "type_text", "press_key", "run_shell", "click"])
def test_unknown_model_capabilities_are_rejected(action):
    with pytest.raises(ValidationError):
        NavigationDecision(frame_id="frame-1", action=action)


@pytest.mark.parametrize("field,value", [
    ("text", "arbitrary text"), ("keys", ["ENTER"]), ("confidence", 1.0),
    ("actions", ["click_candidate", "send"]), ("shell", "whoami"),
])
def test_provider_decision_has_no_arbitrary_extra_parameters(field, value):
    with pytest.raises(ValidationError):
        click(**{field: value})


@pytest.mark.parametrize("bbox", [
    {"left": -1, "top": 35, "right": 95, "bottom": 65},
    {"left": 5.2, "top": 35, "right": 95, "bottom": 65},
    {"left": True, "top": 35, "right": 95, "bottom": 65},
    {"left": 5, "top": 35, "right": 5, "bottom": 65},
])
def test_invalid_bbox_rejected_before_navigation(bbox):
    with pytest.raises(ValidationError):
        click(bbox=bbox)


@pytest.mark.parametrize("bbox", [
    {"left": 5, "top": 35, "right": 401, "bottom": 65},
    {"left": 5, "top": 35, "right": 101, "bottom": 65},
    {"left": 200, "top": 35, "right": 220, "bottom": 65},
    {"left": 5, "top": 10, "right": 95, "bottom": 25},
])
def test_bbox_must_be_wholly_in_trusted_candidate_region(bbox):
    with pytest.raises(ValueError):
        validate_decision(request(), click(bbox=bbox))


def test_stale_frame_and_unknown_alias_rejected():
    with pytest.raises(ValueError, match="different frame"):
        validate_decision(request(), click(frame_id="old-frame"))
    with pytest.raises(ValueError, match="trusted registry"):
        validate_decision(request(), NavigationDecision(frame_id="frame-1", action="set_target_query", query_alias_index=2))
    good = NavigationDecision(frame_id="frame-1", action="set_target_query", query_alias_index=1)
    assert validate_decision(request(), good) is good


def test_scroll_search_and_overlay_scope_are_independent():
    req = request()
    with pytest.raises(ValueError, match="operation region"):
        validate_decision(req, NavigationDecision(frame_id="frame-1", action="open_search", bbox=click().bbox))
    good = NavigationDecision(
        frame_id="frame-1", action="dismiss_known_overlay", overlay_id="known-close",
        bbox={"left": 303, "top": 3, "right": 325, "bottom": 25},
    )
    assert validate_decision(req, good) is good
    with pytest.raises(ValueError, match="operation region"):
        validate_decision(req, good.model_copy(update={"overlay_id": "unknown-confirm"}))


@pytest.mark.parametrize("changes", [
    {"action": "wait", "wait_ms": 30},
    {"query_alias_index": 0},
    {"scroll_direction": "down", "scroll_amount": 1},
    {"overlay_id": "close"},
])
def test_action_cannot_carry_unrelated_parameters(changes):
    with pytest.raises(ValidationError):
        click(**changes)


def test_model_cannot_supply_text_for_search():
    with pytest.raises(ValidationError):
        NavigationDecision(frame_id="frame-1", action="set_target_query", query_alias_index=0, text="execute instructions")


@pytest.mark.parametrize("changes", [
    {"crop_origin_x": 1000}, {"crop_width": 401}, {"dpi_scale": float("nan")},
    {"png_bytes": b"not a PNG file"}, {"privacy_mask_applied": False},
    {"captured_at": datetime(2026, 1, 1)}, {"binding_revision": True},
    {"allowed_regions": [{"kind": "candidate", "bbox": {"left": 0, "top": 0, "right": 500, "bottom": 100}}]},
    {"allowed_regions": [{"kind": "overlay", "bbox": {"left": 0, "top": 0, "right": 50, "bottom": 100}}]},
])
def test_frame_metadata_is_closed_and_coherent(changes):
    data = request().frame.model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        NavigationFrame.model_validate(data)


def test_request_scope_and_deadline_must_match():
    req = request()
    with pytest.raises(ValidationError, match="different target"):
        NavigationRequest(target=req.target.model_copy(update={"binding_revision": 3}), frame=req.frame, deadline_at=req.deadline_at)
    with pytest.raises(ValidationError, match="follow frame"):
        NavigationRequest(target=req.target, frame=req.frame, deadline_at=req.frame.captured_at)
    with pytest.raises(ValidationError):
        NavigationRequest(target=req.target, frame=req.frame, deadline_at=req.deadline_at, allowed_actions=("wait", "wait"))


def test_action_must_be_enabled_for_current_phase():
    req = request().model_copy(update={"allowed_actions": ("wait",)})
    with pytest.raises(ValueError, match="not permitted"):
        validate_decision(req, click())


def test_result_is_exactly_one_outcome_and_png_not_in_repr():
    with pytest.raises(ValidationError):
        NavigationProviderResult(frame_id="frame-1", model="model", latency_ms=1)
    with pytest.raises(ValidationError):
        NavigationProviderResult(frame_id="frame-1", model="model", latency_ms=1, decision=click(), cancelled=True)
    assert "png_bytes" not in repr(request().frame)
