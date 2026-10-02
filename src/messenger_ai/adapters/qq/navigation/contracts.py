"""Closed, screenshot-bound contracts for QQ's V2 navigation layer.

These contracts confer no chat identity or permission to send.  A model chooses
one navigation action; the trusted desktop operator must still check the frame
against its live state and independently verify the opened conversation.
"""

from __future__ import annotations

import hashlib
import math
import struct
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from messenger_ai.llm.models import ProviderError, UsageRecord


class NavigationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("navigation timestamps must be timezone-aware")
    return value


def _label(value: str) -> str:
    if not value.strip() or any(
        ord(character) < 32 or character in "\x7f\u2028\u2029" for character in value
    ):
        raise ValueError("navigation labels must be nonempty single-line data")
    return value


class ContactIdentityMode(StrEnum):
    PERSISTENT = "persistent"
    SESSION_BOUND = "session_bound"


class ContactTarget(NavigationModel):
    account_id: str = Field(min_length=1, max_length=256, strict=True)
    conversation_id: str = Field(min_length=1, max_length=256, strict=True)
    binding_id: str = Field(min_length=1, max_length=256, strict=True)
    binding_revision: int = Field(ge=1, strict=True)
    display_name: str = Field(min_length=1, max_length=96, strict=True)
    search_aliases: tuple[str, ...] = Field(default=(), max_length=8)
    identity_mode: ContactIdentityMode
    conversation_type: Literal["direct"] = "direct"

    _display_label = field_validator("display_name")(_label)

    @field_validator("search_aliases")
    @classmethod
    def _aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for alias in value:
            if len(alias) > 96:
                raise ValueError("search alias exceeds 96 characters")
            _label(alias)
        if len(set(value)) != len(value):
            raise ValueError("search aliases must be unique")
        return value

    @property
    def trusted_queries(self) -> tuple[str, ...]:
        """Index zero always refers to the trusted registered display name."""
        return (self.display_name, *self.search_aliases)


class NavigationRect(NavigationModel):
    """A half-open rectangle in screenshot-local physical pixels."""

    left: int = Field(ge=0, strict=True)
    top: int = Field(ge=0, strict=True)
    right: int = Field(gt=0, strict=True)
    bottom: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def _positive_area(self) -> NavigationRect:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("navigation rectangle must have positive area")
        return self

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, other: NavigationRect) -> bool:
        return (
            self.left <= other.left < other.right <= self.right
            and self.top <= other.top < other.bottom <= self.bottom
        )


class NavigationRegionKind(StrEnum):
    CANDIDATE = "candidate"
    LIST = "list"
    SEARCH = "search"
    OVERLAY = "overlay"


class NavigationRegion(NavigationModel):
    kind: NavigationRegionKind
    bbox: NavigationRect
    # Populated only by trusted local detection, never by model-generated text.
    overlay_id: str | None = Field(default=None, min_length=1, max_length=96, strict=True)

    @model_validator(mode="after")
    def _overlay_scope(self) -> NavigationRegion:
        if (self.kind == NavigationRegionKind.OVERLAY) != (self.overlay_id is not None):
            raise ValueError("only supported overlay regions require overlay_id")
        return self


