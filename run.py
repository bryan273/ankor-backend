"""Dev entry point. Use this instead of `uvicorn app.main:app` on Windows.

Why it exists: psycopg's async driver refuses to run on Windows' default
`ProactorEventLoop`, and `uvicorn.run(loop="asyncio")` explicitly resets the event-loop
policy to the platform default before it starts — so setting a policy at import time is
silently undone. The fix is to keep the loop ours: build the server with `loop="none"`
and drive it under an event loop we chose.

The symptom this prevents is nasty precisely because it is not an exception: the pool
just never opens, and thirty seconds later every database call reports a closed pool.

It doubles as the container entry point. `PORT` is injected by every platform worth
deploying this to (Cloud Run, Fly, Render, Railway) and a container that binds 127.0.0.1
is unreachable from outside itself — so both are read from the environment, with the dev
values as defaults so running it locally is unchanged.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Windows gives a redirected stdout the cp1252 codec, and structlog writes straight to it.
# A log line carrying Chinese — the brief's own example is 明天要开派对 — raised
# UnicodeEncodeError INSIDE the log call, i.e. inside the request that was being logged.
# Logging must never be able to fail a customer's turn.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
import sys

import uvicorn

from app.config import settings


def main() -> None:
    config = uvicorn.Config(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level=settings.log_level.lower(),
        loop="none",  # do not let uvicorn install a policy over ours
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
