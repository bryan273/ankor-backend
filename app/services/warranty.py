"""Warranty eligibility — a rule engine, deliberately not an LLM.

An LLM that "decides" warranty will eventually promise a refund the company owes
nobody, and it will do it fluently. So the decision is a pure function over a table,
the LLM only phrases the verdict, and the guard node refuses to let a coverage
sentence through unless this function actually ran.

The matrix is SPECIFICATION §3.6. Scenario S3 (a dealer order that is not in the
order system) is the `needs_proof → covered_via_dealer` path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class Verdict(str, Enum):
    COVERED = "covered"
    COVERED_VIA_DEALER = "covered_via_dealer"
    COVERED_PENDING_VERIFICATION = "covered_pending_verification"
    NEEDS_PROOF = "needs_proof"
    NOT_COVERED_POLICY = "not_covered_policy"
    EXPIRED = "expired"
    ESCALATE_HUMAN = "escalate_human"


class Channel(str, Enum):
    OFFICIAL_STORE = "official_store"
    MARKETPLACE = "marketplace"
    AMAZON = "amazon"
    DEALER = "dealer"
    UNKNOWN = "unknown"


class DamageClass(str, Enum):
    DEFECT = "defect"
    PHYSICAL_DAMAGE = "physical_damage"
    WEAR = "wear"
    UNKNOWN = "unknown"


DEFAULT_TERM_MONTHS = 12

# Verdicts that let the composer write a coverage sentence at all.
POSITIVE = {Verdict.COVERED, Verdict.COVERED_VIA_DEALER, Verdict.COVERED_PENDING_VERIFICATION}


@dataclass
class WarrantyInput:
    sku: Optional[str] = None
    category: Optional[str] = None
    purchase_date: Optional[date] = None
    channel: Channel = Channel.UNKNOWN
    damage_class: DamageClass = DamageClass.UNKNOWN
    order_found: bool = False
    dealer_matched: bool = False
    proof_present: bool = False
    term_months: int = DEFAULT_TERM_MONTHS
    safety_concern: bool = False


@dataclass
class WarrantyDecision:
    verdict: Verdict
    reason_code: str
    explanation: str
    required_evidence: List[str] = field(default_factory=list)
    next_action: Optional[str] = None
    warranty_until: Optional[date] = None
    months_remaining: Optional[int] = None

    def as_payload(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "required_evidence": self.required_evidence,
            "next_action": self.next_action,
            "warranty_until": self.warranty_until.isoformat() if self.warranty_until else None,
            "months_remaining": self.months_remaining,
        }


def add_months(d: date, months: int) -> date:
    """Calendar-correct month arithmetic; clamps the day so 31 Jan + 1 month is 28/29 Feb."""
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last_day))


def within_term(purchase: Optional[date], months: int, today: Optional[date] = None):
    """Returns `(in_term, expiry, months_remaining)`. An unknown purchase date is
    unknown, never a silent 'yes' — that asymmetry is the whole point of the engine."""
    if purchase is None:
        return None, None, None
    today = today or date.today()
    expiry = add_months(purchase, months)
    remaining = max((expiry - today).days, 0) // 30
    return today <= expiry, expiry, remaining


def decide(inp: WarrantyInput, today: Optional[date] = None) -> WarrantyDecision:
    """The whole matrix, in evaluation order. Ordering matters: safety outranks
    everything, and 'we cannot tell' outranks any guess about coverage."""
    in_term, expiry, remaining = within_term(inp.purchase_date, inp.term_months, today)

    # 0. Safety first. A swelling battery is not a warranty conversation.
    if inp.safety_concern:
        return WarrantyDecision(
            Verdict.ESCALATE_HUMAN, "SAFETY_CONCERN",
            "This looks like a safety issue, so it goes straight to a specialist rather "
            "than through the normal warranty flow.",
            required_evidence=["photo_of_device", "serial_number"],
            next_action="escalate_urgent", warranty_until=expiry, months_remaining=remaining,
        )

    # 1. Dealer channel — scenario S3.
    if inp.channel == Channel.DEALER or (not inp.order_found and inp.dealer_matched):
        if inp.dealer_matched and inp.proof_present:
            if in_term is False:
                return WarrantyDecision(
                    Verdict.EXPIRED, "DEALER_ORDER_OUT_OF_TERM",
                    "The dealer invoice checks out, but the coverage window has closed.",
                    next_action="offer_paid_repair", warranty_until=expiry,
                    months_remaining=remaining,
                )
            if inp.damage_class == DamageClass.PHYSICAL_DAMAGE:
                return WarrantyDecision(
                    Verdict.NOT_COVERED_POLICY, "PHYSICAL_DAMAGE_EXCLUDED",
                    "Accidental damage sits outside the warranty, dealer purchase or not.",
                    next_action="offer_paid_repair", warranty_until=expiry,
                    months_remaining=remaining,
                )
            return WarrantyDecision(
                Verdict.COVERED_VIA_DEALER, "DEALER_VERIFIED",
                "The dealer is an authorised partner and the invoice is in term, so this "
                "is covered — handled through that dealer's service channel.",
                next_action="route_to_dealer", warranty_until=expiry, months_remaining=remaining,
            )
        return WarrantyDecision(
            Verdict.NEEDS_PROOF, "DEALER_ORDER_NOT_IN_SYSTEM",
            "That number looks like a dealer invoice rather than a store order, which is "
            "why it does not show up here. With a photo of the invoice and the dealer's "
            "name the claim can go ahead.",
            required_evidence=["invoice_photo", "dealer_name"],
            next_action="upload_proof", warranty_until=expiry, months_remaining=remaining,
        )

    # 2. Official store — the purchase record itself is the proof.
    if inp.channel == Channel.OFFICIAL_STORE and inp.order_found:
        if in_term is False:
            return WarrantyDecision(
                Verdict.EXPIRED, "OUT_OF_TERM",
                "The order is on file, but its coverage window has closed.",
                next_action="offer_paid_repair", warranty_until=expiry,
                months_remaining=remaining,
            )
        if inp.damage_class == DamageClass.PHYSICAL_DAMAGE:
            return WarrantyDecision(
                Verdict.NOT_COVERED_POLICY, "PHYSICAL_DAMAGE_EXCLUDED",
                "The warranty covers manufacturing faults; accidental damage falls outside "
                "it. A paid repair or a trade-in is usually the cheaper route.",
                next_action="offer_paid_repair", warranty_until=expiry,
                months_remaining=remaining,
            )
        if inp.damage_class == DamageClass.WEAR:
            return WarrantyDecision(
                Verdict.NOT_COVERED_POLICY, "CONSUMABLE_WEAR",
                "Consumable parts like brushes, filters and ear tips wear out by design, so "
                "they are replacement items rather than warranty claims.",
                next_action="offer_replacement_part", warranty_until=expiry,
                months_remaining=remaining,
            )
        if in_term is None:
            return WarrantyDecision(
                Verdict.NEEDS_PROOF, "PURCHASE_DATE_UNKNOWN",
                "The order is on file but without a purchase date the coverage window "
                "cannot be confirmed.",
                required_evidence=["purchase_date"], next_action="ask_purchase_date",
            )
        return WarrantyDecision(
            Verdict.COVERED, "IN_TERM_DEFECT",
            "The order is on file and still in its coverage window, so a manufacturing "
            "fault is covered.",
            next_action="start_claim", warranty_until=expiry, months_remaining=remaining,
        )

    # 3. Marketplace — real purchase, someone else's order system.
    if inp.channel in (Channel.MARKETPLACE, Channel.AMAZON):
        if not inp.proof_present:
            return WarrantyDecision(
                Verdict.NEEDS_PROOF, "MARKETPLACE_PROOF_REQUIRED",
                "Marketplace orders do not appear in this system, so the claim needs the "
                "order confirmation or invoice.",
                required_evidence=["invoice_photo"], next_action="upload_proof",
                warranty_until=expiry, months_remaining=remaining,
            )
        if in_term is False:
            return WarrantyDecision(
                Verdict.EXPIRED, "OUT_OF_TERM", "The receipt is in order but the coverage "
                "window has closed.", next_action="offer_paid_repair",
                warranty_until=expiry, months_remaining=remaining,
            )
        if inp.damage_class == DamageClass.PHYSICAL_DAMAGE:
            return WarrantyDecision(
                Verdict.NOT_COVERED_POLICY, "PHYSICAL_DAMAGE_EXCLUDED",
                "Accidental damage sits outside the warranty.",
                next_action="offer_paid_repair", warranty_until=expiry,
                months_remaining=remaining,
            )
        return WarrantyDecision(
            Verdict.COVERED_PENDING_VERIFICATION, "MARKETPLACE_PROOF_SUPPLIED",
            "With the receipt supplied this is covered once the team verifies the seller "
            "was an authorised one — usually same day.",
            next_action="start_claim", warranty_until=expiry, months_remaining=remaining,
        )

    # 4. Nothing identifies the purchase. Escalate rather than guess.
    if not inp.order_found and not inp.proof_present:
        return WarrantyDecision(
            Verdict.ESCALATE_HUMAN, "NO_PURCHASE_EVIDENCE",
            "Without an order number or a receipt the purchase cannot be verified here, "
            "so a human agent should take a look.",
            required_evidence=["order_no", "invoice_photo"], next_action="escalate",
        )

    return WarrantyDecision(
        Verdict.ESCALATE_HUMAN, "UNMATCHED_CASE",
        "This combination does not map to a standard warranty path, so it goes to a "
        "human agent rather than an automated answer.",
        next_action="escalate", warranty_until=expiry, months_remaining=remaining,
    )


# ── input helpers ─────────────────────────────────────────────────────────────

_SAFETY_PATTERNS = [
    r"\bswell(?:ing|ed|s)?\b", r"\bbulg(?:e|ing|ed)\b", r"\bburn(?:ing|t|ed)?\b",
    r"\bsmok(?:e|ing)\b", r"\bfire\b", r"\bsparks?\b", r"\bmelt(?:ing|ed)?\b",
    r"\boverheat(?:ing|ed)?\b", r"\bexplo(?:de|ded|sion)\b", r"\bshock(?:ed)?\b",
    r"\bleak(?:ing|ed)?\s+(?:battery|acid|fluid)\b", r"\bhot to touch\b",
    r"\belectric shock\b", r"\bcaught fire\b",
]
_SAFETY_RE = re.compile("|".join(_SAFETY_PATTERNS), re.IGNORECASE)


def detect_safety_concern(*texts: Optional[str]) -> bool:
    """A cheap, deliberately over-eager check. A false positive costs one unnecessary
    escalation; a false negative costs someone's kitchen."""
    for t in texts:
        if t and _SAFETY_RE.search(t):
            return True
    return False


