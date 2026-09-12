"""Chat endpoints — the streaming turn and the block-action resume."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.agent.graph import Agent
from app.deps import require_api_key
from app.config import settings
from app.errors import BlockStale, Busy, NotFound
from app.routes.health import bump
from app.schemas.agent import AgentState, Perception
from app.schemas.api import ChatActionRequest, ChatRequest
from app.services import sessions as session_svc
from app.sse import EVENT_VOCAB_VERSION, Event, SSEStream

log = structlog.get_logger(__name__)
router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "X-Event-Vocab": EVENT_VOCAB_VERSION,
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # nginx would otherwise hold the stream in a buffer
    "Connection": "keep-alive",
}

# Admission control. Accepting unlimited turns does not make the model any faster — it
# just converts a queue the server can see into a queue the customer experiences as a
# six-minute silence. Past this many in-flight turns we say so and let the client retry.
_inflight = asyncio.Semaphore(settings.max_concurrent_turns)


class _Slot:
    """Holds an admission slot for the lifetime of one streamed turn."""

    def __init__(self) -> None:
        self.held = False

    async def acquire_or_shed(self) -> bool:
        try:
            await asyncio.wait_for(_inflight.acquire(), timeout=0.01)
        except asyncio.TimeoutError:
            return False
        self.held = True
        return True

    def release(self) -> None:
        if self.held:
            self.held = False
            _inflight.release()


async def _run_and_finish(agent: Agent, state: AgentState, stream: SSEStream,
                          slot: "_Slot") -> None:
    """Drive the agent, then persist and emit exactly one terminal event."""
    try:
        await agent.run(state)
    except Exception:  # noqa: BLE001 — agent.run already emitted `error`
        bump("errors")
        await _persist(state, agent, failed=True)
        return
    finally:
        slot.release()
    await _finish(state, agent, stream)


async def _resume_and_finish(agent: Agent, state: AgentState, stream: SSEStream,
                             action_id: str, value: Dict[str, Any],
                             slot: "_Slot") -> None:
    try:
        await agent.resume(state, action_id, value)
    except Exception:  # noqa: BLE001
        bump("errors")
        await _persist(state, agent, failed=True)
        return
    finally:
        slot.release()
    await _finish(state, agent, stream)


async def _finish(state: AgentState, agent: Agent, stream: SSEStream) -> None:
    await _persist(state, agent)
    await stream.emit(Event.USAGE, {
        "input": state.input_tokens, "output": state.output_tokens,
        "total": state.input_tokens + state.output_tokens,
        "cost_credits": round(state.cost_credits, 6),
        "iterations": state.iterations,
    })
    await stream.emit(Event.COMPLETE, {
        "message_id": state.message_id,
        "blocks": [b.block_id for b in state.blocks],
        "resolved_sku": state.resolved.sku if state.resolved else None,
        "ticket_id": state.ticket_id,
        "awaiting_action": state.awaiting_action,
        "guard_hits": [h.rule_id for h in state.guard_hits],
    })
    bump("turns")
    bump("cost_credits", state.cost_credits)
    bump("tool_calls", len(state.observations))
    bump("guard_hits", len(state.guard_hits))


async def _persist(state: AgentState, agent: Agent, failed: bool = False) -> None:
    """Writes are best-effort: a database hiccup must not turn a good answer into an
    error the customer sees."""
    try:
        message_id = await session_svc.add_message(
            state.session_id, "assistant",
            state.answer or ("(failed)" if failed else ""),
            emotion=state.perception.emotion.value,
            intensity=state.perception.intensity,
            intent=state.perception.intent.value,
            urgency=state.perception.urgency.model_dump(),
        )
        for block in state.blocks:
            await session_svc.save_block(message_id, block.model_dump())
        await session_svc.save_tool_traces(message_id, agent.traces)
        await session_svc.save_guard_hits(
            message_id, [h.model_dump() for h in state.guard_hits])
    except Exception as e:  # noqa: BLE001
        log.warning("chat.persist_failed", error=str(e)[:200])


@router.post("/chat")
async def chat(req: ChatRequest, _: str = Depends(require_api_key)) -> StreamingResponse:
    slot = _Slot()
    if not await slot.acquire_or_shed():
        bump("shed")
        raise Busy("too many conversations in flight right now",
                   {"retry_after_seconds": 5,
                    "in_flight_limit": settings.max_concurrent_turns})

    try:
        session = await session_svc.ensure_session(
            req.session_id, req.client_context.customer_email, req.locale)
        session_id = session["session_id"]

        await session_svc.add_message(session_id, "user", req.message)
        history = await session_svc.history(session_id, limit=12)
        # `history` includes the message just written; the agent wants what came before it.
        history = history[:-1] if history else []
    except Exception:
        slot.release()
        raise

    state = AgentState(
        session_id=session_id,
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        user_message=req.message,
        locale=req.locale,
        attachment_ids=req.attachment_ids,
        customer_email=req.client_context.customer_email or session.get("email"),
        customer_id=session.get("customer_id"),
        history=history,
        turn=len(history) // 2 + 1,
    )

    stream = SSEStream()
    await stream.status(session_id, state.message_id)
    agent = Agent(stream)
    asyncio.create_task(_run_and_finish(agent, state, stream, slot))
    return StreamingResponse(stream.drain(), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.post("/chat/action")
async def chat_action(req: ChatActionRequest,
                      _: str = Depends(require_api_key)) -> StreamingResponse:
    slot = _Slot()
    if not await slot.acquire_or_shed():
        bump("shed")
        raise Busy("too many conversations in flight right now",
                   {"retry_after_seconds": 5,
                    "in_flight_limit": settings.max_concurrent_turns})

    # Everything up to handing the slot to the background task can still reject the
    # request. Releasing on that path matters: a stale-block click is a normal thing for
    # a user to do, and leaking a slot each time would starve the server by attrition.
    try:
        block = await session_svc.get_block(req.block_id)
        if not block:
            raise NotFound("no such block", {"block_id": req.block_id})
        if block["state"] != "active":
            raise BlockStale("this block has already been used",
                             {"block_id": req.block_id, "state": block["state"]})

        session = await session_svc.get_session(req.session_id)
        if not session:
            raise NotFound("no such session", {"session_id": req.session_id})

        await session_svc.answer_block(req.block_id, req.value)
        checkpoint = await session_svc.load_checkpoint(req.session_id) or {}
        history = await session_svc.history(req.session_id, limit=12)
    except Exception:
        slot.release()
        raise

    state = AgentState(
        session_id=req.session_id,
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        user_message=checkpoint.get("user_message", ""),
        rewritten_query=checkpoint.get("rewritten_query", ""),
        attachment_ids=checkpoint.get("attachment_ids", []),
        vlm_facts=checkpoint.get("vlm_facts", []),
        candidates=checkpoint.get("candidates", []),
        customer_email=session.get("email"),
        customer_id=session.get("customer_id"),
        history=history,
        turn=int(checkpoint.get("turn", 1)) + 1,
    )
    if checkpoint.get("perception"):
        try:
            state.perception = Perception.model_validate(checkpoint["perception"])
        except Exception:  # noqa: BLE001 — a stale checkpoint shape must not block resume
            log.warning("chat.checkpoint_perception_invalid")

    await session_svc.clear_checkpoint(req.session_id)

    stream = SSEStream()
    await stream.status(req.session_id, state.message_id)
    agent = Agent(stream)
    asyncio.create_task(
        _resume_and_finish(agent, state, stream, req.action_id, req.value, slot))
    return StreamingResponse(stream.drain(), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.get("/assist/{session_id}")
async def assist(session_id: str, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    """The handoff briefing for a human agent: summary, how the customer is feeling,
    what to avoid, and two or three replies they can send as themselves.

    Deliberately a separate call rather than part of the turn — it is produced when a
    person picks the case up, which is usually not the moment the turn ended.
    """
    from app.services import assist as assist_svc

    session = await session_svc.get_session(session_id)
    if not session:
        raise NotFound("no such session", {"session_id": session_id})
    return await assist_svc.build_briefing(session_id)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    session = await session_svc.get_session(session_id)
    if not session:
        raise NotFound("no such session", {"session_id": session_id})
    return session


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, limit: int = 50,
                       _: str = Depends(require_api_key)) -> Dict[str, Any]:
    return {"messages": await session_svc.messages_with_blocks(session_id, limit)}
