"""Frame differ alias for the M4 pipeline."""

from .temporal import TemporalFrameDiffer

FrameDiffer = TemporalFrameDiffer

__all__ = ["FrameDiffer", "TemporalFrameDiffer"]
