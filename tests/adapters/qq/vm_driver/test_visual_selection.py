from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.models import QQConversation, QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.transport import (
    UIAUnavailable,
    WindowsUIAQQAccessibility,
    _ConversationRowRef,
    _NEUTRAL_HOVER_MARGIN,
    _encode_bgra_png,
)
from messenger_ai.adapters.qq.vm_driver.visual_selection import (
    QQ_VM_ROW_PALETTE_PROFILE as STRIP_PROFILE,
    QQ_VM_ROW_ENVIRONMENT_FINGERPRINT,
    QQ_VM_LEGACY_ROW_PALETTE_PROFILE as QQ_VM_ROW_PALETTE_PROFILE,
    ConversationSelectionActuator,
    ConversationSelectionStatus,
    ConversationRowPaletteProfile,
    DeepSeekVisualSelectionProvider,
    RowBorderSample,
    RowPaletteState,
    ScreenRect,
    VisualRowDecision,
    VisualRowFrame,
    VisualSelectionProviderResult,
    classify_row_border,
    runtime_id_digest,
    summarize_border_pixels,
)

PNG = b"\x89PNG\r\n\x1a\n"


def frame(*, suffix: bytes = b"") -> VisualRowFrame:
    width, height = 250, 64
    return VisualRowFrame(
        process_id=17,
        window_handle=23,
        conversation_internal_id="runtime:row-2",
        rect=ScreenRect(left=56, top=732, right=56 + width, bottom=732 + height),
        png_bytes=PNG + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + suffix,
    )


def window() -> QQWindow:
    return QQWindow(process_id=17, window_handle=23, class_name="Chrome_WidgetWin_1")


def conversation() -> QQConversation:
    return QQConversation(
        internal_id="runtime:row-2",
        participant_signature="uncertified:runtime:row-2",
        tree_digest="a" * 64,
    )


def selector() -> QQSelector:
    return QQSelector(
        name="conversation_item",
        control_type="Group",
        class_name_tokens=("recent-contact-item",),
        selected_class_name_token="selected",
    )


class Transport:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return {
            "output_text": json.dumps(self.output, ensure_ascii=False),
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        }


class LoopBoundTransport(Transport):
    def __init__(self, output: dict) -> None:
        super().__init__(output)
        self.loop = None
        self.closed_loop = None
        self.calls = 0

    async def create(self, **kwargs):
        current = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = current
        elif self.loop is not current:
            raise RuntimeError("visual transport event loop changed")
        self.calls += 1
        return super().create(**kwargs)

    async def aclose(self):
        self.closed_loop = asyncio.get_running_loop()


def test_visual_provider_sends_only_one_row_image_and_no_tools() -> None:
    transport = Transport(
        {
            "decision": "match",
            "observed_label": "联系人乙",
            "confidence": 0.99,
            "reason": "exact_label",
        }
    )
    provider = DeepSeekVisualSelectionProvider(
        api_key="test-key", transport=transport
    )
    result = asyncio.run(
        provider.inspect_row(
            request_for(frame())
        )
    )

    assert result.decision is not None
    assert result.decision.decision == "match"
    assert transport.kwargs["model"] == "deepseek-v4-flash-vision-exp"
    assert transport.kwargs["store"] is False
    assert transport.kwargs["tools"] == []
    content = transport.kwargs["input"][0]["content"]
    assert [item["type"] for item in content] == ["input_text", "input_image"]
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert content[1]["detail"] == "original"
    schema = transport.kwargs["text"]["format"]["schema"]
    assert set(schema["properties"]) == {
        "decision", "observed_label", "confidence", "reason"
    }
    assert "reply_text" not in json.dumps(transport.kwargs)
    assert "test-key" not in repr(transport.kwargs)


def request_for(row_frame: VisualRowFrame):
    from messenger_ai.adapters.qq.vm_driver.visual_selection import (
        VisualSelectionRequest,
    )

    return VisualSelectionRequest(
        binding_id="session-contact-2",
        target_label="联系人乙",
        frame=row_frame,
    )


def test_visual_provider_rejects_non_vision_or_undocumented_model() -> None:
    with pytest.raises(ValueError, match="requires deepseek-v4-flash-vision-exp"):
        DeepSeekVisualSelectionProvider(
            api_key="test-key", model="deepseek-flash", transport=Transport({})
        )


def test_visual_provider_reuses_one_sync_event_loop_across_worker_calls() -> None:
    transport = LoopBoundTransport(
        {
            "decision": "match",
            "observed_label": "联系人乙",
            "confidence": 0.99,
            "reason": "exact_label",
        }
    )
    provider = DeepSeekVisualSelectionProvider(
        api_key="test-key", transport=transport
    )
    actions = Actions([frame(), frame(), frame(), frame()])
    selection = ConversationSelectionActuator(
        actions=actions,
        provider=provider,
        labels={"session-contact-2": "联系人乙"},
        min_confidence=0.98,
    )

    try:
        assert call(selection).status is ConversationSelectionStatus.ACTION_ATTEMPTED
        assert call(selection).status is ConversationSelectionStatus.ACTION_ATTEMPTED
        assert transport.calls == 2
    finally:
        selection.close()
    assert transport.closed_loop is transport.loop


def test_full_screen_image_is_rejected_before_provider_call() -> None:
    with pytest.raises(ValueError, match="single row crop"):
        ScreenRect(left=0, top=0, right=2560, bottom=1429)


def test_dependency_free_bgra_encoder_produces_valid_rgba_scanline() -> None:
    png = _encode_bgra_png(
        2,
        1,
        bytes((3, 2, 1, 0, 30, 20, 10, 0)),
    )
    assert png.startswith(PNG)
    assert struct.unpack(">II", png[16:24]) == (2, 1)
    offset = 8
    idat = b""
    while offset < len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + length]
        if kind == b"IDAT":
            idat += payload
        offset += 12 + length
    assert zlib.decompress(idat) == bytes((0, 1, 2, 3, 255, 10, 20, 30, 255))


