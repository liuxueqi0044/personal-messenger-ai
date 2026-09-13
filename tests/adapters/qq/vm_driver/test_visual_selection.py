from __future__ import annotations

import asyncio
import json
import struct
import zlib

import pytest

from messenger_ai.adapters.qq.models import QQConversation, QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.transport import (
    WindowsUIAQQAccessibility,
    _encode_bgra_png,
)
from messenger_ai.adapters.qq.vm_driver.visual_selection import (
    ConversationSelectionActuator,
    ConversationSelectionStatus,
    DeepSeekVisualSelectionProvider,
    ScreenRect,
    VisualRowDecision,
    VisualRowFrame,
    VisualSelectionProviderResult,
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
    actions = Actions([row])
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
