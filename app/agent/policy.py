"""Emotion → response policy.

Deterministic, because how you treat an angry customer should not vary run to run. The
table is SPECIFICATION §3.2. `compose` gets the resulting instruction as text; `guard`
checks afterwards that the instruction was actually followed.

The rule that matters most: when someone is angry or frustrated, the reply must not
open with a question. Being asked for your order number while furious is the exact
moment support loses a customer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.schemas.agent import Emotion, Perception


@dataclass
class Policy:
    acknowledge_first: bool
    no_question_opener: bool
    step_granularity: str
    escalate_after_failed_steps: int
    offer_human_early: bool
    max_steps_before_checkin: int
    mention_deadline: bool
    allow_upsell: bool

    def describe(self) -> str:
        """A short label for the trace: which handling this turn got, and why it looks
        the way it does. Shown in the step detail so the tone is inspectable rather than
        something the reader has to infer from the prose."""
        bits = []
        bits.append("acknowledge first" if self.acknowledge_first else "answer directly")
        if self.no_question_opener:
            bits.append("no question opener")
        if self.mention_deadline:
            bits.append("deadline noted")
        if self.offer_human_early:
            bits.append("offer a human")
        if not self.allow_upsell:
            bits.append("no upsell")
        return ", ".join(bits)

    def as_prompt(self, p: Perception) -> str:
        lines: List[str] = []
        if self.acknowledge_first:
            lines.append(
                "Open by acknowledging how they feel, in one short sentence, in your own "
                "words. Not a scripted apology — something a person would actually say. "
                "Then get straight to helping."
            )
        else:
            lines.append("Answer directly. No emotional preamble; they just want the answer.")

        if self.no_question_opener:
            lines.append(
                "Do NOT open with a question. Do not ask for anything they have already "
                "told you. If you need something, help first with what you have, then ask "
                "at the end."
            )
        if self.mention_deadline and p.urgency.deadline_hint:
            lines.append(
                f"They are up against a deadline ({p.urgency.deadline_hint}). Say you have "
                "noted it, put the fastest fix first, and give a fallback if it does not "
                "work in time."
            )
        if self.step_granularity == "fine":
            lines.append(
                "One physical action per step. Say what they should see after each one, so "
                "they know whether it worked."
            )
        if self.max_steps_before_checkin:
            lines.append(
                f"At most {self.max_steps_before_checkin} steps, then stop and ask how it "
                "went before continuing."
            )
        if self.offer_human_early:
            lines.append(
                "Mention that a human can take over, once, briefly, near the end — offered, "
                "not pushed."
            )
        if not self.allow_upsell:
            lines.append("Do not suggest buying anything. Not the moment.")
        return "\n".join(f"- {line}" for line in lines)


_TABLE = {
    Emotion.CALM: Policy(False, False, "normal", 3, False, 0, False, True),
    Emotion.HAPPY: Policy(False, False, "normal", 3, False, 0, False, True),
    Emotion.CONFUSED: Policy(True, False, "fine", 3, False, 4, False, True),
    Emotion.FRUSTRATED: Policy(True, True, "fine", 2, True, 3, True, False),
    Emotion.ANGRY: Policy(True, True, "fine", 1, True, 3, True, False),
    Emotion.ANXIOUS: Policy(True, False, "fine", 2, False, 3, True, False),
}


def policy_for(p: Perception) -> Policy:
    policy = _TABLE.get(p.emotion, _TABLE[Emotion.CALM])
    if p.urgency.has_deadline and not policy.mention_deadline:
        # A deadline changes the handling even for a calm customer.
        policy = Policy(**{**policy.__dict__, "mention_deadline": True,
                          "allow_upsell": False})
    if p.safety_concern:
        policy = Policy(**{**policy.__dict__, "acknowledge_first": True,
                          "no_question_opener": True, "allow_upsell": False,
                          "escalate_after_failed_steps": 0})
    return policy


SAFETY_INSTRUCTION = (
    "This is a SAFETY issue. Do not give troubleshooting steps. Tell them, calmly and "
    "immediately: stop using the device, unplug it if that is safe to do, keep it away "
    "from anything flammable, and do not charge it. Say a specialist is being brought in "
    "right away. Nothing else — no diagnosis, no warranty discussion, no upsell."
)
