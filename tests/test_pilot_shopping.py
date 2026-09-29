"""Regression tests for what the shopping pilot found (scripts/pilot_human.py).

The conversation eval tests the agent against the brief, and the brief is about faults.
Half of real first contact is shopping, and that half had never been tested: a customer
asking for a charger was shown the same charger three times at three prices, and a
customer asking for a fast charging power bank was shown a power strip.

Each test names the conversation that exposed it. No network, no DB.
"""
from app.agent import graph
from app.schemas.agent import AgentState, Intent, Perception, ToolResult
from app.services.products import _forms, _pattern, _terms


def _state(products, intent=Intent.BUY_ADVICE) -> AgentState:
    state = AgentState(session_id="s", message_id="m")
    state.perception = Perception(intent=intent)
    state.observations.append(ToolResult(
        call_id="c1", tool="search_products", ok=True,
        data={"found": True, "count": len(products), "products": products}))
    return state


def _grid(state: AgentState):
    for block in graph._blocks_from_state(state):
        if block.type.value == "product_grid":
            return (block.payload or {}).get("items") or []
    return []


# ── which words a shopping query is actually about (p01, p02, p06) ───────────

def test_two_letter_noise_is_dropped_but_model_codes_survive():
    # "im looking at robot vacuums" searched on "im" and "at" before this.
    words, _ = _terms("im looking at robot vacuums")
    assert "im" not in words and "at" not in words
    assert "robot" in words and "vacuums" in words
    # A model code is two characters and is the entire question.
    assert "s1" in _terms("s1 pro")[0]
    assert "x8" in _terms("x8 pro series")[0]


def test_shopper_plurals_match_singular_product_names():
    # 29 names contain "robot", three contain "vacuums", and all three of those are mop
    # cloths compatible with robot vacuums.
    assert "vacuum" in _forms("vacuums")
    assert "charger" in _forms("chargers")
    # Not everything ending in s is a plural.
    assert _forms("gloss") == ["gloss"]


def test_word_patterns_have_boundaries():
    # "power bank for a trip" was answered with a Power Strip and a tripod, because
    # ILIKE '%trip%' matches both.
    assert _pattern("trip") == r"\y(trip)\y"
    assert "strip" not in _pattern("trip")


# ── what reaches the shelf (p04, p06) ────────────────────────────────────────

def test_a_card_without_a_price_is_not_shown():
    items = _grid(_state([
        {"sku": "A1", "name": "Anker 323 Charger (33W)", "price": 19.99, "currency": "USD"},
        {"sku": "T2080111", "name": "eufy Robot Vacuum Omni S1 Pro", "price": None},
    ]))
    assert [i["sku"] for i in items] == ["A1"]


def test_cards_carry_their_own_currency():
    # Every price used to render with a dollar sign, which is how one charger listed in
    # three countries read as three chargers at three prices.
    items = _grid(_state([
        {"sku": "A2331321", "name": "Anker 323 Charger (33W)", "price": 27.99,
         "currency": "EUR"},
    ]))
    assert items[0]["currency"] == "EUR"


def test_no_shelf_when_the_customer_is_not_shopping():
    assert _grid(_state([{"sku": "A1", "name": "Charger", "price": 9.99}],
                        intent=Intent.TROUBLESHOOT)) == []


def test_the_shelf_stops_at_six():
    items = _grid(_state([
        {"sku": f"A{i}", "name": f"Charger {i}", "price": 10.0 + i} for i in range(9)]))
    assert len(items) == 6


def test_bundles_sit_behind_the_product_itself():
    # "the cheapest one that empties itself" came back with a vacuum taped to two floor
    # cleaners at the front of the shelf.
    items = _grid(_state([
        {"sku": "BUNDLE-X", "name": "eufy Robot Vacuum Omni E28 with 2 Floor Cleaners",
         "price": 899.0},
        {"sku": "T1", "name": "eufy Robot Vacuum Omni S2", "price": 1599.99},
    ]))
    assert [i["sku"] for i in items] == ["T1", "BUNDLE-X"]


def test_a_product_the_answer_names_comes_first():
    state = _state([
        {"sku": "A1", "name": "Anker 323 Charger (33W)", "price": 19.99},
        {"sku": "A2", "name": "Anker 324 Charger (40W)", "price": 22.99},
    ])
    state.answer = "The Anker 324 Charger (40W) is the one to get."
    assert [i["sku"] for i in _grid(state)] == ["A2", "A1"]


# ── a query the ASCII tokeniser cannot see (the Mandarin storefront 500) ──────

