"""Shared FastAPI dependencies."""
from __future__ import annotations

from fastapi import Header
from typing import Optional

from app.config import settings
from app.errors import Unauthorized


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """Demo auth. Real identity comes from `session_id`; this only keeps the local
    service from being open to anything on the machine."""
    if not settings.backend_api_key:
        return "open"
    if x_api_key != settings.backend_api_key:
        raise Unauthorized("missing or incorrect X-API-Key")
    return x_api_key
