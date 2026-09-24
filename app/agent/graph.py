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
import uuid
from typing import Any, Dict, List, Optional

import structlog

from app.agent import guard as guard_mod
from app.agent import prompts, tools
from app.agent.policy import SAFETY_INSTRUCTION, policy_for
from app.clients.llm import get_llm
from app.clients.rkapi import parse_json_loose
from app.config import settings
from app.schemas.agent import (AgentState, Citation, Entities, Intent, Perception,
                               ResolvedProduct, ToolCall, ToolResult, Urgency)
from app.schemas.blocks import (Block, BlockType, DiagnosticStep, DiagnosticStepsPayload,
                                HumanHandoffPayload, OrderCardPayload, OrderItemView,
                                ProductCardItem, ProductGridPayload, ProductOption,
                                TicketStatusPayload, WarrantyResultPayload,
                                diagnostic_steps, human_handoff, product_picker,
                                warranty_result)
from app.services import products as product_svc
from app.services import sessions as session_svc
from app.sse import Event, SSEStream

log = structlog.get_logger(__name__)

# The pipeline, as the interface shows it. Each entry is one step in a visible
# left-to-right flow, so a judge can watch the agent think and line each step up against
# the architecture rather than taking "it reasoned" on trust.
#
# `label` is what the customer reads; `description` explains the step's job; `node` is
# the internal name. `investigate` is the only step that repeats — the ReAct loop — and
# its events carry an iteration number so the UI can show it looping instead of
# pretending the pipeline is a straight line.
STEPS = {
    "photo":       (1, "Reading your photo",
                    "Vision model extracts the error code, device type and any damage"),
    "understand":  (2, "Understanding you",
                    "One call classifies emotion, urgency, intent and the entities mentioned"),
    "identify":    (3, "Identifying the device",
                    "Alias table first, then purchase history, symptom wording, photo — "
                    "asks only if those cannot decide"),
    "investigate": (4, "Investigating",
                    "ReAct loop: think, call a tool, read the result, decide whether to "
                    "continue"),
    "answer":      (5, "Writing the answer",
                    "Composes the reply under the emotion policy for this conversation"),
    "check":       (6, "Checking against the rules",
                    "Six guard rules run on the draft; a violation rewrites it before you "
                    "see it"),
}

STAGE_LABELS = {k: v[1] for k, v in STEPS.items()}