def test_row_capture_visibility_points_cover_only_the_bounded_row() -> None:
    rect = ScreenRect(left=56, top=732, right=306, bottom=796)
    points = WindowsUIAQQAccessibility._row_capture_points(rect)

    assert points == (
        (56, 732),
        (305, 732),
        (181, 764),
        (56, 795),
        (305, 795),
    )
    assert all(
        rect.left <= x < rect.right and rect.top <= y < rect.bottom
        for x, y in points
    )


class Provider:
    def __init__(self, decision: VisualRowDecision | None = None, *, error=False) -> None:
        self.decision = decision
        self.error = error
        self.requests = []

    async def inspect_row(self, request):
        self.requests.append(request)
        if self.error:
            from messenger_ai.llm.models import ProviderError

            return VisualSelectionProviderResult(
                model="deepseek-v4-flash-vision-exp",
                latency_ms=7,
                error=ProviderError(
                    category="timeout", message="timeout", retryable=True
                ),
            )
        return VisualSelectionProviderResult(
            decision=self.decision,
            model="deepseek-v4-flash-vision-exp",
            latency_ms=7,
        )


class SequencedProvider(Provider):
    def __init__(self, decisions: list[VisualRowDecision]) -> None:
        super().__init__()
        self.decisions = list(decisions)

    async def inspect_row(self, request):
        self.requests.append(request)
        return VisualSelectionProviderResult(
            decision=self.decisions.pop(0),
            model="deepseek-v4-flash-vision-exp",
            latency_ms=7,
        )


class Actions:
    def __init__(self, frames, *, selected=False, click_error=False) -> None:
        self.frames = list(frames)
        self.selected = selected
        self.click_error = click_error
        self.capture_calls = 0
        self.click_calls = 0

    def is_conversation_selected(self, *_args):
        return self.selected

    def capture_conversation_row(self, *_args):
        value = self.frames[self.capture_calls]
        self.capture_calls += 1
        return value

    def click_conversation_row(self, _window, _conversation, _selector, expected_rect):
        self.click_calls += 1
        assert expected_rect == self.frames[-1].rect
        if self.click_error:
            raise RuntimeError("input completion is unknown")
        return True


def actuator(actions: Actions, provider: Provider) -> ConversationSelectionActuator:
    return ConversationSelectionActuator(
        actions=actions,
        provider=provider,
        labels={"session-contact-2": "联系人乙"},
        min_confidence=0.98,
    )


def call(actuator_value: ConversationSelectionActuator):
    return actuator_value.select(
        window=window(),
        binding_id="session-contact-2",
        conversation=conversation(),
        selector=selector(),
    )


def exact_match(*, confidence=0.99, label="联系人乙") -> VisualRowDecision:
    return VisualRowDecision(
        decision="match",
        observed_label=label,
        confidence=confidence,
        reason="exact_label",
    )


def test_already_selected_never_captures_calls_model_or_clicks() -> None:
    actions = Actions([], selected=True)
    provider = Provider(exact_match())
    outcome = call(actuator(actions, provider))
    assert outcome.status is ConversationSelectionStatus.NOT_NEEDED
    assert actions.capture_calls == actions.click_calls == 0
    assert provider.requests == []


def test_exact_stable_visual_match_attempts_one_row_click() -> None:
    row = frame()
    actions = Actions([row, row])
    provider = Provider(exact_match())
    outcome = call(actuator(actions, provider))
    assert outcome.status is ConversationSelectionStatus.ACTION_ATTEMPTED
    assert outcome.frame_sha256 == row.sha256
    assert outcome.visual_decision == "match"
    assert outcome.visual_reason == "exact_label"
    assert outcome.visual_confidence == 0.99
    assert outcome.normalized_label_match is True
    assert actions.capture_calls == 2
    assert actions.click_calls == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "decision",
    [
        VisualRowDecision(
            decision="not_match",
            observed_label="联系人丙",
            confidence=1,
            reason="different_label",
        ),
        exact_match(confidence=0.97),
        exact_match(label="联系人乙 2"),
    ],
)
def test_uncertified_visual_result_never_clicks(decision) -> None:
    row = frame()
    actions = Actions([row, row])
    outcome = call(actuator(actions, Provider(decision)))
    assert outcome.status is ConversationSelectionStatus.REJECTED
    assert outcome.error_code == "visual_target_not_certified"
    assert outcome.visual_decision == decision.decision
    assert outcome.visual_reason == decision.reason
    assert outcome.visual_confidence == decision.confidence
    assert outcome.normalized_label_match is (
        decision.observed_label == "联系人乙"
    )
    assert actions.click_calls == 0
    assert actions.capture_calls == 2


def test_uncertified_first_reviewed_match_attempts_exactly_one_click() -> None:
    row = frame()
    actions = Actions([row, row, row])
    provider = SequencedProvider(
        [
            VisualRowDecision(
                decision="not_match",
                observed_label="联系人丙",
                confidence=1,
                reason="different_label",
            ),
            exact_match(),
        ]
    )

    outcome = call(actuator(actions, provider))

    assert outcome.status is ConversationSelectionStatus.ACTION_ATTEMPTED
    assert actions.capture_calls == 3
    assert actions.click_calls == 1
    assert len(provider.requests) == 2


def test_two_uncertified_decisions_are_rejected_without_clicking() -> None:
    row = frame()
    actions = Actions([row, row])
    provider = SequencedProvider(
        [
            exact_match(confidence=0.97),
            exact_match(label="联系人乙 2"),
        ]
    )

    outcome = call(actuator(actions, provider))

    assert outcome.status is ConversationSelectionStatus.REJECTED
    assert outcome.error_code == "visual_target_not_certified"
    assert actions.capture_calls == 2
    assert actions.click_calls == 0
    assert len(provider.requests) == 2


