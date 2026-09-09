"""Named window-binding entry point for callers following the M4 diagram."""

from .compatibility import CompatibilityError, CompatibilityMatrix, StrictWindowBinder

WeChatWindowBinder = StrictWindowBinder

__all__ = [
    "CompatibilityError",
    "CompatibilityMatrix",
    "StrictWindowBinder",
    "WeChatWindowBinder",
]
