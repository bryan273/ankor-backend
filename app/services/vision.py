"""Vision — reading the photos customers send.

The VLM pass runs at upload time, not at answer time. By the time the customer finishes
typing "and now it does this", the facts are already in the database, so the turn does
not pay 2-3 seconds for something that could have happened while they typed.

What comes out is deliberately structured rather than prose: an error code the agent can
look up exactly, a form factor that can discriminate a robot vacuum from a breast pump,
a damage class the warranty engine can consume. A caption alone would force every
downstream step to re-read English.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import structlog

from app.agent.prompts import VLM_SYSTEM
from app.clients import db
from app.clients import storage
from app.clients.llm import get_llm

log = structlog.get_logger(__name__)

MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/jpg"}


def to_data_uri(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def resize(raw: bytes, max_px: int, quality: int = 82) -> Optional[bytes]:
    """Downscale to fit `max_px` on the long edge. None if it cannot be decoded.

    Two very different callers, same reason. A phone photo is 3-5 MB and every one of
    those bytes was being base64'd into the vision request — paying upload time and
    image tokens for detail the model downsamples away anyway. And the same 1.6 MB file
    was being shipped to the browser to be drawn at 56x56, which took four seconds a
    thumbnail.
    """
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(raw))
        img = img.convert("RGB") if img.mode in ("RGBA", "P", "LA") else img
        if max(img.size) <= max_px:
            # Already small. Re-encoding would cost quality for nothing.
            return None
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001 — an odd file must not fail the upload
        log.debug("vision.resize_failed", error=str(e)[:120])
        return None


# What the vision model actually needs. Beyond roughly this, detail is discarded by the
# model's own preprocessing and you are paying for pixels nobody reads.
VLM_MAX_PX = 1280
THUMB_MAX_PX = 480


async def describe_image(raw: bytes, mime: str,
                         question: Optional[str] = None) -> Dict[str, Any]:
    """One vision call. Returns the structured facts, or an empty-but-valid shape so a
    vision failure degrades the turn instead of ending it."""
    ask = question or "Describe this image for a support agent."
    smaller = resize(raw, VLM_MAX_PX)
    if smaller:
        log.debug("vision.downscaled", from_bytes=len(raw), to_bytes=len(smaller))
        raw, mime = smaller, "image/jpeg"
    try:
        # Explicitly the vision route: DeepSeek handles the text calls but
        # cannot see, and silently sending an image there returns confident
        # nonsense rather than an error.
        data, usage = await get_llm().vision_json(
            [{"role": "system", "content": VLM_SYSTEM},
             {"role": "user", "content": [
                 {"type": "text", "text": ask},
                 {"type": "image_url", "image_url": {"url": to_data_uri(raw, mime)}},
             ]}],
            max_tokens=2500, default={},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("vision.failed", error=str(e)[:160])
        return {"caption": "", "ocr_text": "", "detected": {}, "safety_flags": [],
                "error": str(e)[:160]}

    detected = data.get("detected") or {}
    return {
        "caption": str(data.get("caption") or "")[:400],
        "ocr_text": str(data.get("ocr_text") or "")[:2000],
        # This rebuilds `detected` field by field rather than passing the model's object
        # through, so that a hallucinated extra key cannot reach the rest of the system.
        # The cost of that is real: a field added to VLM_SYSTEM and not added here is
        # extracted correctly, returned correctly, and then silently discarded here. If
        # you add to the prompt, add to this list.
        "detected": {
            "brand": (detected.get("brand") or "unknown"),
            "form_factor": (detected.get("form_factor") or "unknown"),
            "model_number": str(detected.get("model_number") or "")[:60],
            "model_number_visible": bool(detected.get("model_number_visible")),
            "where_to_look": str(detected.get("where_to_look") or "")[:160],
            "distinguishing_features": [
                str(f)[:80] for f in (detected.get("distinguishing_features") or [])][:6],
            "error_code": (detected.get("error_code") or ""),
            "damage_class": (detected.get("damage_class") or "unknown"),
            "confidence": float(detected.get("confidence") or 0.0),
        },
        "safety_flags": [str(f) for f in (data.get("safety_flags") or [])][:5],
    }


async def save_attachment(session_id: Optional[str], raw: bytes, mime: str,
                          filename: str = "") -> Dict[str, Any]:
    """Store the bytes and the facts.

    Supabase Storage is the real home: the container's disk does not survive a restart,
    and a photo row that promises an image the disk no longer has is worse than no row.
    The local copy is still written as a fallback for when the bucket is unreachable or
    unconfigured — a failed archive write must never fail the customer's upload.
    """
    facts = await describe_image(raw, mime)
    import datetime
    import pathlib
    import uuid
    att_uuid = uuid.uuid4().hex
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
    folder = pathlib.Path("data/uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{att_uuid}{ext}"
    path.write_bytes(raw)

    # Foldered by month so the bucket stays browsable once there are thousands.
    month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y/%m")
    remote = await storage.upload(f"{month}/{att_uuid}{ext}", raw, mime)
    storage_path = remote or str(path)

    # A separate small copy for previews. The chat draws these at 56 px and the console
    # at 64; shipping the original meant four seconds and 1.6 MB to paint a thumbnail.
    thumb_path = None
    thumb = resize(raw, THUMB_MAX_PX)
    if thumb:
        thumb_path = await storage.upload(f"{month}/thumb/{att_uuid}.jpg", thumb,
                                          "image/jpeg")
        if not thumb_path:
            local_thumb = folder / f"{att_uuid}_thumb.jpg"
            local_thumb.write_bytes(thumb)
            thumb_path = str(local_thumb)

    row = await db.fetch_one(
        """
        insert into attachments (session_id, storage_path, thumb_path, mime, bytes,
                                 vlm_facts)
        values (%s, %s, %s, %s, %s, %s)
        returning id::text as attachment_id
        """,
        (session_id, storage_path, thumb_path, mime, len(raw), json.dumps(facts)),
    )
    log.info("vision.saved", attachment=row["attachment_id"],
             stored="supabase" if remote else "local-disk",
             form_factor=facts["detected"].get("form_factor"),
             error_code=facts["detected"].get("error_code"))
    return {"attachment_id": row["attachment_id"], "url": f"/api/v1/attachments/"
                                                          f"{row['attachment_id']}/raw",
            "vlm_facts": facts}


async def get_facts(attachment_ids: List[str]) -> List[Dict[str, Any]]:
    if not attachment_ids:
        return []
    rows = await db.fetch(
        "select id::text as attachment_id, vlm_facts, storage_path from attachments "
        "where id::text = any(%s)",
        (attachment_ids,),
    )
    out = []
    for r in rows:
        facts = r["vlm_facts"]
        if isinstance(facts, str):
            facts = json.loads(facts)
        out.append({**(facts or {}), "attachment_id": r["attachment_id"]})
    return out


async def analyze_attachment(attachment_id: str, question: str) -> Dict[str, Any]:
    """Second pass on an image already uploaded, for a question the first pass did not
    anticipate ("is the brush bent, or is that the shadow?")."""
    row = await db.fetch_one(
        "select storage_path, mime from attachments where id::text = %s", (attachment_id,))
    if not row:
        return {"error": "attachment_not_found"}
    raw = await read_bytes(row["storage_path"])
    if raw is None:
        return {"error": "file_missing"}
    return await describe_image(raw, row["mime"] or "image/jpeg", question)


async def read_bytes(storage_path: str) -> Optional[bytes]:
    """The bytes for a stored attachment, wherever they ended up.

    Rows written before Supabase Storage existed hold a filesystem path, and rows written
    when the bucket was unreachable hold one too. Both keep working.
    """
    import pathlib
    if storage.is_remote(storage_path):
        got = await storage.download(storage_path)
        if got:
            return got[0]
        return None
    path = pathlib.Path(storage_path)
    return path.read_bytes() if path.exists() else None


async def session_facts(session_id: str, limit: int = 4) -> List[Dict[str, Any]]:
    """Every photo sent in this conversation, oldest first.

    A photo is not a property of one message. Someone sends a picture of their dock,
    answers two questions about it, then asks "so which part do I order?" — and the turn
    that has to answer that carried no attachment of its own, so the agent had forgotten
    the device it had been looking at thirty seconds earlier. Reading the conversation's
    photos rather than the turn's keeps the picture in context for as long as the
    conversation is about it, and makes a reopened session look the same as a live one.
    """
    if not session_id:
        return []
    rows = await db.fetch(
        """
        select a.id::text as attachment_id, a.vlm_facts, a.created_at
        from attachments a
        where a.session_id::text = %s
        order by a.created_at desc
        limit %s
        """,
        (session_id, limit),
    )
    out = []
    for r in reversed(rows):
        facts = r["vlm_facts"]
        if isinstance(facts, str):
            facts = json.loads(facts)
        if facts:
            out.append({**facts, "attachment_id": r["attachment_id"]})
    return out