def test_row_change_before_uncertified_review_stops_without_second_provider_call() -> None:
    actions = Actions([frame(), frame(suffix=b"changed")])
    provider = SequencedProvider(
        [
            VisualRowDecision(
                decision="not_match",
                observed_label="联系人丙",
                confidence=1,
                reason="different_label",
            ),
            exact_match(),
        ]
    )

    outcome = call(actuator(actions, provider))

    assert outcome.status is ConversationSelectionStatus.REJECTED
    assert outcome.error_code == "visual_row_changed_before_action"
    assert actions.click_calls == 0
    assert len(provider.requests) == 1


def test_row_change_after_uncertified_review_before_click_never_clicks() -> None:
    row = frame()
    actions = Actions([row, row, frame(suffix=b"changed")])
    provider = SequencedProvider(
        [
            exact_match(confidence=0.97),
            exact_match(),
        ]
    )

    outcome = call(actuator(actions, provider))

    assert outcome.status is ConversationSelectionStatus.REJECTED
    assert outcome.error_code == "visual_row_changed_before_action"
    assert actions.capture_calls == 3
    assert actions.click_calls == 0
    assert len(provider.requests) == 2


def test_changed_row_after_model_response_never_clicks() -> None:
    actions = Actions([frame(), frame(suffix=b"changed")])
    outcome = call(actuator(actions, Provider(exact_match())))
    assert outcome.status is ConversationSelectionStatus.REJECTED
    assert outcome.error_code == "visual_row_changed_before_action"
    assert actions.click_calls == 0


def test_click_port_failure_is_conservatively_action_attempted() -> None:
    row = frame()
    actions = Actions([row, row], click_error=True)

    outcome = call(actuator(actions, Provider(exact_match())))

    assert outcome.status is ConversationSelectionStatus.ACTION_ATTEMPTED
    assert outcome.error_code == "visual_action_outcome_unknown"
    assert outcome.frame_sha256 == row.sha256
    assert actions.click_calls == 1


def test_provider_failure_never_clicks() -> None:
    actions = Actions([frame()])
    outcome = call(actuator(actions, Provider(error=True)))
    assert outcome.status is ConversationSelectionStatus.REJECTED
    assert outcome.error_code == "visual_provider_failed"
    assert outcome.provider_error_category == "timeout"
    assert actions.click_calls == 0


def call_with_deadline(
    actuator_value: ConversationSelectionActuator, deadline: datetime
):
    return actuator_value.select(
        window=window(),
        binding_id="session-contact-2",
        conversation=conversation(),
        selector=selector(),
        deadline=deadline,
    )


class DeadlineBurningProvider(Provider):
    """A provider that makes the wall-clock deadline expire before it returns.

    The vision call is the slow, externally-visible stage: this model simply
    keeps working until the passed deadline is already in the past, then
    returns a fully valid *match*.  A correct actuator must discard that
    certification instead of clicking an already-stale row.
    """

    def __init__(self, decision: VisualRowDecision, *, deadline: datetime) -> None:
        super().__init__(decision)
        self.deadline = deadline

    async def inspect_row(self, request):
        while datetime.now(UTC) < self.deadline:
            await asyncio.sleep(0.005)
        return await super().inspect_row(request)


class DeadlineBurningActions(Actions):
    """Actions whose post-provider stability capture burns the time budget."""

    def __init__(self, frames, *, deadline: datetime) -> None:
        super().__init__(frames)
        self.deadline = deadline

    def capture_conversation_row(self, *args):
        value = super().capture_conversation_row(*args)
        if self.capture_calls == 2:
            while datetime.now(UTC) < self.deadline:
                time.sleep(0.005)
        return value


def test_expired_deadline_is_rejected_before_any_capture_or_provider() -> None:
    actions = Actions([frame(), frame()])
    provider = Provider(exact_match())

    with pytest.raises(RuntimeError, match="deadline_expired"):
        call_with_deadline(
            actuator(actions, provider), datetime.now(UTC) - timedelta(seconds=1)
        )

    assert actions.capture_calls == 0
    assert actions.click_calls == 0
    assert provider.requests == []


def test_provider_that_expires_the_deadline_never_clicks() -> None:
    row = frame()
    actions = Actions([row, row])
    deadline = datetime.now(UTC) + timedelta(milliseconds=400)
    provider = DeadlineBurningProvider(exact_match(), deadline=deadline)

    with pytest.raises(RuntimeError, match="deadline_expired"):
        call_with_deadline(actuator(actions, provider), deadline)

    assert actions.click_calls == 0
    assert actions.capture_calls == 1
    assert len(provider.requests) == 1


def test_deadline_expiring_before_the_click_never_clicks() -> None:
    row = frame()
    deadline = datetime.now(UTC) + timedelta(milliseconds=400)
    actions = DeadlineBurningActions([row, row], deadline=deadline)
    provider = Provider(exact_match())

    with pytest.raises(RuntimeError, match="deadline_expired"):
        call_with_deadline(actuator(actions, provider), deadline)

    assert actions.capture_calls == 2
    assert actions.click_calls == 0


def test_deadline_expiring_during_uncertified_review_never_clicks() -> None:
    row = frame()
    deadline = datetime.now(UTC) + timedelta(milliseconds=400)
    actions = DeadlineBurningActions([row, row], deadline=deadline)
    provider = SequencedProvider([exact_match(confidence=0.97), exact_match()])

    with pytest.raises(RuntimeError, match="deadline_expired"):
        call_with_deadline(actuator(actions, provider), deadline)

    assert actions.capture_calls == 2
    assert actions.click_calls == 0
    assert len(provider.requests) == 1


