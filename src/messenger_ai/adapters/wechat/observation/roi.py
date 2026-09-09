"""ROI detector named after the M4 pipeline component."""

from .capture import CaptureBoundaryError, WindowRegionDetector

RegionOfInterestDetector = WindowRegionDetector

__all__ = ["CaptureBoundaryError", "RegionOfInterestDetector", "WindowRegionDetector"]
