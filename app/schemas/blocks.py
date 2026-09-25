"""UI block schemas — `docs/API_CONTRACT.md` §4.

These models are the reason the frontend can render generative UI without guessing:
every block the agent emits is one of these shapes, validated before it hits the wire.
`scripts/gen_ts_types.py` turns them into TypeScript so the two repos cannot drift.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def new_block_id() -> str:
    return f"blk_{uuid.uuid4().hex[:10]}"


class BlockType(str, Enum):
    PRODUCT_PICKER = "product_picker"
    DIAGNOSTIC_STEPS = "diagnostic_steps"
    ORDER_CARD = "order_card"
    WARRANTY_RESULT = "warranty_result"
    UPLOAD_REQUEST = "upload_request"
    PRODUCT_CARD = "product_card"
    PRODUCT_GRID = "product_grid"
    COMPARISON_TABLE = "comparison_table"
    TICKET_STATUS = "ticket_status"
    HUMAN_HANDOFF = "human_handoff"
    FORM = "form"
    QUICK_REPLIES = "quick_replies"
    LINK_LIST = "link_list"
    VIDEO_GUIDE = "video_guide"


class BlockAction(BaseModel):
    id: str
    label: str
    style: Literal["primary", "secondary", "danger", "ghost"] = "secondary"
    value: Dict[str, Any] = Field(default_factory=dict)


class Block(BaseModel):
    block_id: str = Field(default_factory=new_block_id)
    type: BlockType
    payload: Dict[str, Any] = Field(default_factory=dict)
    actions: List[BlockAction] = Field(default_factory=list)
    state: Literal["active", "answered", "expired"] = "active"
    # True when the graph pauses on this block and waits for the user.
    blocking: bool = False


# ── payload shapes, one per block type ────────────────────────────────────────

class ProductOption(BaseModel):
    sku: str
    name: str
    brand: str
    image_url: Optional[str] = None
    hint: str = ""
    price: Optional[float] = None
    category: Optional[str] = None


class ProductPickerPayload(BaseModel):
    question: str
    options: List[ProductOption]


class DiagnosticStep(BaseModel):
    step_id: str
    ord: int
    instruction: str
    why: str = ""
    expected: str = ""
    image_url: Optional[str] = None
    citation: Optional[int] = None


class DiagnosticStepsPayload(BaseModel):
    title: str
    symptom: str = ""
    estimated_minutes: Optional[int] = None
    steps: List[DiagnosticStep]
    current_step: Optional[str] = None


class OrderItemView(BaseModel):
    sku: Optional[str] = None
    name: str
    qty: int = 1
    serial: Optional[str] = None
    # A line of text is a worse answer to "is this the right order?" than the picture of
    # the thing. The join to `products` was already there; only the column was missing.
    image_url: Optional[str] = None


class OrderCardPayload(BaseModel):
    order_no: str
    channel: str
    purchase_date: Optional[str] = None
    status: Optional[str] = None
    items: List[OrderItemView] = Field(default_factory=list)
    warranty_until: Optional[str] = None
    dealer_name: Optional[str] = None
    source: str = "demo"


class WarrantyResultPayload(BaseModel):
    verdict: str
    reason_code: str
    explanation: str
    required_evidence: List[str] = Field(default_factory=list)
    next_action: Optional[str] = None
    dealer: Optional[Dict[str, Any]] = None
    warranty_until: Optional[str] = None


class UploadRequestPayload(BaseModel):
    prompt: str
    accepts: List[str] = Field(default_factory=lambda: ["image/jpeg", "image/png"])
    max_files: int = 2
    purpose: str = "evidence"


class ProductCardItem(BaseModel):
    sku: str
    name: str
    price: Optional[float] = None
    currency: str = "USD"
    image_url: Optional[str] = None
    url: Optional[str] = None
    badges: List[str] = Field(default_factory=list)
    reason: str = ""


class ProductGridPayload(BaseModel):
    items: List[ProductCardItem]
    title: str = ""


class ComparisonRow(BaseModel):
    label: str
    values: List[str]


class ComparisonTablePayload(BaseModel):
    skus: List[str]
    names: List[str] = Field(default_factory=list)
    rows: List[ComparisonRow]
    recommendation: Optional[Dict[str, str]] = None


class TicketEventView(BaseModel):
    kind: str
    at: str
    note: str = ""


class TicketStatusPayload(BaseModel):
    ticket_id: str
    status: str
    priority: str
    summary: str
    timeline: List[TicketEventView] = Field(default_factory=list)
    eta: Optional[str] = None


class HumanHandoffPayload(BaseModel):
    reason: str
    queue_position: Optional[int] = None
    eta_minutes: Optional[int] = None
    summary_preview: str = ""
    channels: List[str] = Field(default_factory=lambda: ["chat", "email"])


class FormField(BaseModel):
    id: str
    label: str
    type: Literal["text", "email", "tel", "date", "select", "textarea"] = "text"
    required: bool = False
    hint: str = ""
    options: List[str] = Field(default_factory=list)


class FormPayload(BaseModel):
    title: str
    fields: List[FormField]
    submit_label: str = "Submit"


class QuickReply(BaseModel):
    text: str


class QuickRepliesPayload(BaseModel):
    items: List[QuickReply]


class LinkItem(BaseModel):
    title: str
    url: str
    kind: str = "article"
    page: Optional[int] = None


class LinkListPayload(BaseModel):
    items: List[LinkItem]
    title: str = ""


# ── builders — the agent calls these, never constructs a dict by hand ─────────

def product_picker(question: str, options: List[ProductOption]) -> Block:
    return Block(
        type=BlockType.PRODUCT_PICKER,
        payload=ProductPickerPayload(question=question, options=options).model_dump(),
        actions=[
            BlockAction(id="select_product", label=o.name, style="primary", value={"sku": o.sku})
            for o in options
        ],
        blocking=True,
    )


def diagnostic_steps(payload: DiagnosticStepsPayload) -> Block:
    return Block(
        type=BlockType.DIAGNOSTIC_STEPS,
        payload=payload.model_dump(),
        actions=[
            BlockAction(id="step_result", label="It worked", style="primary",
                        value={"outcome": "worked"}),
            BlockAction(id="step_result", label="Still not fixed", style="secondary",
                        value={"outcome": "failed"}),
            BlockAction(id="step_result", label="I'm stuck", style="ghost",
                        value={"outcome": "stuck"}),
        ],
    )


def warranty_result(payload: WarrantyResultPayload) -> Block:
    actions: List[BlockAction] = []
    if payload.next_action == "upload_proof":
        actions.append(BlockAction(id="upload_proof", label="Upload my invoice",
                                   style="primary", value={"purpose": "invoice"}))
    if payload.verdict in ("escalate_human", "covered", "covered_via_dealer"):
        actions.append(BlockAction(id="open_ticket", label="Start the claim",
                                   style="primary", value={}))
    return Block(type=BlockType.WARRANTY_RESULT, payload=payload.model_dump(), actions=actions)


def quick_replies(texts: List[str]) -> Block:
    return Block(
        type=BlockType.QUICK_REPLIES,
        payload=QuickRepliesPayload(items=[QuickReply(text=t) for t in texts]).model_dump(),
        actions=[BlockAction(id="quick_reply", label=t, value={"text": t}) for t in texts],
    )


def human_handoff(payload: HumanHandoffPayload) -> Block:
    return Block(
        type=BlockType.HUMAN_HANDOFF,
        payload=payload.model_dump(),
        actions=[
            BlockAction(id="confirm_handoff", label="Yes, connect me", style="primary", value={}),
            BlockAction(id="keep_trying", label="Let's keep trying", style="ghost", value={}),
        ],
    )


def upload_request(payload: UploadRequestPayload) -> Block:
    return Block(type=BlockType.UPLOAD_REQUEST, payload=payload.model_dump(),
                 actions=[BlockAction(id="upload", label="Choose a photo", style="primary")])