def test_naive_deadline_is_rejected() -> None:
    actions = Actions([frame(), frame()])
    provider = Provider(exact_match())

    with pytest.raises(ValueError, match="timezone-aware"):
        call_with_deadline(actuator(actions, provider), datetime(2026, 1, 1))

    assert actions.capture_calls == 0
    assert actions.click_calls == 0
    assert provider.requests == []


# ---------------------------------------------------------------------------
# Deterministic local row-border palette proof.
#
# Every Windows API call is replaced by an in-process fake so the whole flow
# runs on the host: no guest, no UIA tree and no real screen capture is needed.
# ---------------------------------------------------------------------------

ROW_WIDTH, ROW_HEIGHT, ROW_INSET = 250, 64, 10
# Retain all v1 regression cases. The deployed v2 mask has separate raw-pixel
# and full transport cases below, including the real antialiasing failure.
SELECTED_RGB = (225, 225, 225)
HOVER_RGB = (235, 235, 235)
UNSELECTED_RGB = (245, 245, 245)
_FIELD_BAND_PIXELS = 5880
_FIELD_DOMINANT_PIXELS = 4584


def _row_rect(top: int, left: int = 56) -> ScreenRect:
    return ScreenRect(left=left, top=top, right=left + ROW_WIDTH, bottom=top + ROW_HEIGHT)


def _distinct_colours(base: tuple[int, int, int], count: int) -> list[tuple[int, int, int]]:
    return [
        ((base[0] + 2 + index) % 256, (base[1] + 3 + index) % 256, (base[2] + 5 + index) % 256)
        for index in range(count)
    ]


def _row_frame(
    dominant: tuple[int, int, int],
    *,
    others: list[tuple[int, int, int]] = (),
    dominant_count: int | None = None,
) -> bytes:
    """Raw top-down BGRA for one 250x64 row with a fully controlled border band."""

    grid = [[(0, 0, 0)] * ROW_WIDTH for _ in range(ROW_HEIGHT)]
    band: list[tuple[int, int]] = []
    for y in range(ROW_HEIGHT):
        for x in range(ROW_WIDTH):
            if ROW_INSET <= x < ROW_WIDTH - ROW_INSET and ROW_INSET <= y < ROW_HEIGHT - ROW_INSET:
                continue
            band.append((x, y))
    total = len(band)
    count = total if dominant_count is None else dominant_count
    palette = [dominant] * count
    for index in range(total - count):
        palette.append(others[index % len(others)] if others else (1, 2, 3))
    for (x, y), colour in zip(band, palette):
        grid[y][x] = colour
    bgra = bytearray()
    for y in range(ROW_HEIGHT):
        for x in range(ROW_WIDTH):
            red, green, blue = grid[y][x]
            bgra += bytes((blue, green, red, 255))
    return bytes(bgra)


SELECTED_FRAME = _row_frame(
    SELECTED_RGB, others=_distinct_colours(SELECTED_RGB, 11), dominant_count=_FIELD_DOMINANT_PIXELS
)
HOVER_FRAME = _row_frame(
    HOVER_RGB, others=_distinct_colours(HOVER_RGB, 7), dominant_count=_FIELD_DOMINANT_PIXELS
)
UNSELECTED_FRAME = _row_frame(UNSELECTED_RGB)
# A second selected sample whose dominant count drifts by one pixel: still the
# selected palette, but a different sample, so it can never stabilize.
SELECTED_DRIFT_FRAME = _row_frame(
    SELECTED_RGB, others=_distinct_colours(SELECTED_RGB, 11), dominant_count=_FIELD_DOMINANT_PIXELS - 1
)

FAST_PROFILE = QQ_VM_ROW_PALETTE_PROFILE.model_copy(
    update={"poll_interval_seconds": 0.0, "hover_settle_seconds": 0.0}
)


def _row(internal_id: str, top: int) -> _ConversationRowRef:
    return _ConversationRowRef(internal_id=internal_id, item=object(), rect=_row_rect(top))


def _conversation_for(internal_id: str) -> QQConversation:
    return QQConversation(
        internal_id=internal_id,
        participant_signature="uncertified-target",
        tree_digest="a" * 64,
    )


def _capture_by_row(mapping: dict[int, bytes]):
    return lambda _window, rect: mapping[rect.top]


class _SequencedCapture:
    """Serve one planned frame map per sampling round."""

    def __init__(self, row_count: int, plans: list[dict[int, bytes]]) -> None:
        self._row_count = row_count
        self._plans = list(plans)
        self._sample = 0
        self._served = 0

    def __call__(self, _window, rect: ScreenRect) -> bytes:
        plan = self._plans[min(self._sample, len(self._plans) - 1)]
        frame_bytes = plan[rect.top]
        self._served += 1
        if self._served % self._row_count == 0:
            self._sample += 1
        return frame_bytes


def _fake_access(
    *,
    rows,
    capture,
    bounds=(0, 0, 1600, 1000),
    screen=(0, 0, 1600, 1000),
):
    moves: list[tuple[int, int]] = []
    access = object.__new__(WindowsUIAQQAccessibility)
    access.ensure_guest_foreground = lambda _window, timeout_seconds=2.0: None
    access._visible_conversation_rows = (
        rows if callable(rows) else (lambda _window, _selector: list(rows))
    )
    access._window_screen_bounds = (
        bounds if callable(bounds) else (lambda _window: bounds)
    )
    access._virtual_screen_metrics = lambda: screen
    access._point_belongs_to_window = lambda _window, _x, _y: True
    access._send_guest_mouse_move = lambda x, y: moves.append((x, y))
    access._capture_row_bgra = capture
    return access, moves


def _certify(access, target: str, profile=FAST_PROFILE, *, deadline=None):
    return access.certify_conversation_selected_visual(
        window(),
        _conversation_for(target),
        selector(),
        profile,
        deadline=deadline,
    )


