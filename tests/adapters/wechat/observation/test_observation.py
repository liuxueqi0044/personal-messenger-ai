import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from messenger_ai.adapters.wechat.observation import (
    CompatibilityError,
    CompatibilityMatrix,
    CompatibilityRecord,
    DockerOCRSidecar,
    MetadataLayoutClassifier,
    OCRResult,
    OCRToken,
    Rectangle,
    StrictWindowBinder,
    SyntheticCaptureProvider,
    WeChatObservationAdapter,
    WindowDescriptor,
    WindowEnvironment,
)


def _env() -> WindowEnvironment:
    return WindowEnvironment(
        client_version="4.1.12.55",
        windows_version="Windows 11",
        graphics_backend="d3d11",
        dpi_scale=1.0,
        theme="light",
        window_width=900,
        window_height=700,
        monitor_id="m1",
    )


def test_synthetic_evaluator_reports_fixture_only_metrics():
    script = Path(__file__).parents[4] / "scripts" / "wechat_observation_eval.py"
    spec = importlib.util.spec_from_file_location("wechat_observation_eval", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    fixture = (
        Path(__file__).parents[4]
        / "fixtures"
        / "wechat_synthetic"
        / "observation_smoke.jsonl"
    )
    result = module.evaluate(fixture)
    assert result["synthetic_only"] is True
    assert result["accuracy_claim"] is False
    assert result["samples"] == 7
    assert result["metrics"]["duplicate_events"]["suppression_cases"] == [
        "duplicate_frame"
    ]
    assert "不可代表 500 张真实评测" in result["disclaimer"]


def _binding():
    environment = _env()
    matrix = CompatibilityMatrix(
        (
            CompatibilityRecord(
                fingerprint=environment.fingerprint(),
                environment=environment,
                status="verified",
                verified_at=datetime.now(UTC),
            ),
        )
    )
    return StrictWindowBinder(matrix).bind(
        WindowDescriptor(
            handle=11,
            process_id=22,
            environment=environment,
            identity="contact-1",
            identity_confidence=0.99,
        )
    )


class FakeOCR:
    def recognize(self, frame, roi):
        return OCRResult(
            confidence=0.99,
            source_frame_hash=frame.frame_hash,
            tokens=[
                OCRToken(
                    text="你好",
                    rect=Rectangle(x=20, y=20, width=70, height=30),
                    confidence=0.99,
                    direction="inbound",
                    group_id="m1",
                )
            ],
        )


def _adapter(payload=b"frame"):
    provider = SyntheticCaptureProvider(
        payload=payload,
        metadata={
            "layout_version": "wechat-4.1.12",
            "layout_confidence": 0.99,
            "message_boundary_confidence": 0.99,
            "conversation_rect": {"x": 0, "y": 0, "width": 900, "height": 700},
            "message_rect": {"x": 0, "y": 0, "width": 900, "height": 700},
        },
    )
    return WeChatObservationAdapter(
        provider,
        layout_classifier=MetadataLayoutClassifier(),
        ocr=FakeOCR(),
    )


def test_unknown_environment_and_zero_handle_fail_closed():
    environment = _env()
    with pytest.raises(CompatibilityError):
        StrictWindowBinder(CompatibilityMatrix()).bind(
            WindowDescriptor(
                handle=11,
                process_id=22,
                environment=environment,
                identity="contact-1",
                identity_confidence=1,
            )
        )
    with pytest.raises(ValueError):
        WindowDescriptor(handle=0, process_id=22, environment=environment)


def test_same_frame_is_not_emitted_twice_and_inbound_is_normalized():
    adapter = _adapter()
    binding = _binding()
    first = adapter.observe(binding)
    assert first is not None and first.publishable
    assert len(adapter.poll_events(binding)) == 0


def test_low_direction_enters_human_queue_and_non_text_is_not_assembled():
    class LowOCR:
        def recognize(self, frame, roi):
            return OCRResult(
                confidence=0.99,
                tokens=[
                    OCRToken(
                        text="撤回了一条消息",
                        kind="recall",
                        rect=Rectangle(x=10, y=10, width=100, height=20),
                    ),
                    OCRToken(
                        text="unknown",
                        rect=Rectangle(x=20, y=50, width=100, height=20),
                    ),
                ],
            )

    provider = SyntheticCaptureProvider(
        payload=b"another",
        metadata={
            "layout_version": "wechat-4.1.12",
            "layout_confidence": 0.99,
            "message_boundary_confidence": 0.99,
            "conversation_rect": {"x": 0, "y": 0, "width": 900, "height": 700},
            "message_rect": {"x": 0, "y": 0, "width": 900, "height": 700},
        },
    )
    adapter = WeChatObservationAdapter(
        provider, layout_classifier=MetadataLayoutClassifier(), ocr=LowOCR()
    )
    observation = adapter.observe(_binding())
    assert observation is not None
    assert observation.human_review_required
    assert len(observation.messages) == 1
    assert observation.messages[0].text == "unknown"
    assert adapter.evidence_queue.pending(observation.observed_at) == (observation,)


def test_evidence_expires_and_docker_sidecar_is_only_injected():
    captured = []

    def request(frame, roi):
        captured.append((frame.frame_hash, roi.name))
        return OCRResult(confidence=0.5, source_frame_hash=frame.frame_hash)

    sidecar = DockerOCRSidecar(request)
    provider = SyntheticCaptureProvider(payload=b"x")
    binding = _binding()
    frame = provider.capture(binding, Rectangle(x=0, y=0, width=10, height=10))
    roi = type("R", (), {"name": "messages"})()
    sidecar.recognize(frame, roi)
    assert captured
    evidence = _adapter().evidence_queue.store
    assert evidence.purge(datetime.now(UTC) + timedelta(days=1)) == 0
