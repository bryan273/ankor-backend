"""Agent state and the structured outputs the model is asked for.

`AgentState` is what flows through the graph. It is a plain Pydantic model rather than
a TypedDict so that a node returning a malformed field fails loudly at the boundary
instead of three nodes later.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.blocks import Block


class Emotion(str, Enum):
    CALM = "calm"
    CONFUSED = "confused"
    FRUSTRATED = "frustrated"
    ANGRY = "angry"
    ANXIOUS = "anxious"
    HAPPY = "happy"


class Intent(str, Enum):
    TROUBLESHOOT = "troubleshoot"
    PRODUCT_QUESTION = "product_question"
    ORDER_STATUS = "order_status"
    WARRANTY_CLAIM = "warranty_claim"
    RETURN_REFUND = "return_refund"
    BUY_ADVICE = "buy_advice"
    HOW_TO = "how_to"
    COMPLAINT = "complaint"
    CHITCHAT = "chitchat"
    ESCALATE_REQUEST = "escalate_request"
    UNCLEAR = "unclear"


class Urgency(BaseModel):
    has_deadline: bool = False
    deadline_hint: str = ""
    level: str = "normal"  # low | normal | high


class Entities(BaseModel):
    product_mentions: List[str] = Field(default_factory=list)
    error_codes: List[str] = Field(default_factory=list)
    order_refs: List[str] = Field(default_factory=list)
    purchase_channel_hint: Optional[str] = None
    symptoms: List[str] = Field(default_factory=list)


class Perception(BaseModel):
    """One structured call produces all of this — see SPECIFICATION §3.2 for why the
    fields are inferred together rather than by three separate classifiers."""
    emotion: Emotion = Emotion.CALM
    intensity: float = 0.0
    urgency: Urgency = Field(default_factory=Urgency)
    intent: Intent = Intent.UNCLEAR
    entities: Entities = Field(default_factory=Entities)
    language: str = "en"
    needs_image: bool = False
    safety_concern: bool = False
    # The customer says a fix we gave them did not work. Counted across the whole
    # conversation (sessions.meta.failed_attempts) — escalation keys off that count.
    fix_failed: bool = False
    # Accidental damage described anywhere in the conversation: none|drop|liquid|crack|wear.
    damage: str = "none"
    summary: str = ""


class ToolCall(BaseModel):
    call_id: str
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool = True
    ms: int = 0
    summary: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)

    def compact(self, limit: int = 1800) -> str:
        """What the ReAct loop sees. The composer gets the full object; the planner
        gets a trimmed view, because observations are the fastest-growing and least
        re-read part of the context."""
        import json
        body = json.dumps(self.data, ensure_ascii=False, default=str)
        if len(body) > limit:
            body = body[:limit] + f"…(+{len(body) - limit} chars, full result kept server-side)"
        return f"{self.tool} → {'ok' if self.ok else 'FAILED'}: {body}"


class Citation(BaseModel):
    n: int
    title: str
    url: str = ""
    section: str = ""
    sku: Optional[str] = None
    page: Optional[int] = None

    @field_validator("sku", mode="before")
    @classmethod
    def _first_sku(cls, v: Any) -> Optional[str]:
        """A KB article can cover several products, so its `sku` metadata is a list.
        Take the first one rather than rejecting the citation — a validation error here
        killed the turn *after* the answer had already streamed to the customer."""
        if isinstance(v, (list, tuple)):
            return str(v[0]) if v else None
        return str(v) if v is not None else None


class GuardHit(BaseModel):
    rule_id: str
    detail: str
    repaired: bool = False


class ResolvedProduct(BaseModel):
    sku: str
    name: str
    brand: str
    product_id: str
    category: Optional[str] = None
    how: str = "unknown"  # alias_unique | purchase_history | photo | user_pick | vector


class AgentState(BaseModel):
    # identity
    session_id: str
    message_id: str
    turn: int = 1

    # input
    user_message: str = ""
    rewritten_query: str = ""
    locale: str = "en"
    attachment_ids: List[str] = Field(default_factory=list)
    vlm_facts: List[Dict[str, Any]] = Field(default_factory=list)
    customer_email: Optional[str] = None
    customer_id: Optional[str] = None
    history: List[Dict[str, str]] = Field(default_factory=list)

    # understanding
    perception: Perception = Field(default_factory=Perception)
    resolved: Optional[ResolvedProduct] = None
    # The product this SESSION already settled on — by a picker click, purchase history,
    # a photo — read back at the start of every turn. It used to be written and never
    # read, so each turn re-identified from scratch: a customer who had just clicked
    # "robot vacuum" asked "is it under warranty?" and got the same picker again.
    session_sku: Optional[str] = None
    # Facts about the purchase this session already established from the RECORDS
    # (order table, dealer directory, the customer's own orders). The warranty engine
    # reads these instead of whatever the model types into its arguments.
    purchase: Dict[str, Any] = Field(default_factory=dict)
    # How many times, across the whole conversation, the customer has reported that a
    # fix did not work. Policy escalates on this, not on the current turn alone.
    failed_attempts: int = 0
    # The failure count at which this customer last said "let's keep trying" rather than
    # take a human. Re-offering the same escalation on the very next turn is how a
    # refusal turns into nagging, so the offer waits for a NEW failure. -1 means they
    # have never declined one.
    handoff_declined_at: int = -1
    # The ticket this session already opened, so a follow-up ("how long will that take?")
    # is answered from it instead of from nothing, and a second ticket is never opened.
    open_ticket: Dict[str, Any] = Field(default_factory=dict)
    # Once a conversation has been a safety case it stays one. The follow-up "can I keep
    # using it until the replacement comes?" carries no alarming words, so it used to be
    # answered as a fresh question: "that depends on what's wrong with it".
    safety_case: bool = False
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    # Every SKU this conversation has actually put on the customer's screen. A shopper
    # who has just been shown two chargers and asks "does it come with a cable" is
    # asking about one of THOSE two. Without this the follow-up reached the picker with
    # no anchor and offered three products that had never appeared, under the heading
    # "which one do you have" — a question a person who owns nothing yet cannot answer.
    shown_skus: List[str] = Field(default_factory=list)
    # The repair flow the customer is currently standing on, and which step they are on.
    # The steps block was built from THIS turn's `get_troubleshooting_flow` result only,
    # so clicking "I'm stuck" on step 1 ran the planner again, it did not think to look
    # the flow up a second time, and the steps vanished from under the customer. The
    # reply then said there was no guide on file for a device that had just been given
    # one, two messages further up the same conversation.
    active_flow: Dict[str, Any] = Field(default_factory=dict)

    # reasoning
    iterations: int = 0
    tool_calls: List[ToolCall] = Field(default_factory=list)
    observations: List[ToolResult] = Field(default_factory=list)
    scratchpad: List[str] = Field(default_factory=list)

    # output
    answer: str = ""
    blocks: List[Block] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    guard_hits: List[GuardHit] = Field(default_factory=list)
    ticket_id: Optional[str] = None
    awaiting_action: bool = False

    # accounting
    cost_credits: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def observation_by_tool(self, tool: str) -> Optional[ToolResult]:
        for o in reversed(self.observations):
            if o.tool == tool and o.ok:
                return o
        return None

    def tools_used(self) -> List[str]:
        return [o.tool for o in self.observations]

    def add_usage(self, usage: Any) -> None:
        self.cost_credits += getattr(usage, "cost_credits", 0.0)
        self.input_tokens += getattr(usage, "input", 0)
        self.output_tokens += getattr(usage, "output", 0)
