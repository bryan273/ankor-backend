"""Attachment upload — the front door for the multimodal path."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi import Response
from fastapi.responses import RedirectResponse

from app.clients import db
from app.deps import require_api_key
from app.errors import BadRequest, NotFound, VoiceDisabled
from app.config import settings
from app.clients import storage
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
async def raw(attachment_id: str, size: str = "full") -> Response:
    """Serve the image, from Supabase Storage or from disk.

    Deliberately unauthenticated so an `<img src>` can load it: the id is a UUID, the
    route is read-only, and the alternative is signed URLs the frontend would have to
    refresh mid-conversation. The bucket itself stays private, so this endpoint is the
    only way in.
    """
    row = await db.fetch_one(
        "select storage_path, thumb_path, mime from attachments where id::text = %s",
        (attachment_id,))
    if not row:
        raise NotFound("no such attachment", {"attachment_id": attachment_id})
    # `?size=thumb` for previews. The original is a phone photo drawn at 56 px, so the
    # small copy is ~1% of the bytes; falling back to the original keeps rows that
    # predate thumbnails working.
    wanted = row["thumb_path"] if (size == "thumb" and row["thumb_path"]) else row["storage_path"]
    # Big files get a signed redirect so the browser pulls them straight from the bucket.
    # Thumbnails do NOT: signing is a round trip of its own, and for 14 KB that round
    # trip IS the latency — two hops to deliver less than one packet's worth of image.
    # Relaying the small copy is measurably faster than being clever about it.
    if wanted is not row["thumb_path"]:
        direct = await storage.signed_url(wanted)
        if direct:
            return RedirectResponse(direct, status_code=307)

    raw_bytes = await vision.read_bytes(wanted)
    if raw_bytes is None:
        raise NotFound("attachment file is gone", {"attachment_id": attachment_id})
    return Response(content=raw_bytes, media_type=row["mime"] or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


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
