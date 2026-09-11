"""The warranty engine is the piece that must never be wrong, so it gets the most tests.

Every row of the matrix in SPECIFICATION §3.6 is covered, plus the asymmetries that
matter commercially: unknown beats guessed, safety beats everything, and a negative
verdict is never quietly upgraded.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.warranty import (Channel, DamageClass, Verdict, WarrantyInput,
                                   add_months, classify_channel, classify_damage,
                                   decide, detect_safety_concern, parse_date, within_term)

TODAY = date(2026, 9, 11)


def inp(**kw):
    base = dict(sku="T2080111", category="robot_vacuum", term_months=12)
    base.update(kw)
    return WarrantyInput(**base)


# ── official store ────────────────────────────────────────────────────────────

def test_official_store_in_term_defect_is_covered():
    d = decide(inp(channel=Channel.OFFICIAL_STORE, order_found=True,
                   purchase_date=TODAY - timedelta(days=120),
                   damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.COVERED
    assert d.reason_code == "IN_TERM_DEFECT"
    assert d.warranty_until == date(2027, 5, 14)


def test_official_store_physical_damage_is_excluded_even_in_term():
    d = decide(inp(channel=Channel.OFFICIAL_STORE, order_found=True,
                   purchase_date=TODAY - timedelta(days=30),
                   damage_class=DamageClass.PHYSICAL_DAMAGE), TODAY)
    assert d.verdict is Verdict.NOT_COVERED_POLICY
    assert d.next_action == "offer_paid_repair"


def test_official_store_out_of_term_is_expired():
    d = decide(inp(channel=Channel.OFFICIAL_STORE, order_found=True,
                   purchase_date=TODAY - timedelta(days=800),
                   damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.EXPIRED


def test_consumable_wear_is_not_a_warranty_claim():
    d = decide(inp(channel=Channel.OFFICIAL_STORE, order_found=True,
                   purchase_date=TODAY - timedelta(days=60),
                   damage_class=DamageClass.WEAR), TODAY)
    assert d.verdict is Verdict.NOT_COVERED_POLICY
    assert d.reason_code == "CONSUMABLE_WEAR"


def test_unknown_purchase_date_asks_rather_than_assuming():
    """The engine must never treat 'we cannot tell' as 'yes'."""
    d = decide(inp(channel=Channel.OFFICIAL_STORE, order_found=True,
                   purchase_date=None, damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.NEEDS_PROOF
    assert d.reason_code == "PURCHASE_DATE_UNKNOWN"


# ── dealer: scenario S3 ───────────────────────────────────────────────────────

def test_dealer_order_not_in_system_needs_proof():
    d = decide(inp(channel=Channel.DEALER, order_found=False), TODAY)
    assert d.verdict is Verdict.NEEDS_PROOF
    assert d.reason_code == "DEALER_ORDER_NOT_IN_SYSTEM"
    assert "invoice_photo" in d.required_evidence
    assert "dealer_name" in d.required_evidence
    assert d.next_action == "upload_proof"


def test_dealer_matched_with_invoice_in_term_is_covered_via_dealer():
    d = decide(inp(channel=Channel.DEALER, dealer_matched=True, proof_present=True,
                   purchase_date=TODAY - timedelta(days=120),
                   damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.COVERED_VIA_DEALER
    assert d.next_action == "route_to_dealer"


def test_dealer_matched_but_out_of_term_expires():
    d = decide(inp(channel=Channel.DEALER, dealer_matched=True, proof_present=True,
                   purchase_date=TODAY - timedelta(days=700),
                   damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.EXPIRED


def test_dealer_physical_damage_still_excluded():
    d = decide(inp(channel=Channel.DEALER, dealer_matched=True, proof_present=True,
                   purchase_date=TODAY - timedelta(days=60),
                   damage_class=DamageClass.PHYSICAL_DAMAGE), TODAY)
    assert d.verdict is Verdict.NOT_COVERED_POLICY


def test_order_missing_but_dealer_matched_routes_to_dealer_branch():
    """The S3 entry condition: `lookup_order` found nothing, the directory did."""
    d = decide(inp(channel=Channel.UNKNOWN, order_found=False, dealer_matched=True), TODAY)
    assert d.verdict is Verdict.NEEDS_PROOF
    assert d.reason_code == "DEALER_ORDER_NOT_IN_SYSTEM"


# ── marketplace ───────────────────────────────────────────────────────────────

def test_marketplace_without_proof_asks_for_it():
    d = decide(inp(channel=Channel.AMAZON, order_found=False), TODAY)
    assert d.verdict is Verdict.NEEDS_PROOF
    assert d.reason_code == "MARKETPLACE_PROOF_REQUIRED"


def test_marketplace_with_proof_is_pending_verification_not_covered():
    d = decide(inp(channel=Channel.MARKETPLACE, proof_present=True,
                   purchase_date=TODAY - timedelta(days=100),
                   damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.COVERED_PENDING_VERIFICATION


# ── safety and fallbacks ──────────────────────────────────────────────────────

def test_safety_outranks_every_other_rule():
    d = decide(inp(channel=Channel.OFFICIAL_STORE, order_found=True,
                   purchase_date=TODAY - timedelta(days=2000),
                   damage_class=DamageClass.PHYSICAL_DAMAGE, safety_concern=True), TODAY)
    assert d.verdict is Verdict.ESCALATE_HUMAN
    assert d.reason_code == "SAFETY_CONCERN"
    assert d.next_action == "escalate_urgent"


def test_no_evidence_at_all_escalates_rather_than_guessing():
    d = decide(inp(channel=Channel.UNKNOWN, order_found=False, proof_present=False), TODAY)
    assert d.verdict is Verdict.ESCALATE_HUMAN
    assert d.reason_code == "NO_PURCHASE_EVIDENCE"


def test_longer_category_term_is_respected():
    """A power station carries 60 months; the engine must use the term it is given."""
    d = decide(WarrantyInput(sku="A1780", category="power_station", term_months=60,
                             channel=Channel.OFFICIAL_STORE, order_found=True,
                             purchase_date=TODAY - timedelta(days=1000),
                             damage_class=DamageClass.DEFECT), TODAY)
    assert d.verdict is Verdict.COVERED


# ── date arithmetic ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("start,months,expected", [
    (date(2026, 1, 31), 1, date(2026, 2, 28)),
    (date(2024, 1, 31), 1, date(2024, 2, 29)),   # leap year
    (date(2026, 3, 14), 12, date(2027, 3, 14)),
    (date(2026, 12, 15), 12, date(2027, 12, 15)),
    (date(2026, 8, 31), 6, date(2027, 2, 28)),
])
def test_add_months_clamps_the_day(start, months, expected):
    assert add_months(start, months) == expected


def test_within_term_reports_unknown_for_unknown_purchase():
    in_term, expiry, remaining = within_term(None, 12, TODAY)
    assert in_term is None and expiry is None and remaining is None


def test_within_term_boundary_day_is_still_covered():
    purchase = date(2025, 9, 11)
    in_term, expiry, _ = within_term(purchase, 12, TODAY)
    assert expiry == TODAY
    assert in_term is True


# ── input classifiers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("official_store", Channel.OFFICIAL_STORE), ("Anker Store", Channel.OFFICIAL_STORE),
    ("amazon", Channel.AMAZON), ("tokopedia", Channel.MARKETPLACE),
    ("reseller", Channel.DEALER), ("dealer", Channel.DEALER),
    ("", Channel.UNKNOWN), (None, Channel.UNKNOWN), ("something else", Channel.UNKNOWN),
])
def test_classify_channel(raw, expected):
    assert classify_channel(raw) is expected


@pytest.mark.parametrize("raw,expected", [
    ("defect", DamageClass.DEFECT), ("water_damage", DamageClass.PHYSICAL_DAMAGE),
    ("wear", DamageClass.WEAR), ("", DamageClass.UNKNOWN), (None, DamageClass.UNKNOWN),
])
def test_classify_damage(raw, expected):
    assert classify_damage(raw) is expected


@pytest.mark.parametrize("text", [
    "the battery is swelling", "there's a burning smell", "it started smoking",
    "sparks came out of the port", "the casing is melting", "I got an electric shock",
    "the unit caught fire", "it's overheating badly",
])
def test_safety_detector_catches_real_danger(text):
    assert detect_safety_concern(text) is True


@pytest.mark.parametrize("text", [
    "it won't suck anymore", "the app says offline", "battery drains fast",
    "my order hasn't arrived", "the brush is dirty", "it's warm to the touch after charging",
])
def test_safety_detector_does_not_fire_on_ordinary_faults(text):
    assert detect_safety_concern(text) is False


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-14", date(2026, 3, 14)), ("14/03/2026", date(2026, 3, 14)),
    ("2026/03/14", date(2026, 3, 14)), ("", None), (None, None), ("not a date", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected
