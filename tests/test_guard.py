"""Guard rules.

Each test states the failure it prevents, because a guardrail whose purpose is unclear
gets deleted the first time it inconveniences someone.
"""
from __future__ import annotations

import pytest

from app.agent import guard
from app.agent.guard import check, repair_instruction
from app.schemas.agent import (AgentState, Emotion, Perception, ResolvedProduct,
                               ToolResult, Urgency)


def state(**kw) -> AgentState:
    base = dict(session_id="s1", message_id="m1", user_message="my vacuum stopped working")
    base.update(kw)
    return AgentState(**base)


def tool(name: str, data: dict, ok: bool = True) -> ToolResult:
    return ToolResult(call_id="c1", tool=name, ok=ok, data=data)


# ── G1: no warranty verdict without the rule engine ───────────────────────────

def test_g1_blocks_coverage_language_with_no_engine_result():
    """Prevents: the model fluently promising a free replacement nobody authorised."""
    hits, replan = check(state(), "Good news — this is covered under warranty and we'll "
                                 "replace it free of charge.")
    assert "G1" in [h.rule_id for h in hits]
    assert replan is True


def test_g1_allows_coverage_language_once_the_engine_has_run():
    s = state(observations=[tool("check_warranty", {"verdict": "covered",
                                                    "explanation": "in term"})])
    hits, _ = check(s, "This is covered under warranty, so the repair costs you nothing.")
    assert "G1" not in [h.rule_id for h in hits]


def test_g1_is_not_triggered_by_an_answer_that_avoids_warranty():
    hits, replan = check(state(), "Pop the dustbin out and rinse the filter under cold water.")
    assert [h.rule_id for h in hits if h.rule_id.startswith("G1")] == []
    assert replan is False


# ── G1b: the draft may not overrule the engine ────────────────────────────────

@pytest.mark.parametrize("verdict", ["not_covered_policy", "expired", "needs_proof",
                                     "escalate_human"])
def test_g1b_catches_a_draft_that_upgrades_a_negative_verdict(verdict):
    """Prevents the most expensive failure: the engine says no, the model says yes."""
    s = state(observations=[tool("check_warranty", {"verdict": verdict,
                                                    "explanation": "out of term"})])
    hits, replan = check(s, "Don't worry, this is covered and we'll replace it free.")
    assert "G1b" in [h.rule_id for h in hits]
    assert replan is True


def test_g1b_accepts_a_draft_that_states_the_negative_verdict():
    s = state(observations=[tool("check_warranty", {"verdict": "expired",
                                                    "explanation": "bought 2 years ago"})])
    hits, _ = check(s, "The warranty window closed last March, so a repair would be chargeable.")
    assert "G1b" not in [h.rule_id for h in hits]


# ── G2: steps need a source ───────────────────────────────────────────────────

def test_g2_flags_invented_repair_steps():
    hits, _ = check(state(), "1. Remove the panel\n2. Reseat the connector\n3. Restart it")
    assert "G2" in [h.rule_id for h in hits]


def test_g2_passes_when_retrieval_backed_the_steps():
    s = state(observations=[tool("get_troubleshooting_flow", {"found": True, "steps": []})])
    hits, _ = check(s, "1. Remove the panel\n2. Reseat the connector")
    assert "G2" not in [h.rule_id for h in hits]


# ── G3: no invented values ────────────────────────────────────────────────────

def test_g3_catches_a_price_that_appears_in_no_tool_result():
    hits, _ = check(state(), "A replacement filter is $24.99.")
    assert "G3" in [h.rule_id for h in hits]


def test_g3_allows_a_price_that_came_from_a_tool():
    s = state(observations=[tool("get_product", {"price": 24.99, "sku": "T2080111"})])
    hits, _ = check(s, "A replacement is $24.99.")
    assert "G3" not in [h.rule_id for h in hits]


def test_g3_catches_an_invented_sku():
    hits, _ = check(state(), "You want the T9999XYZ instead.")
    assert "G3" in [h.rule_id for h in hits]


