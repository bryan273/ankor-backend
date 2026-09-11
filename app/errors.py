"""Error taxonomy — `docs/API_CONTRACT.md` §1.

One exception class with a code, so the handler in `main.py` can render every failure
in the same envelope and the frontend has one shape to parse.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    code = "INTERNAL"
    status = 500

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None,
                 *, code: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        if code:
            self.code = code
        if status:
            self.status = status


class BadRequest(AppError):
    code, status = "BAD_REQUEST", 400


class Unauthorized(AppError):
    code, status = "UNAUTHORIZED", 401


class NotFound(AppError):
    code, status = "NOT_FOUND", 404


class BlockStale(AppError):
    code, status = "BLOCK_STALE", 409


class GuardBlocked(AppError):
    code, status = "GUARD_BLOCKED", 422


class Busy(AppError):
    code, status = "BUSY", 429


class RateLimited(AppError):
    code, status = "RATE_LIMITED", 429


class UpstreamError(AppError):
    code, status = "UPSTREAM_5XX", 502


class VoiceDisabled(AppError):
    code, status = "VOICE_DISABLED", 503
