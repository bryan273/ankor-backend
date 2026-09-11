"""Scenario evaluation — does the agent actually do what the brief asks?

Each case drives a real turn through the real HTTP endpoint and asserts on what came
back: which tools ran, which blocks appeared, whether the guard fired, whether the
answer said what it had to say. Assertions are structural rather than string matches
wherever possible, because a good answer can be phrased a hundred ways and a test that
demands one phrasing measures the phrasing, not the behaviour.

Where judgement is genuinely needed — did it acknowledge the emotion before
troubleshooting? — a second model call scores it against a rubric. That is the only
part that is not deterministic, and it is reported separately so a rubric wobble never
looks like a regression in the agent.

    python scripts/eval_run.py                 # everything
    python scripts/eval_run.py --scenario S2   # one scenario
    python scripts/eval_run.py --repeat 3      # consistency check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from app.clients.rkapi import get_rkapi  # noqa: E402
from app.config import settings  # noqa: E402
from scripts.eval_cases import CASES  # noqa: E402
from scripts.eval_turn import Turn  # noqa: E402

log = structlog.get_logger("eval")

BASE = "http://127.0.0.1:8000"


async def run_turn(client: httpx.AsyncClient, payload: Dict[str, Any],
                   path: str = "/api/v1/chat") -> Turn:
    turn = Turn()
    started = time.perf_counter()
    async with client.stream("POST", f"{BASE}{path}", json=payload,
                             headers={"X-API-Key": settings.backend_api_key},
                             timeout=300.0) as response:
        response.raise_for_status()
        name, data_lines = "message", []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                if data_lines:
                    try:
                        turn.feed(name, json.loads("\n".join(data_lines)))
                    except json.JSONDecodeError:
                        pass
                name, data_lines = "message", []
    turn.ms = int((time.perf_counter() - started) * 1000)
    return turn


# ── the rubric judge ──────────────────────────────────────────────────────────

JUDGE_PROMPT = """You are grading one reply from a customer-support agent. Grade what \
is there, strictly, and do not reward good intentions.

Customer wrote:
{message}

Agent replied:
{reply}

Score each 0-5 and reply ONLY with JSON:
{{"empathy": 0, "clarity": 0, "proactivity": 0, "grounded": 0, "notes": ""}}

  empathy      — did it meet the customer's emotional state? An upset customer needs \
acknowledgement BEFORE instructions. A calm one needs no preamble at all: scoring a calm \
exchange low for lacking warmth is wrong.
  clarity      — could a non-technical person act on this without rereading it?
  proactivity  — does it move the case forward, and anticipate the obvious next question?
  grounded     — is it specific and concrete rather than hedged filler? Admitting what it \