def test_g3_catches_an_invented_error_code_meaning():
    """E-05 must not acquire a meaning the database never gave it."""
    hits, _ = check(state(), "E-05 means the water tank is empty.")
    assert "G3" in [h.rule_id for h in hits]


def test_g3_allows_an_error_code_that_was_looked_up():
    s = state(observations=[tool("lookup_error_code",
                                 {"found": True, "matches": [{"code": "E-05",
                                                              "meaning": "Brush roll blocked"}]})])
    hits, _ = check(s, "E-05 means the brush roll is blocked.")
    assert "G3" not in [h.rule_id for h in hits]


# ── G4: safety outranks troubleshooting ───────────────────────────────────────

def test_g4_blocks_repair_steps_on_a_safety_case():
    s = state(perception=Perception(safety_concern=True))
    hits, replan = check(s, "1. Open the battery cover\n2. Check the cells")
    assert "G4" in [h.rule_id for h in hits]
    assert replan is True


def test_g4_accepts_stop_using_it_guidance():
    s = state(perception=Perception(safety_concern=True))
    hits, _ = check(s, "Please stop using it right now and unplug it.\n1. Move it away from "
                       "anything flammable\n2. Do not charge it again")
    assert "G4" not in [h.rule_id for h in hits]


# ── G5: no device-specific advice while ambiguous ─────────────────────────────

def test_g5_blocks_steps_while_the_product_is_still_ambiguous():
    """This is what stops vacuum instructions reaching a breast-pump customer."""
    s = state(candidates=[{"sku": "A", "name": "Omni S1 Pro"},
                          {"sku": "B", "name": "Breast Pump S1 Pro"}])
    hits, replan = check(s, "1. Turn it over and clean the brush roll")
    assert "G5" in [h.rule_id for h in hits]
    assert replan is True


def test_g5_allows_steps_once_resolved():
    s = state(resolved=ResolvedProduct(sku="T2080111", name="Omni S1 Pro", brand="eufy",
                                       product_id="p1"),
              candidates=[{"sku": "A"}, {"sku": "B"}],
              observations=[tool("search_kb", {"found": True})])
    hits, _ = check(s, "1. Turn it over and clean the brush roll")
    assert "G5" not in [h.rule_id for h in hits]


# ── G6: emotion policy ────────────────────────────────────────────────────────

@pytest.mark.parametrize("emotion", [Emotion.ANGRY, Emotion.FRUSTRATED])
def test_g6_flags_a_question_opener_to_an_upset_customer(emotion):
    """Being asked for your order number while furious is where support loses people."""
    s = state(perception=Perception(emotion=emotion))
    hits, _ = check(s, "Could you tell me your order number first?")
    assert "G6" in [h.rule_id for h in hits]


def test_g6_accepts_acknowledge_then_help():
    s = state(perception=Perception(emotion=Emotion.ANGRY))
    hits, _ = check(s, "That's a rotten thing to happen the day before a party. "
                       "Let's get it running again.")
    assert "G6" not in [h.rule_id for h in hits]


def test_g6b_flags_an_unacknowledged_deadline():
    s = state(perception=Perception(
        urgency=Urgency(has_deadline=True, deadline_hint="party tomorrow evening")))
    hits, _ = check(s, "Try rinsing the filter and refitting it.")
    assert "G6b" in [h.rule_id for h in hits]


def test_g6b_passes_when_the_deadline_is_acknowledged():
    s = state(perception=Perception(
        urgency=Urgency(has_deadline=True, deadline_hint="party tomorrow evening")))
    hits, _ = check(s, "You've got a party tomorrow, so let's do the fastest fix first.")
    assert "G6b" not in [h.rule_id for h in hits]


# ── repair instructions ───────────────────────────────────────────────────────

def test_repair_instruction_quotes_the_engine_verdict():
    s = state(observations=[tool("check_warranty", {"verdict": "expired",
                                                    "explanation": "window closed in March"})])
    hits, _ = check(s, "This is covered and we'll replace it free.")
    text = repair_instruction(hits, s)
    assert "expired" in text
    assert "window closed in March" in text


