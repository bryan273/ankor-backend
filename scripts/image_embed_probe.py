"""Does image-similarity search identify which MODEL a customer photographed?

This exists because the answer sounds obviously yes and is measurably no, and that is
worth a repeatable experiment rather than an argument. `gemini-embedding-2` is natively
multimodal and available on our key, so the only real question is whether the products
are far enough apart in image space to tell apart.

The measurement, not an opinion:

- **Self-retrieval** — embed a product's own catalog image and search with it. This is
  the most favourable case possible, an identical file, so it is an upper bound rather
  than a result.
- **Margin** — how far the correct product beats the nearest WRONG product of the same
  kind. This is the number that decides everything. A customer's photo is taken in a
  dim room at an angle against a carpet, which moves the query far more than the gap
  between two similar docks does. If the margin is thin on identical files, a real
  photo cannot land inside it.
- **Category separation** — same-category versus cross-category similarity. Worth
  measuring separately because it is the part that does work, and it is already what
  the vision pass gives us for free.

    python scripts/image_embed_probe.py
"""
from __future__ import annotations

import asyncio
import math
import pathlib
import sys
from typing import Any, Dict, List, Optional

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402  — event-loop policy on Windows
from app.clients import db  # noqa: E402
from app.config import settings  # noqa: E402

MODEL = "gemini-embedding-2"
ENDPOINT = (f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
            f":embedContent")

SAMPLE = {
    "robot_vacuum": 10,
    "breast_pump": 2,
    "charger": 2,
    "power_station": 2,
}


def cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def embed_image(client: httpx.AsyncClient, raw: bytes,
                      mime: str = "image/jpeg") -> Optional[List[float]]:
    import base64
    payload = {
        "model": f"models/{MODEL}",
        "content": {"parts": [{"inline_data": {
            "mime_type": mime, "data": base64.b64encode(raw).decode()}}]},
    }
    r = await client.post(f"{ENDPOINT}?key={settings.gemini_embed_api_key}", json=payload)
    if r.status_code != 200:
        print(f"    embed failed {r.status_code}: {r.text[:120]}")
        return None
    return r.json().get("embedding", {}).get("values")


async def main() -> int:
    await db.init_pool()
    try:
        rows: List[Dict[str, Any]] = []
        for category, n in SAMPLE.items():
            rows += await db.fetch(
                """
                select sku, name, category, hero_image from products
                where category = %s and hero_image is not null
                  and name not like '%%+%%' and sku not ilike 'BUNDLE-%%'
                order by price desc nulls last limit %s
                """,
                (category, n),
            )
        print(f"sampling {len(rows)} products\n")

        vectors: List[Dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            for row in rows:
                try:
                    img = await client.get(row["hero_image"])
                    if img.status_code != 200 or len(img.content) > 6_000_000:
                        continue
                    vec = await embed_image(client, img.content)
                    if vec:
                        vectors.append({**row, "vec": vec})
                        print(f"  embedded {row['category']:<15} {row['name'][:48]}")
                except Exception as e:  # noqa: BLE001
                    print(f"  skipped {row['sku']}: {type(e).__name__}")

        if len(vectors) < 6:
            print("\nnot enough vectors to measure")
            return 1

        vacuums = [v for v in vectors if v["category"] == "robot_vacuum"]
        others = [v for v in vectors if v["category"] != "robot_vacuum"]

        print(f"\n{'=' * 66}")
        print("SELF-RETRIEVAL — query with a product's OWN catalog image")
        print("(an identical file: the best case that can ever happen)\n")
        top1 = 0
        margins: List[float] = []
        for query in vacuums:
            ranked = sorted(
                ((cosine(query["vec"], c["vec"]), c) for c in vectors if c is not query),
                key=lambda pair: pair[0], reverse=True)
            best_score, best = ranked[0]
            # The nearest wrong answer of the same kind — the thing it must beat.
            rival = next((s for s, c in ranked if c["category"] == "robot_vacuum"), 0.0)
            self_score = 1.0  # identical file
            margins.append(self_score - rival)
            hit = best["sku"] == query["sku"]
            top1 += hit
            print(f"  {query['name'][:40]:<42} nearest other vacuum {rival:.3f}"
                  f"  gap {1.0 - rival:.3f}")

        print(f"\n{'=' * 66}")
        print("HOW FAR APART ARE THESE PRODUCTS IN IMAGE SPACE?\n")

        def pairs(group_a, group_b, same: bool) -> List[float]:
            out = []
            for i, a in enumerate(group_a):
                for j, b in enumerate(group_b):
                    if same and j <= i:
                        continue
                    out.append(cosine(a["vec"], b["vec"]))
            return out

        within = pairs(vacuums, vacuums, True)
        across = pairs(vacuums, others, False)
        avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
        print(f"  two DIFFERENT robot vacuums   mean {avg(within):.3f}  "
              f"max {max(within):.3f}  min {min(within):.3f}")
        print(f"  vacuum vs another category    mean {avg(across):.3f}  "
              f"max {max(across):.3f}  min {min(across):.3f}")
        print(f"  separation between the two    {avg(within) - avg(across):.3f}")
        print(f"\n  median gap from an identical file to the nearest wrong vacuum: "
              f"{sorted(margins)[len(margins) // 2]:.3f}")

        print(f"\n{'=' * 66}")
        print("READ THIS AS:")
        print("  Category separation is what the gap on the second line buys you —")
        print("  and the vision pass already reports form_factor for free.")
        print("  Model identification depends on the LAST number: that is all the room")
        print("  a real customer photo has to land in, before lighting, angle, carpet")
        print("  and a half-open dock lid move it further than that.")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
