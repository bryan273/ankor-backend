"""Postgres access over the Supabase session pooler.

Direct SQL rather than PostgREST: the agent's queries join across four tables and use
trigram similarity, both of which are awkward through REST and trivial here. The
secret API key stays for Storage, which has no SQL equivalent.

A single async pool is opened at startup and closed at shutdown.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

log = structlog.get_logger(__name__)

_pool: Optional[AsyncConnectionPool] = None


async def init_pool(min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        url = settings.db_url
        if not url:
            raise RuntimeError("SUPABASE_REF and SUPABASE_DB_PASSWORD are required for the DB pool")
        _pool = AsyncConnectionPool(
            url, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row},
            # Supabase's session pooler drops connections that have been idle, and a
            # pool that hands one out without looking fails the *next* request after a
            # quiet spell — which is to say, the first request of a demo. `check`
            # validates a connection before lending it and quietly replaces dead ones;
            # `max_idle` retires them before the server does it for us.
            check=AsyncConnectionPool.check_connection,
            max_idle=180.0,
            reconnect_timeout=30.0,
        )
        await _pool.open(wait=True, timeout=30)
        log.info("db.pool_open", min=min_size, max=max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() during startup")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    async with pool().connection() as conn:
        yield conn


async def _with_retry(run, what: str, attempts: int = 3):
    """Run a query, retrying when the connection dies underneath it.

    Checking a connection on checkout catches one that was already dead; it cannot help
    one that dies mid-query, which is what Supabase's pooler does to a long-running job
    that spends minutes between writes. A crawl that had fetched hundreds of pages lost
    all of them to a single dropped socket.

    Only connection-level failures retry. A syntax error or a constraint violation is
    deterministic, and retrying it twice more just makes the log worse.
    """
    import asyncio

    from psycopg import OperationalError

    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return await run()
        except OperationalError as e:
            last = e
            log.warning("db.connection_lost", op=what, attempt=attempt, error=str(e)[:120])
            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise last  # type: ignore[misc]


async def fetch(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    async def run():
        async with connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()
    return await _with_retry(run, "fetch")


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    async def run():
        async with connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()
    return await _with_retry(run, "fetch_one")


async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    async def run():
        async with connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount
    return await _with_retry(run, "execute")


async def execute_many(sql: str, rows: Sequence[Sequence[Any]]) -> int:
    if not rows:
        return 0

    async def run():
        async with connection() as conn, conn.cursor() as cur:
            await cur.executemany(sql, rows)
            return cur.rowcount
    return await _with_retry(run, "execute_many")


async def ping() -> bool:
    try:
        row = await fetch_one("select 1 as ok")
        return bool(row and row.get("ok") == 1)
    except Exception as e:  # noqa: BLE001
        log.warning("db.ping_failed", error=str(e)[:160])
        return False
