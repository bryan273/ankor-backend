"""Regression tests for defects found by the conversation eval (scripts/eval_conversations.py).

Each test names the conversation that exposed it. They run with no network and no DB.
"""
from datetime import date

from app.agent import graph
from app.agent.tools import facts_from_dealer, facts_from_order
from app.schemas.agent import AgentState, Emotion, Perception
from app.services import warranty
from app.services.products import _has_model_token, normalise_code
from app.services.warranty import Channel, Verdict, WarrantyInput, decide

TODAY = date(2026, 9, 19)


# ── error codes are matched on shape (c01, c02, c07) ─────────────────────────

def test_error_code_shapes_normalise_together():
    # The seeded row is `E-05`; the robot's screen, the app and the customer say `E05`.
    assert normalise_code("E-05") == normalise_code("E05") == normalise_code("e 5") == "E5"
    assert normalise_code("EB01") == "EB1"


def test_error_code_normalising_keeps_distinct_codes_distinct():
    assert normalise_code("E10") == "E10"
    assert normalise_code("E01") != normalise_code("E10")
    assert normalise_code("C10") != normalise_code("E10")


# ── the warranty engine (c11, c12, c13) ──────────────────────────────────────

def _dealer(**kw) -> WarrantyInput:
    base = dict(sku="X", category="charger", purchase_date=date(2026, 5, 14),
                channel=Channel.DEALER, term_months=12)
    base.update(kw)
    return WarrantyInput(**base)


def test_unauthorised_dealer_is_never_covered():
    # GM774120 — Grey Market Imports, authorized=false — came out covered_via_dealer.
    d = decide(_dealer(dealer_matched=True, dealer_authorized=False, proof_present=True),
               today=TODAY)
    assert d.verdict == Verdict.NOT_COVERED_POLICY
    assert d.reason_code == "UNAUTHORISED_DEALER"


def test_dealer_record_is_evidence_without_an_extra_upload():
    # SE-482911 found in the dealer directory, authorised, in term: that record is the
    # purchase evidence, as an order on file is for the official store.
    d = decide(_dealer(dealer_matched=True, dealer_authorized=True), today=TODAY)
    assert d.verdict == Verdict.COVERED_VIA_DEALER


def test_dealer_pattern_only_still_needs_proof():
    # The invoice SHAPE matches Sinar, but no such invoice is in the directory.
    d = decide(_dealer(dealer_matched=False, dealer_authorized=True, purchase_date=None),
               today=TODAY)
    assert d.verdict == Verdict.NEEDS_PROOF


def test_dealer_record_out_of_term_is_expired():
    d = decide(_dealer(dealer_matched=True, dealer_authorized=True,
                       purchase_date=date(2024, 1, 1)), today=TODAY)
    assert d.verdict == Verdict.EXPIRED


def test_marketplace_order_on_file_is_its_own_proof():
    # ANK-2026-86091 was shown to the customer on an order card, then answered
    # "marketplace orders do not appear in this system — upload the invoice".
    d = decide(WarrantyInput(sku="A1751113", category="power_station",
                             purchase_date=date(2026, 6, 17), channel=Channel.AMAZON,
                             order_found=True, term_months=60), today=TODAY)
    assert d.verdict == Verdict.COVERED


def test_marketplace_order_not_on_file_still_needs_proof():
    d = decide(WarrantyInput(sku="A1751113", purchase_date=date(2026, 6, 17),
                             channel=Channel.AMAZON, order_found=False, term_months=60),
               today=TODAY)
    assert d.verdict == Verdict.NEEDS_PROOF


def test_same_facts_same_verdict():
    # The engine was deterministic all along; the flip-flop came from its inputs.
    inp = _dealer(dealer_matched=True, dealer_authorized=True)
    assert len({decide(inp, today=TODAY).verdict for _ in range(5)}) == 1


# ── facts come from the records, not the model (c10, c11) ────────────────────

