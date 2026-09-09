"""Public M4 WeChat observation API."""

from .adapter import WeChatObservationAdapter
from .assembly import MessageAssembler
from .capture import (
    CaptureBoundaryError,
    SyntheticCaptureProvider,
    WindowRegionDetector,
)
from .compatibility import CompatibilityError, CompatibilityMatrix, StrictWindowBinder
from .confidence import ConfidenceCalibrator
from .differ import FrameDiffer
from .evidence import EvidenceStore, HumanEvidenceQueue, RedactedObservationLogger
from .layout import MetadataLayoutClassifier
from .models import *
from .normalize import EventNormalizer, WeChatEventNormalizer
from .ocr import DockerOCRSidecar, UnavailableLocalOCR
from .ports import LocalOCRPort, WindowCapturePort
from .temporal import TemporalFrameDiffer
from .window import WeChatWindowBinder

WindowCaptureProvider = WindowCapturePort
RegionOfInterestDetector = WindowRegionDetector
LocalOCR = LocalOCRPort
LayoutClassifier = MetadataLayoutClassifier

__all__ = [
    "CaptureBoundaryError",
    "CompatibilityError",
    "CompatibilityMatrix",
    "ConfidenceCalibrator",
    "DockerOCRSidecar",
    "EventNormalizer",
    "EvidenceStore",
    "FrameDiffer",
    "HumanEvidenceQueue",
    "LayoutClassifier",
    "LocalOCR",
    "LocalOCRPort",
    "MessageAssembler",
    "MetadataLayoutClassifier",
    "RedactedObservationLogger",
    "RegionOfInterestDetector",
    "StrictWindowBinder",
    "SyntheticCaptureProvider",
    "TemporalFrameDiffer",
    "UnavailableLocalOCR",
    "WeChatEventNormalizer",
    "WeChatObservationAdapter",
    "WeChatWindowBinder",
    "WindowCaptureProvider",
    "WindowRegionDetector",
]