def test_field_calibrated_profile_is_frozen_and_versioned() -> None:
    profile = QQ_VM_ROW_PALETTE_PROFILE

    assert profile.client_version == "9.9.33.51802"
    assert (profile.row_width, profile.row_height, profile.border_inset) == (250, 64, 10)
    assert profile.selected.dominant_rgb == SELECTED_RGB
    assert profile.selected.ratio == pytest.approx(0.779592)
    assert profile.selected.unique_count == 12
    assert profile.hover.dominant_rgb == HOVER_RGB
    assert profile.hover.unique_count == 8
    assert profile.unselected.dominant_rgb == UNSELECTED_RGB
    assert (profile.unselected.ratio, profile.unselected.unique_count) == (1.0, 1)
    assert profile.channel_tolerance <= 2
    assert profile.environment_fingerprint == QQ_VM_ROW_ENVIRONMENT_FINGERPRINT
    assert len(profile.environment_fingerprint) == 64
    assert profile.stable_samples == 2
    with pytest.raises(ValidationError):
        profile.selected = RowPaletteState(dominant_rgb=(1, 1, 1), ratio=1.0, unique_count=1)


def test_profile_rejects_indistinguishable_palette_states() -> None:
    payload = QQ_VM_ROW_PALETTE_PROFILE.model_dump()
    payload["hover"]["dominant_rgb"] = (226, 226, 226)

    with pytest.raises(ValidationError, match="distinguishable"):
        ConversationRowPaletteProfile(**payload)


def test_border_band_summary_reproduces_the_recorded_field_numbers() -> None:
    sample = summarize_border_pixels(SELECTED_FRAME, width=ROW_WIDTH, height=ROW_HEIGHT, inset=ROW_INSET)

    assert sample.pixel_count == _FIELD_BAND_PIXELS
    assert sample.dominant_count == _FIELD_DOMINANT_PIXELS
    assert sample.ratio == pytest.approx(0.779592, abs=1e-6)
    assert sample.unique_count == 12
    assert sample.dominant_rgb == SELECTED_RGB


def test_pure_palette_classification_separates_all_field_states() -> None:
    states = {
        name: classify_row_border(
            summarize_border_pixels(pixels, width=ROW_WIDTH, height=ROW_HEIGHT, inset=ROW_INSET),
            QQ_VM_ROW_PALETTE_PROFILE,
        )
        for name, pixels in (
            ("selected", SELECTED_FRAME),
            ("hover", HOVER_FRAME),
            ("unselected", UNSELECTED_FRAME),
        )
    }
    unknown = classify_row_border(
        RowBorderSample(
            pixel_count=_FIELD_BAND_PIXELS,
            dominant_rgb=(200, 200, 200),
            dominant_count=_FIELD_DOMINANT_PIXELS,
            ratio=0.779592,
            unique_count=12,
        ),
        QQ_VM_ROW_PALETTE_PROFILE,
    )
    near_miss = classify_row_border(
        RowBorderSample(
            pixel_count=_FIELD_BAND_PIXELS,
            dominant_rgb=SELECTED_RGB,
            dominant_count=_FIELD_DOMINANT_PIXELS,
            ratio=0.5,
            unique_count=12,
        ),
        QQ_VM_ROW_PALETTE_PROFILE,
    )

    assert states == {"selected": "selected", "hover": "hover", "unselected": "unselected"}
    assert unknown == "unknown"
    assert near_miss == "unknown"


def test_two_consecutive_identical_samples_certify_the_exact_target_row() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    capture = _capture_by_row({100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME})
    access, moves = _fake_access(rows=rows, capture=capture)

    attestation = _certify(access, target)

    assert attestation.sample_count == 2
    assert attestation.stable_sample_count == 2
    assert attestation.profile_id == QQ_VM_ROW_PALETTE_PROFILE.profile_id
    assert attestation.client_version == "9.9.33.51802"
    assert (attestation.process_id, attestation.window_handle) == (17, 23)
    assert attestation.target_runtime_id_digest == runtime_id_digest(target)
    assert attestation.row_rect == _row_rect(200)
    assert attestation.selected.dominant_rgb == SELECTED_RGB
    assert attestation.selected.unique_count == 12
    assert attestation.selected.pixel_count == _FIELD_BAND_PIXELS
    assert attestation.unselected.dominant_rgb == UNSELECTED_RGB
    assert attestation.unselected_control_count == 2
    assert len(moves) == 2


def test_partially_clipped_non_target_row_is_excluded_from_certification() -> None:
    target = "runtime:target"
    rows = [
        _row("runtime:control-a", 100),
        _row(target, 200),
        _row("runtime:control-b", 300),
        _row("runtime:clipped", 950),
    ]
    capture = _capture_by_row({
        100: UNSELECTED_FRAME,
        200: SELECTED_FRAME,
        300: UNSELECTED_FRAME,
    })
    access, _moves = _fake_access(rows=rows, capture=capture)

    attestation = _certify(access, target)

    assert attestation.unselected_control_count == 2
    assert attestation.row_rect == _row_rect(200)


def test_hover_row_is_explicitly_rejected_fail_closed() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    capture = _capture_by_row({100: UNSELECTED_FRAME, 200: HOVER_FRAME, 300: UNSELECTED_FRAME})
    access, _moves = _fake_access(rows=rows, capture=capture)

    with pytest.raises(UIAUnavailable, match="hover"):
        _certify(access, target)