def test_clean_answer_produces_no_hits():
    s = state(
        resolved=ResolvedProduct(sku="T2080111", name="Omni S1 Pro", brand="eufy",
                                 product_id="p1"),
        observations=[tool("search_kb", {"found": True, "passages": []})],
    )
    hits, replan = check(s, "Rinse the filter under cold water and leave it to dry fully "
                            "before refitting [1].")
    assert hits == []
    assert replan is False


# ── quoting the customer is not fabrication ───────────────────────────────────

def test_g3_does_not_flag_a_model_number_the_customer_supplied():
    """Refusing to help with an unknown product means naming it. Flagging that as an
    invented SKU sent the composer into a repair loop over its own correct answer."""
    s = state(user_message="my Anker Hyperdrive X999 Quantum won't turn on")
    hits, _ = check(s, "I can't find an Anker Hyperdrive X999 Quantum in our product "
                       "records, so I don't have verified steps for it.")
    assert "G3" not in [h.rule_id for h in hits]


def test_g3_still_flags_a_sku_nobody_mentioned():
    s = state(user_message="my vacuum is broken")
    hits, _ = check(s, "You should replace it with the T7777 instead.")
    assert "G3" in [h.rule_id for h in hits]


def test_g3_does_not_flag_an_error_code_the_customer_reported():
    s = state(user_message="the display shows E-42, what is that?")
    hits, _ = check(s, "E-42 isn't in our error-code table for your model — could you "
                       "send a photo of the screen?")
    assert "G3" not in [h.rule_id for h in hits]


# ── G1 fires on a ruling, not on the topic ────────────────────────────────────

@pytest.mark.parametrize("draft", [
    "Good news — this is covered under warranty.",
    "Unfortunately it is not covered.",
    "We'll replace it free of charge.",
    "You're eligible for a free replacement.",
    "The warranty has expired, so a repair would be chargeable.",
    "That's still under warranty.",
    "The warranty doesn't cover accidental damage.",
])
def test_g1_fires_on_an_actual_coverage_ruling(draft):
    hits, _ = check(state(), draft)
    assert "G1" in [h.rule_id for h in hits], f"should have caught: {draft!r}"


@pytest.mark.parametrize("draft", [
    "I'll help you with your warranty claim — first, what's the serial number?",
    "Let's check the warranty situation once we know the purchase date.",
    "Your claim is logged as TCK-9633 and an agent will pick it up.",
    "To start the claim, bring the invoice to any Sinar service point.",
    "Warranty questions go to the dealer who sold it.",
])
def test_g1_stays_quiet_when_warranty_is_merely_the_topic(draft):
    """The original pattern matched the bare words 'warranty' and 'claim', so ordinary
    helpful sentences forced a full re-draft — and the customer watched the answer
    vanish and retype itself on roughly one turn in three."""
    hits, _ = check(state(), draft)
    assert "G1" not in [h.rule_id for h in hits], f"false positive on: {draft!r}"


# ── G1 coverage detection: the two senses of "covered" ────────────────────────
#
# This pattern has been rewritten three times, and every regression looked the same from
# the outside: the answer visibly rewriting itself mid-stream, because a troubleshooting
# sentence tripped G1 and forced a re-draft. "The brush is covered in hair" is not a
# warranty ruling. Both directions are asserted, because narrowing it to kill the false
# positives is exactly how the real rulings started escaping.

@pytest.mark.parametrize("sentence", [
    "Your details are covered in the manual.",
    "The steps below are covered in more detail on the support page.",
    "Make sure the vent is covered by the filter before you run it.",
    "The brush is covered in hair — that's what triggers E-05.",
    "Cleaning is covered in section 3.",
    "The base station is covered by a plastic lid.",
    "That topic is covered by our setup guide.",
    "The sensor was covered with dust.",
    "I can help with your warranty claim.",
    "What do you need from me for the claim?",
])
def test_ordinary_english_is_not_a_coverage_ruling(sentence: str) -> None:
    assert guard.COVERAGE_RE.search(sentence) is None, sentence


