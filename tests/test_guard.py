"""Guard rules.

Each test states the failure it prevents, because a guardrail whose purpose is unclear
gets deleted the first time it inconveniences someone.
"""
from __future__ import annotations

import pytest

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
