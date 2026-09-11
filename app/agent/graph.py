"""The agent loop: ingest → rewrite → perceive → disambiguate → plan/act → guard →
compose → persist.

Written as explicit async nodes rather than assembled with LangGraph. The deciding
factor was streaming: every node here emits SSE while it runs — stage pills, friendly
thinking narration, per-tool call and result events — and the customer must never watch
a blank screen. Driving that through a graph framework's callback layer costs more
control than the framework's scheduling is worth for a linear pipeline with one loop.

The one thing LangGraph would have given us for free is interrupt/resume, so that is
built explicitly: `disambiguate` can pause the turn, write a checkpoint to Postgres,
and `resume_from_action()` picks the same turn back up after the user clicks. Surviving
a page reload was the requirement, and a process-memory graph would not have.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

import structlog

from app.agent import guard as guard_mod
from app.agent import prompts, tools
from app.agent.policy import SAFETY_INSTRUCTION, policy_for
from app.clients.rkapi import get_rkapi, parse_json_loose
from app.config import settings
from app.schemas.agent import (AgentState, Citation, Entities, Intent, Perception,
                               ResolvedProduct, ToolCall, ToolResult, Urgency)
from app.schemas.blocks import (Block, BlockType, DiagnosticStep, DiagnosticStepsPayload,
                                HumanHandoffPayload, OrderCardPayload, OrderItemView,
                                ProductCardItem, ProductGridPayload, ProductOption,
                                TicketStatusPayload, WarrantyResultPayload,
                                diagnostic_steps, human_handoff, product_picker,
                                quick_replies, warranty_result)
from app.services import products as product_svc
from app.services import sessions as session_svc
from app.sse import Event, SSEStream

log = structlog.get_logger(__name__)

STAGE_LABELS = {
    "understand": "Reading your message",
    "photo": "Looking at your photo",
    "identify": "Working out which device this is",
    "investigate": "Digging into it",
    "check": "Double-checking before I answer",
    "answer": "Writing your answer",
}


class Agent:
    def __init__(self, stream: SSEStream):
        self.stream = stream
        self.llm = get_rkapi()
        self.traces: List[Dict[str, Any]] = []

    # ── entry points ──────────────────────────────────────────────────────────

    async def run(self, state: AgentState) -> AgentState:
        try:
            await self._ingest(state)
            if await self._safety_shortcut(state):
                return state
            # Both read the raw message and the history; neither reads the other's
            # output. Running them in sequence spent a whole round trip for nothing, and
            # on a 3-lane key pool a round trip is seconds the customer is watching.
            await asyncio.gather(self._rewrite(state), self._perceive(state))
            paused = await self._disambiguate(state)
            if paused:
                return state
            await self._react(state)
            await self._compose(state)
        except Exception as e:  # noqa: BLE001 — one turn failing must not kill the stream
            log.exception("agent.failed", session=state.session_id)
            await self.stream.error("INTERNAL", f"{type(e).__name__}: {str(e)[:200]}",
                                    retryable=True)
            raise
        return state

    async def resume(self, state: AgentState, action_id: str,
                     value: Dict[str, Any]) -> AgentState:
        """Continue a paused turn after a block action, without replaying it."""
        await self.stream.stage_start("understand", "Picking up where we left off")
        if action_id == "select_product":
            row = await product_svc.get_product(value.get("sku", ""))
            if row:
                state.resolved = ResolvedProduct(
                    sku=row["sku"], name=row["name"], brand=row["brand"],
                    product_id=row["product_id"], category=row.get("category"), how="user_pick")
                await session_svc.set_resolved_sku(state.session_id, row["sku"])
        elif action_id == "step_result":
            outcome = value.get("outcome", "failed")
            state.observations.append(ToolResult(
                call_id="resume", tool=f"step_result_{outcome}", ok=True,
                summary=f"customer reports: {outcome}",
                data={"outcome": outcome, "step_id": value.get("step_id")}))
            state.user_message = {
                "worked": "That fixed it.",
                "failed": "I tried that and it still isn't working.",
                "stuck": "I'm stuck on that step.",
            }.get(outcome, state.user_message)
        elif action_id in ("confirm_handoff", "open_ticket"):
            state.user_message = "Please put me through to a person."
            state.perception.intent = Intent.ESCALATE_REQUEST
        elif action_id == "quick_reply":
            state.user_message = value.get("text", state.user_message)
        elif action_id == "submit":
            state.observations.append(ToolResult(
                call_id="resume", tool="form_submitted", ok=True,
                summary="customer filled in the form", data=value))
        await self.stream.stage_complete("understand")

        await self._react(state)
        await self._compose(state)
        return state

    # ── nodes ─────────────────────────────────────────────────────────────────

    async def _ingest(self, state: AgentState) -> None:
        """Attachments are processed at upload time, so this reads facts rather than
        computing them — the turn should not pay for vision twice."""
        if not state.attachment_ids:
            return
        await self.stream.stage_start("photo", STAGE_LABELS["photo"])
        from app.services.vision import get_facts
        facts = await get_facts(state.attachment_ids)
        state.vlm_facts = facts
        for f in facts:
            caption = f.get("caption", "")
            if caption:
                await self.stream.thinking("photo", f"I can see {caption.lower()}")
            detected = f.get("detected") or {}
            if detected.get("error_code"):
                await self.stream.thinking(
                    "photo", f" — and the code {detected['error_code']} on the display.")
        await self.stream.stage_complete("photo")

    async def _safety_shortcut(self, state: AgentState) -> bool:
        """A burning smell does not wait for a ReAct loop.

        Runs on the raw text before any model call, because the cheapest way to be fast
        about danger is not to think about it first.
        """
        from app.services.warranty import detect_safety_concern
        texts = [state.user_message] + [f.get("caption", "") + " " + f.get("ocr_text", "")
                                        for f in state.vlm_facts]
        flags = [flag for f in state.vlm_facts for flag in (f.get("safety_flags") or [])]
        if not (detect_safety_concern(*texts) or flags):
            return False

        state.perception.safety_concern = True
        state.perception.intent = Intent.ESCALATE_REQUEST
        log.warning("agent.safety_shortcut", session=state.session_id)
        await self.stream.emit(Event.EMOTION, {"emotion": "anxious", "intensity": 0.9,
                                               "urgency": {"level": "high",
                                                           "has_deadline": False}})
        await self.stream.stage_start("answer", "This one needs immediate attention")

        result = await tools.run_tool(state, "create_ticket", {
            "summary": f"SAFETY: {state.user_message[:160]}",
            "priority": "urgent", "reason": "safety keywords detected"})
        state.observations.append(result)
        self._trace(result)
        if result.ok:
            state.ticket_id = result.data.get("ticket_no")

        await self._stream_answer(state, extra_instruction=SAFETY_INSTRUCTION)
        if result.ok:
            # The ticket is the customer's handle on this. Render it, don't just mention it.
            block = Block(type=BlockType.TICKET_STATUS, payload=TicketStatusPayload(
                ticket_id=result.data.get("ticket_no", ""), status="open", priority="urgent",
                summary="Safety issue — escalated to a specialist",
                eta=result.data.get("eta", "within 1 hour")).model_dump())
            state.blocks.append(block)
            await self.stream.block(block.model_dump())
        await self.stream.stage_complete("answer")
        if result.ok:
            await self.stream.emit(Event.TICKET_UPDATE, {
                "ticket_id": result.data.get("ticket_no"), "status": "open",
                "priority": "urgent", "summary": "Safety escalation"})
        return True

    async def _rewrite(self, state: AgentState) -> None:
        """Only worth a call when there is history to resolve against."""
        if not state.history:
            state.rewritten_query = state.user_message
            return
        history = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in state.history[-6:])
        data, usage = await self.llm.json_complete(
            [{"role": "system", "content": prompts.REWRITE_SYSTEM},
             {"role": "user", "content": f"{history}\nuser: {state.user_message}"}],
            max_tokens=1200, default={"query": state.user_message},
        )
        state.add_usage(usage)
        state.rewritten_query = (data.get("query") or state.user_message).strip()
        if state.rewritten_query != state.user_message:
            log.debug("agent.rewritten", before=state.user_message[:80],
                      after=state.rewritten_query[:80])

    async def _perceive(self, state: AgentState) -> None:
        await self.stream.stage_start("understand", STAGE_LABELS["understand"])
        history = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in state.history[-4:]) \
            or "(this is the first message)"
        vlm = json.dumps([{k: v for k, v in f.items() if k != "raw"} for f in state.vlm_facts],
                         ensure_ascii=False) if state.vlm_facts else "(no photos)"
        data, usage = await self.llm.json_complete(
            [{"role": "system", "content": prompts.PERCEIVE_SYSTEM},
             {"role": "user", "content": prompts.PERCEIVE_USER.format(
                 history=history, vlm=vlm, message=state.user_message)}],
            max_tokens=2500, default={},
        )
        state.add_usage(usage)
        state.perception = _coerce_perception(data, state.user_message)
        await self.stream.emit(Event.EMOTION, {
            "emotion": state.perception.emotion.value,
            "intensity": state.perception.intensity,
            "urgency": state.perception.urgency.model_dump(),
        })
        await self.stream.stage_complete("understand")

    async def _disambiguate(self, state: AgentState) -> bool:
        """Resolve the product, or pause and ask. Returns True if the turn paused."""
        mentions = state.perception.entities.product_mentions
        if not mentions:
            return False
        await self.stream.stage_start("identify", STAGE_LABELS["identify"])
        resolved, candidates, how = await product_svc.disambiguate(
            mentions, f"{state.user_message} {state.rewritten_query}",
            customer_id=state.customer_id, vlm_facts=state.vlm_facts,
        )
        state.candidates = candidates

        if resolved:
            state.resolved = ResolvedProduct(
                sku=resolved["sku"], name=resolved["name"], brand=resolved["brand"],
                product_id=resolved["product_id"], category=resolved.get("category"), how=how)
            await session_svc.set_resolved_sku(state.session_id, resolved["sku"])
            if how != "alias_unique":
                # Say which way the guess went, so a wrong one is cheap to correct.
                await self.stream.thinking(
                    "identify", f"Going by {_how_phrase(how)}, this is the {resolved['name']}.")
            await self.stream.stage_complete("identify")
            return False

        if len(candidates) > 1:
            question = await self._picker_question(mentions[0], candidates)
            options = [ProductOption(
                sku=c["sku"], name=c["name"], brand=c["brand"],
                image_url=c.get("hero_image"), price=c.get("price"),
                category=c.get("category"), hint=_option_hint(c)) for c in candidates[:4]]
            block = product_picker(question, options)
            state.blocks.append(block)
            state.awaiting_action = True
            await self.stream.stage_complete("identify")
            await self.stream.content(question)
            state.answer = question
            await self.stream.block(block.model_dump())
            await session_svc.save_checkpoint(state.session_id, {
                "message_id": state.message_id,
                "user_message": state.user_message,
                "rewritten_query": state.rewritten_query,
                "perception": state.perception.model_dump(),
                "candidates": candidates,
                "vlm_facts": state.vlm_facts,
                "attachment_ids": state.attachment_ids,
                "turn": state.turn,
            })
            log.info("agent.paused_for_pick", n=len(candidates))
            return True

        await self.stream.stage_complete("identify")
        return False

    async def _picker_question(self, mention: str, candidates: List[Dict[str, Any]]) -> str:
        options = ", ".join(f"{c['brand']} {c['name']}" for c in candidates[:4])
        try:
            data, usage = await self.llm.json_complete(
                [{"role": "user", "content": prompts.PICKER_QUESTION.format(
                    mention=mention, options=options)}],
                max_tokens=900, default={},
            )
            if data.get("question"):
                return data["question"]
        except Exception as e:  # noqa: BLE001
            log.warning("agent.picker_question_failed", error=str(e)[:120])
        return (f"Quick check — \"{mention}\" is used for more than one of our products. "
                "Which of these is yours?")

    async def _react(self, state: AgentState) -> None:
        """The reasoning loop. Think, act, observe, repeat — with a hard iteration cap."""
        await self.stream.stage_start("investigate", STAGE_LABELS["investigate"])
        seen_calls: set = set()

        for _ in range(settings.max_react_iterations):
            state.iterations += 1
            decision = await self._plan(state)
            thought = (decision.get("thought") or "").strip()
            action = (decision.get("action") or "answer").strip()
            args = decision.get("args") or {}

            if thought:
                await self.stream.thinking("investigate", thought)
                state.scratchpad.append(thought)

            if action in ("answer", "", "none", "final"):
                break

            signature = f"{action}:{json.dumps(args, sort_keys=True, default=str)}"
            if signature in seen_calls:
                state.scratchpad.append(
                    f"(already called {action} with those arguments — moving on)")
                break
            seen_calls.add(signature)

            tool = tools.REGISTRY.get(action)
            label = decision.get("user_facing") or (tool.label if tool else action)
            call = ToolCall(call_id=f"call_{state.iterations}", tool=action, args=args)
            state.tool_calls.append(call)
            await self.stream.emit(Event.TOOL_CALL, {
                "call_id": call.call_id, "tool": action, "label": label,
                "args_preview": _preview(args)})

            result = await tools.run_tool(state, action, args, call_id=call.call_id)
            state.observations.append(result)
            self._trace(result)
            await self.stream.emit(Event.TOOL_RESULT, {
                "call_id": result.call_id, "ok": result.ok, "ms": result.ms,
                "summary": result.summary})

            await self._absorb(state, result)

            if _is_enough(state, result):
                break

        await self.stream.stage_complete("investigate")

    async def _plan(self, state: AgentState) -> Dict[str, Any]:
        situation = self._situation(state)
        excluded = [] if state.resolved else ["get_troubleshooting_flow"]
        data, usage = await self.llm.json_complete(
            [{"role": "system", "content": prompts.PLAN_SYSTEM.format(
                tools=tools.tool_specs(excluded), situation=situation)},
             {"role": "user", "content": state.rewritten_query or state.user_message}],
            max_tokens=2000, default={"action": "answer"},
        )
        state.add_usage(usage)
        return data

    def _situation(self, state: AgentState) -> str:
        """What the planner knows. Observations are summarised, not pasted: they are the
        fastest-growing part of the context and the least re-read."""
        lines = [f"Customer said: {state.user_message}"]
        if state.rewritten_query and state.rewritten_query != state.user_message:
            lines.append(f"Which means: {state.rewritten_query}")
        p = state.perception
        lines.append(f"They sound {p.emotion.value} ({p.intensity:.1f}); intent is "
                     f"{p.intent.value}.")
        if p.urgency.has_deadline:
            lines.append(f"Deadline: {p.urgency.deadline_hint}")
        if p.entities.error_codes:
            lines.append(f"Error codes mentioned: {', '.join(p.entities.error_codes)}")
        if p.entities.order_refs:
            lines.append(f"Order references: {', '.join(p.entities.order_refs)}")
        if state.customer_email:
            lines.append(f"Customer email on file: {state.customer_email}")
        if state.resolved:
            lines.append(f"Product resolved: {state.resolved.name} (SKU {state.resolved.sku}, "
                         f"{state.resolved.category}) — identified via {state.resolved.how}")
        elif state.candidates:
            lines.append("Product still ambiguous between: " +
                         ", ".join(c["name"] for c in state.candidates[:4]))
        if state.vlm_facts:
            lines.append("Photo shows: " + "; ".join(
                f.get("caption", "") for f in state.vlm_facts if f.get("caption")))
        if state.observations:
            lines.append("\nWhat you have found so far:")
            for o in state.observations[-6:]:
                lines.append(f"  {o.compact(900)}")
        else:
            lines.append("\nNo tools called yet.")
        return "\n".join(lines)

    async def _absorb(self, state: AgentState, result: ToolResult) -> None:
        """Fold a tool result back into state so later nodes do not re-derive it."""
        if not result.ok:
            return
        if result.tool == "search_products" and not state.resolved:
            found = result.data.get("products") or []
            if len(found) == 1 and found[0].get("sku"):
                row = await product_svc.get_product(found[0]["sku"])
                if row:
                    state.resolved = ResolvedProduct(
                        sku=row["sku"], name=row["name"], brand=row["brand"],
                        product_id=row["product_id"], category=row.get("category"),
                        how="vector")
        if result.tool == "lookup_order" and result.data.get("found"):
            items = result.data.get("items") or []
            if items and items[0].get("sku") and not state.resolved:
                row = await product_svc.get_product(items[0]["sku"])
                if row:
                    state.resolved = ResolvedProduct(
                        sku=row["sku"], name=row["name"], brand=row["brand"],
                        product_id=row["product_id"], category=row.get("category"),
                        how="purchase_history")
        if result.tool == "create_ticket" and result.data.get("created"):
            state.ticket_id = result.data.get("ticket_no")

    async def _compose(self, state: AgentState) -> None:
        """Write the answer live, then check it — and rewrite in place if the check fails.

        The obvious order is check-then-show, but it costs the customer the entire
        composition time staring at a spinner. Most drafts pass the guard, so we stream
        optimistically and keep `content_reset` for the minority that do not. That event
        exists in the contract for exactly this: discard what was painted and start again.
        """
        sources = self._sources(state)

        await self.stream.stage_start("answer", STAGE_LABELS["answer"])
        draft = await self._draft(state, sources, stream_live=True)
        await self.stream.stage_complete("answer")

        await self.stream.stage_start("check", STAGE_LABELS["check"])
        hits, must_replan = guard_mod.check(state, draft)
        state.guard_hits.extend(hits)
        if hits:
            instruction = guard_mod.repair_instruction(hits, state)
            log.info("agent.guard_repair", rules=[h.rule_id for h in hits])
            if must_replan and _missing_warranty_call(hits):
                # G1: the rule engine genuinely has to run. Call it, then re-draft.
                if await self._force_warranty(state):
                    sources = self._sources(state)
            # Take back what was shown before writing the corrected version. Leaving a
            # retracted warranty promise on screen would be worse than the slower path.
            await self.stream.emit(Event.CONTENT_RESET, {})
            draft = await self._draft(state, sources, extra_instruction=instruction)
            for h in hits:
                h.repaired = True
            hits2, _ = guard_mod.check(state, draft)
            state.guard_hits.extend(hits2)
            answer, suggestions = _split_suggestions(draft)
            await self._emit_text(answer)
        else:
            answer, suggestions = _split_suggestions(draft)
        await self.stream.stage_complete("check")

        state.answer = answer
        state.suggestions = suggestions
        await self._emit_citations(state, sources)
        await self._emit_blocks(state)
        if suggestions:
            await self.stream.emit(Event.SUGGESTIONS,
                                   {"items": [{"text": s} for s in suggestions]})

    async def _draft(self, state: AgentState, sources: List[Citation],
                     extra_instruction: str = "", stream_live: bool = False) -> str:
        policy = policy_for(state.perception)
        policy_text = policy.as_prompt(state.perception)
        if extra_instruction:
            policy_text += "\n\nCORRECTIONS — the previous draft broke these rules:\n" \
                           + extra_instruction
        observations = "\n\n".join(o.compact(2200) for o in state.observations) \
            or "(no tools were needed)"
        source_lines = "\n".join(
            f"[{c.n}] {c.title} — {c.section or c.url}" for c in sources) or "(none)"
        context = self._context_line(state)
        messages = [
            {"role": "system", "content": prompts.COMPOSE_SYSTEM.format(
                policy=policy_text, language=_language_name(state.perception.language))},
            {"role": "user", "content": prompts.COMPOSE_USER.format(
                message=state.user_message, context=context,
                observations=observations, sources=source_lines)},
        ]

        if not stream_live:
            text, usage = await self.llm.complete(messages, max_tokens=3000)
            state.add_usage(usage)
            return text.strip()

        # Live path: forward tokens as they arrive, but hold back the trailing
        # SUGGESTIONS line — it is machine-readable scaffolding, not part of the reply,
        # and the customer should never watch it being typed.
        parts: List[str] = []
        shown = 0
        async for chunk in self.llm.stream(messages, max_tokens=3000):
            if chunk["type"] == "usage":
                state.add_usage(chunk["usage"])
                continue
            parts.append(chunk["text"])
            whole = "".join(parts)
            cut = _suggestions_cut(whole)
            if cut > shown:
                await self.stream.content(whole[shown:cut])
                shown = cut
        return "".join(parts).strip()

    async def _stream_answer(self, state: AgentState, extra_instruction: str = "") -> None:
        """Compose and stream in one pass — used by the safety path, which has no guard
        repair step to wait for."""
        draft = await self._draft(state, [], extra_instruction=extra_instruction)
        answer, suggestions = _split_suggestions(draft)
        state.answer = answer
        state.suggestions = suggestions
        await self._emit_text(answer)

    async def _emit_text(self, answer: str) -> None:
        """Paint in word-sized pieces. The draft already exists, but delivering it in one
        block reads as a stall to someone who has been watching stage pills."""
        for piece in re.findall(r"\S+\s*", answer):
            await self.stream.content(piece)
            await asyncio.sleep(0)

    async def _force_warranty(self, state: AgentState) -> bool:
        """G1 repair: run the rule engine with what the turn already knows."""
        order = state.observation_by_tool("lookup_order")
        dealer = state.observation_by_tool("lookup_dealer_order")
        args: Dict[str, Any] = {"order_found": bool(order and order.data.get("found"))}
        if order and order.data.get("found"):
            args["channel"] = order.data.get("channel")
            args["purchase_date"] = order.data.get("purchase_date")
        if dealer:
            args["dealer_matched"] = bool(dealer.data.get("found"))
            if dealer.data.get("found"):
                args["channel"] = "dealer"
                args["purchase_date"] = dealer.data.get("purchase_date")
        result = await tools.run_tool(state, "check_warranty", args)
        state.observations.append(result)
        self._trace(result)
        await self.stream.emit(Event.TOOL_RESULT, {
            "call_id": result.call_id, "ok": result.ok, "ms": result.ms,
            "summary": result.summary})
        return result.ok

    def _sources(self, state: AgentState) -> List[Citation]:
        """Number every retrieved passage once, in the order the composer will meet it."""
        citations: List[Citation] = []
        seen: set = set()
        for o in state.observations:
            if not o.ok:
                continue
            if o.tool == "search_kb":
                for p in o.data.get("passages", []):
                    key = (p.get("url"), p.get("section"))
                    if key in seen or not p.get("title"):
                        continue
                    seen.add(key)
                    citations.append(Citation(
                        n=len(citations) + 1, title=p.get("title", ""), url=p.get("url", ""),
                        section=p.get("section", ""), sku=p.get("sku"), page=p.get("page")))
            elif o.tool == "get_troubleshooting_flow" and o.data.get("found"):
                url = o.data.get("source_url") or ""
                if url not in seen:
                    seen.add(url)
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"Troubleshooting: {o.data.get('symptom', '')}", url=url))
            elif o.tool == "lookup_error_code" and o.data.get("found"):
                for m in o.data.get("matches", [])[:2]:
                    url = m.get("source_url") or ""
                    key = ("code", m.get("code"))
                    if key in seen:
                        continue
                    seen.add(key)
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"Error {m.get('code')}: {m.get('meaning', '')[:60]}",
                        url=url, sku=m.get("sku")))
            elif o.tool == "web_search":
                for r in o.data.get("results", [])[:3]:
                    if r.get("url") in seen:
                        continue
                    seen.add(r.get("url"))
                    citations.append(Citation(n=len(citations) + 1, title=r.get("title", ""),
                                              url=r.get("url", "")))
        return citations[:8]

    async def _emit_citations(self, state: AgentState, sources: List[Citation]) -> None:
        """Only emit citations the answer actually used — a chip pointing at a passage
        the text never referenced is noise dressed as rigour."""
        used = {int(n) for n in re.findall(r"\[(\d{1,2})\]", state.answer)}
        for c in sources:
            if c.n in used:
                state.citations.append(c)
                await self.stream.emit(Event.CITATION, c.model_dump())

    async def _emit_blocks(self, state: AgentState) -> None:
        """Turn tool results into interactive UI. This is where the answer stops being
        text and becomes something the customer can act on."""
        for block in _blocks_from_state(state):
            state.blocks.append(block)
            await self.stream.block(block.model_dump())

    def _context_line(self, state: AgentState) -> str:
        bits = []
        if state.resolved:
            bits.append(f"Their device: {state.resolved.brand} {state.resolved.name} "
                        f"(SKU {state.resolved.sku})")
            if state.resolved.how in ("purchase_history", "symptom_vocabulary", "photo"):
                bits.append(f"(worked out from {_how_phrase(state.resolved.how)} — say so "
                            "in passing so they can correct you)")
        if state.customer_email:
            bits.append(f"Account: {state.customer_email}")
        p = state.perception
        bits.append(f"They sound {p.emotion.value}.")
        if p.urgency.has_deadline:
            bits.append(f"Deadline: {p.urgency.deadline_hint}.")
        if state.ticket_id:
            bits.append(f"Ticket opened: {state.ticket_id}")
        return " ".join(bits)

    def _trace(self, result: ToolResult) -> None:
        self.traces.append({"call_id": result.call_id, "tool": result.tool,
                            "args": {}, "result": result.data, "ms": result.ms,
                            "ok": result.ok})


# ── helpers ───────────────────────────────────────────────────────────────────

def _coerce_perception(data: Dict[str, Any], fallback_message: str) -> Perception:
    """Model output into a typed Perception, tolerating every shape it actually emits."""
    def enum_or(value: Any, enum_cls, default):
        try:
            return enum_cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return default

    from app.schemas.agent import Emotion
    urgency_raw = data.get("urgency") or {}
    entities_raw = data.get("entities") or {}
    return Perception(
        emotion=enum_or(data.get("emotion"), Emotion, Emotion.CALM),
        intensity=float(data.get("intensity") or 0.0),
        urgency=Urgency(
            has_deadline=bool(urgency_raw.get("has_deadline")),
            deadline_hint=str(urgency_raw.get("deadline_hint") or ""),
            level=str(urgency_raw.get("level") or "normal"),
        ),
        intent=enum_or(data.get("intent"), Intent, Intent.UNCLEAR),
        entities=Entities(
            product_mentions=[str(x) for x in (entities_raw.get("product_mentions") or [])][:5],
            error_codes=[str(x) for x in (entities_raw.get("error_codes") or [])][:5],
            order_refs=[str(x) for x in (entities_raw.get("order_refs") or [])][:5],
            purchase_channel_hint=entities_raw.get("purchase_channel_hint"),
            symptoms=[str(x) for x in (entities_raw.get("symptoms") or [])][:6],
        ),
        language=str(data.get("language") or "en")[:8],
        needs_image=bool(data.get("needs_image")),
        safety_concern=bool(data.get("safety_concern")),
        summary=str(data.get("summary") or fallback_message[:160]),
    )


def _how_phrase(how: str) -> str:
    return {
        "purchase_history": "your order history",
        "symptom_vocabulary": "what you described",
        "photo": "your photo",
        "vector": "the closest match in the catalog",
        "user_pick": "what you picked",
    }.get(how, "what you told me")


def _option_hint(c: Dict[str, Any]) -> str:
    return {
        "robot_vacuum": "The robot vacuum that docks in a station",
        "breast_pump": "The wearable pump",
        "charger": "The wall charger",
        "power_bank": "The portable battery",
        "power_station": "The large portable power station",
        "audio": "The earbuds/headphones",
        "security_camera": "The security camera",
        "projector": "The projector",
    }.get(c.get("category") or "", c.get("brand", ""))


def _preview(args: Dict[str, Any]) -> str:
    if not args:
        return ""
    parts = [f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3]]
    return ", ".join(parts)


def _is_enough(state: AgentState, result: ToolResult) -> bool:
    """Early exits that save the customer a wait.

    A warranty verdict is terminal by construction: the engine has spoken and no further
    tool changes what the answer must say.
    """
    if result.tool == "check_warranty" and result.ok:
        return True
    if result.tool == "create_ticket" and result.ok:
        return True
    return False


def _missing_warranty_call(hits) -> bool:
    return any(h.rule_id == "G1" for h in hits)


def _suggestions_cut(text: str) -> int:
    """How much of a partially-streamed draft is safe to show.

    The composer ends with a `SUGGESTIONS:` line that becomes chips, not prose. While
    streaming we cannot know whether a trailing "SUGG" is the start of that marker or a
    real word, so anything that could still grow into the marker is withheld until the
    next token settles it. Worst case a few characters arrive one chunk late.
    """
    marker = re.search(r"\n?\s*SUGGESTIONS\s*:", text, re.IGNORECASE)
    if marker:
        return marker.start()
    tail = text[-14:]
    for i in range(len(tail)):
        candidate = tail[i:].lstrip("\n ").upper()
        if candidate and "SUGGESTIONS:".startswith(candidate):
            return len(text) - len(tail) + i
    return len(text)


def _split_suggestions(draft: str) -> tuple[str, List[str]]:
    """Pull the trailing SUGGESTIONS line off the answer."""
    match = re.search(r"^\s*SUGGESTIONS\s*:\s*(.+)$", draft, re.MULTILINE | re.IGNORECASE)
    if not match:
        return draft.strip(), []
    raw = match.group(1).strip()
    answer = draft[:match.start()].strip()
    if raw.lower() in ("none", "-", "n/a"):
        return answer, []
    items = [s.strip(" -•") for s in raw.split("|")]
    return answer, [s for s in items if s][:3]


def _language_name(code: str) -> str:
    return {"en": "English", "zh": "Chinese", "id": "Indonesian", "de": "German",
            "fr": "French", "es": "Spanish", "ja": "Japanese", "ko": "Korean"}.get(
        (code or "en").lower()[:2], "the same language the customer wrote in")


def _blocks_from_state(state: AgentState) -> List[Block]:
    """Which interactive blocks this turn earned."""
    blocks: List[Block] = []

    flow = state.observation_by_tool("get_troubleshooting_flow")
    if flow and flow.data.get("found"):
        steps_raw = flow.data.get("steps") or []
        steps = []
        for i, s in enumerate(steps_raw[:8], start=1):
            if isinstance(s, str):
                steps.append(DiagnosticStep(step_id=f"s{i}", ord=i, instruction=s))
            elif isinstance(s, dict):
                steps.append(DiagnosticStep(
                    step_id=s.get("step_id") or f"s{i}", ord=i,
                    instruction=s.get("instruction") or s.get("text") or "",
                    why=s.get("why", ""), expected=s.get("expected", ""),
                    image_url=s.get("image_url")))
        if steps:
            blocks.append(diagnostic_steps(DiagnosticStepsPayload(
                title=f"Fixing: {flow.data.get('symptom', 'the problem')}",
                symptom=flow.data.get("symptom", ""),
                estimated_minutes=flow.data.get("estimated_minutes"),
                steps=steps, current_step=steps[0].step_id)))

    order = state.observation_by_tool("lookup_order")
    if order and order.data.get("found"):
        d = order.data
        blocks.append(Block(type=BlockType.ORDER_CARD, payload=OrderCardPayload(
            order_no=d.get("order_no", ""), channel=d.get("channel", ""),
            purchase_date=d.get("purchase_date"), status=d.get("status"),
            items=[OrderItemView(sku=i.get("sku"), name=i.get("name") or "",
                                 qty=i.get("qty") or 1, serial=i.get("serial"))
                   for i in d.get("items", [])],
        ).model_dump()))

    dealer = state.observation_by_tool("lookup_dealer_order")
    if dealer and dealer.data.get("found"):
        d = dealer.data
        blocks.append(Block(type=BlockType.ORDER_CARD, payload=OrderCardPayload(
            order_no=d.get("order_no", ""), channel="dealer",
            purchase_date=d.get("purchase_date"),
            dealer_name=(d.get("dealer") or {}).get("name"),
            items=[OrderItemView(sku=d.get("sku"), name=d.get("product_name") or "")],
        ).model_dump()))

    w = state.observation_by_tool("check_warranty")
    if w and w.data.get("decided"):
        d = w.data
        dealer_info = None
        if dealer and (dealer.data.get("dealer") or dealer.data.get("found")):
            dealer_info = dealer.data.get("dealer") or {
                "name": (dealer.data.get("dealer") or {}).get("name")}
        blocks.append(warranty_result(WarrantyResultPayload(
            verdict=d.get("verdict", ""), reason_code=d.get("reason_code", ""),
            explanation=d.get("explanation", ""),
            required_evidence=d.get("required_evidence") or [],
            next_action=d.get("next_action"), dealer=dealer_info,
            warranty_until=d.get("warranty_until"))))

    ticket = state.observation_by_tool("create_ticket")
    if ticket and ticket.data.get("created"):
        d = ticket.data
        blocks.append(Block(type=BlockType.TICKET_STATUS, payload=TicketStatusPayload(
            ticket_id=d.get("ticket_no", ""), status=d.get("status", "open"),
            priority=d.get("priority", "normal"), summary=d.get("summary", ""),
            eta=d.get("eta")).model_dump()))

    search = state.observation_by_tool("search_products")
    if search and state.perception.intent in (Intent.BUY_ADVICE, Intent.PRODUCT_QUESTION):
        items = [ProductCardItem(
            sku=p.get("sku", ""), name=p.get("name", ""), price=p.get("price"),
            image_url=p.get("image_url"), url=p.get("url"),
            badges=["discontinued"] if p.get("status") == "discontinued" else [])
            for p in (search.data.get("products") or [])[:6] if p.get("sku")]
        if items:
            blocks.append(Block(type=BlockType.PRODUCT_GRID,
                                payload=ProductGridPayload(items=items).model_dump()))

    policy = policy_for(state.perception)
    failed = sum(1 for o in state.observations if o.tool == "step_result_failed")
    if policy.offer_human_early and not state.ticket_id and failed >= 1:
        blocks.append(human_handoff(HumanHandoffPayload(
            reason="A couple of fixes have not worked and you have a deadline.",
            eta_minutes=8, channels=["chat", "email"],
            summary_preview=state.perception.summary)))

    if state.suggestions:
        blocks.append(quick_replies(state.suggestions))
    return blocks
