"""Product disambiguation — scenario S2 and the ways it can go wrong.

Two failure modes matter, and they pull in opposite directions:

  - Resolving when it should ask. "S1 Pro isn't sucking" names a robot vacuum AND a
    breast pump. Picking one silently answers a breast-pump question with vacuum
    instructions, which is the worst thing this system can do.
  - Asking when it should resolve. "How do I reset my Omni S1 Pro robot vacuum" answers
    the category question in the same sentence; asking it back reads as not listening.

These test the pure scoring functions, so they run without a database.
"""
from __future__ import annotations

import pytest

from app.services.products import (best_name_match, category_from_symptom,
                                   narrow_by_symptom, normalise_alias, score_by_photo)

VACUUM = {"sku": "T2080111", "name": "eufy Robot Vacuum Omni S1 Pro",
          "category": "robot_vacuum", "product_id": "p1"}
VACUUM_BASE = {"sku": "T2080V11", "name": "eufy Robot Vacuum Omni S1",
               "category": "robot_vacuum", "product_id": "p2"}
PUMP = {"sku": "T8D04121", "name": "eufy Wearable Breast Pump S1 Pro",
        "category": "breast_pump", "product_id": "p3"}
ALL = [VACUUM, VACUUM_BASE, PUMP]


# ── the category the words point at ───────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("the milk isn't coming out properly", "breast_pump"),
    ("it leaves hair on the carpet", "robot_vacuum"),
    ("the mopping function stopped", "robot_vacuum"),
    ("my flange doesn't seal", "breast_pump"),
])
def test_unambiguous_wording_names_a_category(text, expected):
    assert category_from_symptom(text) == expected


@pytest.mark.parametrize("text", [
    "it isn't sucking anymore",          # true of both a vacuum and a pump
    "it stopped working",
    "",
])
def test_ambiguous_wording_names_no_category(text):
    """'Suction' is deliberately absent from the signal lists: it fits both devices,
    and a signal that fits both is not a signal."""
    assert category_from_symptom(text) is None


# ── narrowing ─────────────────────────────────────────────────────────────────

def test_narrowing_drops_the_ruled_out_category():
    narrowed = narrow_by_symptom(ALL, "my S1 Pro robot vacuum leaves hair everywhere")
    assert PUMP not in narrowed
    assert VACUUM in narrowed


def test_narrowing_keeps_everything_when_wording_does_not_discriminate():
    assert narrow_by_symptom(ALL, "it isn't sucking anymore") == ALL


def test_narrowing_never_empties_the_list():
    """A category signal that matches no candidate must not wipe them all out — that
    would turn a resolvable question into 'no such product'."""
    assert narrow_by_symptom([PUMP], "my robot vacuum is broken") == [PUMP]


# ── name matching ─────────────────────────────────────────────────────────────

def test_exact_mention_resolves_between_variants():
    """'Omni S1 Pro' is contained by both vacuum names; the plain model should win over
    a longer variant, since the customer typed the plain model."""
    assert best_name_match([VACUUM, VACUUM_BASE], "Omni S1 Pro") is VACUUM


def test_name_match_refuses_to_choose_across_categories():
    """This is scenario S2. Both names contain 'S1 Pro', and picking the shorter one
    would answer a breast-pump question with vacuum instructions."""
    assert best_name_match([VACUUM, PUMP], "S1 Pro") is None


def test_name_match_returns_none_when_nothing_contains_the_mention():
    assert best_name_match(ALL, "X999 Quantum") is None


# ── photos ────────────────────────────────────────────────────────────────────

def test_photo_resolves_by_form_factor():
    facts = [{"detected": {"form_factor": "breast_pump", "brand": "eufy"}}]
    assert score_by_photo(ALL, facts) is PUMP


def test_photo_does_not_resolve_when_two_candidates_share_the_form_factor():
    facts = [{"detected": {"form_factor": "robot_vacuum"}}]
    assert score_by_photo([VACUUM, VACUUM_BASE], facts) is None


# ── alias normalisation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["S1 Pro", "s1-pro", "S1  PRO", "s1_pro"])
def test_alias_normalisation_collapses_punctuation_and_case(raw):
    assert normalise_alias(raw) == "s1 pro"
