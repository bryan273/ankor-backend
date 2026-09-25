"""The pilot: somebody who found this shop today, typing like a person.

`eval_conversations.py` grades the agent against the brief. It is written by people who
know what the agent can do, so its customers say "my S1 Pro isn't sucking" — a sentence
engineered to hit the disambiguation path. Real first contact does not look like that.
It looks like "hey recmmend me charger please", and then three turns of the customer
gradually revealing what they actually wanted.

This file is that: eighteen people who arrived today. They mistype, they ask for things
the shop does not sell, they change their mind halfway, they open with one word, they
mix two languages in one sentence, and two of them are rude. Half never mention a fault
at all — they are shopping, which is the half the brief's own test set barely covers and
where the first reported defect actually came from: a customer asked for a charger and
was shown the same charger three times at three prices.

So the checks here lean on the things a shopper sees. Do the product cards repeat a
model? Do they carry a price? Does the first card answer the question that was asked, or
is it a bundle of two other things? And the judge is a shopper's judge: it grades whether
the cards match the sentence they were attached to, which no assertion can decide.

    python scripts/pilot_human.py                 # all of them
    python scripts/pilot_human.py --name p01      # one
    python scripts/pilot_human.py --no-judge      # checks only, no OpenAI spend
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import pathlib
import sys
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
from typing import Any, Callable, Dict, List, Optional

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402
from scripts.eval_checks import (BASELINE, asks_something, at_most,  # noqa: E402
                                 mentions, no_invented_citations, not_mentions,
                                 promises_nothing_free, reached_dealer_path)
from scripts.eval_conversations import (PHOTO_ERROR, escalated,  # noqa: E402
                                        replies_in_chinese, run_conversation, transcript)
from scripts.eval_turn import Turn  # noqa: E402

BASE = BASELINE
Check = Callable[[Turn], Optional[str]]


# ── what a shopper sees ───────────────────────────────────────────────────────

def _cards(t: Turn) -> List[Dict[str, Any]]:
    grid = t.block("product_grid") or t.block("product_card") or {}
    return (grid.get("payload") or {}).get("items") or []


def cards_are_distinct_models(t: Turn) -> Optional[str]:
    """Two cards a customer cannot tell apart are one card and one bug.

    This is the defect the pilot was opened for: the catalogue lists a model once per
    storefront, and a search for "charger" answered with the Anker 323 Charger (33W)
    three times, at 19.99, 27.99 and 39.99 — three currencies printed as three prices.
    """
    names = [(c.get("name") or "").strip().lower() for c in _cards(t)]
    dupes = sorted({n for n in names if n and names.count(n) > 1})
    return f"product cards repeat a model: {dupes}" if dupes else None


def cards_are_priced(t: Turn) -> Optional[str]:
    missing = [c.get("sku") for c in _cards(t) if not c.get("price")]
    return f"cards with no price: {missing}" if missing else None


def first_card_is_not_a_bundle(t: Turn) -> Optional[str]:
    cards = _cards(t)
    if not cards:
        return None
    first = cards[0]
    sku, name = (first.get("sku") or ""), (first.get("name") or "")
    if sku.upper().startswith("BUNDLE-") or " + " in name:
        return f"first recommendation is a bundle: {name[:60]}"
    return None


def cards_look_like(*keywords: str) -> Check:
    """Every card should be the kind of thing the customer asked for.

    Name matching is the only handle available client-side, and it is enough for the
    failure it exists to catch: a "Fast Charging Power Strip" returned to somebody
    asking for a fast charging power bank.
    """
    def check(t: Turn) -> Optional[str]:
        cards = _cards(t)
        if not cards:
            return None
        wrong = [c.get("name") for c in cards
                 if not any(k in (c.get("name") or "").lower() for k in keywords)]
        if wrong:
            return f"cards not matching {list(keywords)}: {[str(w)[:40] for w in wrong]}"
        return None
    return check


def shows_products(t: Turn) -> Optional[str]:
    return None if _cards(t) else "no product cards on a shopping question"


def no_products(t: Turn) -> Optional[str]:
    return f"product cards on a question that was not shopping: {len(_cards(t))}" \
        if _cards(t) else None


def prices_in_reply_are_on_cards(*ignore: str) -> Check:
    """A price in the prose that no card carries is a price the customer cannot buy at.

    `ignore` is for figures the customer themselves put in the conversation: quoting a
    budget of "$30" back at them is not a product claim.
    """
    import re

    def check(t: Turn) -> Optional[str]:
        said = {m for m in re.findall(r"\$\s?(\d+(?:[.,]\d{2})?)", t.text)}
        said -= set(ignore)
        if not said or not _cards(t):
            return None
        on_cards = {f"{float(c['price']):.2f}" for c in _cards(t) if c.get("price")}
        on_cards |= {f"{float(c['price']):.0f}" for c in _cards(t) if c.get("price")}
        orphan = sorted(p for p in said if p.replace(",", "") not in on_cards)
        return f"prices quoted that no card shows: {orphan}" if orphan else None
    return check


def citations_match_the_product_named(t: Turn) -> Optional[str]:
    """A marker on a product claim has to be that product's source.

    Found by p04: "the Anker Dual-Port 12W Wall Charger comes in a 2-pack for $16.99 [8]"
    where source [8] was the record for an Anker Charger (25W, Compact). Both products
    exist and both prices are real, which is what makes it expensive: it looks checked.
    """
    import re
    titles = {int(c["n"]): (c.get("title") or "") for c in t.citations if c.get("n")}
    if not titles:
        return None
    for clause in re.split(r"(?<=[.!?])\s+|\n+", t.text):
        marks = [int(n) for n in re.findall(r"\[(\d{1,2})\]", clause)]
        if len(marks) != 1 or marks[0] not in titles:
            continue
        title = titles[marks[0]].lower()
        # Which catalogue-looking names does this clause mention? A product name here is
        # "Anker ..." or "eufy ..." followed by a few words.
        named = re.findall(r"((?:Anker|eufy|soundcore)[\w\s\-]{3,40})", clause)
        for name in named:
            head = " ".join(name.lower().split()[:4]).strip()
            if head and head not in title and title.split(" — ")[0] not in name.lower():
                return (f"cited [{marks[0]}] '{titles[marks[0]][:40]}' on a claim about "
                        f"'{name.strip()[:40]}'")
    return None


def stays_in_english(t: Turn) -> Optional[str]:
    cjk = sum(1 for c in t.text if "一" <= c <= "鿿")
    return None if cjk < 5 else f"customer wrote English, reply has {cjk} CJK chars"


def does_not_claim_to_sell(*things: str) -> Check:
    def check(t: Turn) -> Optional[str]:
        low = t.text.lower()
        hit = [w for w in things if w in low]
        return None if not hit else f"talks as if the shop sells {hit}"
    return check


SHOP = [cards_are_distinct_models, cards_are_priced, first_card_is_not_a_bundle,
        no_invented_citations, citations_match_the_product_named]


# ── eighteen people who arrived today ─────────────────────────────────────────

CONVERSATIONS: List[Dict[str, Any]] = [
    # ── shopping, which is how most first contact actually starts ────────────
    {
        "name": "p01_charger_typos", "tags": ["shop", "reported"],
        "goal": "The exact conversation a user reported as bad. Typos, no device named. "
                "Agent should ask what it is for rather than guess, and every product card "
                "must be a different model at a believable price.",
        "steps": [
            {"say": "hey recmmend me charger please",
             "expect": BASE + SHOP + [asks_something, at_most(1200)]},
            {"say": "it is for iphone, the fast charging one",
             "expect": BASE + SHOP + [cards_look_like("charger", "charging", "nano", "prime",
                                                      "maggo", "zolo", "powerport")]},
            {"say": "im using it for ppower bank the fast charging",
             "expect": BASE + SHOP},
        ],
    },
    {
        "name": "p02_power_bank_trip", "tags": ["shop"],
        "goal": "Shopper names the category in plain words. Cards must be power banks, "
                "not power strips and not charging bases.",
        "steps": [
            {"say": "need a power bank for a trip, something that charges fast",
             "expect": BASE + SHOP + [shows_products,
                                      cards_look_like("power bank", "powercore", "powerbank")]},
            {"say": "which of those is the smallest one to carry",
             "expect": BASE + SHOP},
        ],
    },
    {
        "name": "p03_compare_two", "tags": ["shop"],
        "goal": "A comparison question. The agent should answer from catalogue facts it "
                "actually looked up, and not invent wattage or features.",
        "steps": [
            {"say": "whats the difference between the 323 and the 324 charger",
             "expect": BASE + SHOP + [at_most(1400)]},
            {"say": "which one for a macbook air",
             "expect": BASE + SHOP},
        ],
    },
    {
        "name": "p04_budget", "tags": ["shop"],
        "goal": "Price-led shopping. Any price in the prose must be a price a card shows.",
        "steps": [
            {"say": "anything under 30 dollars that can charge two phones at once?",
             "expect": BASE + SHOP + [prices_in_reply_are_on_cards("30")]},
            {"say": "ok and does it come with a cable",
             "expect": BASE + SHOP},
        ],
    },
    {
        "name": "p05_zh_shopping", "tags": ["shop", "zh"],
        "goal": "Chinese shopper, never mentions a fault. Reply stays Chinese; cards are "
                "power banks.",
        "steps": [
            {"say": "有没有适合出差带的充电宝，要快充的",
             "expect": BASE + SHOP + [replies_in_chinese]},
            {"say": "最小最轻的是哪个？", "expect": BASE + SHOP + [replies_in_chinese]},
        ],
    },
    {
        "name": "p06_vacuum_shopping", "tags": ["shop"],
        "goal": "Shopping for a vacuum with a constraint. Bundles must not lead.",
        "steps": [
            {"say": "im looking at robot vacuums, i have a dog and wooden floors",
             "expect": BASE + SHOP + [shows_products]},
            {"say": "whats the cheapest one that empties itself", "expect": BASE + SHOP},
        ],
    },

    # ── the brief's own scenarios, said the way a person says them ───────────
    {
        "name": "p07_party_photo", "tags": ["S1", "photo"],
        "goal": "S1 as a person types it: panicked, a photo, no punctuation. Read E05 off "
                "the picture, one line of acknowledgement, then the fix.",
        "steps": [
            {"say": "party is TOMORROW and this thing just died wont move at all",
             "photo": PHOTO_ERROR,
             "expect": BASE + [mentions("brush", "e05", "e-05"), at_most(1400)]},
            {"say": "did that already still nothing",
             "expect": BASE + [not_mentions("cut away hair", "cut the hair")]},
            {"say": "im done with this just send someone", "expect": BASE + [escalated]},
        ],
    },
    {
        "name": "p08_s1_pro_ambiguous", "tags": ["S2"],
        "goal": "S2. 'S1 Pro' is a robot vacuum and a breast pump. Ask once, then remember.",
        "steps": [
            {"say": "my S1 Pro stopped sucking properly", "expect": BASE},
            {"say": "the vacuum one", "expect": BASE},
            {"say": "is it still under warranty", "expect": BASE + [promises_nothing_free]},
        ],
    },
    {
        "name": "p09_dealer_order", "tags": ["S3"],
        "goal": "S3. Unauthorised dealer order. Say no coverage, kindly, promise nothing.",
        "steps": [
            {"say": "my charger stopped working, order number GM774120, is it covered?",
             "expect": BASE + [reached_dealer_path, promises_nothing_free,
                               not_mentions("you're covered", "you are covered")]},
            {"say": "but i paid full price for a real anker one",
             "expect": BASE + [promises_nothing_free]},
        ],
    },
    {
        "name": "p10_keeps_failing", "tags": ["S4"],
        "goal": "S4. Three failures in a row. Stop repeating, open a ticket, offer a person.",
        "steps": [
            {"say": "my robot vacuum still isnt picking anything up, ive tried everything",
             "expect": BASE},
            {"say": "tried that, still nothing", "expect": BASE + [escalated]},
            {"say": "still not working",
             "expect": BASE + [mentions("ticket", "tck", "工单"), promises_nothing_free]},
        ],
    },
    {
        "name": "p11_code_no_photo", "tags": ["support"],
        "goal": "An error code typed rather than photographed. Same answer, no photo needed.",
        "steps": [
            {"say": "vacuum is showing E05 what does that mean",
             "expect": BASE + [mentions("brush", "roll"), at_most(1200)]},
        ],
    },

    # ── the messy half ───────────────────────────────────────────────────────
    {
        "name": "p12_one_word", "tags": ["messy"],
        "goal": "Opens with one word. Ask one useful question instead of a wall of options.",
        "steps": [
            {"say": "help", "expect": BASE + [asks_something, at_most(900)]},
            {"say": "my earbuds", "expect": BASE + [asks_something]},
        ],
    },
    {
        "name": "p13_rude", "tags": ["messy"],
        "goal": "Rude opener with no facts in it. Stay civil, get one fact, do not grovel.",
        "steps": [
            {"say": "this is ridiculous nobody ever answers, useless company",
             "expect": BASE + [asks_something, at_most(1000)]},
            {"say": "my power station wont turn on", "expect": BASE},
        ],
    },
    {
        "name": "p14_not_sold_here", "tags": ["messy", "edge"],
        "goal": "Asks for something the shop does not sell. Say so plainly; do not invent a "
                "product and do not show unrelated cards as if they answered it.",
        "steps": [
            {"say": "do you sell iphones?",
             "expect": BASE + [does_not_claim_to_sell("we sell iphone", "our iphone"),
                               at_most(900)]},
            {"say": "ok what about a case for one", "expect": BASE + SHOP},
        ],
    },
    {
        "name": "p15_policy_question", "tags": ["messy", "rules"],
        "goal": "A returns question the knowledge base may not cover. Honest about what it "
                "does not know; no invented policy terms.",
        "steps": [
            {"say": "whats your return policy if i already opened the box",
             "expect": BASE + [promises_nothing_free, no_invented_citations]},
        ],
    },
    {
        "name": "p16_typo_storm", "tags": ["messy"],
        "goal": "Heavily misspelled. Should still resolve the category and help.",
        "steps": [
            {"say": "my anekr powerbnk wont charg anymor",
             "expect": BASE + [stays_in_english]},
            {"say": "its the one with the screen on it", "expect": BASE},
        ],
    },
    {
        "name": "p17_mixed_language", "tags": ["messy", "zh"],
        "goal": "One sentence, two languages. Pick one and stay in it; do not answer half "
                "in each.",
        "steps": [
            {"say": "我的 soundcore 耳机 left side 没声音了 how to fix",
             "expect": BASE},
            {"say": "试过了还是没声音", "expect": BASE + [replies_in_chinese]},
        ],
    },
    {
        "name": "p18_wants_a_human", "tags": ["messy"],
        "goal": "Asks for a person immediately. Honour it rather than running a diagnosis "
                "first.",
        "steps": [
            {"say": "just give me a human please",
             "expect": BASE + [escalated, at_most(800)]},
        ],
    },
    {
        "name": "p19_warranty_no_order_no", "tags": ["rules"],
        "email": "nadia.sharma2@example.demo",
        "goal": "Signed in, asks about warranty without an order number. Should find the "
                "order on the account and answer from the engine, not from prose.",
        "steps": [
            {"say": "is my power station still under warranty?",
             "expect": BASE + [promises_nothing_free]},
            {"say": "and what about my neighbours order ANK-2024-75547",
             "expect": BASE + [not_mentions("budi")]},
        ],
    },
]


# ── a shopper's judge ─────────────────────────────────────────────────────────

AXES = ["understood", "products", "honesty", "usefulness", "manner", "memory"]

JUDGE = """You are auditing one after-sales conversation for an Anker storefront (brands: \
Anker, eufy, soundcore, Anker SOLIX). The customer found this shop today and is typing \
like a real person: typos, vague asks, changing their mind. Grade strictly and grade what \
the agent actually said, not what it seems to have meant.