@pytest.mark.parametrize("sentence", [
    # Contractions are load-bearing: "it is covered" was caught while "it's covered"
    # walked past for as long as the verb list omitted them.
    "it's fully covered",
    "you're covered",
    "it is covered",
    "that's still covered",
    "it was covered",
    "it isn't covered",
    "no longer covered",
    "it's still under warranty",
    "it is covered by the warranty",
    "it's covered by us",
    "not covered by the warranty",
    "that is out of warranty",
    "we'll replace it",
    "the warranty has expired",
    "covered under the warranty",
    "you are entitled for a free replacement",
])
def test_a_coverage_ruling_is_always_caught(sentence: str) -> None:
    assert guard.COVERAGE_RE.search(sentence) is not None, sentence


# ── G3b: a code's MEANING must come from the code table ───────────────────────
#
# Caught by `data_unknown_error_code_not_faked` failing one run in three. The customer
# asked what error C10 means; the agent told them. C10 is not an error code — the row in
# `error_codes` has no meaning and no steps, and its `source_url` is a product manual:
#
#     Robot-Vacuum-Auto-Empty-C10-Руководство-пользователя-T2292
#
# The crawler read a product model number as an error code, and 25 other rows are the
# same mistake. `lookup_error_code` correctly returns `found: false`, so the composer
# built the definition out of KB passages about the C10 *vacuum* instead. G3 could not
# catch it twice over: its pattern only matched `E`/`F` prefixes, and even widened it
# only asks whether the code appears in the evidence — which it did.

def test_g3b_blocks_a_meaning_with_no_code_table_entry():
    s = state(observations=[tool("lookup_error_code",
                                 {"found": False, "code": "C10", "matches": []})])
    hits, replan = check(s, "C10 means the dust bag is full and needs replacing.")
    assert "G3b" in [h.rule_id for h in hits]
    assert replan is True


def test_g3b_fires_even_when_a_manual_mentions_the_code():
    """The exact failure: the code IS in the evidence, as a product name."""
    s = state(observations=[
        tool("lookup_error_code", {"found": False, "code": "C10", "matches": []}),
        tool("search_kb", {"found": True, "passages": [
            {"text": "Robot Vacuum Auto Empty C10 user manual", "title": "C10"}]}),
    ])
    hits, _ = check(s, "Error C10 indicates a blocked brush roll.")
    assert "G3b" in [h.rule_id for h in hits]


def test_g3b_allows_a_meaning_that_came_from_the_table():
    s = state(observations=[tool("lookup_error_code", {
        "found": True, "code": "E-05",
        "matches": [{"code": "E-05", "meaning": "Brush roll blocked",
                     "fix_steps": ["Remove the brush"]}]})])
    hits, _ = check(s, "E-05 means the brush roll is blocked. Here is how to clear it.")
    assert "G3b" not in [h.rule_id for h in hits]


def test_g3b_ignores_a_draft_that_only_names_the_code():
    """Repeating the code back is not a definition — it must not force a re-draft."""
    s = state(observations=[tool("lookup_error_code",
                                 {"found": False, "code": "C10", "matches": []})])
    hits, _ = check(s, "I don't have C10 in my error-code table. What is the robot doing?")
    assert "G3b" not in [h.rule_id for h in hits]


def test_g3b_repair_instruction_names_the_code():
    s = state(observations=[tool("lookup_error_code",
                                 {"found": False, "code": "C10", "matches": []})])
    hits, _ = check(s, "C10 means the dust bag is full.")
    assert "C10" in repair_instruction([h for h in hits if h.rule_id == "G3b"], s)


def test_g3_now_sees_c_prefixed_codes():
    """`ERROR_CODE_RE` was `[EeFf]` only, so C-codes bypassed G3 entirely."""
    assert guard.ERROR_CODE_RE.search("the display shows C10") is not None
    assert guard.ERROR_CODE_RE.search("error E-05 again") is not None
