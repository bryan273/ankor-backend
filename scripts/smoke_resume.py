"""End-to-end check of the pause/resume path — the headline S2 interaction.

The agent hits a genuinely ambiguous product name, stops mid-turn, asks which one, and
waits. The customer clicks. The turn must then *continue* — not restart, not re-ask, not
lose what it already knew. That round trip crosses a checkpoint written to Postgres, so
it is the one flow that a unit test cannot honestly cover.

    python scripts/smoke_resume.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any, Dict, List, Tuple

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.config import settings  # noqa: E402

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-API-Key": settings.backend_api_key}


async def stream(client: httpx.AsyncClient, path: str,
                 payload: Dict[str, Any]) -> Tuple[List[Tuple[str, Dict]], str]:
    events: List[Tuple[str, Dict]] = []
    text = ""
    async with client.stream("POST", f"{BASE}{path}", json=payload, headers=HEADERS,
                             timeout=300.0) as response:
        response.raise_for_status()
        name, lines = "message", []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                lines.append(line[5:].strip())
            elif line == "":
                if lines:
                    try:
                        data = json.loads("\n".join(lines))
                        events.append((name, data))
                        if name == "content_delta":
                            text += data.get("delta", "")
                        elif name == "content_reset":
                            text = ""
                    except json.JSONDecodeError:
                        pass
                name, lines = "message", []
    return events, text


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  :: ' + detail) if detail else ''}")
    return ok


async def main() -> int:
    ok = True
    async with httpx.AsyncClient() as client:
        print("turn 1 — ambiguous product")
        events, text = await stream(client, "/api/v1/chat",
                                    {"message": "my S1 Pro isn't sucking anymore",
                                     "locale": "en"})
        by_name = {n for n, _ in events}
        session_id = next(d["session_id"] for n, d in events if n == "status")
        blocks = [d for n, d in events if n == "ui_block"]
        complete = next((d for n, d in events if n == "complete"), {})

        picker = next((b for b in blocks if b["type"] == "product_picker"), None)
        ok &= check("emitted a product_picker", picker is not None,
                    f"blocks: {[b['type'] for b in blocks]}")
        if picker is None:
            return 1

        options = picker["payload"]["options"]
        categories = {o.get("category") for o in options}
        ok &= check("picker crosses product categories", len(categories) >= 2,
                    str(sorted(c for c in categories if c)))
        ok &= check("turn paused awaiting the answer",
                    bool(complete.get("awaiting_action")), str(complete.get("awaiting_action")))
        ok &= check("asked the question in the chat too", "?" in text, text[:90])
        ok &= check("no device-specific steps while ambiguous",
                    not any(w in text.lower() for w in ("brush roll", "dustbin", "duckbill")),
                    text[:90])

        # The customer picks the breast pump — the option a vacuum-biased guess gets wrong.
        pump = next((o for o in options if o.get("category") == "breast_pump"), options[-1])
        print(f"\nturn 2 — customer picks {pump['name']!r}")
        events2, text2 = await stream(client, "/api/v1/chat/action", {
            "session_id": session_id, "block_id": picker["block_id"],
            "action_id": "select_product", "value": {"sku": pump["sku"]},
        })
        names2 = [n for n, _ in events2]
        complete2 = next((d for n, d in events2 if n == "complete"), {})

        ok &= check("resumed and answered", len(text2.strip()) > 60, text2[:110])
        ok &= check("resolved to the product the customer picked",
                    complete2.get("resolved_sku") == pump["sku"],
                    f"{complete2.get('resolved_sku')} vs {pump['sku']}")
        ok &= check("did not ask again",
                    not any(d.get("type") == "product_picker"
                            for n, d in events2 if n == "ui_block"))
        ok &= check("advice now matches the resolved product",
                    any(w in text2.lower() for w in
                        ("valve", "diaphragm", "flange", "pump", "milk", "suction")),
                    text2[:110])
        ok &= check("exactly one terminal event",
                    len([n for n in names2 if n in ("complete", "error")]) == 1)

        # A second click on the same block must be refused — the turn has moved on.
        print("\nturn 3 — the same block is clicked again")
        stale = await client.post(f"{BASE}/api/v1/chat/action", headers=HEADERS, json={
            "session_id": session_id, "block_id": picker["block_id"],
            "action_id": "select_product", "value": {"sku": options[0]["sku"]},
        }, timeout=60.0)
        ok &= check("stale block is rejected with BLOCK_STALE", stale.status_code == 409,
                    f"HTTP {stale.status_code}")

    print(f"\n{'ALL PASS' if ok else 'FAILURES ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
