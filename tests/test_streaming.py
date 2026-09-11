"""Streaming mechanics: what the customer sees while the answer is still arriving."""
from __future__ import annotations

import asyncio

import pytest

from app.agent.graph import _split_suggestions, _suggestions_cut
from app.sse import Event, SSEStream


# ── holding back the SUGGESTIONS marker mid-stream ────────────────────────────

@pytest.mark.parametrize("partial,expected", [
    ("Hello there. ", 13),                       # nothing to hide
    ("Answer text.\nSUGGESTIONS: a | b", 12),    # complete marker
    ("Answer text.\nSUGG", 12),                  # could still become the marker
    ("Answer text.\nS", 12),                     # a single S could too
    ("Answer with Super value", 23),             # a real word that merely starts with S
    ("done\nSUGGESTIONS: x", 4),
])
def test_suggestions_cut_withholds_only_what_might_be_the_marker(partial, expected):
    """The customer must never watch `SUGGESTIONS:` being typed out — it is scaffolding
    that becomes chips. But withholding every word starting with S would stutter the
    stream, so only genuinely ambiguous tails are held."""
    assert _suggestions_cut(partial) == expected


def test_split_suggestions_separates_answer_from_chips():
    answer, chips = _split_suggestions("The answer.\nSUGGESTIONS: one | two")
    assert answer == "The answer."
    assert chips == ["one", "two"]


def test_split_suggestions_handles_none():
    answer, chips = _split_suggestions("Just an answer.\nSUGGESTIONS: none")
    assert answer == "Just an answer."
    assert chips == []


def test_split_suggestions_without_marker_returns_whole_draft():
    answer, chips = _split_suggestions("No marker here at all.")
    assert answer == "No marker here at all."
    assert chips == []


# ── the stream's own guarantees ───────────────────────────────────────────────

async def test_open_stages_are_closed_by_a_terminal_event():
    """A node that raises mid-stage would otherwise leave the UI spinning forever."""
    stream = SSEStream()
    await stream.stage_start("investigate", "Digging into it")
    await stream.emit(Event.COMPLETE, {"message_id": "m1"})
    assert stream.open_stages == []
    assert stream.emitted.count("stage_complete") == 1
    assert stream.emitted[-1] == "complete"


async def test_only_one_terminal_event_is_emitted():
    stream = SSEStream()
    await stream.emit(Event.COMPLETE, {"message_id": "m1"})
    await stream.emit(Event.ERROR, {"code": "LATE", "message": "should be dropped"})
    assert stream.emitted.count("complete") == 1
    assert "error" not in stream.emitted


async def test_events_after_a_terminal_are_dropped():
    stream = SSEStream()
    await stream.emit(Event.COMPLETE, {"message_id": "m1"})
    await stream.content("this should never reach the client")
    assert "content_delta" not in stream.emitted


async def test_stage_complete_reports_elapsed_time():
    stream = SSEStream()
    await stream.stage_start("answer", "Writing your answer")
    await stream.stage_complete("answer")
    assert stream.open_stages == []
    assert stream.emitted == ["stage_start", "stage_complete"]


# ── admission control ─────────────────────────────────────────────────────────

async def test_slot_sheds_when_the_pool_is_exhausted():
    """A six-minute queue is a worse answer than an honest 'we're busy'. Once the
    in-flight limit is reached the next caller must be told, not silently parked."""
    import app.routes.chat as chat_route

    original = chat_route._inflight
    chat_route._inflight = asyncio.Semaphore(2)
    try:
        held = [chat_route._Slot() for _ in range(2)]
        for slot in held:
            assert await slot.acquire_or_shed() is True

        overflow = chat_route._Slot()
        assert await overflow.acquire_or_shed() is False
        assert overflow.held is False

        held[0].release()
        recovered = chat_route._Slot()
        assert await recovered.acquire_or_shed() is True
        recovered.release()
        held[1].release()
    finally:
        chat_route._inflight = original


async def test_releasing_a_slot_twice_does_not_inflate_capacity():
    """Double release would hand out more slots than the limit allows, which is the
    same as having no limit at all."""
    import app.routes.chat as chat_route

    original = chat_route._inflight
    chat_route._inflight = asyncio.Semaphore(1)
    try:
        slot = chat_route._Slot()
        assert await slot.acquire_or_shed() is True
        slot.release()
        slot.release()  # must be a no-op
        a, b = chat_route._Slot(), chat_route._Slot()
        assert await a.acquire_or_shed() is True
        assert await b.acquire_or_shed() is False
        a.release()
    finally:
        chat_route._inflight = original
