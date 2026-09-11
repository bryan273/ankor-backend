"""Are the vectors in Pinecone still backed by rows in Postgres?

A retrieved chunk is only useful if its text can be looked up again: `kb.search_kb`
hydrates matches from `kb_chunks` by `pinecone_id`, and a match with no row comes back
with no text. It still occupies a slot in the top-k, so an orphan does not merely fail —
it displaces a passage that would have answered the question.

Orphans appear when the corpus is re-chunked after embedding: re-crawling an article can
change its body, which changes how many chunks it splits into, and the vectors written
under the old `ord` numbering no longer match anything.

    python scripts/audit_vectors.py            # report
    python scripts/audit_vectors.py --fix      # clear the kb namespace for a clean re-embed
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from app.clients.vectors import NS_KB, get_vectors  # noqa: E402

log = structlog.get_logger("audit")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true",
                        help="delete the kb namespace so embed_corpus can rebuild it cleanly")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    await db.init_pool()
    vectors = get_vectors()
    try:
        stats = await vectors.stats()
        namespaces = stats.get("namespaces") or {}
        print("Pinecone:")
        for ns, info in namespaces.items():
            print(f"  {ns:12} {info.get('vectorCount', 0)}")

        rows = await db.fetch_one(
            """
            select count(*) as chunks,
                   count(*) filter (where pinecone_id is not null) as with_id,
                   count(distinct pinecone_id) as distinct_ids
            from kb_chunks
            """
        )
        articles = await db.fetch_one("select count(*) as n from kb_articles")
        kb_vectors = int(namespaces.get(NS_KB, {}).get("vectorCount", 0))

        print(f"\nPostgres: {articles['n']} articles, {rows['chunks']} chunks, "
              f"{rows['distinct_ids']} distinct pinecone_ids")

        orphans = kb_vectors - rows["distinct_ids"]
        print(f"\nkb vectors:            {kb_vectors}")
        print(f"backed by a chunk row: {rows['distinct_ids']}")
        print(f"orphaned:              {orphans} "
              f"({orphans / kb_vectors * 100:.0f}% of the namespace)" if kb_vectors else "")

        if orphans > kb_vectors * 0.02:
            print(
                "\nFAIL: a retrieved orphan hydrates to no text and still consumes a "
                "top-k slot, displacing a passage that would have answered the question."
            )
            if args.fix:
                print(f"\nclearing the '{NS_KB}' namespace…")
                await vectors.delete_namespace(NS_KB)
                await db.execute("update kb_chunks set pinecone_id = null, embedded_at = null")
                print("done — now run: python scripts/embed_corpus.py --only kb")
            else:
                print("re-run with --fix to clear it, then re-embed.")
            return 1

        print("\nPASS: every kb vector is backed by a chunk row.")
        return 0
    finally:
        await vectors.aclose()
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
