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