class Agent:
    def __init__(self, stream: SSEStream):
        self.stream = stream
        self.llm = get_llm()
        self.traces: List[Dict[str, Any]] = []

    async def _step_start(self, key: str, iteration: int = 0,
                          label: Optional[str] = None) -> None:
        ord_, default_label, description = STEPS[key]
        await self.stream.stage_start(key, label or default_label, description=description,
                                      ord=ord_, node=key, iteration=iteration)

    async def _step_done(self, key: str, **detail: Any) -> None:
        """Close a step with what it concluded — the intent it classified, the device it
        resolved, the verdict it reached. A stepper without that shows activity; with it,
        it shows reasoning."""
        await self.stream.stage_complete(key, detail={k: v for k, v in detail.items()
                                                      if v not in (None, "", [], {})})

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
            if state.perception.fix_failed:
                await self._count_failure(state)
            if state.safety_case:
                # Stays a safety conversation; the policy table then keeps it calm,
                # question-free and upsell-free, and the engine keeps escalating it.
                state.perception.safety_concern = True
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

    async def _count_failure(self, state: AgentState) -> None:
        state.failed_attempts += 1
        try:
            await session_svc.set_meta(state.session_id, "failed_attempts", state.failed_attempts)
        except Exception as e:  # noqa: BLE001 — the count is advisory, never a gate
            log.warning("agent.failed_attempts_persist", error=str(e))

    async def resume(self, state: AgentState, action_id: str,
                     value: Dict[str, Any]) -> AgentState:
        """Continue a paused turn after a block action, without replaying it."""
        await self._step_start("understand", label="Picking up where we left off")
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
            if outcome == "failed":
                state.perception.fix_failed = True
                await self._count_failure(state)
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
        await self._step_done("understand", action=action_id,
                              resolved=state.resolved.name if state.resolved else None)

        await self._react(state)
        await self._compose(state)
        return state

    # ── nodes ─────────────────────────────────────────────────────────────────

    async def _ingest(self, state: AgentState) -> None:
        """Read the photo facts. Computed at upload time, so this never pays for vision
        twice — and it reads the whole CONVERSATION's photos, not just this turn's.

        A customer sends a picture, then asks three follow-up questions about it. Only
        the first of those turns carries an attachment, so scoping this to the turn made
        the agent forget the device it had been looking at seconds earlier. It also made
        a reopened session blind to everything the customer had ever sent.
        """
        from app.services.vision import get_facts, session_facts

        facts = await get_facts(state.attachment_ids) if state.attachment_ids else []
        new_ids = {f.get("attachment_id") for f in facts}
        earlier = [f for f in await session_facts(state.session_id)
                   if f.get("attachment_id") not in new_ids]
        state.vlm_facts = earlier + facts
        if not state.vlm_facts:
            return
        # The stage only narrates photos that arrived on THIS turn: "I can see…" about a
        # picture from four turns ago reads as the system losing track of the thread.
        if not facts:
            await self._step_done("photo", photos=len(earlier), from_earlier_turns=True)
            return
        await self._step_start("photo")
        for f in facts:
            caption = f.get("caption", "")
            if caption:
                await self.stream.thinking("photo", f"I can see {caption.lower()}")
            detected = f.get("detected") or {}
            if detected.get("error_code"):
                await self.stream.thinking(
                    "photo", f" — and the code {detected['error_code']} on the display.")
        first = (facts[0].get("detected") or {}) if facts else {}
        await self._step_done("photo", photos=len(facts),
                              error_code=first.get("error_code"),
                              device=first.get("form_factor"),
                              condition=first.get("damage_class"))

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
        await self._step_start("answer", label="This one needs immediate attention")

        # Announced like any other call — the ticket number in the reply must be visibly
        # backed by a tool that ran, or it reads (to a customer and to an auditor) as made up.
        call_id = f"call_safety_{uuid.uuid4().hex[:6]}"
        await self.stream.emit(Event.TOOL_CALL, {
            "call_id": call_id, "tool": "create_ticket",
            "label": "Opening an urgent ticket", "args_preview": "safety escalation"})
        result = await tools.run_tool(state, "create_ticket", {
            "summary": f"SAFETY: {state.user_message[:160]}",
            "priority": "urgent", "reason": "safety keywords detected"}, call_id=call_id)
        state.observations.append(result)
        self._trace(result)
        await self.stream.emit(Event.TOOL_RESULT, {
            "call_id": result.call_id, "ok": result.ok, "ms": result.ms,
            "summary": result.summary})
        if result.ok:
            state.ticket_id = result.data.get("ticket_no")
        state.safety_case = True
        try:
            await session_svc.set_meta(state.session_id, "safety", True)
        except Exception as e:  # noqa: BLE001
            log.warning("agent.safety_persist_failed", error=str(e))

        await self._stream_answer(state, extra_instruction=SAFETY_INSTRUCTION)
        if result.ok:
            # The ticket is the customer's handle on this. Render it, don't just mention it.
            block = Block(type=BlockType.TICKET_STATUS, payload=TicketStatusPayload(
                ticket_id=result.data.get("ticket_no", ""), status="open", priority="urgent",
                summary="Safety issue — escalated to a specialist",
                eta=result.data.get("eta", "within 1 hour")).model_dump())
            state.blocks.append(block)
            await self.stream.block(block.model_dump())
        await self._step_done("answer", outcome="safety path — diagnosis skipped",
                              ticket=result.data.get("ticket_no") if result.ok else None)
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
        await self._step_start("understand")
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
        p = state.perception
        await self._step_done(
            "understand",
            intent=p.intent.value, emotion=p.emotion.value,
            intensity=round(p.intensity, 2), language=p.language,
            deadline=p.urgency.deadline_hint if p.urgency.has_deadline else None,
            mentions=p.entities.product_mentions, error_codes=p.entities.error_codes,
            order_refs=p.entities.order_refs,
        )

    async def _disambiguate(self, state: AgentState) -> bool:
        """Resolve the product, or pause and ask. Returns True if the turn paused."""
        mentions = state.perception.entities.product_mentions
        has_photo = bool(state.vlm_facts)
        prior = (await product_svc.get_product(state.session_sku)
                 if state.session_sku else None)
        if not mentions and not has_photo:
            # Nothing new named: stay on the product this conversation already settled.
            # Without this, a follow-up ("is it still under warranty?") reached the tools
            # with no product at all, and the rewrite's "it → S1 Pro" re-opened the picker.
            # Unless the message is plainly about another KIND of device ("back to the
            # earbuds") — then the remembered product is the wrong one to assume.
            if prior and not state.resolved and not _talks_about_other_category(
                    state.user_message, prior.get("category")):
                state.resolved = _resolved_from(prior, "conversation")
            return False
        await self._step_start("identify")

        resolved: Optional[Dict[str, Any]] = None
        candidates: List[Dict[str, Any]] = []
        how = "no_match"
        if mentions:
            resolved, candidates, how = await product_svc.disambiguate(
                mentions, f"{state.user_message} {state.rewritten_query}",
                customer_id=state.customer_id, vlm_facts=state.vlm_facts,
                current=state.user_message,
            )
        # The customer already answered this question earlier in the conversation. If
        # the product they settled on is one of the candidates, that is the answer —
        # asking again is the single most "you are not listening" thing an agent can do.
        if not resolved and prior and any(c.get("sku") == prior["sku"] for c in candidates):
            resolved, how = next(c for c in candidates if c.get("sku") == prior["sku"]), \
                "conversation"

        # The photo is the fallback, and it has to cover two cases that look different
        # and dead-end identically:
        #
        #   "this is my vacuum" + a picture  — no mention at all, so this step used to
        #   be skipped outright and the customer got a paragraph saying we could not
        #   tell, with no way forward.
        #
        #   a picture whose FINE PRINT got read as a model name. The vision pass is good
        #   enough to pick "OmniHub 4.0" and "CoverStation" off the dock, perception
        #   dutifully reports them as product mentions, and neither is a product — so the
        #   catalog lookup returns nothing and a non-empty mentions list masked the photo
        #   path entirely. Better vision made identification worse.
        #
        # Either way the photo still knows the category, and the customer's own orders
        # usually know the rest.
        if not resolved and len(candidates) < 2 and has_photo:
            photo_candidates, photo_how = await product_svc.candidates_from_photo(
                state.vlm_facts, state.customer_id)
            if photo_candidates:
                candidates, how = photo_candidates, photo_how
                resolved = candidates[0] if len(candidates) == 1 else None
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
            await self._step_done("identify", resolved=resolved["name"],
                                  sku=resolved["sku"], decided_by=how,
                                  candidates_considered=len(candidates))
            return False

        if len(candidates) > 1:
            question = await self._picker_question(
                "" if how.startswith("photo") else (mentions[0] if mentions else ""),
                candidates,
                where_to_look=_where_to_look(state.vlm_facts))
            options = [ProductOption(
                sku=c["sku"], name=c["name"], brand=c["brand"],
                image_url=c.get("hero_image"), price=c.get("price"),
                category=c.get("category"), hint=_option_hint(c)) for c in candidates[:4]]
            block = product_picker(question, options)
            state.blocks.append(block)
            state.awaiting_action = True
            await self._step_done("identify", outcome="ambiguous — asking the customer",
                                  candidates=[c["name"] for c in candidates[:4]],
                                  decided_by="needs the customer")
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

        await self._step_done("identify", outcome="no catalog match for that name",
                              mentions=mentions)
        return False

    async def _picker_question(self, mention: str, candidates: List[Dict[str, Any]],
                               where_to_look: str = "") -> str:
        """The one line above the picker.

        Two different situations share this: a NAME that matches several products, and a
        PHOTO that identified a category but no model. The photo case has no mention to
        quote, and it has something better to offer — where the label actually is, so the
        customer can settle it themselves in one go instead of guessing from thumbnails.
        """
        options = ", ".join(f"{c['brand']} {c['name']}" for c in candidates[:4])
        prompt = (prompts.PICKER_FROM_PHOTO.format(options=options,
                                                   where_to_look=where_to_look
                                                   or "on a sticker on the device")
                  if not mention else
                  prompts.PICKER_QUESTION.format(mention=mention, options=options))
        try:
            data, usage = await self.llm.json_complete(
                [{"role": "user", "content": prompt}], max_tokens=900, default={})
            if data.get("question"):
                return data["question"]
        except Exception as e:  # noqa: BLE001
            log.warning("agent.picker_question_failed", error=str(e)[:120])
        if not mention:
            return ("I can see the type of device, but not which model — the model number "
                    f"is usually {where_to_look or 'on a sticker on the device'}. "
                    "Is it one of these?")
        return (f"Quick check — \"{mention}\" is used for more than one of our products. "
                "Which of these is yours?")

    async def _react(self, state: AgentState) -> None:
        """The reasoning loop. Think, act, observe, repeat — with a hard iteration cap."""
        await self._step_start("investigate")
        seen_calls: set = set()

        for _ in range(settings.max_react_iterations):
            state.iterations += 1
            decision = await self._plan(state)
            thought = (decision.get("thought") or "").strip()
            action = (decision.get("action") or "answer").strip()
            args = decision.get("args") or {}

            if thought:
                # Tagged with the iteration so the UI can show the loop turning rather
                # than one long undifferentiated "thinking" blur.
                # Through `thinking`, not `emit`: the recorder lives there, and this is
                # the trail that matters most. Emitting it directly meant the loop's own
                # reasoning — the part an operator needs to explain an answer — streamed
                # past the live viewer and was never written down.
                await self.stream.thinking(
                    "investigate", thought,
                    iteration=state.iterations,
                    decision=action if action not in ("answer", "", "none") else "answer",
                )
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

        await self._step_done("investigate", iterations=state.iterations,
                              tools_used=[o.tool for o in state.observations],
                              tools_succeeded=sum(1 for o in state.observations if o.ok))

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
        if state.purchase:
            p = state.purchase
            lines.append(f"Purchase already established from records ({p.get('source')}): "
                         f"{p.get('order_no') or ''} {p.get('channel') or ''} "
                         f"{p.get('purchase_date') or ''} "
                         f"{('dealer ' + p['dealer_name']) if p.get('dealer_name') else ''}".strip())
        lines.extend(_memory_lines(state))
        if _escalation_due(state):
            lines.append(
                f"ESCALATE NOW: the customer has reported {state.failed_attempts} failed "
                "fix(es) in this conversation, which is past the limit for how they feel. "
                "Stop troubleshooting — do not repeat or rephrase steps. Call create_ticket "
                "so a human takes over, then answer.")
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

        await self._step_start("answer")
        draft = await self._draft(state, sources, stream_live=True)
        await self._step_done("answer", characters=len(draft),
                              sources_available=len(sources),
                              tone=policy_for(state.perception).describe())

        await self._step_start("check")
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
        await self._step_done(
            "check",
            rules_checked=6,
            violations=[h.rule_id for h in hits] or None,
            outcome=("rewrote the answer" if hits else "passed, nothing to fix"),
        )

        state.answer = answer
        # A long answer sometimes ends without the trailing suggestions line. Ending a
        # support turn with no offered next step is the passivity this agent exists to
        # avoid, so the conversation state supplies them when the model forgets.
        if not suggestions and state.perception.intent != Intent.CHITCHAT:
            suggestions = fallback_suggestions(state)
            log.info("agent.suggestions_fallback", n=len(suggestions))
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
                policy=policy_text,
                language=_language_name(state.perception.language),
                citation_rule=(
                    prompts.CITATION_RULE_WITH_SOURCES.format(
                        numbers=", ".join(f"[{c.n}]" for c in sources))
                    if sources else prompts.CITATION_RULE_NO_SOURCES))},
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
        valid = {c.n for c in sources}
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
                await self.stream.content(_drop_dead_markers(whole[shown:cut], valid))
                shown = cut
        return plain_punctuation("".join(parts).strip())

    async def _stream_answer(self, state: AgentState, extra_instruction: str = "") -> None:
        """Compose and stream in one pass — used by the safety path, which has no guard
        repair step to wait for."""
        draft = await self._draft(state, [], extra_instruction=extra_instruction)
        answer, suggestions = _split_suggestions(draft)
        state.answer = answer
        # A long answer sometimes ends without the trailing suggestions line. Ending a
        # support turn with no offered next step is the passivity this agent exists to
        # avoid, so the conversation state supplies them when the model forgets.
        if not suggestions and state.perception.intent != Intent.CHITCHAT:
            suggestions = fallback_suggestions(state)
            log.info("agent.suggestions_fallback", n=len(suggestions))
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
        # Announce it like any other call. It used to emit a result with no call, so the
        # pipeline strip never showed the engine running — yet its card appeared.
        call_id = f"call_g1_{uuid.uuid4().hex[:6]}"
        await self.stream.emit(Event.TOOL_CALL, {
            "call_id": call_id, "tool": "check_warranty",
            "label": "Checking the warranty rules", "args_preview": "guard G1 repair"})
        result = await tools.run_tool(state, "check_warranty", args, call_id=call_id)
        result.data["forced_by_guard"] = True
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
            # Commerce results are real, checkable sources — an order record, a dealer's
            # published service path, a rule-engine verdict. Leaving them out meant the
            # composer had facts it could see but no number to attach them to, and it
            # invented one: answers arrived citing [2] and [3] when zero sources existed.
            elif o.tool == "lookup_order" and o.data.get("found"):
                key = ("order", o.data.get("order_no"))
                if key not in seen:
                    seen.add(key)
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"Order {o.data.get('order_no')} — {o.data.get('channel', '')}",
                        section="your order record"))
            elif o.tool == "lookup_dealer_order":
                dealer = (o.data.get("dealer") or {})
                name = dealer.get("name") or ""
                if name and ("dealer", name) not in seen:
                    seen.add(("dealer", name))
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"{name} — authorised dealer record",
                        section=(dealer.get("service_path") or "")[:120],
                        url=""))
            elif o.tool in ("search_products", "get_product"):
                for prod in (o.data.get("products") or
                             ([o.data] if o.data.get("found") and o.data.get("sku") else [])):
                    key = ("product", prod.get("sku"))
                    if key in seen or not prod.get("sku"):
                        continue
                    seen.add(key)
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"{prod.get('name', '')} — product record",
                        url=prod.get("url") or "", sku=prod.get("sku"),
                        section=f"{prod.get('brand', '')} catalog"))
            elif o.tool == "search_tickets":
                for tk in (o.data.get("tickets") or [])[:2]:
                    key = ("ticket", tk.get("symptom"))
                    if key in seen or not tk.get("symptom"):
                        continue
                    seen.add(key)
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"Resolved case: {tk.get('symptom', '')[:70]}",
                        section=str(tk.get("resolution", ""))[:140], sku=tk.get("sku")))
            elif o.tool == "check_warranty" and o.data.get("decided"):
                key = ("warranty", o.data.get("reason_code"))
                if key not in seen:
                    seen.add(key)
                    citations.append(Citation(
                        n=len(citations) + 1,
                        title=f"Warranty rule engine — {o.data.get('verdict')}",
                        section=o.data.get("reason_code", ""),
                        sku=o.data.get("sku")))
        return citations[:8]

    async def _emit_citations(self, state: AgentState, sources: List[Citation]) -> None:
        """Emit the citations the answer used, and delete the ones it invented.

        A marker with nothing behind it is worse than no marker: it looks clickable,
        the reader clicks it, and nothing happens. So markers with no matching source
        are stripped from the text before it is stored.
        """
        available = {c.n for c in sources}
        used = {int(n) for n in re.findall(r"\[(\d{1,2})\]", state.answer)}
        invented = used - available
        if invented:
            log.info("agent.stripped_invented_citations", numbers=sorted(invented),
                     available=sorted(available))
            state.answer = re.sub(
                r"\s*\[(" + "|".join(str(n) for n in sorted(invented)) + r")\]", "",
                state.answer)
            used &= available
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
        bits.extend(_memory_lines(state))

        # The photo. The ReAct planner was given this and the COMPOSER was not, so every
        # turn with an attachment was written blind — the customer sent a picture of a
        # USB-C cable, the vision pass described it correctly, and the reply said "I
        # can't see your device from here". That sentence was not a hallucination; it was
        # an accurate report of a context nobody had put the photo into.
        for facts in state.vlm_facts or []:
            detected = facts.get("detected") or {}
            caption = (facts.get("caption") or "").strip()
            if caption:
                bits.append(f"They sent a photo. It shows: {caption}")
            seen = [f"{k}: {detected[k]}" for k in ("brand", "form_factor", "damage_class")
                    if detected.get(k) and detected[k] != "unknown"]
            if seen:
                bits.append("Vision read — " + ", ".join(seen) + ".")
            if (facts.get("ocr_text") or "").strip():
                bits.append(f"Text visible in the photo: \"{facts['ocr_text'][:200]}\"")
            if detected.get("error_code"):
                bits.append(f"Error code in the photo: {detected['error_code']}")
            # Saying this plainly is what keeps the reply honest without making it blind:
            # it CAN describe what it sees, it just cannot read a number that is not there.
            if not detected.get("model_number_visible"):
                where = detected.get("where_to_look") or ""
                bits.append(
                    "No model number is readable in the photo, so do NOT guess one — "
                    "describe what you can see and ask them to check the label"
                    + (f" ({where})." if where else "."))
            elif detected.get("model_number"):
                bits.append(f"Model number read off the photo: {detected['model_number']}")

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
        fix_failed=bool(data.get("fix_failed")),
        damage=str(data.get("damage") or "none").lower(),
        summary=str(data.get("summary") or fallback_message[:160]),
    )


