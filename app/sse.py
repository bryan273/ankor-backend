"""SSE emitter and the v1 event vocabulary.

The vocabulary is a contract shared with the frontend (`docs/API_CONTRACT.md` §3), so
it lives in one enum rather than as string literals scattered through the nodes. A
rename here is a rename in both repos.

The emitter enforces the two ordering guarantees the contract makes, because a
guarantee nobody checks is a wish:

  - every `stage_start` is closed by exactly one `stage_complete` or `stage_error`
  - exactly one terminal event (`complete` or `error`) ends the stream

`close_open_stages()` runs in the terminal path, so a node that raises halfway cannot
leave the UI with a spinner that never stops.
"""
from __future__ import annotations

import asyncio
import json
import time
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

EVENT_VOCAB_VERSION = "1"


class Event(str, Enum):
    STATUS = "status"
    STAGE_START = "stage_start"
    STAGE_COMPLETE = "stage_complete"
    STAGE_ERROR = "stage_error"
    EMOTION = "emotion"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONTENT_DELTA = "content_delta"
    CONTENT_RESET = "content_reset"
    UI_BLOCK = "ui_block"
    CITATION = "citation"
    SUGGESTIONS = "suggestions"
    TICKET_UPDATE = "ticket_update"
    USAGE = "usage"
    COMPLETE = "complete"
    ERROR = "error"


TERMINAL = {Event.COMPLETE, Event.ERROR}


def format_sse(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


class SSEStream:
    """An async queue the graph writes into and the HTTP response drains."""

    def __init__(self, *, heartbeat: float = 15.0):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._open_stages: Dict[str, float] = {}
        self._terminated = False
        self._heartbeat = heartbeat
        self.emitted: List[str] = []  # for tests and the trace drawer

    # ── emit ──────────────────────────────────────────────────────────────────
    async def emit(self, event: Event, data: Optional[Dict[str, Any]] = None) -> None:
        if self._terminated:
            log.debug("sse.after_terminal_dropped", dropped_event=event.value)
            return
        if event in TERMINAL:
            await self.close_open_stages()
            self._terminated = True
        self.emitted.append(event.value)
        await self._queue.put((event.value, data or {}))
        if event in TERMINAL:
            await self._queue.put(None)  # sentinel: drain loop exits

    async def status(self, session_id: str, message_id: str, state: str = "started") -> None:
        await self.emit(Event.STATUS, {"session_id": session_id, "message_id": message_id,
                                       "state": state})

    async def stage_start(self, stage_id: str, label: str) -> None:
        self._open_stages[stage_id] = time.perf_counter()
        await self.emit(Event.STAGE_START, {"stage_id": stage_id, "label": label})

    async def stage_complete(self, stage_id: str) -> None:
        started = self._open_stages.pop(stage_id, None)
        ms = int((time.perf_counter() - started) * 1000) if started else 0
        await self.emit(Event.STAGE_COMPLETE, {"stage_id": stage_id, "ms": ms})

    async def stage_error(self, stage_id: str, code: str, message: str) -> None:
        self._open_stages.pop(stage_id, None)
        await self.emit(Event.STAGE_ERROR, {"stage_id": stage_id, "code": code,
                                            "message": message})

    async def close_open_stages(self) -> None:
        """Terminal safety net: a node that raised mid-stage would otherwise leave the
        UI spinning forever."""
        for stage_id in list(self._open_stages):
            started = self._open_stages.pop(stage_id)
            ms = int((time.perf_counter() - started) * 1000)
            self.emitted.append(Event.STAGE_COMPLETE.value)
            await self._queue.put((Event.STAGE_COMPLETE.value, {"stage_id": stage_id, "ms": ms}))

    async def thinking(self, stage_id: str, delta: str) -> None:
        await self.emit(Event.THINKING_DELTA, {"stage_id": stage_id, "delta": delta})

    async def content(self, delta: str) -> None:
        await self.emit(Event.CONTENT_DELTA, {"delta": delta})

    async def block(self, block: Dict[str, Any]) -> None:
        await self.emit(Event.UI_BLOCK, block)

    async def error(self, code: str, message: str, retryable: bool = False) -> None:
        await self.emit(Event.ERROR, {"code": code, "message": message, "retryable": retryable})

    # ── drain ─────────────────────────────────────────────────────────────────
    async def drain(self) -> AsyncIterator[str]:
        """Yield formatted SSE frames until a terminal event.

        A comment heartbeat keeps proxies from closing an idle connection while a slow
        tool runs; SSE comments are ignored by every client.
        """
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._heartbeat)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            event, data = item
            yield format_sse(event, data)

    @property
    def terminated(self) -> bool:
        return self._terminated

    @property
    def open_stages(self) -> List[str]:
        return list(self._open_stages)