def test_target_must_be_the_only_selected_row() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    both_selected = _capture_by_row(
        {100: SELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
    )
    access, _moves = _fake_access(rows=rows, capture=both_selected)
    with pytest.raises(UIAUnavailable, match="more than one conversation row"):
        _certify(access, target)

    other_selected = _capture_by_row(
        {100: SELECTED_FRAME, 200: UNSELECTED_FRAME, 300: UNSELECTED_FRAME}
    )
    access, _moves = _fake_access(rows=rows, capture=other_selected)
    with pytest.raises(UIAUnavailable, match="different conversation row"):
        _certify(access, target)


def test_target_row_that_never_becomes_selected_fails_closed() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    capture = _capture_by_row({100: UNSELECTED_FRAME, 200: UNSELECTED_FRAME, 300: UNSELECTED_FRAME})
    access, _moves = _fake_access(rows=rows, capture=capture)
    bounded = FAST_PROFILE.model_copy(update={"max_samples": 3})

    with pytest.raises(UIAUnavailable, match="exhausted"):
        _certify(access, target, bounded)


def test_geometry_change_is_rejected_before_any_sample() -> None:
    target = "runtime:target"
    rows = [
        _row("runtime:control-a", 100),
        _ConversationRowRef(
            internal_id=target,
            item=object(),
            rect=ScreenRect(left=56, top=200, right=296, bottom=264),
        ),
        _row("runtime:control-b", 300),
    ]
    access, _moves = _fake_access(
        rows=rows, capture=_capture_by_row({100: UNSELECTED_FRAME, 300: UNSELECTED_FRAME})
    )

    with pytest.raises(UIAUnavailable, match="geometry does not match"):
        _certify(access, target)


# A maximized QQ window reports a ``GetWindowRect`` inflated by its invisible
# resize border, so raw window corners sit off the visible guest desktop even
# though ``WindowFromPoint`` still resolves them to the QQ HWND.
MAXIMIZED_WINDOW_BOUNDS = (-8, -8, 1608, 1008)
MAXIMIZED_VIRTUAL_SCREEN = (0, 0, 1600, 1000)


def _virtual_screen_guard(screen):
    """Reproduce the fail-closed virtual-screen check in the mouse mover."""

    vx, vy, width, height = screen
    moves: list[tuple[int, int]] = []

    def move(x: int, y: int) -> None:
        if width <= 1 or height <= 1 or not (
            vx <= x < vx + width and vy <= y < vy + height
        ):
            raise UIAUnavailable(
                "neutral hover point is outside the guest virtual screen"
            )
        moves.append((x, y))

    return move, moves


def test_maximized_window_certifies_without_leaving_the_virtual_screen() -> None:
    # Regression for r20260914-01: every neutral candidate of a maximized
    # window sat off the guest desktop, so certification failed closed with
    # "neutral_point_outside_screen" before a single sample was taken.
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
        ),
        bounds=MAXIMIZED_WINDOW_BOUNDS,
        screen=MAXIMIZED_VIRTUAL_SCREEN,
    )
    access._point_belongs_to_window = lambda _window, x, y: (
        MAXIMIZED_WINDOW_BOUNDS[0] <= x < MAXIMIZED_WINDOW_BOUNDS[2]
        and MAXIMIZED_WINDOW_BOUNDS[1] <= y < MAXIMIZED_WINDOW_BOUNDS[3]
    )
    move, moves = _virtual_screen_guard(MAXIMIZED_VIRTUAL_SCREEN)
    access._send_guest_mouse_move = move

    attestation = _certify(access, target)

    assert attestation.sample_count == 2
    assert len(moves) == 2
    assert all(0 <= x < 1600 and 0 <= y < 1000 for x, y in moves)


def test_neutral_hover_point_clamps_to_the_visible_guest_desktop() -> None:
    rows = [_row("runtime:control-a", 100), _row("runtime:target", 200)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row({}),
        bounds=MAXIMIZED_WINDOW_BOUNDS,
        screen=MAXIMIZED_VIRTUAL_SCREEN,
    )
    access._point_belongs_to_window = lambda _window, x, y: (
        MAXIMIZED_WINDOW_BOUNDS[0] <= x < MAXIMIZED_WINDOW_BOUNDS[2]
        and MAXIMIZED_WINDOW_BOUNDS[1] <= y < MAXIMIZED_WINDOW_BOUNDS[3]
    )

    assert access._neutral_hover_point(
        window(), rows, MAXIMIZED_WINDOW_BOUNDS
    ) == (1597, 2)


def test_neutral_hover_point_skips_candidates_near_any_conversation_row() -> None:
    rows = [
        _ConversationRowRef(
            "runtime:target", object(), ScreenRect(left=0, top=0, right=250, bottom=64)
        ),
        _ConversationRowRef(
            "runtime:control-a", object(), ScreenRect(left=150, top=0, right=400, bottom=64)
        ),
        _ConversationRowRef(
            "runtime:control-b", object(), ScreenRect(left=0, top=150, right=250, bottom=214)
        ),
    ]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row({}),
        bounds=(0, 0, 400, 200),
        screen=(0, 0, 400, 200),
    )
    access._point_belongs_to_window = lambda _window, x, y: 0 <= x < 400 and 0 <= y < 200

    point = access._neutral_hover_point(window(), rows, (0, 0, 400, 200))

    assert point == (397, 197)
    x, y = point
    assert 0 <= x < 400 and 0 <= y < 200
    for ref in rows:
        rect = ref.rect
        assert not (
            rect.left - _NEUTRAL_HOVER_MARGIN <= x < rect.right + _NEUTRAL_HOVER_MARGIN
            and rect.top - _NEUTRAL_HOVER_MARGIN <= y < rect.bottom + _NEUTRAL_HOVER_MARGIN
        )


def test_neutral_hover_point_fails_closed_when_every_candidate_is_blocked() -> None:
    rows = [
        _ConversationRowRef(
            "runtime:target", object(), ScreenRect(left=0, top=0, right=398, bottom=198)
        )
    ]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row({}),
        bounds=(0, 0, 400, 200),
        screen=(0, 0, 400, 200),
    )
    access._point_belongs_to_window = lambda _window, _x, _y: True

    with pytest.raises(UIAUnavailable, match="no neutral hover point"):
        access._neutral_hover_point(window(), rows, (0, 0, 400, 200))