does not know scores WELL here, not badly."""


async def judge(message: str, reply: str, evidence: str = "") -> Dict[str, Any]:
    if not reply.strip():
        return {"empathy": 0, "clarity": 0, "proactivity": 0, "grounded": 0,
                "notes": "empty reply"}
    try:
        data, _ = await get_rkapi().json_complete(
            [{"role": "user", "content": JUDGE_PROMPT.format(
                message=message, reply=reply,
                evidence=evidence or "(no tools were called)")}],
            max_tokens=1500, default={},
        )
        return {k: float(data.get(k, 0) or 0) for k in
                ("empathy", "clarity", "proactivity", "grounded")} | {
            "notes": str(data.get("notes", ""))[:200]}
    except Exception as e:  # noqa: BLE001 — a judge failure is not an agent failure
        return {"empathy": 0, "clarity": 0, "proactivity": 0, "grounded": 0,
                "notes": f"judge failed: {e}"}


# ── cases ─────────────────────────────────────────────────────────────────────

async def run_case(client: httpx.AsyncClient, case: Dict[str, Any],
                   want_judge: bool) -> Dict[str, Any]:
    turn = await run_turn(client, {"message": case["message"], "locale": "en"})
    failures = []
    for check in case["checks"]:
        try:
            reason = check(turn)
        except Exception as e:  # noqa: BLE001 — a broken check is a failed check
            reason = f"check raised {type(e).__name__}: {e}"
        if reason:
            failures.append(reason)

    scores = {}
    if want_judge and case.get("judge") and not turn.error:
        scores = await judge(case["message"], turn.text, turn.evidence())

    return {
        "name": case["name"], "scenario": case["scenario"], "passed": not failures,
        "failures": failures, "scores": scores, "ms": turn.ms,
        "tools": turn.tools, "blocks": turn.block_types, "emotion": turn.emotion,
        "guard_hits": turn.guard_hits, "text": turn.text,
        "cost": turn.usage.get("cost_credits", 0),
    }


async def persist(result: Dict[str, Any]) -> None:
    try:
        row = await db.fetch_one(
            """
            insert into eval_cases (name, scenario, input, expect)
            values (%s,%s,%s,%s)
            on conflict (name) do update set scenario = excluded.scenario
            returning id::text as id
            """,
            (result["name"], result["scenario"], json.dumps({}), json.dumps({})),
        )
        await db.execute(
            """
            insert into eval_runs (case_id, passed, score, transcript, ms, cost)
            values (%s,%s,%s,%s,%s,%s)
            """,
            (row["id"], result["passed"], json.dumps(result["scores"]),
             json.dumps({"text": result["text"][:4000], "tools": result["tools"],
                         "blocks": result["blocks"], "failures": result["failures"]}),
             result["ms"], json.dumps({"credits": result["cost"]})),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("eval.persist_failed", error=str(e)[:160])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="", help="S1|S2|S3|S4|edge")
    parser.add_argument("--name", default="", help="run one case by name")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    cases = CASES
    if args.scenario:
        cases = [c for c in cases if c["scenario"] == args.scenario]
    if args.name:
        cases = [c for c in cases if args.name in c["name"]]
    if not cases:
        print("no cases matched")
        return 1

    await db.init_pool()
    results: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient() as client:
        async def guarded(case: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                try:
                    return await run_case(client, case, not args.no_judge)
                except Exception as e:  # noqa: BLE001
                    return {"name": case["name"], "scenario": case["scenario"],
                            "passed": False, "failures": [f"harness error: {e}"],
                            "scores": {}, "ms": 0, "tools": [], "blocks": [],
                            "emotion": None, "guard_hits": [], "text": "", "cost": 0}

        for run in range(args.repeat):
            if args.repeat > 1:
                print(f"\n──── run {run + 1}/{args.repeat} ────")
            batch = await asyncio.gather(*(guarded(c) for c in cases))
            results.extend(batch)
            for r in sorted(batch, key=lambda x: x["scenario"]):
                mark = "PASS" if r["passed"] else "FAIL"
                print(f"{mark}  {r['scenario']:5} {r['name']:38} "
                      f"{r['ms'] / 1000:5.1f}s  tools={len(r['tools'])}"
                      f"  {'guard=' + ','.join(r['guard_hits']) if r['guard_hits'] else ''}")
                for f in r["failures"]:
                    print(f"        · {f}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n{'=' * 70}")
    print(f"{passed}/{len(results)} passed ({passed / len(results) * 100:.0f}%)")

    by_scenario: Dict[str, List[bool]] = {}
    for r in results:
        by_scenario.setdefault(r["scenario"], []).append(r["passed"])
    for scenario, outcomes in sorted(by_scenario.items()):
        print(f"  {scenario:6} {sum(outcomes)}/{len(outcomes)}")

    judged = [r for r in results if r["scores"]]
    if judged:
        print("\nrubric (0-5, judged separately from the structural checks):")
        for key in ("empathy", "clarity", "proactivity", "grounded"):
            values = [r["scores"].get(key, 0) for r in judged]
            print(f"  {key:12} {statistics.mean(values):.2f}  (min {min(values):.0f})")

    latencies = [r["ms"] for r in results if r["ms"]]
    if latencies:
        latencies.sort()
        print(f"\nlatency  p50 {latencies[len(latencies) // 2] / 1000:.1f}s  "
              f"p95 {latencies[int(len(latencies) * 0.95) - 1] / 1000:.1f}s  "
              f"max {latencies[-1] / 1000:.1f}s")
    total_cost = sum(r["cost"] for r in results)
    print(f"cost     {total_cost:.3f} credits total, "
          f"{total_cost / max(len(results), 1):.4f} per turn")

    for r in results:
        await persist(r)
    await db.close_pool()
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
