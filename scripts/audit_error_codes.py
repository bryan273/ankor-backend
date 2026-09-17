"""Which rows in `error_codes` are actually error codes?

Of 139 rows, 73 are the demo seed (`provenance='demo_seed'`, each with its own meaning
and steps) and 66 were scraped. **Not one scraped row has a meaning.** They split into
two failure modes, both of them extraction bugs rather than missing data.

**1. Product model numbers read as error codes (26 rows).** Nineteen come from a single
page, `Eufy-Smart-Display-E10-T87A0-Compatibility-List` — a table of compatible eufy
MODELS. The rest trace to product manuals: `Robot-Vacuum-Auto-Empty-C10-…`,
`EufyCam-C37-…`, a USB cable FAQ. `C10` is a vacuum, not a fault.

These already answer nothing (`_HAS_SOMETHING_TO_SAY` filters them), but they are marked
so the table stops claiming 139 codes when it holds at most 80, and so a future relaxation
of that filter cannot quietly serve them.

**2. One article's steps stamped onto every code it mentions (33 rows).** Thirty of them
point at `S1-Pro-Common-Voice-Errors-and-Basic-Troubleshooting-Guide` and carry the SAME
12 steps — visibly several different remedies concatenated: dustbin (1-3), main brush
(4-8), side brush (9-11), water tank (12). The extractor took the whole article body
rather than the row for each code.

This one is serious, because of what those 33 rows are attached to: **the eufy Wearable
Breast Pump S1 Pro.** "S1 Pro" names both the Robot Vacuum Omni S1 Pro and the Wearable
Breast Pump S1 Pro — the ambiguity this project's S2 scenario is built on — and the
extractor resolved it to the pump, then attached the vacuum's troubleshooting guide.

So today a mother asking about error E72 on her breast pump is told to "turn the robot
over" and "clean the garbage on the brush and in the brush slot". `kb.py`'s own docstring
calls a robot-vacuum instruction in a breast-pump thread "the worst failure this system
can make", and it is being served by the deterministic tool the agent is told to trust
over RAG.

Re-pointing them at the vacuum would not help: every code would still carry the same 12
steps, so 33 different faults would get one identical answer. The honest state is
unattributed.

Nothing is deleted. `provenance` records what each row is, and `find_error_code` serves
only rows whose content is specific to the code.

    python scripts/audit_error_codes.py --check
    python scripts/audit_error_codes.py
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections import defaultdict

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402

log = structlog.get_logger("errcodes")

MODEL_NUMBER = "scraped_model_number"
UNATTRIBUTED = "scraped_unattributed"


async def main(check_only: bool) -> int:
    await db.init_pool()

    rows = await db.fetch(
        "select id::text as id, code, meaning, fix_steps::text as steps, source_url, "
        "       provenance "
        "from error_codes")

    empty = [r for r in rows
             if not (r["meaning"] or "").strip() and r["steps"] in (None, "[]")]

    # A step list shared by more than one DISTINCT CODE belongs to the article, not to
    # any one code.
    #
    # "shared by more than one row" is the wrong test and flags 106 of 139: the demo seed
    # stores E-05 eight times, once per product the code can appear on, with the same
    # meaning and steps each time. That repetition is correct — one fault, several
    # products. The extraction bug looks different: 33 DIFFERENT codes carrying one
    # identical step list.
    by_steps = defaultdict(list)
    for r in rows:
        if r["steps"] and r["steps"] != "[]":
            by_steps[r["steps"]].append(r)
    shared = [r for group in by_steps.values()
              if len({x["code"].upper() for x in group}) > 1
              for r in group]

    log.info("errcodes.plan", total=len(rows), model_numbers=len(empty),
             article_level=len(shared),
             distinct_step_lists=len(by_steps),
             code_specific=len(rows) - len(empty) - len(shared))

    for group in sorted(by_steps.values(), key=lambda g: -len(g))[:3]:
        if len({x["code"].upper() for x in group}) > 1:
            log.info("errcodes.shared_steps", n=len(group),
                     codes=sorted(r["code"] for r in group)[:8],
                     source=(group[0]["source_url"] or "")[-58:])

    if check_only:
        await db.close_pool()
        return 0

    await db.execute(
        "update error_codes set provenance = %s where id = any(%s::uuid[]) "
        "and provenance is distinct from %s",
        (MODEL_NUMBER, [r["id"] for r in empty], MODEL_NUMBER))
    await db.execute(
        "update error_codes set provenance = %s where id = any(%s::uuid[]) "
        "and provenance is distinct from %s",
        (UNATTRIBUTED, [r["id"] for r in shared], UNATTRIBUTED))

    after = await db.fetch(
        "select coalesce(provenance, '(null)') as provenance, count(*) as n "
        "from error_codes group by 1 order by n desc")
    for row in after:
        log.info("errcodes.provenance", value=row["provenance"], n=row["n"])

    still = await db.fetch_one("select count(*) as n from error_codes")
    log.info("errcodes.done", rows=still["n"], rows_deleted=0)
    assert still["n"] == len(rows), "a row was deleted — provenance is a label, not a purge"
    await db.close_pool()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.check)))
