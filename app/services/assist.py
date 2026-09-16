"""Agent assist — what a human sees the moment a case lands on their desk.

Modelled on the pattern Google's Agent Assist established for contact centres: when a
conversation moves from the virtual agent to a person, that person should not have to
read the transcript. They get a summary, a few replies they can send as themselves, and
a note on how to handle this particular customer.

The distinction that matters: everything here is written for the **human agent**, not
for the customer. The summary says "customer is on their second failed fix and has a
deadline" — a sentence you would never show the customer, and exactly the sentence the
agent needs before saying hello.

Smart replies are drafts, never sent automatically. The human picks one, edits it, or
ignores all three. An agent-assist feature that acts on its own has stopped assisting.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import structlog

from app.clients.llm import get_llm
from app.services import sessions as session_svc

log = structlog.get_logger(__name__)

ASSIST_PROMPT = """A support case is being handed from an automated agent to a human \
colleague. Brief them.

Write for the COLLEAGUE, not the customer. They have not read any of this and are about \
to speak to someone who may already be annoyed.

Conversation so far:
{transcript}

What the automated agent established:
{facts}

Return ONLY JSON:

{{
  "summary": "2-3 sentences: who they are, what is broken, what has been established, \
where it stands right now",
  "customer_state": "one short phrase for how this person is feeling and why",
  "watch_out": "the single thing most likely to go wrong in this conversation — a \
promise not to make, a step already tried, a sensitivity. One sentence.",
  "open_questions": ["anything still unknown that the colleague will need"],
  "suggested_replies": [
    {{"label": "3-5 words naming the approach", "text": "the message itself, written in \
the colleague's voice, ready to send"}}
  ]
}}

Rules for `suggested_replies`: give two or three, each a genuinely different approach — \
not three phrasings of the same thing. Write them SHORT, the way a person types in a \
chat window, and never promise anything the facts above do not support."""


def _transcript(messages: List[Dict[str, Any]], limit: int = 14) -> str:
    lines = []
    for m in messages[-limit:]:
        who = "Customer" if m.get("role") == "user" else "Agent"
        text = (m.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text[:600]}")
    return "\n\n".join(lines) or "(nothing said yet)"


async def _facts(session_id: str) -> str:
    """What the automated side actually established — tools, verdicts, tickets.

    Read from the persisted trace rather than re-derived, so the briefing describes the
    conversation that happened rather than one reconstructed from the text.
    """
    from app.clients import db

    rows = await db.fetch(
        """
        select tt.tool, tt.result, tt.ok
        from tool_traces tt
        join messages m on m.id = tt.message_id
        where m.session_id = %s and tt.ok
        order by tt.created_at desc
        limit 12
        """,
        (session_id,),
    )
    facts: List[str] = []
    seen: set = set()
    for r in rows:
        tool = r["tool"]
        if tool in seen:
            continue
        seen.add(tool)
        data = r["result"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {}
        data = data or {}
        if tool == "check_warranty":
            facts.append(f"- Warranty engine returned '{data.get('verdict')}' "
                         f"({data.get('reason_code')}): {data.get('explanation', '')}")
        elif tool == "lookup_dealer_order" and data.get("found"):
            dealer = (data.get("dealer") or {}).get("name", "")
            facts.append(f"- Order is a dealer invoice from {dealer}, not in our system")
        elif tool == "lookup_order":
            facts.append("- Order found in our system" if data.get("found")
                         else "- Order number is NOT in our order system")
        elif tool == "create_ticket" and data.get("created"):
            facts.append(f"- Ticket {data.get('ticket_no')} opened, "
                         f"priority {data.get('priority')}")
        elif tool == "lookup_error_code" and data.get("found"):
            match = (data.get("matches") or [{}])[0]
            meaning = match.get("meaning")
            steps = match.get("fix_steps") or []
            # Some codes are on file with repair steps but no description — the scraped
            # import never carried one. Interpolating that straight into the sentence
            # briefed the human agent with "Error C1 means: None".
            if meaning:
                facts.append(f"- Error {match.get('code')} means: {meaning}")
            elif steps:
                facts.append(f"- Error {match.get('code')} is on file with "
                             f"{len(steps)} repair steps, but no description")
            else:
                facts.append(f"- Error {match.get('code')} is on file, but we hold "
                             f"nothing about what it means")
        elif tool == "get_troubleshooting_flow" and data.get("found"):
            facts.append(f"- Walked through the '{data.get('symptom')}' repair flow "
                         f"({len(data.get('steps') or [])} steps)")
        elif tool.startswith("step_result_"):
            facts.append(f"- Customer reported a repair step: {tool.split('_')[-1]}")
    return "\n".join(facts) or "(no tools were run)"


async def build_briefing(session_id: str) -> Dict[str, Any]:
    """One model call. Returns a shape the console can render even if the call fails —
    a handoff that breaks because the briefing failed would be worse than no briefing."""
    session = await session_svc.get_session(session_id)
    messages = await session_svc.messages_with_blocks(session_id, limit=20)
    if not messages:
        return {"summary": "Nothing has been said in this conversation yet.",
                "customer_state": "", "watch_out": "", "open_questions": [],
                "suggested_replies": [], "resolved_sku": None}

    last_customer = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    facts = await _facts(session_id)

    try:
        data, usage = await get_llm().json_complete(
            [{"role": "user", "content": ASSIST_PROMPT.format(
                transcript=_transcript(messages), facts=facts)}],
            max_tokens=1600, default={},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("assist.failed", error=str(e)[:160])
        data = {}

    replies = []
    for r in (data.get("suggested_replies") or [])[:3]:
        if isinstance(r, dict) and r.get("text"):
            replies.append({"label": str(r.get("label") or "Suggested reply")[:60],
                            "text": str(r["text"])[:900]})

    return {
        "summary": str(data.get("summary") or "").strip()
                   or "Briefing unavailable — read the transcript.",
        "customer_state": str(data.get("customer_state") or "")[:120],
        "watch_out": str(data.get("watch_out") or "")[:300],
        "open_questions": [str(q)[:160] for q in (data.get("open_questions") or [])][:4],
        "suggested_replies": replies,
        "emotion": last_customer.get("emotion"),
        "intent": last_customer.get("intent"),
        "resolved_sku": (session or {}).get("resolved_sku"),
        "facts": facts,
    }
