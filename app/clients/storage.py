"""Supabase Storage for customer photos.

Photos used to live on the container's local disk. That works until the first restart,
and then every photo a customer ever sent 404s while its row still sits in the database
promising the image is there. On a host that scales to zero — which is the whole point of
deploying this cheaply — that restart happens several times a day.

So the bytes go to Supabase Storage, in the same project as the rows that reference them,
and the disk becomes a fallback rather than the plan.

The bucket is **private**. Support photos routinely show a customer's living room, their
address on a delivery note, or the serial number of a device they own; a public bucket
would make every one of those URLs guessable-but-unlisted, which is not a permission
model. Images are served back through the API, which already knows who is asking.
"""
from __future__ import annotations

from typing import Optional, Tuple

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

BUCKET = "attachments"
# Stored in `attachments.storage_path` so a row says where its bytes actually are. Rows
# written before this existed hold a plain filesystem path and keep working.
SCHEME = "supabase://"


def is_remote(storage_path: str) -> bool:
    return (storage_path or "").startswith(SCHEME)


def object_key(storage_path: str) -> str:
    """`supabase://attachments/2026/09/abc.jpg` → `attachments/2026/09/abc.jpg`."""
    return storage_path[len(SCHEME):] if is_remote(storage_path) else storage_path


def configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_secret_key)


def _headers() -> dict:
    key = settings.supabase_secret_key
    return {"Authorization": f"Bearer {key}", "apikey": key}


async def upload(key: str, raw: bytes, mime: str) -> Optional[str]:
    """Store the bytes and return the `supabase://` path, or None to fall back to disk.

    Never raises. A photo that fails to reach the bucket must still reach the vision
    model and the conversation — losing the archive copy is a smaller problem than
    failing the customer's upload.
    """
    if not configured():
        return None
    url = f"{settings.supabase_url}/storage/v1/object/{BUCKET}/{key}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, content=raw,
                                  headers={**_headers(), "Content-Type": mime,
                                           "x-upsert": "true"})
        if r.status_code in (200, 201):
            return f"{SCHEME}{BUCKET}/{key}"
        log.warning("storage.upload_failed", status=r.status_code, detail=r.text[:160])
    except Exception as e:  # noqa: BLE001
        log.warning("storage.upload_error", error=str(e)[:160])
    return None


async def download(storage_path: str) -> Optional[Tuple[bytes, str]]:
    """Fetch bytes for a `supabase://` path. Returns (bytes, content-type) or None."""
    if not is_remote(storage_path) or not configured():
        return None
    url = f"{settings.supabase_url}/storage/v1/object/{object_key(storage_path)}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url, headers=_headers())
        if r.status_code == 200:
            return r.content, r.headers.get("Content-Type", "image/jpeg")
        log.warning("storage.download_failed", status=r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("storage.download_error", error=str(e)[:160])
    return None


async def signed_url(storage_path: str, expires_in: int = 3600) -> Optional[str]:
    """A time-limited direct URL, so the browser fetches Supabase instead of us.

    Proxying the bytes through this service cost 4-6 seconds per thumbnail: the backend
    downloaded 1.6 MB from the bucket and re-sent it on every single render, and a
    conversation with three photos in it spent twenty seconds moving images that were
    already sitting on a CDN. A signed URL keeps the bucket private and makes the
    browser's request go straight to the place the bytes live.
    """
    if not is_remote(storage_path) or not configured():
        return None
    key = object_key(storage_path)
    bucket, _, path = key.partition("/")
    url = f"{settings.supabase_url}/storage/v1/object/sign/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers={**_headers(),
                                                "Content-Type": "application/json"},
                                  json={"expiresIn": expires_in})
        if r.status_code == 200:
            signed = r.json().get("signedURL") or ""
            if signed:
                return f"{settings.supabase_url}/storage/v1{signed}"
        log.warning("storage.sign_failed", status=r.status_code, detail=r.text[:140])
    except Exception as e:  # noqa: BLE001
        log.warning("storage.sign_error", error=str(e)[:160])
    return None


async def ensure_bucket() -> bool:
    """Create the private bucket if it is not there. Safe to call repeatedly."""
    if not configured():
        return False
    base = f"{settings.supabase_url}/storage/v1/bucket"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            existing = await client.get(base, headers=_headers())
            if existing.status_code == 200 and any(
                    b.get("name") == BUCKET for b in existing.json()):
                return True
            r = await client.post(base, headers={**_headers(),
                                                 "Content-Type": "application/json"},
                                  json={"id": BUCKET, "name": BUCKET, "public": False,
                                        "file_size_limit": 12 * 1024 * 1024,
                                        "allowed_mime_types": ["image/jpeg", "image/png",
                                                               "image/webp"]})
            return r.status_code in (200, 201)
    except Exception as e:  # noqa: BLE001
        log.warning("storage.bucket_error", error=str(e)[:160])
        return False
