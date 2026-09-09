"""Dependency-inversion ports for WeChat visual observation."""

from __future__ import annotations

from typing import Protocol

from .models import (
    CapturedFrame,
    LayoutClassification,
    OCRResult,
    Rectangle,
    RegionOfInterest,
    WindowBinding,
    WindowDescriptor,
    WindowEnvironment,
)


class WindowBindingPort(Protocol):
    def bind(self, descriptor: WindowDescriptor) -> WindowBinding: ...


class WindowCapturePort(Protocol):
    def capture(self, binding: WindowBinding, roi: Rectangle) -> CapturedFrame: ...


class RegionOfInterestPort(Protocol):
    def detect(
        self, frame: CapturedFrame, layout: LayoutClassification
    ) -> tuple[RegionOfInterest, ...]: ...


class LayoutClassifierPort(Protocol):
    def classify(
        self, frame: CapturedFrame, environment: WindowEnvironment
    ) -> LayoutClassification: ...


class LocalOCRPort(Protocol):
    def recognize(self, frame: CapturedFrame, roi: RegionOfInterest) -> OCRResult: ...


class EvidenceClockPort(Protocol):
    def now(self): ...
