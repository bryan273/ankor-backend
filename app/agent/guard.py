"""Guardrails — the rule constraints the brief asks to see combined with orchestration.

These run on the *draft*, before it reaches the customer. Each rule has an id, each
violation is stored in `guard_hits`, and the trace drawer shows them: an agent that can
prove it caught itself is more convincing than one that claims it never errs.

G1 and G3 are the two that matter commercially. G1 stops the model inventing a warranty
outcome; G3 stops it inventing a price or a part number. Both are checked by looking for
the claim in the text and the evidence in the tool results — not by asking the model
whether it was good.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.schemas.agent import AgentState, GuardHit

log = structlog.get_logger(__name__)

# An ASSERTION about coverage, not the topic of coverage.
#
# This was originally "deliberately broad" and matched the bare words `warranty`,
# `coverage` and `claim`. That made it fire on ordinary helpful sentences — "I'll help
# you with your claim", "let's check the warranty" — and each false hit forced a full
# re-draft. The customer watched the answer vanish and retype itself, repeatedly, for
# no reason: roughly one turn in three.
#
# So the pattern now requires a statement about whether something IS or IS NOT covered.
# Discussing warranty is free; ruling on it is what needs the engine. A false negative
# still costs more than a false positive, which is why every phrasing of a verdict is
# here, in both directions.
COVERAGE_RE = re.compile(
    r"\b("
    # "That's still under warranty" contracts the verb away, so the verb is optional
    # when a definiteness word ("still", "no longer") is doing the asserting instead.
    r"(?:(?:is|isn'?t|is not|are|aren'?t|was|wasn'?t|'s|'re)\s+)?"
    r"(?:still\s+|no longer\s+)?(?:under|in|out of)\s+warranty"
    # "Covered" is two different words. One is a warranty ruling; the other is ordinary
    # English — "the brush is covered in hair", "that's covered in the manual", "the
    # vent is covered by the filter". Matching both made G1 fire on a troubleshooting
    # sentence and force a pointless re-draft, which is what the answer visibly
    # rewriting itself mid-stream looked like from the outside.
    #
    # The sense is decided by what follows: "covered in"/"covered with" is never a
    # ruling, and "covered by" is one only when the warranty (or we) is doing the
    # covering. Nothing following at all — "it's fully covered" — is a ruling.
    #
    # The contractions are load-bearing too. Without them "it is covered" was caught
    # while "it's covered" was not, and people contract.
    r"|(?:is|isn'?t|is not|are|aren'?t|'s|'re|was|wasn'?t|were|weren'?t)"
    r"\s+(?:still\s+|fully\s+|completely\s+)*covered"
    r"(?!\s+(?:in|with|by\s+(?!(?:the\s+|your\s+)?(?:warranty|guarantee)\b|us\b)))"
    r"|(?:not|no longer)\s+covered"
    r"(?!\s+(?:in|with|by\s+(?!(?:the\s+|your\s+)?(?:warranty|guarantee)\b|us\b)))"
    r"|covered\s+(?:under|by)\s+(?:the\s+)?warranty"
    r"|free\s+(?:of\s+charge|replacement|repair)"
    r"|replace(?:ment|d)?\s+(?:free|at no cost)"
    r"|we'?(?:ll| will)\s+(?:replace|repair|refund)"
    r"|(?:full|partial)\s+refund|money back|at no charge|no charge to you"
    r"|(?:eligible|entitled)\s+for\s+(?:a\s+)?(?:free|replacement|refund|repair)"
    r"|warranty\s+(?:covers|does not cover|doesn'?t cover|has expired|is valid)"
    r")\b",
    re.IGNORECASE,
)

# A negative statement about warranty is still a warranty statement, so both directions
# require the engine to have run.
PRICE_RE = re.compile(r"[$€£¥]\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:USD|EUR|GBP|RMB|IDR)\b")
SKU_RE = re.compile(r"\b[A-Z]\d{3,4}[A-Z0-9]*\b")
ERROR_CODE_RE = re.compile(r"\b[EeFf][-–]?\d{1,3}\b")

SAFETY_WORDS = re.compile(
    r"\b(swell|swelling|bulging|burn|burning|smoke|smoking|fire|spark|melt|melting|"
    r"overheat|explode|explosion|electric shock)\b", re.IGNORECASE)


def _evidence_text(state: AgentState) -> str:
    """Everything the draft is allowed to draw on, flattened into one haystack.

    The customer's own words belong here alongside the tool results. Repeating a model
    number back — "I can't find an Anker Hyperdrive X999 in our records" — is the
    opposite of fabrication, but a whitelist built only from tool output flags it as an
    invented SKU and sends the composer into a repair loop over its own correct answer.
    """
    import json
    parts = [json.dumps(o.data, ensure_ascii=False, default=str) for o in state.observations]
    for facts in state.vlm_facts:
        parts.append(json.dumps(facts, ensure_ascii=False, default=str))
    if state.resolved:
        parts.append(state.resolved.model_dump_json())
    parts.append(state.user_message)
    parts.append(state.rewritten_query)
    parts.extend(m.get("content", "") for m in state.history)
    return "\n".join(p for p in parts if p)


def check(state: AgentState, draft: str) -> Tuple[List[GuardHit], bool]:
    """Returns `(hits, must_replan)`. `must_replan` means the draft cannot be repaired
    by rewording — a tool genuinely has to run."""
    hits: List[GuardHit] = []
    must_replan = False
    evidence = _evidence_text(state)
    warranty_result = state.observation_by_tool("check_warranty")

    # G1 — no warranty verdict without the rule engine.
    if COVERAGE_RE.search(draft) and warranty_result is None:
        hits.append(GuardHit(rule_id="G1",
                             detail="coverage language without a check_warranty result"))
        must_replan = True

    # G1b — the engine ran, but the draft disagrees with it.
    if warranty_result is not None:
        verdict = warranty_result.data.get("verdict", "")
        negative = verdict in ("not_covered_policy", "expired", "needs_proof", "escalate_human")
        promises = re.search(
            r"\b(is covered|we'?ll replace|free replacement|full refund|no charge)\b",
            draft, re.IGNORECASE)
        if negative and promises:
            hits.append(GuardHit(
                rule_id="G1b",
                detail=f"draft promises coverage but the engine returned '{verdict}'"))
            must_replan = True

    # G2 — technical steps need a source.
    step_like = re.search(r"^\s*(?:\d+[.)]|[-*])\s+\w", draft, re.MULTILINE)
    sourced = any(o.tool in ("search_kb", "get_troubleshooting_flow", "lookup_error_code",
                             "search_tickets", "get_product", "web_search",
                             # Commerce tools are sources too: a dealer's service path and
                             # a warranty verdict are documented facts, and instructions
                             # derived from them are grounded, not invented.
                             "lookup_order", "lookup_dealer_order", "check_warranty",
                             "create_ticket")
                  for o in state.observations if o.ok)
    if step_like and not sourced:
        hits.append(GuardHit(rule_id="G2",
                             detail="numbered steps with no retrieval behind them"))

    # G3 — no invented SKUs, prices or error-code meanings.
    for match in set(PRICE_RE.findall(draft)):
        digits = re.sub(r"[^\d]", "", match)
        if digits and digits not in re.sub(r"[^\d]", "", evidence):
            hits.append(GuardHit(rule_id="G3", detail=f"price not in any tool result: {match}"))
            break
    for sku in set(SKU_RE.findall(draft)):
        if sku not in evidence:
            hits.append(GuardHit(rule_id="G3", detail=f"SKU not in any tool result: {sku}"))
            break
    for code in set(ERROR_CODE_RE.findall(draft)):
        normalised = code.upper().replace("–", "-")
        if normalised not in evidence.upper() and normalised.replace("-", "") not in \
                evidence.upper().replace("-", ""):
            hits.append(GuardHit(rule_id="G3",
                                 detail=f"error code not in any tool result: {code}"))
            break

    # G4 — safety outranks troubleshooting.
    if state.perception.safety_concern:
        if step_like and not re.search(r"\b(stop using|unplug|disconnect|do not charge)\b",
                                       draft, re.IGNORECASE):
            hits.append(GuardHit(rule_id="G4",
                                 detail="safety case answered with troubleshooting steps"))
            must_replan = True

    # G5 — no product-specific instruction before the product is known.
    if state.resolved is None and len(state.candidates) > 1 and step_like:
        hits.append(GuardHit(rule_id="G5",
                             detail="device-specific steps while the product is ambiguous"))
        must_replan = True

    # G6 — emotion policy actually honoured.
    emotion = state.perception.emotion.value
    if emotion in ("angry", "frustrated"):
        first = _first_sentence(draft)
        if first.rstrip().endswith("?"):
            hits.append(GuardHit(rule_id="G6",
                                 detail=f"reply opens with a question while user is {emotion}"))
    if state.perception.urgency.has_deadline:
        hint = state.perception.urgency.deadline_hint.lower()
        keyword = next((w for w in re.findall(r"[a-z]{4,}", hint)), "")
        if keyword and keyword not in draft.lower() and not re.search(
                r"\b(today|tonight|tomorrow|time|deadline|before|by then|in time)\b",
                draft, re.IGNORECASE):
            hits.append(GuardHit(rule_id="G6b", detail="deadline stated but never acknowledged"))

    if hits:
        log.info("guard.hits", rules=[h.rule_id for h in hits], replan=must_replan)
    return hits, must_replan


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    for i, ch in enumerate(stripped):
        if ch in ".!?\n" and i > 10:
            return stripped[:i + 1]
    return stripped[:200]


def repair_instruction(hits: List[GuardHit], state: AgentState) -> str:
    """What to tell the composer on the retry. Specific beats scolding."""
    lines = []
    for h in hits:
        if h.rule_id == "G1":
            lines.append("Do not say anything about warranty coverage — the rule engine has "
                         "not decided this case. Help with the problem instead.")
        elif h.rule_id == "G1b":
            w = state.observation_by_tool("check_warranty")
            verdict = w.data.get("verdict") if w else "unknown"
            explanation = w.data.get("explanation") if w else ""
            lines.append(f"The warranty verdict is '{verdict}': {explanation} State that, "
                         "without softening it or promising anything more.")
        elif h.rule_id == "G2":
            lines.append("Either drop the numbered steps or mark them explicitly as general "
                         "suggestions rather than instructions from the manual.")
        elif h.rule_id == "G3":
            lines.append(f"Remove this — it is not in any tool result: {h.detail.split(': ')[-1]}. "
                         "Only use values that appear in the tool output.")
        elif h.rule_id == "G4":
            lines.append("Safety case: no troubleshooting. Tell them to stop using the device, "
                         "unplug it if safe, keep it clear of anything flammable, and that a "
                         "specialist is taking over.")
        elif h.rule_id == "G5":
            lines.append("The product is still ambiguous — do not give device-specific steps. "
                         "Ask which one they have.")
        elif h.rule_id == "G6":
            lines.append("Do not open with a question. They are upset. Acknowledge, then help.")
        elif h.rule_id == "G6b":
            lines.append("They told you their deadline. Acknowledge it explicitly.")
    return "\n".join(f"- {line}" for line in lines)