def test_order_facts_pick_the_item_under_discussion():
    data = {"found": True, "order_no": "O-1", "channel": "official_store",
            "purchase_date": "2024-04-01",
            "items": [{"sku": "CASE"}, {"sku": "T2080111"}]}
    f = facts_from_order(data, want_sku="T2080111")
    assert f["sku"] == "T2080111" and f["order_found"] is True
    assert f["purchase_date"] == "2024-04-01"


def test_dealer_record_facts_carry_authorisation():
    data = {"found": True, "order_no": "GM774120", "purchase_date": "2025-08-23",
            "sku": "X", "dealer": {"name": "Grey Market Imports", "authorized": False}}
    f = facts_from_dealer(data)
    assert f["dealer_matched"] is True and f["dealer_authorized"] is False


def test_dealer_pattern_facts_are_not_a_record():
    f = facts_from_dealer({"found": False, "match": "pattern",
                           "dealer": {"name": "PT Sinar Elektronik", "authorized": True}})
    assert f["dealer_matched"] is False and f["dealer_authorized"] is True
    assert "purchase_date" not in f


def test_nothing_found_means_no_facts():
    assert facts_from_order({"found": False}) == {}
    assert facts_from_dealer({"found": False, "match": "none"}) == {}


# ── a picker is for a model NAME, not a category (c27) ───────────────────────

def test_model_names_carry_an_identifier():
    for m in ("S1 Pro", "X10 Pro Omni", "535 PowerHouse", "F3800"):
        assert _has_model_token(m), m
    for m in ("power bank", "soundcore earbuds", "my charger", "robot vacuum"):
        assert not _has_model_token(m), m


# ── failed fixes escalate across turns (c28) ─────────────────────────────────

def _state(emotion: Emotion, failed: int) -> AgentState:
    return AgentState(session_id="s", message_id="m", failed_attempts=failed,
                      perception=Perception(emotion=emotion))


def test_escalation_limit_follows_the_policy_table():
    assert graph._escalation_due(_state(Emotion.ANGRY, 1))
    assert not graph._escalation_due(_state(Emotion.FRUSTRATED, 1))
    assert graph._escalation_due(_state(Emotion.FRUSTRATED, 2))
    assert not graph._escalation_due(_state(Emotion.CALM, 2))
    assert graph._escalation_due(_state(Emotion.CALM, 3))


def test_no_escalation_once_a_ticket_exists():
    s = _state(Emotion.ANGRY, 5)
    s.ticket_id = "TCK-1"
    assert not graph._escalation_due(s)


def test_warranty_module_still_exposes_decide():
    assert callable(warranty.decide)


# ── privacy: identity is the session's, never the model's (c24) ──────────────

def test_lookup_by_someone_elses_email_is_refused():
    import asyncio
    from app.agent import tools
    s = AgentState(session_id="s", message_id="m", customer_email="me@example.demo")
    out = asyncio.run(tools.lookup_order(s, email="nadia.putri30@example.demo"))
    assert out["found"] is False and out["reason"] == "not_your_account"


def test_lookup_by_email_when_anonymous_is_refused():
    import asyncio
    from app.agent import tools
    s = AgentState(session_id="s", message_id="m")
    out = asyncio.run(tools.lookup_order(s, email="nadia.putri30@example.demo"))
    assert out["found"] is False and out["reason"] == "not_your_account"


def test_phone_lookup_needs_a_signed_in_customer():
    import asyncio
    from app.agent import tools
    s = AgentState(session_id="s", message_id="m")
    out = asyncio.run(tools.lookup_order(s, phone="+62 811 000 000"))
    assert out["found"] is False


# ── accidental damage needs no receipt to rule on (c17) ──────────────────────

def test_liquid_damage_is_excluded_without_purchase_evidence():
    d = decide(WarrantyInput(sku="X", category="audio",
                             damage_class=warranty.classify_damage("physical_damage")),
               today=TODAY)
    assert d.verdict == Verdict.NOT_COVERED_POLICY
    assert d.reason_code == "PHYSICAL_DAMAGE_EXCLUDED"


def test_safety_still_outranks_damage():
    d = decide(WarrantyInput(damage_class=warranty.DamageClass.PHYSICAL_DAMAGE,
                             safety_concern=True), today=TODAY)
    assert d.verdict == Verdict.ESCALATE_HUMAN


