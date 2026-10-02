"""V2 finite visual navigation; no message or sending capabilities."""

from .contracts import (
    ContactIdentityMode, ContactTarget, DesktopActionResult, DesktopActionStatus,
    NavigationAction, NavigationBudget, NavigationDecision, NavigationFrame,
    NavigationOutcome, NavigationProviderResult, NavigationRect, NavigationRegion,
    NavigationRegionKind, NavigationRequest, NavigationStatus, validate_decision,
)
from .ports import CurrentChatVerifier, DesktopOperator, VisionNavigator
from .provider import ResponsesVisionNavigator
from .desktop import NavigationDesktopError, NavigationDesktopScope, ScopedDesktopOperator, TrustedNavigationBackend

__all__ = [
    "ContactIdentityMode", "ContactTarget", "CurrentChatVerifier", "DesktopActionResult",
    "DesktopActionStatus", "DesktopOperator", "NavigationAction", "NavigationBudget",
    "NavigationDecision", "NavigationFrame", "NavigationOutcome", "NavigationProviderResult",
    "NavigationRect", "NavigationRegion", "NavigationRegionKind", "NavigationRequest",
    "NavigationStatus", "ResponsesVisionNavigator", "VisionNavigator", "validate_decision",
    "NavigationDesktopError", "NavigationDesktopScope", "ScopedDesktopOperator", "TrustedNavigationBackend",
]