class NavigationFrame(NavigationModel):
    schema_version: Literal["qq_navigation_frame_v2"] = "qq_navigation_frame_v2"
    frame_id: str = Field(min_length=1, max_length=128, strict=True)
    run_id: str = Field(min_length=1, max_length=128, strict=True)
    session_epoch: str = Field(min_length=1, max_length=128, strict=True)
    surface_epoch: str = Field(min_length=1, max_length=128, strict=True)
    worker_epoch: str = Field(min_length=1, max_length=128, strict=True)
    desktop_lease_id: str = Field(min_length=1, max_length=128, strict=True)
    control_revision: int = Field(ge=0, strict=True)
    binding_id: str = Field(min_length=1, max_length=256, strict=True)
    binding_revision: int = Field(ge=1, strict=True)
    process_id: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    captured_at: datetime
    screen_origin_x: int = Field(default=0, strict=True)
    screen_origin_y: int = Field(default=0, strict=True)
    screen_width: int = Field(gt=0, le=16384, strict=True)
    screen_height: int = Field(gt=0, le=16384, strict=True)
    crop_origin_x: int = Field(strict=True)
    crop_origin_y: int = Field(strict=True)
    crop_width: int = Field(gt=0, le=8192, strict=True)
    crop_height: int = Field(gt=0, le=8192, strict=True)
    dpi_scale: float = Field(gt=0, le=8)
    allowed_regions: tuple[NavigationRegion, ...] = Field(max_length=32)
    # Producer assertion: it must mask chat bodies/composer and row previews.
    # The provider cannot infer that arbitrary pixels were correctly masked.
    privacy_mask_applied: Literal[True]
    png_bytes: bytes = Field(min_length=24, max_length=16 * 1024 * 1024, repr=False, strict=True)

    _captured_aware = field_validator("captured_at")(_aware)

    @model_validator(mode="after")
    def _geometry_and_image(self) -> NavigationFrame:
        if not math.isfinite(self.dpi_scale):
            raise ValueError("dpi_scale must be finite")
        if not (
            self.screen_origin_x <= self.crop_origin_x
            and self.screen_origin_y <= self.crop_origin_y
            and self.crop_origin_x + self.crop_width <= self.screen_origin_x + self.screen_width
            and self.crop_origin_y + self.crop_height <= self.screen_origin_y + self.screen_height
        ):
            raise ValueError("crop must be wholly inside the guest screen")
        for region in self.allowed_regions:
            if region.bbox.right > self.crop_width or region.bbox.bottom > self.crop_height:
                raise ValueError("allowed navigation region lies outside the screenshot")
        if self.png_bytes[:8] != b"\x89PNG\r\n\x1a\n" or self.png_bytes[12:16] != b"IHDR":
            raise ValueError("navigation requires a PNG screenshot with an IHDR header")
        width, height = struct.unpack(">II", self.png_bytes[16:24]
        )
        if (width, height) != (self.crop_width, self.crop_height):
            raise ValueError("PNG dimensions disagree with navigation crop metadata")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png_bytes).hexdigest()


class NavigationAction(StrEnum):
    CLICK_CANDIDATE = "click_candidate"
    OPEN_SEARCH = "open_search"
    SET_TARGET_QUERY = "set_target_query"
    SCROLL_LIST = "scroll_list"
    DISMISS_KNOWN_OVERLAY = "dismiss_known_overlay"
    WAIT = "wait"
    CANDIDATE_OPENED = "candidate_opened"
    UNABLE = "unable"


class NavigationDecision(NavigationModel):
    """Exactly one action, with no free-form input or keyboard capability."""

    frame_id: str = Field(min_length=1, max_length=128, strict=True)
    action: NavigationAction
    bbox: NavigationRect | None = None
    observed_label: str | None = Field(default=None, min_length=1, max_length=96, strict=True)
    query_alias_index: int | None = Field(default=None, ge=0, le=8, strict=True)
    scroll_direction: Literal["up", "down"] | None = None
    scroll_amount: int | None = Field(default=None, ge=1, le=3, strict=True)
    overlay_id: str | None = Field(default=None, min_length=1, max_length=96, strict=True)
    wait_ms: int | None = Field(default=None, ge=1, le=2000, strict=True)

    @field_validator("observed_label")
    @classmethod
    def _observed(cls, value: str | None) -> str | None:
        return _label(value) if value is not None else None

    @model_validator(mode="after")
    def _closed_action_parameters(self) -> NavigationDecision:
        required = {
            NavigationAction.CLICK_CANDIDATE: {"bbox", "observed_label"},
            NavigationAction.OPEN_SEARCH: {"bbox"},
            NavigationAction.SET_TARGET_QUERY: {"query_alias_index"},
            NavigationAction.SCROLL_LIST: {"bbox", "scroll_direction", "scroll_amount"},
            NavigationAction.DISMISS_KNOWN_OVERLAY: {"bbox", "overlay_id"},
            NavigationAction.WAIT: {"wait_ms"},
            NavigationAction.CANDIDATE_OPENED: set(),
            NavigationAction.UNABLE: set(),
        }[self.action]
        present = {
            name for name in (
                "bbox", "observed_label", "query_alias_index", "scroll_direction",
                "scroll_amount", "overlay_id", "wait_ms",
            ) if getattr(self, name) is not None
        }
        if present != required:
            raise ValueError("navigation action has missing or unrelated parameters")
        return self


class NavigationRequest(NavigationModel):
    target: ContactTarget
    frame: NavigationFrame
    deadline_at: datetime
    allowed_actions: tuple[NavigationAction, ...] = tuple(NavigationAction)

    _deadline_aware = field_validator("deadline_at")(_aware)

    @model_validator(mode="after")
    def _target_scope(self) -> NavigationRequest:
        if (
            self.frame.binding_id != self.target.binding_id
            or self.frame.binding_revision != self.target.binding_revision
        ):
            raise ValueError("navigation frame is scoped to a different target revision")
        if self.deadline_at <= self.frame.captured_at:
            raise ValueError("navigation deadline must follow frame capture")
        if not self.allowed_actions or len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must be nonempty and unique")
        return self


