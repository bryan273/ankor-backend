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
from app.clients.llm import get_llm

log = structlog.get_logger(__name__)

MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/jpg"}


def to_data_uri(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def describe_image(raw: bytes, mime: str,
                         question: Optional[str] = None) -> Dict[str, Any]:
    """One vision call. Returns the structured facts, or an empty-but-valid shape so a
    vision failure degrades the turn instead of ending it."""
    ask = question or "Describe this image for a support agent."
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
    """Store the bytes and the facts. The image itself goes to Supabase Storage when it
    is configured; the demo path keeps it on disk, because a working demo beats a
    perfect one that needs a bucket policy first."""
    facts = await describe_image(raw, mime)
    import pathlib
    import uuid
    att_uuid = uuid.uuid4().hex
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
    folder = pathlib.Path("data/uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{att_uuid}{ext}"
    path.write_bytes(raw)

    row = await db.fetch_one(
        """
        insert into attachments (session_id, storage_path, mime, bytes, vlm_facts)
        values (%s, %s, %s, %s, %s)
        returning id::text as attachment_id
        """,
        (session_id, str(path), mime, len(raw), json.dumps(facts)),
    )
    log.info("vision.saved", attachment=row["attachment_id"],
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
    import pathlib
    path = pathlib.Path(row["storage_path"])
    if not path.exists():
        return {"error": "file_missing"}
    return await describe_image(path.read_bytes(), row["mime"] or "image/jpeg", question)