def test_a_query_with_no_ascii_words_tokenises_to_nothing():
    # Not a Mandarin-only problem, which is why it survived an English test suite:
    # a query made entirely of stopwords empties the same way a Chinese one does.
    assert _terms("扫地机器人报错了") == ([], [])
    assert _terms("充电宝") == ([], [])
    assert _terms("best") == ([], [])
    assert _terms("the best one") == ([], [])


def test_an_untokenisable_query_never_orders_by_a_bare_zero(monkeypatch):
    """`order by (0) desc` is not a constant in Postgres — a bare integer there is an
    ordinal, so it raised `InvalidColumnReference: ORDER BY position 0 is not in select
    list` and every Mandarin turn 500'd in the customer's face."""
    import asyncio

    from app.clients import db
    from app.services import products

    seen = []

    async def fake_fetch(sql, params=None):
        seen.append(sql)
        return []

    monkeypatch.setattr(db, "fetch", fake_fetch)
    for query in ("扫地机器人报错了", "充电宝", "best", "the best one"):
        seen.clear()
        assert asyncio.run(products.search_products(query, limit=3)) == []
        assert seen, f"{query!r} issued no query at all"
        for sql in seen:
            assert "(0) desc" not in sql, f"{query!r} built a bare-zero ordinal: {sql}"


def test_a_query_that_does_tokenise_still_scores_by_term_overlap(monkeypatch):
    """The guard must not cost English queries their ranking."""
    import asyncio

    from app.clients import db
    from app.services import products

    seen = []

    async def fake_fetch(sql, params=None):
        seen.append(sql)
        return [{"c": 5}] if " c from products" in sql else []

    monkeypatch.setattr(db, "fetch", fake_fetch)
    products._DF.clear()
    asyncio.run(products.search_products("robot vacuum", limit=3))
    assert any("::int * " in sql for sql in seen), "term-overlap score went missing"


# ── the picker's option list (Indonesian picker said "eufy eufy …") ──────────

def test_a_name_that_already_carries_its_brand_is_not_doubled():
    from app.agent.graph import _branded

    assert _branded({"brand": "eufy", "name": "eufy Robot Vacuum Omni S2"}) \
        == "eufy Robot Vacuum Omni S2"
    assert _branded({"brand": "eufy", "name": "eufyCam S330 (eufyCam 3)"}) \
        == "eufyCam S330 (eufyCam 3)"
    # Case is a spelling choice, not a different brand.
    assert _branded({"brand": "Anker", "name": "anker 323 Charger"}) == "anker 323 Charger"


def test_a_name_without_its_brand_still_gets_one():
    from app.agent.graph import _branded

    assert _branded({"brand": "eufy", "name": "Omni S2"}) == "eufy Omni S2"
    assert _branded({"brand": "", "name": "Omni S2"}) == "Omni S2"
    assert _branded({"brand": "eufy", "name": ""}) == "eufy"


# ── the repair checklist, across a resumed turn ─────────────────────────────

def _flow_obs(call_id: str, found: bool, symptom: str = "reduced suction"):
    from app.schemas.agent import ToolResult

    return ToolResult(
        call_id=call_id, tool="get_troubleshooting_flow", ok=True,
        summary="", data={"found": found, "symptom": symptom, "estimated_minutes": 8,
                          "steps": [{"step_id": "s1", "instruction": "Empty the dustbin"},
                                    {"step_id": "s2", "instruction": "Rinse the filter"}]}
        if found else {"found": False})


def _kinds(state):
    return [b.type.value for b in graph._blocks_from_state(state)]


def test_a_later_missed_lookup_does_not_erase_the_checklist():
    """Pressing "Still not fixed" re-attaches the flow the customer is standing on, and
    then `_react` runs and may look the flow up again. When that second lookup missed it
    shadowed the first, because `observation_by_tool` returns the LAST match, and the
    steps vanished from under them. Three identical runs of that click returned the
    steps, the steps, then nothing."""
    state = AgentState(session_id="s", message_id="m")
    state.perception = Perception(intent=Intent.TROUBLESHOOT)
    state.observations.append(_flow_obs("resume_flow", found=True))
    state.observations.append(_flow_obs("call_1", found=False))
    assert "diagnostic_steps" in _kinds(state)


def test_a_flow_that_was_never_found_builds_no_checklist():
    state = AgentState(session_id="s", message_id="m")
    state.perception = Perception(intent=Intent.TROUBLESHOOT)
    state.observations.append(_flow_obs("call_1", found=False))
    assert "diagnostic_steps" not in _kinds(state)


def test_a_found_flow_builds_the_checklist():
    state = AgentState(session_id="s", message_id="m")
    state.perception = Perception(intent=Intent.TROUBLESHOOT)
    state.observations.append(_flow_obs("call_1", found=True))
    assert "diagnostic_steps" in _kinds(state)