_ANKER_BRANDS = {"anker", "eufy", "soundcore", "nebula", "anker solix", "solix", "ankermake"}


def _memory_lines(state: AgentState) -> List[str]:
    """What this conversation already settled, for BOTH the planner and the composer.

    Each line is a failure the eval found: a follow-up answered as if the chat had just
    started. "How long will that take??" one turn after a ticket was opened; "can I keep
    using it?" one turn after a burning smell; "still not working" answered with the
    same three steps, or with "which product is it?" about the camera from turn one.
    """
    out: List[str] = []
    if state.safety_case:
        out.append(
            "OPEN SAFETY CASE: earlier in this chat the device showed a safety hazard. It "
            "must not be used, charged or powered on at all until the specialist has dealt "
            "with it. If they ask whether they can keep using it, the answer is a clear no "
            "— say why in one line and point to the open ticket.")
    if state.open_ticket.get("ticket_no"):
        t = state.open_ticket
        out.append(f"Open ticket for this chat: {t['ticket_no']} ({t.get('priority')} "
                   f"priority) — a person picks it up {t.get('eta')}. Do not open another; "
                   "if they ask about timing, give exactly this.")
    if state.perception.fix_failed:
        last = next((m.get("text") or m.get("content") or "" for m in reversed(state.history)
                     if m.get("role") == "assistant"), "")
        out.append(
            f"The customer says the last fix did NOT work (failed attempt "
            f"{state.failed_attempts}). What you told them last time: «{last[:600]}». Do "
            "not repeat or rephrase those steps. Give the NEXT thing to check, or hand over "
            "to a person. Stay on the same device and problem.")
    for facts in state.vlm_facts or []:
        brand = ((facts.get("detected") or {}).get("brand") or "").strip().lower()
        if brand and brand != "unknown" and brand not in _ANKER_BRANDS:
            out.append(
                f"The photo shows a {brand}-branded product, not an Anker, eufy or "
                "soundcore one. Say that first and plainly. Do not treat it as ours, do not "
                "match it to an Anker model, and do not run or describe an Anker warranty "
                "for it.")
    return out