def test_certify_fails_closed_without_touching_the_pointer_when_screen_is_unknown() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    access, moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
        ),
        screen=(0, 0, 0, 0),
    )

    with pytest.raises(UIAUnavailable, match="no neutral hover point"):
        _certify(access, target)

    assert moves == []


def test_window_change_mid_poll_is_rejected() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    bounds = [(0, 0, 1600, 1000), (0, 0, 1600, 1000), (0, 40, 1600, 1040)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
        ),
        bounds=lambda _window: bounds.pop(0),
    )

    with pytest.raises(UIAUnavailable, match="window changed"):
        _certify(access, target)


def test_row_geometry_drift_after_the_first_sample_is_rejected() -> None:
    target = "runtime:target"
    stable = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    drifted = [
        _row("runtime:control-a", 100),
        _row(target, 200),
        _row("runtime:control-b", 304),
    ]
    calls = {"count": 0}

    def rows(_window, _selector):
        calls["count"] += 1
        return list(stable) if calls["count"] == 1 else drifted

    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME, 304: UNSELECTED_FRAME}
        ),
    )

    with pytest.raises(UIAUnavailable, match="geometry changed"):
        _certify(access, target)


def test_bounded_poll_waits_for_an_asynchronous_switch() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    capture = _SequencedCapture(
        3,
        [
            {100: UNSELECTED_FRAME, 200: UNSELECTED_FRAME, 300: UNSELECTED_FRAME},
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME},
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME},
        ],
    )
    access, _moves = _fake_access(rows=rows, capture=capture)

    attestation = _certify(access, target)

    assert attestation.sample_count == 3
    assert attestation.stable_sample_count == 2


def test_unstable_sampling_never_certifies() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    selected = {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
    drifted = {100: UNSELECTED_FRAME, 200: SELECTED_DRIFT_FRAME, 300: UNSELECTED_FRAME}
    alternating = _SequencedCapture(
        3,
        [selected, drifted, selected, drifted],
    )
    access, _moves = _fake_access(rows=rows, capture=alternating)
    bounded = FAST_PROFILE.model_copy(update={"max_samples": 4})

    with pytest.raises(UIAUnavailable, match="exhausted"):
        _certify(access, target, bounded)


def test_too_few_unselected_control_rows_is_rejected() -> None:
    target = "runtime:target"
    rows = [_row(target, 200), _row("runtime:control-a", 300)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row({200: SELECTED_FRAME, 300: UNSELECTED_FRAME}),
    )

    with pytest.raises(UIAUnavailable, match="too few unselected control rows"):
        _certify(access, target)


def test_expired_or_naive_deadline_fails_closed() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
        ),
    )

    with pytest.raises(RuntimeError, match="deadline_expired"):
        _certify(access, target, deadline=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        _certify(access, target, deadline=datetime(2026, 1, 1))


def test_deadline_expiry_during_the_poll_fails_closed() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: UNSELECTED_FRAME, 300: UNSELECTED_FRAME}
        ),
    )
    bounded = FAST_PROFILE.model_copy(
        update={"max_samples": 120, "poll_interval_seconds": 0.005}
    )

    with pytest.raises(RuntimeError, match="deadline_expired"):
        _certify(
            access,
            target,
            bounded,
            deadline=datetime.now(UTC) + timedelta(milliseconds=50),
        )


def test_attestation_never_carries_png_or_row_content() -> None:
    target = "runtime:target"
    rows = [_row("runtime:control-a", 100), _row(target, 200), _row("runtime:control-b", 300)]
    access, _moves = _fake_access(
        rows=rows,
        capture=_capture_by_row(
            {100: UNSELECTED_FRAME, 200: SELECTED_FRAME, 300: UNSELECTED_FRAME}
        ),
    )

    attestation = _certify(access, target)
    payload = attestation.model_dump()

    forbidden = ("png", "text", "title", "label", "message", "name", "image", "byte")
    keys: list[str] = []

    def _walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                keys.append(str(key))
                _walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(payload)
    assert keys
    assert not any(token in key.lower() for token in forbidden for key in keys)
    assert "png" not in json.dumps(payload)
    assert not any(isinstance(value, (bytes, bytearray)) for value in payload.values())
    assert set(payload) == {
        "schema_version",
        "profile_id",
        "client_version",
        "selector_pack_version",
        "environment_fingerprint",
        "process_id",
        "window_handle",
        "target_runtime_id_digest",
        "row_rect",
        "sample_count",
        "stable_sample_count",
        "unselected_control_count",
        "selected",
        "unselected",
    }


def _strip_frame(rgb, *, corner_variants=14, bottom_rgb=None):
    """Spatial row fixture: two clear background strips and noisy excluded pixels."""
    pixels = bytearray(ROW_WIDTH * ROW_HEIGHT * 4)
    for y in range(ROW_HEIGHT):
        for x in range(ROW_WIDTH):
            # Changing corner antialiasing and content must not affect selection.
            value = (x * 17 + y * 31) % corner_variants
            color = (value, value + 1, value + 2)
            if 16 <= x < 234 and (4 <= y < 8 or 56 <= y < 60):
                color = bottom_rgb if y >= 56 and bottom_rgb is not None else rgb
            offset = (y * ROW_WIDTH + x) * 4
            pixels[offset:offset + 4] = bytes((*reversed(color), 255))
    return bytes(pixels)


def _strip_summary(frame):
    return summarize_border_pixels(frame, width=250, height=64, inset=10,
                                   sampling_mask=STRIP_PROFILE.sampling_mask)


