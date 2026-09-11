"""Attachment upload — the front door for the multimodal path."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.clients import db
from app.deps import require_api_key
from app.errors import BadRequest, NotFound, VoiceDisabled
from app.config import settings
from app.services import vision

router = APIRouter(tags=["attachments"])


@router.post("/attachments")
async def upload(file: UploadFile = File(...), session_id: Optional[str] = Form(default=None),
                 _: str = Depends(require_api_key)) -> Dict[str, Any]:
    """The vision pass runs synchronously so the next `/chat` call already has the facts.
    It typically costs 1.5-3 s, spent while the customer is still typing rather than
    while they are waiting for an answer."""
    mime = (file.content_type or "").lower()
    if mime not in vision.ALLOWED:
        raise BadRequest(f"unsupported image type: {mime or 'unknown'}",
                         {"allowed": sorted(vision.ALLOWED)})
    raw = await file.read()
    if not raw:
        raise BadRequest("empty file")
    if len(raw) > vision.MAX_BYTES:
        raise BadRequest("file too large", {"max_bytes": vision.MAX_BYTES,
                                            "got_bytes": len(raw)})
    return await vision.save_attachment(session_id, raw, mime, file.filename or "")


@router.get("/attachments/{attachment_id}/raw")
async def raw(attachment_id: str) -> FileResponse:
    row = await db.fetch_one(
        "select storage_path, mime from attachments where id::text = %s", (attachment_id,))
    if not row:
        raise NotFound("no such attachment", {"attachment_id": attachment_id})
    import pathlib
    path = pathlib.Path(row["storage_path"])
    if not path.exists():
        raise NotFound("attachment file is gone", {"attachment_id": attachment_id})
    return FileResponse(path, media_type=row["mime"] or "image/jpeg")


@router.post("/voice/transcribe")
async def transcribe(file: UploadFile = File(...),
                     _: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Flagged off until a Deepgram key exists. The frontend falls back to the browser's
    own speech recognition, so the demo never blocks on a missing key."""
    if not settings.voice_enabled or not settings.deepgram_api_key:
        raise VoiceDisabled("voice transcription is not enabled",
                            {"hint": "set DEEPGRAM_API_KEY and VOICE_ENABLED=true"})
    import httpx
    raw = await file.read()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-3", "smart_format": "true", "detect_language": "true"},
            headers={"Authorization": f"Token {settings.deepgram_api_key}",
                     "Content-Type": file.content_type or "audio/webm"},
            content=raw,
        )
        r.raise_for_status()
        data = r.json()
    alt = data["results"]["channels"][0]["alternatives"][0]
    return {"text": alt.get("transcript", ""), "confidence": alt.get("confidence", 0.0),
            "language": data["results"]["channels"][0].get("detected_language", "en")}