def _escalation_due(state: AgentState) -> bool:
    """The policy table always had a per-emotion limit on failed fixes (angry 1,
    frustrated 2, calm 3). Nothing read it: failures were counted only from button
    clicks inside the current turn, so a customer typing "still not working" three
    times got a fourth round of the same questions. The count is now conversation-wide."""
    if state.ticket_id or state.perception.safety_concern:
        return False
    limit = policy_for(state.perception).escalate_after_failed_steps
    return state.failed_attempts >= max(1, limit)


def _talks_about_other_category(message: str, category: Optional[str]) -> bool:
    """True when the message carries the vocabulary of a different device category —
    the same symptom vocabulary the disambiguator already narrows on."""
    low = (message or "").lower()
    for cat, words in product_svc.CATEGORY_SIGNALS.items():
        if cat != category and any(w in low for w in words):
            return True
    return False


def _resolved_from(row: Dict[str, Any], how: str) -> ResolvedProduct:
    return ResolvedProduct(sku=row["sku"], name=row["name"], brand=row["brand"],
                           product_id=row["product_id"], category=row.get("category"), how=how)


def _how_phrase(how: str) -> str:
    return {
        "conversation": "what we settled earlier in this chat",
        "purchase_history": "your order history",
        "symptom_vocabulary": "what you described",
        "photo": "your photo",
        "vector": "the closest match in the catalog",
        "user_pick": "what you picked",
        "photo_and_purchase_history": "your photo and what you've ordered",
        "photo_category": "your photo",
    }.get(how, "what you told me")


