"""Request/response models for the HTTP surface — `docs/API_CONTRACT.md` §2."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClientContext(BaseModel):
    customer_email: Optional[str] = None
    timezone: Optional[str] = None
    entry_point: Optional[str] = None


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = ""
    attachment_ids: List[str] = Field(default_factory=list)
    locale: str = "en"
    client_context: ClientContext = Field(default_factory=ClientContext)


class ChatActionRequest(BaseModel):
    session_id: str
    block_id: str
    action_id: str
    value: Dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    version: str
    deps: Dict[str, Any]


class VLMFacts(BaseModel):
    caption: str = ""
    ocr_text: str = ""
    detected: Dict[str, Any] = Field(default_factory=dict)
    safety_flags: List[str] = Field(default_factory=list)


class AttachmentResponse(BaseModel):
    attachment_id: str
    url: Optional[str] = None
    vlm_facts: VLMFacts


class ProductView(BaseModel):
    sku: str
    name: str
    brand: str
    category: Optional[str] = None
    price: Optional[float] = None
    currency: str = "USD"
    url: Optional[str] = None
    hero_image: Optional[str] = None
    status: str = "active"
    warranty_months: Optional[int] = None


class ProductDetail(ProductView):
    specs: Dict[str, str] = Field(default_factory=dict)
    media: List[Dict[str, Any]] = Field(default_factory=list)
    docs: List[Dict[str, Any]] = Field(default_factory=list)
    error_codes: List[Dict[str, Any]] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)


class TicketCreateRequest(BaseModel):
    session_id: Optional[str] = None
    summary: str
    priority: str = "normal"
    reason: str = ""
    sku: Optional[str] = None
    customer_email: Optional[str] = None
