"""Package init.

Windows only, and load-bearing: psycopg's async driver cannot run on the
`ProactorEventLoop` that Python selects by default on Windows, and the failure mode is
a pool that never opens and a 30-second timeout rather than an obvious error. Selecting
the policy here means it is set before uvicorn builds its loop, whichever way the app
is started.
"""
from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
