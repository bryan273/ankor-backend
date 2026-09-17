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

        # The other direction, which is the one that actually bit. The check above asks
        # "does every vector hydrate to text?" and says PASS when it does — it never asks
        # whether every ARTICLE reached the index. A bulk import wrote 11,910 rows into
        # kb_articles without anyone running embed_corpus, and this script reported PASS
        # while 81% of the knowledge base was unreachable by search. Both directions have
        # to be checked, because each one is silent about the other's failure.
        # `embed_status` is set by scripts/triage_kb_corpus.py: `chrome` rows are
        # navigation rails and link lists, `non_english` rows are localised storefronts.
        # Those are excluded ON PURPOSE, so counting them as "never embedded" would make
        # this gate fail for doing its job. But they are printed rather than filtered out
        # silently — an audit that quietly drops rows from its own denominator is exactly
        # how 81% of the corpus went missing while this script said PASS.
        triage = await db.fetch(
            "select coalesce(embed_status, 'keep') as verdict, count(*) as n "
            "from kb_articles group by 1 order by n desc")
        print("\ntriage verdicts:")
        for row in triage:
            print(f"  {row['verdict']:<14} {row['n']}")

        eligible = await db.fetch_one(
            """
            select count(*) as n
            from kb_articles ka
            where length(coalesce(ka.clean_body, ka.body)) > 100
              and coalesce(ka.embed_status, 'keep') = 'keep'
            """
        )
        unindexed = await db.fetch_one(
            """
            select count(*) as n
            from kb_articles ka
            where length(coalesce(ka.clean_body, ka.body)) > 100
              and coalesce(ka.embed_status, 'keep') = 'keep'
              and not exists (select 1 from kb_chunks kc where kc.article_id = ka.id)
            """
        )
        n_eligible = int(eligible["n"])
        missing = int(unindexed["n"])
        indexed = n_eligible - missing
        print(f"\neligible articles:     {n_eligible}")
        print(f"reached the index:     {indexed}")
        print(f"never embedded:        {missing} "
              f"({missing / n_eligible * 100:.0f}% of the eligible corpus)"
              if n_eligible else "")

        if missing > n_eligible * 0.02:
            print(
                "\nFAIL: these articles exist in Postgres and cannot be retrieved. "
                "Search silently answers as if they were never collected."
            )
            print("run: python scripts/embed_corpus.py --only kb")
            return 1

        print("\nPASS: every kb vector is backed by a chunk row, "
              "and every article reached the index.")
        return 0
    finally:
        await vectors.aclose()
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
