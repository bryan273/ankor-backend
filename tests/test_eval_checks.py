"""The eval suite's own assertions, tested.

Worth its own file because this suite has been wrong three times, and a wrong check is
more expensive than a missing one: it fails correct behaviour, and the obvious way to
make the red go away is to change the agent to match the test.

`coverage_only_from_the_engine` is the case in point. It used to demand that
`check_warranty` had run for any warranty question — which failed the correct answer to
"is my order ANK-2026-11111 still under warranty?" (that order does not exist, so there
is nothing to run the engine on) and passed the answer that ran the rule engine against
a nonexistent order.
"""
from __future__ import annotations

from typing import Iterable, List

import pytest

from scripts.eval_checks import coverage_only_from_the_engine


class FakeTurn:
    """Only the three fields this check reads."""

    def __init__(self, text: str, tools: Iterable[str] = (),
                 guard_hits: Iterable[str] = ()) -> None:
        self.text = text
        self.tools: List[str] = list(tools)
        self.guard_hits: List[str] = list(guard_hits)


@pytest.mark.parametrize("text", [
    # The answer that used to fail: no order, so no verdict, so no engine.
    "I couldn't find order ANK-2026-11111 — can you double-check the number?",
    # Talking about warranty is not asserting coverage.
    "I can help you with your warranty claim — what's the order number?",
    "Warranty claims need the original invoice.",
    # Asking for what the engine would need is the opposite of pre-empting it.
    "When did you buy it? That decides whether it's still in the warranty period.",
])
def test_no_coverage_assertion_needs_no_engine(text: str) -> None:
    assert coverage_only_from_the_engine(FakeTurn(text)) is None


@pytest.mark.parametrize("text", [
    "Your power bank is still under warranty, so we'll replace it.",
    "That's out of warranty, I'm afraid.",
    "Good news — it's fully covered.",
    "That isn't covered by the warranty.",
])
def test_coverage_assertion_without_the_engine_is_caught(text: str) -> None:
    reason = coverage_only_from_the_engine(FakeTurn(text))
    assert reason is not None and "without the rule engine" in reason


@pytest.mark.parametrize("tools,guard_hits", [
    (["lookup_order", "check_warranty"], []),
    ([], ["G1"]),
    (["check_warranty"], ["G1"]),
])
def test_coverage_assertion_is_fine_with_the_engine_or_a_guard_hit(
    tools: List[str], guard_hits: List[str],
) -> None:
    turn = FakeTurn("Your power bank is still under warranty.", tools, guard_hits)
    assert coverage_only_from_the_engine(turn) is None