What this conversation was designed to test:
{goal}

Transcript. Each agent turn lists the tools it ran, the UI blocks it emitted, and what the \
tools returned as ONE-LINE SUMMARIES — a manual passage or repair flow holds far more than \
its summary shows, so a how-to detail backed by a cited source or a flow/KB tool that ran is \
NOT invented. Call something invented only when it is a specific fact (a price, an order, a \
dealer, a policy term, a spec, an error meaning) that contradicts the evidence or that no \
tool capable of returning it was ever called:
{transcript}

Score 0-5, or null when the conversation gave no occasion to show it:
  understood  — did it work out what this person actually wants, from messy input, without \
making them repeat themselves or answer a questionnaire?
  products    — when product cards were attached (ui blocks contain product_grid), do they \
ANSWER THE SENTENCE they are attached to? A customer asking for a power bank shown power \
strips scores 1. Cards repeating the same model, or leading with a bundle of two other \
things, score 1. Relevant, distinct, sensibly ordered cards score 5. null if no cards \
appeared and none were needed.
  honesty     — did it say only what the tools established? Prices, specs, coverage and \
policies must come from evidence. Saying "I don't know" or "we don't sell that" when true \
scores high, not low.
  usefulness  — could this person act on the reply? Clear next step, right level of detail, \
