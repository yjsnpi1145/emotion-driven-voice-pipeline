from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from voice_pipeline.models.schemas import StrictModel

LlmOperation = Literal[
    "chapter_plan",
    "reference_correction",
    "connection_test",
    "script_analysis",
    "cast_reconciliation",
    "script_translation",
]
LlmActivityKind = Literal[
    "started",
    "request_sent",
    "retrying",
    "response",
    "completed",
    "failed",
]

_TERMINAL_KINDS = frozenset({"completed", "failed"})
_TRUNCATION_MARKER = "…[已截断]"


class LlmActivityEvent(StrictModel):
    sequence: int = Field(ge=1)
    operation_id: UUID
    operation: LlmOperation
    kind: LlmActivityKind
    message: str
    content: str | None = None
    created_at_utc: datetime


class LlmActivitySnapshot(StrictModel):
    active: bool
    active_operation: LlmOperation | None = None
    active_since_utc: datetime | None = None
    events: tuple[LlmActivityEvent, ...]


class LlmActivityLog:
    """Small in-memory, secret-free activity feed for the local workbench."""

    def __init__(self, *, max_events: int = 80, max_content_chars: int = 65_536) -> None:
        if max_events < 1 or max_content_chars < len(_TRUNCATION_MARKER):
            raise ValueError("LLM activity limits must be positive and usable")
        self._events: deque[LlmActivityEvent] = deque(maxlen=max_events)
        self._max_content_chars = max_content_chars
        self._active: dict[UUID, tuple[LlmOperation, datetime]] = {}
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def emit(
        self,
        *,
        operation_id: UUID,
        operation: LlmOperation,
        kind: LlmActivityKind,
        message: str,
        content: str | None = None,
    ) -> LlmActivityEvent:
        now = datetime.now(UTC)
        async with self._lock:
            self._sequence += 1
            if kind == "started":
                self._active[operation_id] = (operation, now)
            elif kind in _TERMINAL_KINDS:
                self._active.pop(operation_id, None)
            event = LlmActivityEvent(
                sequence=self._sequence,
                operation_id=operation_id,
                operation=operation,
                kind=kind,
                message=message,
                content=self._truncate(content),
                created_at_utc=now,
            )
            self._events.append(event)
            return event

    async def snapshot(self) -> LlmActivitySnapshot:
        async with self._lock:
            active_item = min(self._active.values(), key=lambda item: item[1], default=None)
            return LlmActivitySnapshot(
                active=active_item is not None,
                active_operation=active_item[0] if active_item else None,
                active_since_utc=active_item[1] if active_item else None,
                events=tuple(self._events),
            )

    def _truncate(self, content: str | None) -> str | None:
        if content is None or len(content) <= self._max_content_chars:
            return content
        keep = self._max_content_chars - len(_TRUNCATION_MARKER)
        return f"{content[:keep]}{_TRUNCATION_MARKER}"