def test_memory_lines_cover_safety_ticket_and_failed_fix():
    s = AgentState(session_id="s", message_id="m", safety_case=True,
                   open_ticket={"ticket_no": "TCK-1", "priority": "urgent", "eta": "within 1 hour"},
                   failed_attempts=2, perception=Perception(fix_failed=True),
                   history=[{"role": "assistant", "content": "1. Flip it over. 2. Cut the hair."}])
    text = " ".join(graph._memory_lines(s))
    assert "must not be used" in text and "TCK-1" in text and "Cut the hair" in text


def test_foreign_brand_in_photo_is_called_out():
    s = AgentState(session_id="s", message_id="m",
                   vlm_facts=[{"detected": {"brand": "uni"}}])
    assert any("uni-branded" in line for line in graph._memory_lines(s))
    s.vlm_facts = [{"detected": {"brand": "eufy"}}]
    assert not any("branded product, not an Anker" in line for line in graph._memory_lines(s))


# ── remembered product vs a new topic (c27) ──────────────────────────────────

def test_other_category_vocabulary_is_detected():
    assert graph._talks_about_other_category("ok back to the earbuds, reset which?", "power_bank")
    assert not graph._talks_about_other_category("is it still under warranty?", "robot_vacuum")


# ── retrieval: the right model's manual ───────────────────────────────────────

def test_model_tokens_skip_units_and_counts():
    from app.services.kb import model_tokens
    name = "2× Anker SOLIX F3800 Plus + 2× Expansion Battery + 8× 440W Rigid Solar Panel"
    assert model_tokens(name) == ["f3800"]


def test_same_model_prefers_exact_manual_over_sibling():
    from app.services.kb import same_model
    passages = [
        {"title": "Anker SOLIX F2000 Portable Power Station", "url": "x/F2000-USER-MANUAL"},
        {"title": "Anker SOLIX F3800 Portable Power Station USER GUIDE", "url": "x/F3800-UK"},
    ]
    kept = same_model(passages, "Anker SOLIX F3800 Plus")
    assert [p["url"] for p in kept] == ["x/F3800-UK"]


def test_same_model_keeps_all_when_no_exact_manual():
    from app.services.kb import same_model
    passages = [{"title": "F2000 manual", "url": "a"}, {"title": "C800 guide", "url": "b"}]
    assert same_model(passages, "Anker SOLIX F3800 Plus") == passages
    assert same_model(passages, "") == passages


def test_same_model_token_is_not_a_prefix_match():
    from app.services.kb import same_model
    passages = [{"title": "X10 Pro Omni guide", "url": "a"}, {"title": "X100 guide", "url": "b"}]
    assert [p["url"] for p in same_model(passages, "eufy X10 Pro Omni")] == ["a"]


def test_power_station_phrase_names_the_category():
    from app.services.products import category_from_symptom
    assert category_from_symptom("my power station won't charge from the wall anymore") \
        == "power_station"


# ── warranty: the customer's own date already out of term ─────────────────────

def test_stated_date_out_of_term_is_expired_without_proof():
    from datetime import date
    from app.services import warranty as w
    d = w.decide(w.WarrantyInput(sku="T2080111", category="robot_vacuum",
                                 purchase_date=w.parse_date("2024-04"), term_months=12),
                 today=date(2026, 9, 19))
    assert d.verdict == w.Verdict.EXPIRED
    assert d.reason_code == "OUT_OF_TERM_BY_STATED_DATE"


def test_stated_date_in_term_still_needs_evidence():
    from datetime import date
    from app.services import warranty as w
    d = w.decide(w.WarrantyInput(sku="T2080111", category="robot_vacuum",
                                 purchase_date=w.parse_date("2026-06-01"), term_months=12),
                 today=date(2026, 9, 19))
    assert d.verdict == w.Verdict.ESCALATE_HUMAN


def test_month_only_date_reads_as_last_day():
    from datetime import date
    from app.services.warranty import parse_date
    assert parse_date("2024-04") == date(2024, 4, 30)
    assert parse_date("2024-12") == date(2024, 12, 31)