def _where_to_look(vlm_facts) -> str:
    """Where the model number lives on the thing in the photo, per the vision pass."""
    for facts in vlm_facts or []:
        hint = ((facts.get("detected") or {}).get("where_to_look") or "").strip()
        if hint:
            return hint
    return ""


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


def plain_punctuation(text: str) -> str:
    """Take the typographic dashes out of anything the customer reads.

    A dash-joined aside is the single clearest tell that a machine wrote the sentence, and
    people notice it long before they can say why. The composer is told not to use one;
    this is the part that does not depend on the model complying.

    Only the em and en dash go. The hyphen stays, because `E-05`, `SOLIX F3800` and
    `all-in-one` need it, and a support answer that mangles an error code is worse than
    one that reads like a machine.
    """
    out = re.sub("\\s*[\u2014\u2013]\\s*", ", ", text)
    # The swap leaves doubled punctuation where the dash sat beside a comma, and the
    # citation stripper can leave a space stranded in front of a full stop.
    out = re.sub(",\\s*,", ",", out)
    out = re.sub("[,:;]\\s*([.!?])", "\\1", out)
    out = re.sub("[ \\t]+([.,!?;:])", "\\1", out)
    return out


def _drop_dead_markers(chunk: str, valid: set) -> str:
    """Remove citation markers that point at nothing, as the text streams.

    The composer is told which numbers exist and mostly obeys, but "mostly" is not good
    enough for something the reader will click: a marker with no source behind it looks
    checkable and is not. Cheap to do per chunk, and a marker split across two chunks
    simply survives — the stored answer is cleaned again afterwards.
    """
    if not valid:
        cleaned = re.sub(r"\s*\[\d{1,2}\]", "", chunk)
    else:
        cleaned = re.sub(r"\s*\[(\d{1,2})\]",
                         lambda m: m.group(0) if int(m.group(1)) in valid else "", chunk)
    return plain_punctuation(cleaned)


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


