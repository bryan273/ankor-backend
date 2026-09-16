"""Concurrency test — what happens when 50 people ask at once.

The interesting limit is not CPU, it is the model key pool: keys x per-key concurrency
bounds how many model calls can be in flight, and a turn makes several. So the honest
question this script answers is: with N users arriving at once, does every one of them
get a complete, correct stream, and how long does the slowest wait?

A 429 is not counted as a failure. The server sheds load deliberately rather than
letting a queue turn into a six-minute silence, and the client backs off and returns —
which is what the contract asks real clients to do. Only a client that never gets served
counts against the run.

What counts as a failure here is deliberately strict:

  - a stream that ends without exactly one terminal event
  - a stage that opens and never closes (the UI would spin forever)
  - an empty answer
  - an HTTP error

A slow turn is not a failure; it is a number. Latency is reported as a distribution
because the mean hides exactly the users who had a bad time.

    python scripts/load_test.py --users 50
    python scripts/load_test.py --users 50 --arrival stagger
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import statistics
import sys
import time
from typing import Any, Dict, List

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.config import settings  # noqa: E402

BASE = "http://127.0.0.1:8000"

# A realistic mix: cheap questions, tool-heavy ones, an ambiguous one, a safety case.
MESSAGES = [
    "my robot vacuum won't pick up dirt anymore",
    "how do I clean the brush on my eufy vacuum?",
    "my S1 Pro isn't sucking anymore",
    "order SE-482911 isn't recognised, can I still claim warranty?",
    "the left earbud stopped working",
    "my camera keeps going offline at night",
    "what's the warranty on a power station?",
    "the pump suction is weak on one side",
    "my charger stopped working with my laptop",
    "it's broken and I don't know why",
    "error E-05 on the display, what now?",
    "the battery is swelling and smells like burning",
]


async def one_user(client: httpx.AsyncClient, index: int) -> Dict[str, Any]:
    message = MESSAGES[index % len(MESSAGES)]
    started = time.perf_counter()
    first_byte = None
    first_content = None
    events: List[str] = []
    stages_open: List[str] = []
    text = ""
    tools = 0
    cost = 0.0
    failure = None

    try:
        async with client.stream(
            "POST", f"{BASE}/api/v1/chat",
            json={"message": message, "locale": "en"},
            headers={"X-API-Key": settings.backend_api_key},
            timeout=httpx.Timeout(400.0, connect=30.0),
        ) as response:
            if response.status_code == 429:
                # Shed load, as designed. Back off and come back — that is what the
                # contract asks clients to do, and a retry that succeeds is the proof
                # the shed path is useful rather than merely honest.
                body = (await response.aread()).decode()
                wait = 5.0
                try:
                    wait = float(json.loads(body)["error"]["detail"]["retry_after_seconds"])
                except Exception:
                    pass
                return {"index": index, "ok": None, "retry_after": wait,
                        "failure": None, "ms": int((time.perf_counter() - started) * 1000),
                        "ttfb": 0, "ttfc": 0, "message": message, "tools": 0, "cost": 0}
            if response.status_code != 200:
                body = (await response.aread()).decode()[:200]
                return {"index": index, "ok": False, "failure": f"HTTP {response.status_code}: {body}",
                        "ms": 0, "ttfb": 0, "ttfc": 0, "message": message, "tools": 0, "cost": 0}

            name, data_lines = "message", []
            async for line in response.aiter_lines():
                if first_byte is None:
                    first_byte = time.perf_counter() - started
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    if not data_lines:
                        continue
                    try:
                        data = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        data_lines = []
                        continue
                    events.append(name)
                    if name == "stage_start":
                        stages_open.append(data["stage_id"])
                    elif name in ("stage_complete", "stage_error"):
                        if data["stage_id"] in stages_open:
                            stages_open.remove(data["stage_id"])
                    elif name == "content_delta":
                        if first_content is None:
                            first_content = time.perf_counter() - started
                        text += data.get("delta", "")
                    elif name == "tool_call":
                        tools += 1
                    elif name == "usage":
                        cost = data.get("cost_credits", 0)
                    data_lines = []
    except Exception as e:  # noqa: BLE001
        failure = f"{type(e).__name__}: {str(e)[:160]}"

    elapsed = time.perf_counter() - started
    terminals = [e for e in events if e in ("complete", "error")]

    if failure is None:
        if len(terminals) != 1:
            failure = f"expected 1 terminal event, saw {terminals}"
        elif "error" in terminals:
            failure = "stream ended with error"
        elif stages_open:
            failure = f"stages never closed: {stages_open}"
        elif len(text.strip()) < 20:
            failure = f"answer too short ({len(text)} chars)"

    return {
        "index": index, "ok": failure is None, "failure": failure,
        "ms": int(elapsed * 1000), "ttfb": int((first_byte or 0) * 1000),
        "ttfc": int((first_content or 0) * 1000), "message": message,
        "tools": tools, "cost": cost, "chars": len(text),
    }


def percentile(values: List[int], p: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(int(len(values) * p), len(values) - 1)]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--retries", type=int, default=3,
                        help="how many times a shed (429) client comes back")
    parser.add_argument("--arrival", choices=["burst", "stagger"], default="stagger",
                        help="burst = all at once; stagger = spread over a few seconds")
    parser.add_argument("--spread", type=float, default=10.0,
                        help="seconds to spread arrivals over when staggering")
    args = parser.parse_args()

    print(f"driving {args.users} concurrent users ({args.arrival}) against {BASE}")
    health = httpx.get(f"{BASE}/livez", timeout=10)
    if health.status_code != 200:
        print("backend is not up")
        return 1

    limits = httpx.Limits(max_connections=args.users + 10,
                          max_keepalive_connections=args.users + 10)
    started = time.perf_counter()
    shed = [0]

    # trust_env=False because this talks to 127.0.0.1 and nothing else.
    # A Windows system proxy (VPN clients set one at 127.0.0.1:7897) is handed to httpx
    # by urllib's getproxies() WITHOUT the registry's ProxyOverride bypass list, so httpx
    # tunnels loopback traffic through it and the proxy refuses — every request comes
    # back as a bare 502 with an empty body, before it ever reaches the app. curl reads
    # only the env vars, so it keeps working and the failure looks like a server bug.
    async with httpx.AsyncClient(limits=limits, trust_env=False) as client:
        async def launch(i: int) -> Dict[str, Any]:
            if args.arrival == "stagger":
                await asyncio.sleep(random.uniform(0, args.spread))
            for attempt in range(args.retries + 1):
                result = await one_user(client, i)
                if result["ok"] is not None:
                    result["attempts"] = attempt + 1
                    return result
                shed[0] += 1
                # Jitter the backoff so the whole shed cohort does not return together
                # and shed itself a second time.
                await asyncio.sleep(result["retry_after"] * (1 + random.uniform(0, 0.6)))
            result["ok"] = False
            result["failure"] = f"still shedding after {args.retries} retries"
            return result

        results = await asyncio.gather(*(launch(i) for i in range(args.users)))

    wall = time.perf_counter() - started
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    latencies = [r["ms"] for r in ok]
    ttfb = [r["ttfb"] for r in ok if r["ttfb"]]
    ttfc = [r["ttfc"] for r in ok if r["ttfc"]]

    print(f"\n{'=' * 68}")
    print(f"{len(ok)}/{len(results)} completed cleanly "
          f"({len(ok) / len(results) * 100:.0f}%) in {wall:.1f}s wall clock")

    if latencies:
        print(f"\nfull turn      p50 {percentile(latencies, 0.5) / 1000:5.1f}s   "
              f"p90 {percentile(latencies, 0.9) / 1000:5.1f}s   "
              f"p99 {percentile(latencies, 0.99) / 1000:5.1f}s   "
              f"max {max(latencies) / 1000:5.1f}s")
    if ttfb:
        print(f"first event    p50 {percentile(ttfb, 0.5) / 1000:5.1f}s   "
              f"p90 {percentile(ttfb, 0.9) / 1000:5.1f}s   "
              f"max {max(ttfb) / 1000:5.1f}s   (how fast the UI stops looking dead)")
    if ttfc:
        print(f"first words    p50 {percentile(ttfc, 0.5) / 1000:5.1f}s   "
              f"p90 {percentile(ttfc, 0.9) / 1000:5.1f}s   "
              f"max {max(ttfc) / 1000:5.1f}s")

    total_cost = sum(r["cost"] for r in results)
    print(f"\nthroughput     {len(ok) / wall:.2f} turns/s sustained")
    print(f"cost           {total_cost:.3f} credits "
          f"({total_cost / max(len(results), 1):.4f} per turn)")
    if ok:
        print(f"tool calls     {statistics.mean([r['tools'] for r in ok]):.1f} average per turn")

    if bad:
        print(f"\n{len(bad)} failures:")
        grouped: Dict[str, int] = {}
        for r in bad:
            key = (r["failure"] or "unknown").split(":")[0][:70]
            grouped[key] = grouped.get(key, 0) + 1
        for reason, count in sorted(grouped.items(), key=lambda kv: -kv[1]):
            print(f"  {count:3}x  {reason}")
        print(f"\n  example: user {bad[0]['index']} · {bad[0]['message'][:50]!r}")
        print(f"           {bad[0]['failure']}")

    print(f"\nnote: {len(settings.rkapi_keys)} model keys = "
          f"{len(settings.rkapi_keys)} parallel lanes upstream. Queueing above that is "
          f"expected and is what the p90 measures.")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