@pytest.mark.parametrize("variants", [12, 14, 128])
@pytest.mark.parametrize("rgb,state", [(SELECTED_RGB, "selected"), (HOVER_RGB, "hover"),
                                       (UNSELECTED_RGB, "unselected")])
def test_v2_strip_mask_ignores_corner_antialiasing_and_content(variants, rgb, state):
    sample = _strip_summary(_strip_frame(rgb, corner_variants=variants))
    assert (sample.pixel_count, sample.dominant_count, sample.unique_count) == (1744, 1744, 1)
    assert sample.ratio == 1
    assert classify_row_border(sample, STRIP_PROFILE) == state


@pytest.mark.parametrize("bottom", [HOVER_RGB, UNSELECTED_RGB, (100, 100, 100)])
def test_v2_disagreeing_top_and_bottom_strips_fail_closed(bottom):
    sample = _strip_summary(_strip_frame(SELECTED_RGB, bottom_rgb=bottom))
    assert classify_row_border(sample, STRIP_PROFILE) == "unknown"


def test_v2_single_contaminated_sample_pixel_fails_closed():
    pixels = bytearray(_strip_frame(SELECTED_RGB))
    offset = (4 * ROW_WIDTH + 16) * 4
    pixels[offset:offset + 4] = bytes((224, 224, 224, 255))
    assert classify_row_border(_strip_summary(pixels), STRIP_PROFILE) == "unknown"


def test_v2_wrong_mask_size_and_legacy_samples_cannot_certify():
    with pytest.raises(ValueError, match="250x64"):
        summarize_border_pixels(bytes(249 * 64 * 4), width=249, height=64, inset=10,
                                sampling_mask=STRIP_PROFILE.sampling_mask)
    with pytest.raises(ValidationError, match="250x64"):
        ConversationRowPaletteProfile.model_validate({**STRIP_PROFILE.model_dump(), "row_width":249})
    legacy = summarize_border_pixels(SELECTED_FRAME, width=250, height=64, inset=10)
    assert classify_row_border(legacy, STRIP_PROFILE) == "unknown"
    assert STRIP_PROFILE.profile_id != QQ_VM_ROW_PALETTE_PROFILE.profile_id


def test_v2_real_transport_requires_two_stable_samples_with_exact_target():
    target = "runtime:target"
    rows = [_row("runtime:a", 100), _row(target, 200), _row("runtime:b", 300)]
    plans = [dict(zip((100, 200, 300), (_strip_frame(UNSELECTED_RGB),
                                     _strip_frame(SELECTED_RGB, corner_variants=n),
                                     _strip_frame(UNSELECTED_RGB)))) for n in (12, 14)]
    access, moves = _fake_access(rows=rows, capture=_SequencedCapture(3, plans))
    profile = STRIP_PROFILE.model_copy(update={"hover_settle_seconds":0, "poll_interval_seconds":0})
    attestation = _certify(access, target, profile)
    assert attestation.profile_id == STRIP_PROFILE.profile_id
    assert attestation.selected.pixel_count == 1744
    assert attestation.stable_sample_count == 2
    assert attestation.unselected_control_count == 2
    assert len(moves) == 2


@pytest.mark.parametrize("colors,reason", [
    ((UNSELECTED_RGB, HOVER_RGB, UNSELECTED_RGB), "hover"),
    ((SELECTED_RGB, SELECTED_RGB, UNSELECTED_RGB), "more than one"),
    ((SELECTED_RGB, UNSELECTED_RGB, UNSELECTED_RGB), "different conversation"),
    ((UNSELECTED_RGB, (220, 220, 220), UNSELECTED_RGB), "unrecognized"),
])
def test_v2_transport_rejects_hover_ambiguity_and_unknown_background(colors, reason):
    target = "runtime:target"
    rows = [_row("runtime:a", 100), _row(target, 200), _row("runtime:b", 300)]
    access, _ = _fake_access(rows=rows, capture=_capture_by_row(
        {y:_strip_frame(color) for y,color in zip((100,200,300), colors)}))
    profile = STRIP_PROFILE.model_copy(update={"hover_settle_seconds":0, "poll_interval_seconds":0})
    with pytest.raises(UIAUnavailable, match=reason):
        _certify(access, target, profile)


def _transient_enumerator(*, error_code=-2147220991, always_fail=False, active_phase=None):
    access = object.__new__(WindowsUIAQQAccessibility)
    roots = []
    def fresh_root(_window):
        root = object()
        roots.append(root)
        return root
    def select_rows(root, _selector):
        if always_fail or len(roots) == 1:
            yield "discarded-partial-row"
            error = RuntimeError("provider element unavailable")
            error.hresult = error_code
            raise error
        yield "fresh-row"
    access._window = fresh_root
    access._select = select_rows
    access._property = lambda _item, _name, _default: False
    access._conversation_id = lambda item: "runtime:" + item
    access._row_screen_rect = lambda _item: _row_rect(100)
    access._active_phase = active_phase
    return access, roots


def test_row_enumeration_discards_partial_snapshot_when_element_disappears():
    access, roots = _transient_enumerator()
    rows = access._visible_conversation_rows(window(), selector())
    assert len(roots) == 2 and roots[0] is not roots[1]
    assert [row.internal_id for row in rows] == ["runtime:fresh-row"]


@pytest.mark.parametrize("settings,attempts", [
    ({"always_fail":True}, 2),
    ({"error_code":-2147467259}, 1),
    ({"active_phase":object()}, 1),
])
def test_row_enumeration_retry_is_bounded_and_does_not_reuse_active_phase(settings, attempts):
    access, roots = _transient_enumerator(**settings)
    with pytest.raises(RuntimeError, match="provider element unavailable"):
        access._visible_conversation_rows(window(), selector())
    assert len(roots) == attempts