def classify_channel(raw: Optional[str]) -> Channel:
    if not raw:
        return Channel.UNKNOWN
    r = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if r in ("official_store", "official", "anker_store", "eufy_store", "direct"):
        return Channel.OFFICIAL_STORE
    if r in ("amazon", "amazon_us", "amazon_de"):
        return Channel.AMAZON
    if r in ("marketplace", "lazada", "shopee", "tokopedia", "ebay", "walmart", "aliexpress"):
        return Channel.MARKETPLACE
    if r in ("dealer", "reseller", "distributor", "partner", "authorized_dealer",
             "authorised_dealer", "authorized_reseller", "official_dealer",
             "local_dealer", "retailer"):
        return Channel.DEALER
    return Channel.UNKNOWN


def classify_damage(raw: Optional[str]) -> DamageClass:
    if not raw:
        return DamageClass.UNKNOWN
    r = raw.strip().lower()
    if r in ("defect", "manufacturing_defect", "fault", "malfunction"):
        return DamageClass.DEFECT
    if r in ("physical_damage", "damage", "cracked", "dropped", "water_damage", "liquid"):
        return DamageClass.PHYSICAL_DAMAGE
    if r in ("wear", "consumable", "worn", "normal_wear"):
        return DamageClass.WEAR
    return DamageClass.UNKNOWN


def parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None
