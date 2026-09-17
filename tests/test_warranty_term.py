"""Which warranty term does `check_warranty` actually quote?

The engine in `app/services/warranty.py` is well covered; this file covers the step
BEFORE it — resolving how many months the product is covered for. That resolution had
the precedence backwards: it read the product's own `warranty_months`, then let the
category default overwrite it, so the specific fact lost to the generic one.

Measured against the live catalogue when the bug was found:

    power_station   product=60mo  policy=24mo   x224
    charger         product=12mo  policy=18mo   x7
    audio           product=12mo  policy=18mo   x1

Both directions are defects, but they are not symmetric. Quoting 24 months on a 60-month
SOLIX understates coverage and loses a repair the customer was entitled to. Quoting 18 on
a 12-month charger promises six months of coverage that does not exist — the agent
commits the company to something it will have to walk back. The second is the reason this
is a correctness test and not a polish one.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from app.agent import tools as agent_tools
from app.schemas.agent import AgentState
from app.services import warranty


def _state() -> AgentState:
    return AgentState(session_id="t", message_id="m1",
                      question="is this still under warranty?")


def _run(monkeypatch, *, product: Optional[Dict[str, Any]],
         policy_months: Optional[int]) -> int:
    """Call `check_warranty` with the catalogue stubbed, return the term it used."""
    async def fake_get_product(sku):
        return product

    async def fake_fetch_one(sql, params=()):
        return {"months": policy_months} if policy_months is not None else None

    monkeypatch.setattr(agent_tools.products, "get_product", fake_get_product)
    monkeypatch.setattr(agent_tools.kb.db, "fetch_one", fake_fetch_one)

    seen: Dict[str, int] = {}
    real_decide = warranty.decide

    def spy(inp, today=None):
        seen["term"] = inp.term_months
        return real_decide(inp, today) if today else real_decide(inp)

    monkeypatch.setattr(agent_tools.warranty, "decide", spy)
    asyncio.run(agent_tools.check_warranty(
        _state(), sku="A1000", purchase_date="2026-01-05",
        channel="official store", damage_class="defect", order_found=True))
    return seen["term"]


def test_product_term_beats_the_category_default(monkeypatch):
    """The SOLIX case: 224 products, 60 real months against a 24-month default."""
    term = _run(monkeypatch,
                product={"category": "power_station", "warranty_months": 60},
                policy_months=24)
    assert term == 60


def test_shorter_product_term_also_wins(monkeypatch):
    """The dangerous direction — the default must not inflate a 12-month product."""
    term = _run(monkeypatch,
                product={"category": "charger", "warranty_months": 12},
                policy_months=18)
    assert term == 12, "category default overstated coverage by six months"


def test_category_default_applies_when_the_product_has_no_term(monkeypatch):
    """62 products in the catalogue carry no term of their own; they need the fallback."""
    term = _run(monkeypatch,
                product={"category": "robot_vacuum", "warranty_months": None},
                policy_months=12)
    assert term == 12


def test_falls_back_to_the_global_default_when_nothing_is_known(monkeypatch):
    term = _run(monkeypatch, product=None, policy_months=None)
    assert term == warranty.DEFAULT_TERM_MONTHS


def test_uncategorised_product_still_uses_its_own_term(monkeypatch):
    """No category means no policy row — the product's term is all there is."""
    term = _run(monkeypatch,
                product={"category": None, "warranty_months": 36},
                policy_months=None)
    assert term == 36
