"""Assertions the eval cases are built from.

Each returns None when the turn is fine and a human-readable reason when it is not, so a
failure report reads as prose rather than as a stack of booleans.
"""
from __future__ import annotations

from typing import Callable, Optional

from scripts.eval_turn import Turn

Check = Callable[[Turn], Optional[str]]  # returns None on pass, a reason on failure


def all_stages_closed(t: Turn) -> Optional[str]:
    return f"stages left open: {t.unclosed_stages}" if t.unclosed_stages else None


def terminal_once(t: Turn) -> Optional[str]:
    terminals = [n for n, _ in t.events if n in ("complete", "error")]
    return None if len(terminals) == 1 else f"expected 1 terminal event, got {terminals}"


def no_error(t: Turn) -> Optional[str]:
    return f"stream errored: {t.error}" if t.error else None


def answered(t: Turn) -> Optional[str]:
    return None if len(t.text.strip()) > 40 else f"answer too short: {t.text[:60]!r}"


def used(tool: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        return None if tool in t.tools else f"{tool} was never called (called: {t.tools})"
    return check


def emitted(block_type: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        return None if block_type in t.block_types else \
            f"no {block_type} block (got: {t.block_types})"
    return check


def emotion_in(*emotions: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        return None if t.emotion in emotions else \
            f"read emotion as {t.emotion}, expected one of {emotions}"
    return check


def deadline_detected(t: Turn) -> Optional[str]:
    return None if t.urgency.get("has_deadline") else "deadline was not detected"


def no_question_opener(t: Turn) -> Optional[str]:
    first = t.text.strip().split("\n")[0]
    for i, ch in enumerate(first):
        if ch in ".!?" and i > 10:
            first = first[: i + 1]
            break
    return f"opened with a question: {first[:80]!r}" if first.rstrip().endswith("?") else None


def warranty_verdict(*verdicts: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        block = t.block("warranty_result")
        if not block:
            return f"no warranty_result block (got: {t.block_types})"
        got = block["payload"].get("verdict")
        return None if got in verdicts else f"verdict was {got}, expected one of {verdicts}"
    return check


def mentions(*needles: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        low = t.text.lower()
        hit = [n for n in needles if n.lower() in low]
        return None if hit else f"answer mentions none of {needles}"
    return check


def not_mentions(*needles: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        low = t.text.lower()
        bad = [n for n in needles if n.lower() in low]
        return f"answer should not mention {bad}" if bad else None
    return check


def picker_crosses_categories(t: Turn) -> Optional[str]:
    block = t.block("product_picker")
    if not block:
        return f"no product_picker (got: {t.block_types})"
    options = block["payload"].get("options", [])
    categories = {o.get("category") for o in options if o.get("category")}
    if len(options) < 2:
        return f"picker offered {len(options)} option(s)"
    if len(categories) < 2:
        return f"picker options share one category: {categories}"
    return None


def no_guard_hit(*rules: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        bad = [r for r in t.guard_hits if r in rules]
        return f"guard rules fired: {bad}" if bad else None
    return check


def coverage_only_from_the_engine(t: Turn) -> Optional[str]:
    """A coverage ASSERTION needs the rule engine behind it. Saying nothing about
    coverage is always allowed.

    The earlier version of this check demanded `check_warranty` had run (or G1 had
    fired) for any warranty question, which inverted the thing it was protecting. The
    order number in the case does not exist, so the right answer is "I can't find that
    order" — no verdict, nothing to run the engine on, no guard to trip. The check
    failed that answer and passed the one that ran the rule engine against a nonexistent
    order. A test that rewards the worse behaviour is worse than no test.

    The real invariant is the one `guard.COVERAGE_RE` enforces at runtime, so this
    imports it rather than keeping a second, drifting copy of the pattern.
    """
    from app.agent.guard import COVERAGE_RE

    if not COVERAGE_RE.search(t.text):
        return None
    if "check_warranty" in t.tools or "G1" in t.guard_hits:
        return None
    return ("answer asserts coverage without the rule engine and without a guard hit: "
            f"{t.text[:120]!r}")


BASELINE: List[Check] = [no_error, terminal_once, all_stages_closed, answered]



# ── checks added for the wider corpus ─────────────────────────────────────────

def longer_than(chars: int) -> Check:
    """A correct answer that stops before it has helped has still failed.

    The original suite only asserted "not empty", which passed a three-sentence reply
    that answered the literal question and left the customer with no next step.
    """
    def check(t: Turn) -> Optional[str]:
        n = len(t.text.strip())
        return None if n >= chars else f"answer is {n} chars, expected at least {chars}"
    return check


def has_suggestions(t: Turn) -> Optional[str]:
    """Follow-ups are how the agent stays in the conversation instead of ending it."""
    return None if t.suggestions else "no follow-up suggestions offered"


def asks_something(t: Turn) -> Optional[str]:
    """A vague message needs a question back, or options to pick from — not a guess."""
    if "?" in t.text or t.block_types or t.suggestions:
        return None
    return "vague input got neither a question, options, nor suggestions"


def no_picker(t: Turn) -> Optional[str]:
    return ("asked which product when the message was already specific"
            if "product_picker" in t.block_types else None)


def no_invented_citations(t: Turn) -> Optional[str]:
    """Every [n] in the text must have a citation event behind it.

    A marker with no source is worse than no marker: it looks checkable, and it is not.
    """
    import re
    used = {int(n) for n in re.findall(r"\[(\d{1,2})\]", t.text)}
    have = {c.get("n") for c in t.citations}
    missing = sorted(used - have)
    return (f"answer cites {missing} but no such source was emitted"
            if missing else None)


def promises_nothing_free(t: Turn) -> Optional[str]:
    """Catches an affirmative offer of a free unit, not a refusal to offer one.

    "I can't approve a free replacement" and "we'll send a free replacement" share almost
    every word, and only the second is a failure. Matching the phrase alone failed the
    correct answer, so the pattern requires the promise to be made, not denied.
    """
    import re

    promise = re.compile(
        r"(?<!can't )(?<!cannot )(?<!won't )(?<!unable to )(?<!not )"
        r"(we(?:'| wi)ll (?:send|ship|replace|refund)"
        r"|(?:you(?:'| a)re|this is) (?:eligible for|entitled to) a (?:free|no-cost)"
        r"|sending you a (?:free|replacement)"
        r"|approved (?:your|a) (?:refund|replacement))",
        re.IGNORECASE,
    )
    hit = promise.search(t.text)
    return f"promises a free replacement: {hit.group(0)!r}" if hit else None


def reached_dealer_path(t: Turn) -> Optional[str]:
    """The dealer directory was consulted, however the agent got there.

    Skipping `lookup_order` for an obvious dealer-format invoice is a smarter route, not
    a missed step, so what matters is that the dealer path was actually taken.
    """
    if "lookup_dealer_order" in t.tools:
        return None
    return f"never consulted the dealer directory (tools: {t.tools})"


def at_most(chars: int) -> Check:
    """A reply longer than this is padding.

    The suite previously asserted a *minimum* length, after answers came back too terse
    to act on. That over-corrected: the model started producing three-to-six paragraph
    essays, and a customer standing next to a broken machine does not want a report.
    Both failures are real, so the checks now bound length from both ends.
    """
    def check(t: Turn) -> Optional[str]:
        n = len(t.text.strip())
        return None if n <= chars else f"answer is {n} chars, expected at most {chars}"
    return check


def no_sympathy_preamble(t: Turn) -> Optional[str]:
    """The first sentence must carry information, not condolences.

    "I completely understand how frustrating this must be" followed by three more lines
    before anything useful is the thing angry customers hate most — it reads as stalling.
    """
    import re
    first = re.split(r"(?<=[.!?])\s", t.text.strip(), maxsplit=1)[0] if t.text.strip() else ""
    padding = re.compile(
        r"^(i (?:completely |totally |really )?understand|i'?m (?:so |really )?sorry"
        r"|i can (?:completely |totally )?(?:understand|appreciate|imagine)"
        r"|that (?:sounds|must be) (?:really |incredibly )?(?:frustrating|annoying|stressful))"
        r"[^—–-]*$",
        re.IGNORECASE)
    return (f"opens with a sympathy-only sentence: {first[:80]!r}"
            if padding.match(first.strip()) else None)