def fallback_suggestions(state: AgentState) -> List[str]:
    """Follow-ups derived from where the conversation actually is.

    The composer is asked for these, and on a long answer it sometimes finishes the
    prose and forgets the trailing line. Leaving the customer with no next move is the
    exact passivity this agent is supposed to avoid, so the state can produce sensible
    ones on its own: what happened in this turn determines what they will want next.

    Written in the customer's voice, because they appear as chips the customer taps.
    """
    warranty = state.observation_by_tool("check_warranty")
    verdict = (warranty.data.get("verdict") if warranty else "") or ""
    product = state.resolved.name if state.resolved else "it"

    if state.perception.safety_concern:
        return ["Is it safe to leave it unplugged in the house?",
                "How soon will someone contact me?"]
    if verdict in ("needs_proof", "covered_pending_verification"):
        return ["What exactly needs to be visible in the photo?",
                "How long does verification usually take?",
                "Can I still use it while the claim is open?"]
    if verdict == "covered_via_dealer":
        return ["What if the dealer won't help?",
                "Do I need the original packaging?",
                "How long should the repair take?"]
    if verdict in ("not_covered_policy", "expired"):
        return ["What would a paid repair cost?",
                "Is it worth repairing or replacing?",
                "Do you have a trade-in option?"]
    if state.ticket_id:
        return ["How do I check on this ticket later?",
                "Can I add a photo to the ticket?"]
    if state.observation_by_tool("get_troubleshooting_flow") or \
            state.observation_by_tool("search_kb"):
        return [f"What if none of that fixes {product}?",
                "How often should I be doing this?",
                "Can I talk to a person instead?"]
    if state.candidates and not state.resolved:
        return ["I'm not sure which one I have — how do I tell?"]
    return ["Can I talk to a person instead?",
            "What else should I check?"]


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
    # The G1 repair runs the engine because the DRAFT drifted into coverage language, not
    # because the customer asked. When that run cannot decide anything (no purchase on
    # file) and warranty was never the question, the card is noise: a customer asking
    # why their pump lost suction was shown "Warranty — escalate to a human". The answer
    # text is still rewritten without the coverage sentence; only the card is withheld.
    unasked = (w is not None and w.data.get("forced_by_guard")
               and w.data.get("verdict") in ("escalate_human", "needs_proof")
               and state.perception.intent not in (Intent.WARRANTY_CLAIM,
                                                   Intent.RETURN_REFUND))
    if w and w.data.get("decided") and not unasked:
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
    elif (state.open_ticket.get("ticket_no")
          and state.perception.intent in (Intent.ESCALATE_REQUEST, Intent.COMPLAINT)):
        # "Just get someone to sort it out" one turn after the ticket was opened. The
        # ticket is reused, not duplicated, so no tool runs and no card appeared: the
        # customer asked for a person and saw only text. Show the ticket they already have.
        t = state.open_ticket
        blocks.append(Block(type=BlockType.TICKET_STATUS, payload=TicketStatusPayload(
            ticket_id=t.get("ticket_no", ""), status="open",
            priority=t.get("priority") or "normal", summary=t.get("summary", ""),
            eta=t.get("eta")).model_dump()))

    search = state.observation_by_tool("search_products")
    if search and state.perception.intent in (Intent.BUY_ADVICE, Intent.PRODUCT_QUESTION):
        items = [ProductCardItem(
            sku=p.get("sku", ""), name=p.get("name", ""), price=p.get("price"),
            currency=p.get("currency") or "USD",
            image_url=p.get("image_url"), url=p.get("url"),
            badges=["discontinued"] if p.get("status") == "discontinued" else [])
            for p in (search.data.get("products") or [])[:6] if p.get("sku")]
        if items:
            blocks.append(Block(type=BlockType.PRODUCT_GRID,
                                payload=ProductGridPayload(items=items).model_dump()))

    policy = policy_for(state.perception)
    failed = max(sum(1 for o in state.observations if o.tool == "step_result_failed"),
                 state.failed_attempts)
    created = state.observation_by_tool("create_ticket")
    if (not state.ticket_id and not (created and created.data.get("created"))
            and ((policy.offer_human_early and failed >= 1) or _escalation_due(state))):
        blocks.append(human_handoff(HumanHandoffPayload(
            reason="A couple of fixes have not worked and you have a deadline.",
            eta_minutes=8, channels=["chat", "email"],
            summary_preview=state.perception.summary)))

    # Suggestions travel on their own `suggestions` event, which the UI renders under
    # "You might also ask". Adding a quick_replies BLOCK as well printed the same three
    # chips twice, one group directly above the other — the kind of thing that reads as
    # a broken page rather than a duplicated payload.
    return blocks