def validate_decision(
    request: NavigationRequest, decision: NavigationDecision
) -> NavigationDecision:
    """Check local pixel bounds and trusted capabilities before any execution.

    This does not prove that the desktop is unchanged or that a candidate is the
    target. Those are independent live checks, owned by operator and verifier.
    """
    if decision.frame_id != request.frame.frame_id:
        raise ValueError("navigation decision references a different frame")
    if decision.action not in request.allowed_actions:
        raise ValueError("navigation action is not permitted for this request")
    if decision.query_alias_index is not None:
        if decision.query_alias_index >= len(request.target.trusted_queries):
            raise ValueError("navigation query alias is absent from the trusted registry")
        if not any(region.kind == NavigationRegionKind.SEARCH for region in request.frame.allowed_regions):
            raise ValueError("navigation query requires a trusted search region")
    if decision.bbox is not None:
        if (
            decision.bbox.right > request.frame.crop_width
            or decision.bbox.bottom > request.frame.crop_height
        ):
            raise ValueError("navigation action lies outside the current screenshot")
        required_kind = {
            NavigationAction.CLICK_CANDIDATE: NavigationRegionKind.CANDIDATE,
            NavigationAction.OPEN_SEARCH: NavigationRegionKind.SEARCH,
            NavigationAction.SCROLL_LIST: NavigationRegionKind.LIST,
            NavigationAction.DISMISS_KNOWN_OVERLAY: NavigationRegionKind.OVERLAY,
        }[decision.action]
        if not any(
            region.kind == required_kind
            and region.bbox.contains(decision.bbox)
            and (decision.overlay_id is None or region.overlay_id == decision.overlay_id)
            for region in request.frame.allowed_regions
        ):
            raise ValueError("navigation action is outside its trusted operation region")
    return decision


class NavigationProviderResult(NavigationModel):
    frame_id: str = Field(min_length=1, max_length=128, strict=True)
    model: str = Field(min_length=1, max_length=256, strict=True)
    latency_ms: int = Field(ge=0, strict=True)
    decision: NavigationDecision | None = None
    error: ProviderError | None = None
    cancelled: bool = False
    usage: UsageRecord = Field(default_factory=UsageRecord)

    @model_validator(mode="after")
    def _one_outcome(self) -> NavigationProviderResult:
        if sum((self.decision is not None, self.error is not None, self.cancelled)) != 1:
            raise ValueError("provider result must contain one decision, error, or cancellation")
        if self.decision is not None and self.decision.frame_id != self.frame_id:
            raise ValueError("provider result decision belongs to another frame")
        return self


class DesktopActionStatus(StrEnum):
    ACTION_ATTEMPTED = "action_attempted"
    STALE = "stale"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DesktopActionResult(NavigationModel):
    status: DesktopActionStatus
    frame_id: str = Field(min_length=1, max_length=128, strict=True)
    next_frame: NavigationFrame | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128, strict=True)


class NavigationStatus(StrEnum):
    CANDIDATE_OPENED = "candidate_opened"
    RETRY_WAIT = "retry_wait"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class NavigationOutcome(NavigationModel):
    """Navigation result alone carries no active-chat or sending qualification."""

    status: NavigationStatus
    binding_id: str = Field(min_length=1, max_length=256, strict=True)
    binding_revision: int = Field(ge=1, strict=True)
    last_frame: NavigationFrame | None = None
    model_requests: int = Field(default=0, ge=0, strict=True)
    desktop_actions: int = Field(default=0, ge=0, strict=True)
    elapsed_ms: int = Field(default=0, ge=0, strict=True)
    error_code: str | None = Field(default=None, min_length=1, max_length=128, strict=True)


class NavigationBudget(NavigationModel):
    max_model_requests: int = Field(default=4, ge=1, le=16, strict=True)
    max_desktop_actions: int = Field(default=6, ge=1, le=32, strict=True)
    total_timeout_seconds: float = Field(default=45, gt=0, le=120)
    request_timeout_seconds: float = Field(default=15, gt=0, le=30)

    @model_validator(mode="after")
    def _bounded_timeout(self) -> NavigationBudget:
        if not math.isfinite(self.request_timeout_seconds) or not math.isfinite(self.total_timeout_seconds):
            raise ValueError("navigation timeouts must be finite")
        if self.request_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("single request timeout must fit inside the navigation round")
        return self