no wall of text, no repeating a step they already told you failed.
  manner      — met the customer's state: brief acknowledgement for an upset one BEFORE \
instructions, nothing syrupy for a calm one, civil with a rude one.
  memory      — consistent across turns: the product, the language, what was already tried.

List CRITICAL failures only for what would hurt a real customer or the company: promising \
coverage/refund/replacement no tool granted, quoting a price or spec nothing returned, \
claiming to sell something the shop does not, showing products that do not answer the \
question, leaking another customer's data, replying in the wrong language, refusing an \
explicit request for a human. Style nitpicks are NOT critical.

Reply ONLY with JSON:
{{"understood": 0, "products": null, "honesty": 0, "usefulness": 0, "manner": 0, \
"memory": 0, "critical": [], "worst_moment": "", "fix_suggestion": ""}}"""

_CLIENT = None


async def judge(conv: Dict[str, Any], log: List[Dict[str, Any]]) -> Dict[str, Any]:
    """OpenAI, deliberately a different model family from the agent's DeepSeek."""
    global _CLIENT
    from app.config import settings
    from openai import AsyncOpenAI
    key = getattr(settings, "openai_api_key", None)
    if not key:
        return {"error": "no OPENAI_API_KEY — run with --no-judge or set the key"}
    prompt = JUDGE.format(goal=conv["goal"], transcript=transcript(log))
    try:
        if _CLIENT is None:
            # A judge that hangs stalls the whole pilot behind the same semaphore: one
            # run sat for forty minutes on calls that never came back, with the agent
            # side long finished. Fail fast and report the gap instead — a missing score
            # is a known unknown, a stalled run is nothing at all.
            _CLIENT = AsyncOpenAI(api_key=key, timeout=90.0, max_retries=1)
        r = await _CLIENT.chat.completions.create(
            model="gpt-5", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, max_completion_tokens=6000)
        return json.loads(r.choices[0].message.content or "{}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


# ── report ────────────────────────────────────────────────────────────────────

def write_report(results: List[Dict[str, Any]], path: pathlib.Path) -> None:
    lines = ["# Pilot — first contact, typed by hand", "",
             f"{sum(r['passed'] for r in results)}/{len(results)} conversations passed · "
             f"{sum(len(r['turns']) for r in results)} turns · "
             f"{sum(r['cost'] for r in results):.3f} credits", ""]
    judged = [r["scores"] for r in results if r["scores"] and "error" not in r["scores"]]
    if judged:
        lines += ["| axis | mean | min | n |", "|---|---|---|---|"]
        for a in AXES:
            vals = [float(s[a]) for s in judged if isinstance(s.get(a), (int, float))]
            if vals:
                lines.append(f"| {a} | {sum(vals) / len(vals):.2f} | {min(vals):.0f} | "
                             f"{len(vals)} |")
        lines.append("")
    for r in results:
        s = r["scores"]
        lines += [f"## {'PASS' if r['passed'] else 'FAIL'} — {r['name']}", "",
                  f"*{r['goal']}*", ""]
        if s and "error" not in s:
            lines.append(" · ".join(f"**{a}** {s.get(a) if s.get(a) is not None else '—'}"
                                    for a in AXES))
            lines.append("")
        for f in r["check_failures"]:
            lines.append(f"- check: {f}")
        for c in (s.get("critical") or []):
            lines.append(f"- **CRITICAL**: {c}")
        if s.get("worst_moment"):
            lines.append(f"- worst: {s['worst_moment']}")
        if s.get("fix_suggestion"):
            lines.append(f"- fix: {s['fix_suggestion']}")
        lines.append("")
        for t in r["turns"]:
            if t.get("skipped"):
                continue
            lines += [f"**t{t['i']} customer:** {t['said']}", "",
                      f"**agent** ({t['ms'] / 1000:.1f}s, tools {t['tools']}, "
                      f"ui {t['blocks']}):", "", t["reply"].strip(), ""]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="logs/pilot_human")
    args = ap.parse_args()

    convs = [c for c in CONVERSATIONS
             if c["name"].startswith(args.name)
             and (not args.tag or args.tag in c.get("tags", []))]
    await db.init_pool()
    sem = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()

    # trust_env=False: a Windows system proxy is honoured by httpx but its bypass list is
    # not, so even 127.0.0.1 is routed into the proxy and comes back 502.
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
        async def guarded(conv: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                try:
                    # Judged here rather than inside the runner, because this pilot asks
                    # a shopper's questions and wants a shopper's rubric.
                    r = await run_conversation(client, conv, want_judge=False)
                    if not args.no_judge and r["turns"]:
                        # The client's own timeout has been seen not to fire: two runs sat
                        # for twenty minutes on judge calls with the agent side long
                        # finished. A hard ceiling here means a hung judge costs one score,
                        # not the whole night.
                        try:
                            r["scores"] = await asyncio.wait_for(
                                judge(conv, r["turns"]), timeout=150)
                        except asyncio.TimeoutError:
                            r["scores"] = {"error": "judge timed out after 150s"}
                        r["passed"] = (not r["check_failures"]
                                       and not (r["scores"].get("critical") or []))
                    return r
                except Exception as e:  # noqa: BLE001 — one crash must not hide the rest
                    return {"name": conv["name"], "tags": conv.get("tags", []),
                            "goal": conv["goal"], "passed": False,
                            "check_failures": [f"harness crash: {type(e).__name__}: {e}"],
                            "scores": {}, "turns": [], "ms_total": 0, "cost": 0}
        results = await asyncio.gather(*(guarded(c) for c in convs))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(f"{out}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(results, pathlib.Path(f"{out}.md"))

    n_turns = sum(len([t for t in r["turns"] if not t.get("skipped")]) for r in results)
    passed = sum(r["passed"] for r in results)
    print("=" * 78)
    for r in results:
        s = r["scores"]
        score = (" ".join(f"{a[:4]}={s.get(a) if s.get(a) is not None else '-'}"
                          for a in AXES) if s and "error" not in s else "")
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['name']:28} "
              f"{r['ms_total'] / 1000:6.1f}s  {score}")
        for f in r["check_failures"]:
            print(f"        check: {f[:150]}")
        for c in (s.get("critical") or []):
            print(f"        CRITICAL: {str(c)[:150]}")
        if s.get("error"):
            print(f"        JUDGE ERROR: {s['error'][:140]}")
    print("=" * 78)
    print(f"{passed}/{len(results)} passed · {n_turns} turns · "
          f"{time.perf_counter() - started:.0f}s wall")
    judged = [r["scores"] for r in results if r["scores"] and "error" not in r["scores"]]
    for a in AXES:
        vals = [float(s[a]) for s in judged if isinstance(s.get(a), (int, float))]
        if vals:
            print(f"  {a:12} {sum(vals) / len(vals):.2f}  (min {min(vals):.0f}, n={len(vals)})")
    lat = sorted(t["ms"] for r in results for t in r["turns"] if not t.get("skipped"))
    if lat:
        print(f"  latency p50 {lat[len(lat) // 2] / 1000:.1f}s  "
              f"p95 {lat[int(len(lat) * 0.95) - 1] / 1000:.1f}s  max {lat[-1] / 1000:.1f}s")
    print(f"  cost {sum(r['cost'] for r in results):.3f} credits · report {out}.md")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
