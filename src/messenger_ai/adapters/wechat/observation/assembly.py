"""Convert OCR tokens into safe message candidates."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .models import (
    LayoutClassification,
    MessageCandidate,
    MessageDirection,
    OCRResult,
    OCRToken,
    VisualContentType,
)

_WHITESPACE = re.compile(r"\s+")
_TIME_BAR = re.compile(r"^(?:昨天|今天|前天|星期[一二三四五六日天]|\d{1,2}:\d{2})$")
_RECALL_NOTICE = re.compile(
    r"(?:撤回了一条消息|recalled a message|message was recalled)", re.IGNORECASE
)


class MessageAssembler:
    """Group only text bubble tokens; notices and media never become text."""

    def assemble(
        self, result: OCRResult, layout: LayoutClassification
    ) -> tuple[MessageCandidate, ...]:
        grouped: dict[str, list[OCRToken]] = defaultdict(list)
        for index, token in enumerate(result.tokens):
            if token.kind != VisualContentType.TEXT:
                continue
            text = _WHITESPACE.sub(" ", token.text).strip()
            if not text or _TIME_BAR.fullmatch(text) or _RECALL_NOTICE.search(text):
                continue
            group = token.group_id or f"line-{index}"
            grouped[group].append(token)
        candidates: list[MessageCandidate] = []
        for group, tokens in grouped.items():
            tokens.sort(key=lambda token: (token.rect.y, token.rect.x))
            text = " ".join(
                token.text.strip() for token in tokens if token.text.strip()
            )
            if not text:
                continue
            directions = {token.direction for token in tokens}
            direction = (
                next(iter(directions))
                if len(directions) == 1
                else MessageDirection.UNKNOWN
            )
            direction_confidence = min(
                token.direction_confidence
                if token.direction_confidence is not None
                else token.confidence
                for token in tokens
            )
            boundary = layout.message_boundary_confidence
            payload = f"{group}:{text}:{tokens[0].rect.model_dump_json()}"
            key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
            candidates.append(
                MessageCandidate(
                    platform_message_key=key,
                    text=text,
                    direction=direction,
                    content_type=VisualContentType.TEXT,
                    confidence=min(result.confidence, direction_confidence),
                    boundary_confidence=boundary,
                    displayed_time=tokens[0].displayed_time,
                    rect=tokens[0].rect,
                )
            )
        return tuple(candidates)
