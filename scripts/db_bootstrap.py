"""Find a working Postgres route into the Supabase project, then apply db/schema.sql.

Supabase exposes three routes and which one works depends on the project's age and
the network's IPv6 support, so we try them in order rather than guessing:

  1. direct        db.<ref>.supabase.co:5432          user `postgres`        (often IPv6-only)
  2. pooler/session aws-N-<region>.pooler.supabase.com:5432  user `postgres.<ref>`  (IPv4, DDL-safe)
  3. pooler/txn     aws-N-<region>.pooler.supabase.com:6543  user `postgres.<ref>`  (IPv4, no session state)

Usage:
    python scripts/db_bootstrap.py            # probe only, report which routes work
    python scripts/db_bootstrap.py --apply    # probe, then run db/schema.sql
    python scripts/db_bootstrap.py --verify   # list the tables that exist

Reads SUPABASE_REF, SUPABASE_DB_PASSWORD, SUPABASE_REGION from the environment.
"""
from __future__ import annotations

import os
import pathlib
import sys

import psycopg

REF = os.getenv("SUPABASE_REF", "")
PASSWORD = os.getenv("SUPABASE_DB_PASSWORD", "")
REGION = os.getenv("SUPABASE_REGION", "ap-northeast-1")

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "db" / "schema.sql"


def routes() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = [
        ("direct", {"host": f"db.{REF}.supabase.co", "port": 5432, "user": "postgres"}),
    ]
    for n in (0, 1):
        host = f"aws-{n}-{REGION}.pooler.supabase.com"
        out.append((f"pooler-session-aws{n}",
                    {"host": host, "port": 5432, "user": f"postgres.{REF}"}))
        out.append((f"pooler-txn-aws{n}",
                    {"host": host, "port": 6543, "user": f"postgres.{REF}"}))
    return out


def connect(params: dict, timeout: int = 15) -> psycopg.Connection:
    return psycopg.connect(
        dbname="postgres", password=PASSWORD, connect_timeout=timeout,
        sslmode="require", **params,
    )


def probe() -> tuple[str, dict] | None:
    """Return the first route that connects, printing a verdict for each."""
    winner = None
    for name, params in routes():
        try:
            with connect(params) as conn, conn.cursor() as cur:
                cur.execute("select current_user, version()")
                user, ver = cur.fetchone()
                print(f"PASS {name}: {params['host']}:{params['port']} as {user} :: {ver[:40]}")
                winner = winner or (name, params)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {params['host']}:{params['port']} :: "
                  f"{type(e).__name__} {str(e).strip()[:120]}")
    return winner


def apply_schema(params: dict) -> None:
    sql = SCHEMA.read_text(encoding="utf-8")
    with connect(params, timeout=60) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"PASS apply: {SCHEMA.name} executed")


def verify(params: dict) -> None:
    with connect(params) as conn, conn.cursor() as cur:
        cur.execute("""
            select table_name from information_schema.tables
            where table_schema = 'public' order by table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
        cur.execute("select count(*) from warranty_policies")
        policies = cur.fetchone()[0]
    print(f"PASS verify: {len(tables)} tables :: {', '.join(tables)}")
    print(f"PASS verify: warranty_policies seeded with {policies} rows")


def main() -> int:
    if not REF or not PASSWORD:
        print("FAIL config: SUPABASE_REF and SUPABASE_DB_PASSWORD must be set")
        return 2
    winner = probe()
    if not winner:
        print("FAIL: no route reachable — check the password, or the network's IPv6 support")
        return 1
    name, params = winner
    print(f"--- using route: {name}")
    if "--apply" in sys.argv:
        apply_schema(params)
    if "--apply" in sys.argv or "--verify" in sys.argv:
        verify(params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
