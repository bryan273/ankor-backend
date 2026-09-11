"""Seed the demo commerce data.

Everything written here carries `source='demo'` and the UI labels it as such. No real
customer data is involved; the names are invented and the emails all end in a domain
nobody owns.

Three fixtures matter more than volume, because they are the scenarios:

  S3  `SE-482911` exists ONLY in `dealer_orders`. `lookup_order` must return not-found
      for it while `lookup_dealer_order` finds it and names the dealer. That asymmetry
      is the whole scenario, so it is a fixture rather than an accident of random data.
  S1  an in-warranty official-store order for the Omni S1 Pro, so the angry-customer
      turn has a real purchase behind it.
  S2  the same customer owns exactly one of the two "S1 Pro" products, which lets
      purchase history resolve the ambiguity silently in one variant of the demo.

    python scripts/seed_demo.py
    python scripts/seed_demo.py --reset   # wipe demo rows first
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import structlog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: F401,E402
from app.clients import db  # noqa: E402

log = structlog.get_logger("seed")
random.seed(20260911)  # reproducible demo data

DEALERS = [
    ("PT Sinar Elektronik", "ID", r"^SE-\d{6}$", "service@sinarelektronik.demo",
     "Walk in to any Sinar service point with the invoice, or email their service desk.", True),
    ("Tokopedia Official Reseller", "ID", r"^TP-\d{4}-\d{4}$", "care@tp-reseller.demo",
     "Raise the claim in the Tokopedia order page; they forward it to Anker.", True),
    ("Lazada MY Partner", "MY", r"^LZ\d{8}$", "support@lazada-partner.demo",
     "Claims go through Lazada's returns flow within 14 days, then direct to us.", True),
    ("MediaMarkt DE", "DE", r"^MM-\d{3}-\d{5}$", "service@mediamarkt.demo",
     "Take it to any MediaMarkt store; they handle the RMA with us.", True),
    ("Currys UK", "UK", r"^CUR\d{7}$", "repairs@currys.demo",
     "Book a repair slot online with the receipt number.", True),
    ("Best Buy US", "US", r"^BBY-\d{6}-\d{2}$", "geeksquad@bestbuy.demo",
     "Geek Squad handles the first-line diagnosis, then routes to us.", True),
    ("Shopee Preferred Seller", "SG", r"^SP\d{10}$", "help@shopee-pref.demo",
     "Claim through the Shopee order, keep the invoice photo.", True),
    ("Amazon Japan Marketplace", "JP", r"^AJP-\d{3}-\d{7}$", "support@ajp.demo",
     "Amazon handles the return window; after that it comes to us directly.", True),
    ("Digital Mall Kuningan", "ID", r"^DMK/\d{4}/\d{4}$", "cs@dmk.demo",
     "Bring the printed invoice to the counter on level 3.", True),
    ("TechZone Distribution", "AU", r"^TZ-AU-\d{5}$", "warranty@techzone.demo",
     "Email the invoice; they ship a replacement from local stock.", True),
    ("Grey Market Imports", "XX", r"^GM\d{6}$", "",
     "Not an authorised dealer — no manufacturer warranty applies.", False),
    ("Anker Store Jakarta", "ID", r"^AJK-\d{5}$", "jakarta@anker-store.demo",
     "Bring it to the store; they can swap in-warranty units on the spot.", True),
]

FIRST = ["Sarah", "Budi", "Mei", "Tom", "Priya", "Ahmad", "Lena", "Kenji", "Nadia",
         "Marcus", "Yuki", "Rosa", "Daniel", "Aisyah", "Chen", "Emma", "Ravi", "Sofia",
         "Liam", "Nur"]
LAST = ["Chen", "Santoso", "Wijaya", "Miller", "Sharma", "Rahman", "Novak", "Tanaka",
        "Putri", "Silva", "Kim", "Garcia", "Brown", "Binti", "Wang", "Nguyen"]

CHANNELS = ["official_store", "official_store", "official_store", "amazon", "marketplace"]

# Symptom → steps. Written the way a support engineer would order them: the cheap check
# that fixes half the cases first, the annoying one last.
FLOWS: List[Dict[str, Any]] = [
    {"category": "robot_vacuum", "symptom": "reduced suction / not picking up dirt",
     "est": 8, "steps": [
         {"instruction": "Pull the dustbin out and empty it, then hold the filter up to a "
                         "light.", "why": "A filter clogged with fine dust is the cause "
                         "about half the time.", "expected": "You can see light through "
                         "the filter mesh"},
         {"instruction": "Rinse the filter under cold water, shake it out, and leave it to "
                         "dry for 24 hours before refitting.",
          "why": "A damp filter chokes airflow and can grow mould.",
          "expected": "Filter is clean and completely dry"},
         {"instruction": "Turn the robot over and cut away any hair wound around the brush "
                         "roll with scissors.",
          "why": "Wrapped hair stops the brush turning, so nothing reaches the intake.",
          "expected": "Brush spins freely by hand"},
         {"instruction": "Check the suction inlet just behind the brush for a blockage — a "
                         "sock or a large crumb will sit there invisibly.",
          "why": "A blocked inlet gives full motor noise with no pickup.",
          "expected": "Inlet is clear end to end"},
         {"instruction": "Run a short clean on a hard floor and listen: the motor should "
                         "change pitch when it meets dirt.",
          "why": "Confirms the airflow path is restored.",
          "expected": "Debris is picked up on the first pass"},
     ]},
    {"category": "robot_vacuum", "symptom": "error code on the display / won't start",
     "est": 6, "steps": [
         {"instruction": "Note the exact code shown, then lift the robot off the dock and "
                         "put it back on, seating it until the charging contacts click.",
          "why": "Most dock errors are a contact alignment problem, not a fault.",
          "expected": "Charging indicator comes on"},
         {"instruction": "Wipe the charging contacts on both the robot and the dock with a "
                         "dry cloth.", "why": "Dust on the contacts reads as a charging "
                         "fault.", "expected": "Contacts look bright, not grey"},
         {"instruction": "Hold the power button for 10 seconds to force a restart.",
          "why": "Clears a stuck firmware state without losing the map.",
          "expected": "The robot reboots and the code clears"},
     ]},
    {"category": "breast_pump", "symptom": "reduced suction / not expressing",
     "est": 6, "steps": [
         {"instruction": "Take the flange apart and check the white duckbill valve for a "
                         "split or a milk residue film.",
          "why": "A tired valve is by far the most common cause of lost suction.",
          "expected": "Valve is intact and flexible"},
         {"instruction": "Check the silicone diaphragm sits flat in its seat with no "
                         "wrinkle.", "why": "A lifted edge lets air in and the vacuum never "
                         "builds.", "expected": "Diaphragm is seated flat all the way round"},
         {"instruction": "Reassemble while everything is completely dry, then cup your palm "
                         "over the flange and start a cycle.",
          "why": "Water between the seals breaks the vacuum; your palm tests suction "
                 "without a session.", "expected": "You feel steady pull against your palm"},
         {"instruction": "If suction is still weak, fit a fresh valve and diaphragm — they "
                         "are consumables and are replaced every 4-8 weeks.",
          "why": "Silicone fatigues with heat and washing.",
          "expected": "Full suction returns"},
     ]},
    {"category": "breast_pump", "symptom": "won't charge / battery drains fast",
     "est": 5, "steps": [
         {"instruction": "Wipe the charging pins on the pump and inside the case with a dry "
                         "cotton bud.", "why": "Milk residue on the pins is an insulator.",
          "expected": "Pins are clean and dry"},
         {"instruction": "Charge from a wall adapter rather than a laptop port for 30 "
                         "minutes.", "why": "Laptop ports often cannot supply enough "
                         "current.", "expected": "Charge indicator advances"},
     ]},
    {"category": "charger", "symptom": "not charging / device not recognised", "est": 4,
     "steps": [
         {"instruction": "Try a different cable you know works, in the same port.",
          "why": "Cables fail far more often than chargers do.",
          "expected": "Device starts charging"},
         {"instruction": "Try the same cable in a different port on the charger.",
          "why": "Separates a dead port from a dead charger.",
          "expected": "You can tell which part is at fault"},
     ]},
    {"category": "audio", "symptom": "one earbud not working / won't pair", "est": 5,
     "steps": [
         {"instruction": "Put both buds in the case, close it for 10 seconds, then open it.",
          "why": "Re-pairs the buds to each other, which is usually the actual problem.",
          "expected": "Both buds show a light"},
         {"instruction": "Forget the device in your phone's Bluetooth settings, then pair "
                         "again from scratch.", "why": "A corrupted pairing record survives "
                         "a restart.", "expected": "Both buds play"},
         {"instruction": "Clean the charging contacts inside the case with a dry cotton bud.",
          "why": "One bud not charging looks exactly like one bud being broken.",
          "expected": "Both buds charge in the case"},
     ]},
    {"category": "security_camera", "symptom": "offline / no live feed", "est": 6,
     "steps": [
         {"instruction": "Check the camera is on the 2.4 GHz network, not the 5 GHz one.",
          "why": "Many cameras only join 2.4 GHz, and a combined-SSID router hides this.",
          "expected": "Camera appears online in the app"},
         {"instruction": "Power-cycle the HomeBase, wait for it to finish booting, then the "
                         "camera.", "why": "Order matters — the camera looks for a base "
                         "that is already up.", "expected": "Live feed returns"},
     ]},
    {"category": "power_station", "symptom": "won't turn on / no output", "est": 5,
     "steps": [
         {"instruction": "Hold the main power button for 5 seconds — a short press only "
                         "wakes the display.", "why": "Deep sleep needs a long press to "
                         "exit.", "expected": "Display lights up"},
         {"instruction": "Plug it into mains for 15 minutes, then try again.",
          "why": "Below a protective threshold the unit refuses to output until it has "
                 "some charge.", "expected": "Output switches respond"},
     ]},
]

ERROR_CODES = {
    "robot_vacuum": [
        ("E-01", "Left wheel jammed", "normal", ["Lift the robot and turn the left wheel by "
                                                 "hand", "Remove hair or thread from the axle"]),
        ("E-02", "Right wheel jammed", "normal", ["Turn the right wheel by hand",
                                                  "Clear anything wound around the axle"]),
        ("E-05", "Brush roll blocked", "normal", ["Turn the robot over",
                                                  "Cut away hair wrapped on the brush",
                                                  "Refit the brush until it clicks"]),
        ("E-08", "Dustbin or filter missing", "normal", ["Reseat the dustbin until it clicks"]),
        ("E-13", "Robot is stuck", "normal", ["Move it to open floor and restart the clean"]),
        ("E-21", "Battery temperature out of range", "safety",
         ["Stop charging", "Let it reach room temperature before charging again"]),
    ],
    "breast_pump": [
        ("E-01", "Motor blocked or seal not seated", "normal",
         ["Check the diaphragm is flat", "Reassemble dry"]),
        ("E-03", "Battery too low to run a cycle", "normal", ["Charge for 30 minutes"]),
        ("E-07", "Overheat protection triggered", "safety",
         ["Stop using it", "Let it cool for 30 minutes", "If it recurs, stop and contact support"]),
    ],
    "power_station": [
        ("F-02", "Output overload", "normal", ["Unplug everything",
                                               "Reconnect one device at a time"]),
        ("F-06", "Cell temperature out of range", "safety",
         ["Stop charging and discharging", "Move it somewhere cool", "Do not cover it"]),
    ],
}

TICKET_TEMPLATES = [
    ("robot_vacuum", "Suction dropped after a few months", "Filter was clogged with fine dust; "
     "rinsed and dried it, suction returned to normal."),
    ("robot_vacuum", "E-05 keeps coming back", "Hair wrapped tightly around the brush axle "
     "under the end cap — removing the cap and cutting it out cleared it for good."),
    ("robot_vacuum", "Robot won't return to the dock", "Dock was against a wall corner; moving "
     "it to give 0.5 m clearance either side fixed docking."),
    ("breast_pump", "Suction weak on one side only", "Duckbill valve had a hairline split. "
     "Replaced the valve, output matched the other side again."),
    ("breast_pump", "Pump stopped mid-session", "Battery was at 4%; the motor cuts out before "
     "full discharge to protect the cells. Charging fixed it."),
    ("breast_pump", "Milk leaking around the flange", "Wrong flange size — moved from 24 mm to "
     "27 mm and the leak stopped."),
    ("charger", "Charger stopped working with my laptop", "Cable was the fault, not the "
     "charger. A 100 W-rated cable restored full speed."),
    ("audio", "Left earbud silent", "Contacts in the case were coated in pocket lint; cleaning "
     "them restored charging on that side."),
    ("security_camera", "Camera keeps going offline at night", "Router was steering it to 5 GHz "
     "overnight. Splitting the SSIDs kept it stable."),
    ("power_station", "Unit shows F-02 under load", "Combined draw was above the rated output; "
     "moving the kettle to another circuit resolved it."),

    # Variety matters more than volume here. `search_tickets` retrieves by symptom
    # similarity and deduplicates by resolution, so ten templates spread over two hundred
    # tickets means the tool can only ever offer ten answers — and a top-5 collapses to
    # one. These read the way an engineer closes a ticket: what it actually was.
    ("robot_vacuum", "Leaves a wet trail when mopping", "Mop pad was saturated and water flow "
     "was set to high; a fresh pad on medium flow stopped the streaking."),
    ("robot_vacuum", "Misses the same patch of floor every run", "A dark rug was reading as a "
     "cliff to the drop sensors; excluding that zone fixed coverage."),
    ("robot_vacuum", "Loud rattling on hard floors", "A small screw had been picked up and was "
     "loose inside the dustbin housing."),
    ("robot_vacuum", "Map keeps resetting", "Robot was being carried between floors; saving a "
     "second map for the upper floor stopped the resets."),
    ("robot_vacuum", "Says dustbin missing when it is fitted", "Contact tab on the bin was bent; "
     "gently straightening it cleared the error."),
    ("breast_pump", "Motor runs but nothing is expressed", "Diaphragm had been refitted upside "
     "down after washing; reseating it restored the vacuum."),
    ("breast_pump", "Much noisier than when new", "Silicone parts had stiffened from sterilising "
     "too hot; a replacement set restored normal noise."),
    ("breast_pump", "Won't charge in its case", "Milk residue on the charging pins was insulating "
     "them; a dry cotton bud cleared it."),
    ("charger", "Only charges slowly on one port", "The two ports share a power budget; "
     "unplugging the second device restored full speed to the first."),
    ("charger", "Gets hot to the touch", "Within spec for GaN under load, but it was sitting on "
     "a bed. Moving it to a hard surface brought the temperature down."),
    ("audio", "Earbuds keep disconnecting on a walk", "Phone was in a back pocket — the body "
     "blocks 2.4 GHz. Front pocket resolved the dropouts."),
    ("audio", "Noise cancelling much weaker than before", "Ear tips were the wrong size so the "
     "seal was poor; a larger tip restored isolation."),
    ("audio", "Only one earbud pairs", "Buds had lost sync with each other; a reset in the case "
     "re-paired them."),
    ("security_camera", "Motion alerts every few minutes", "A tree branch in frame was triggering "
     "detection; an activity zone excluding it stopped the false alerts."),
    ("security_camera", "Night footage is a white blur", "Infrared was reflecting off a window the "
     "camera was mounted behind; moving it outside fixed it."),
    ("security_camera", "Battery lasts days instead of months", "Detection sensitivity was at "
     "maximum on a busy street; lowering it restored expected battery life."),
    ("power_station", "Won't wake up at all", "Had been stored at low charge and entered "
     "protection; 20 minutes on mains brought it back."),
    ("power_station", "Solar input shows zero", "Panels were wired in parallel below the minimum "
     "input voltage; rewiring them in series started the charge."),
    ("smart_lock", "Fingerprint fails in cold weather", "Dry winter skin reads poorly; enrolling "
     "the same finger a second time improved recognition."),
    ("stick_vacuum", "Runs for two minutes then stops", "Filter was blocked, tripping the motor's "
     "thermal cutout; washing and fully drying it restored runtime."),
    ("projector", "Image is dim and washed out", "Eco mode was on in a bright room; standard mode "
     "with the blinds closed restored contrast."),
    ("baby_monitor", "Video stutters in another room", "Base was inside a media cabinet; moving it "
     "into the open resolved the stuttering."),
]


async def products_by_category() -> Dict[str, List[Dict[str, Any]]]:
    rows = await db.fetch(
        "select id::text as id, sku, name, category, warranty_months from products "
        "where category is not null")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["category"], []).append(r)
    return out


async def seed_dealers() -> Dict[str, str]:
    ids: Dict[str, str] = {}
    for name, region, pattern, contact, path, authorized in DEALERS:
        row = await db.fetch_one(
            """
            insert into dealers (name, region, order_no_pattern, contact, service_path,
                                 authorized, source)
            values (%s,%s,%s,%s,%s,%s,'demo')
            on conflict do nothing
            returning id::text as id
            """,
            (name, region, pattern, contact, path, authorized),
        )
        if row is None:
            row = await db.fetch_one("select id::text as id from dealers where name = %s",
                                     (name,))
        ids[name] = row["id"]
    log.info("seed.dealers", n=len(ids))
    return ids


async def seed_customers(n: int = 40) -> List[Dict[str, Any]]:
    out = []
    for i in range(n):
        name = f"{random.choice(FIRST)} {random.choice(LAST)}"
        email = f"{name.split()[0].lower()}.{name.split()[1].lower()}{i}@example.demo"
        row = await db.fetch_one(
            """
            insert into customers (email, name, phone, locale, source)
            values (%s,%s,%s,%s,'demo')
            on conflict (email) do update set name = excluded.name
            returning id::text as id, email, name
            """,
            (email, name, f"+62-8{random.randint(10000000, 99999999)}",
             random.choice(["en", "en", "en", "zh", "id"])),
        )
        out.append(row)
    log.info("seed.customers", n=len(out))
    return out


async def seed_orders(customers: List[Dict[str, Any]],
                      by_cat: Dict[str, List[Dict[str, Any]]], n: int = 120) -> int:
    all_products = [p for ps in by_cat.values() for p in ps]
    if not all_products:
        log.warning("seed.no_products")
        return 0
    made = 0
    for i in range(n):
        customer = random.choice(customers)
        product = random.choice(all_products)
        channel = random.choice(CHANNELS)
        purchased = date.today() - timedelta(days=random.randint(10, 900))
        order_no = f"ANK-{purchased.year}-{random.randint(10000, 99999)}"
        row = await db.fetch_one(
            """
            insert into orders (order_no, customer_id, channel, purchase_date, status,
                                total, currency, source)
            values (%s,%s,%s,%s,%s,%s,'USD','demo')
            on conflict (order_no) do nothing
            returning id::text as id
            """,
            (order_no, customer["id"], channel, purchased,
             random.choice(["delivered", "delivered", "delivered", "shipped"]),
             round(random.uniform(29, 1499), 2)),
        )
        if not row:
            continue
        await db.execute(
            "insert into order_items (order_id, product_id, qty, serial) values (%s,%s,1,%s)",
            (row["id"], product["id"],
             f"{product['sku'][:6].upper()}{random.randint(100000, 999999)}"),
        )
        made += 1
    log.info("seed.orders", n=made)
    return made


async def seed_dealer_orders(dealers: Dict[str, str],
                             by_cat: Dict[str, List[Dict[str, Any]]]) -> None:
    """Includes the S3 fixture. These order numbers exist ONLY here."""
    all_products = [p for ps in by_cat.values() for p in ps]
    if not all_products:
        return
    vacuum = (by_cat.get("robot_vacuum") or all_products)[0]

    fixtures = [
        ("PT Sinar Elektronik", "SE-482911", vacuum,
         date.today() - timedelta(days=120), "Sarah C."),
        ("PT Sinar Elektronik", "SE-193044", random.choice(all_products),
         date.today() - timedelta(days=500), "Budi S."),
        ("Digital Mall Kuningan", "DMK/2026/0412", random.choice(all_products),
         date.today() - timedelta(days=45), "Mei W."),
        ("Grey Market Imports", "GM774120", random.choice(all_products),
         date.today() - timedelta(days=200), "unknown"),
        ("MediaMarkt DE", "MM-114-88213", random.choice(all_products),
         date.today() - timedelta(days=30), "Lena N."),
    ]
    for dealer_name, order_no, product, purchased, ref in fixtures:
        await db.execute(
            """
            insert into dealer_orders (dealer_id, order_no, product_id, purchase_date,
                                       customer_ref, source)
            values (%s,%s,%s,%s,%s,'demo')
            on conflict (dealer_id, order_no) do nothing
            """,
            (dealers[dealer_name], order_no, product["id"], purchased, ref),
        )

    for _ in range(25):
        dealer_name = random.choice([d[0] for d in DEALERS])
        pattern_owner = next(d for d in DEALERS if d[0] == dealer_name)
        order_no = _fake_order_no(pattern_owner[2])
        await db.execute(
            """
            insert into dealer_orders (dealer_id, order_no, product_id, purchase_date,
                                       customer_ref, source)
            values (%s,%s,%s,%s,%s,'demo')
            on conflict (dealer_id, order_no) do nothing
            """,
            (dealers[dealer_name], order_no, random.choice(all_products)["id"],
             date.today() - timedelta(days=random.randint(20, 800)),
             f"{random.choice(FIRST)} {random.choice(LAST)[0]}."),
        )
    log.info("seed.dealer_orders", fixtures=len(fixtures))


def _fake_order_no(pattern: str) -> str:
    """Generate a number that matches a dealer's regex, so pattern matching has something
    real to recognise."""
    out = pattern.strip("^$")
    out = out.replace(r"\d{6}", str(random.randint(100000, 999999)))
    out = out.replace(r"\d{5}", str(random.randint(10000, 99999)))
    out = out.replace(r"\d{4}", str(random.randint(1000, 9999)))
    out = out.replace(r"\d{3}", str(random.randint(100, 999)))
    out = out.replace(r"\d{2}", str(random.randint(10, 99)))
    out = out.replace(r"\d{7}", str(random.randint(1000000, 9999999)))
    out = out.replace(r"\d{8}", str(random.randint(10000000, 99999999)))
    out = out.replace(r"\d{10}", str(random.randint(10**9, 10**10 - 1)))
    return out


async def seed_flows(by_cat: Dict[str, List[Dict[str, Any]]]) -> int:
    made = 0
    for flow in FLOWS:
        targets = by_cat.get(flow["category"], [])[:6]
        steps = [{"step_id": f"s{i}", "ord": i, **s}
                 for i, s in enumerate(flow["steps"], start=1)]
        if not targets:
            # Keep the flow even with no product to attach it to — a generic match is
            # better than no steps at all.
            await db.execute(
                """
                insert into troubleshooting_flows (product_id, symptom, steps, est_minutes)
                values (null, %s, %s, %s)
                """,
                (flow["symptom"], json.dumps(steps), flow["est"]),
            )
            made += 1
            continue
        for product in targets:
            await db.execute(
                """
                insert into troubleshooting_flows (product_id, symptom, steps, est_minutes)
                values (%s,%s,%s,%s)
                """,
                (product["id"], flow["symptom"], json.dumps(steps), flow["est"]),
            )
            made += 1
    log.info("seed.flows", n=made)
    return made


async def seed_error_codes(by_cat: Dict[str, List[Dict[str, Any]]]) -> int:
    made = 0
    for category, codes in ERROR_CODES.items():
        for product in by_cat.get(category, [])[:8]:
            for code, meaning, severity, steps in codes:
                await db.execute(
                    """
                    insert into error_codes (product_id, code, meaning, severity, fix_steps)
                    values (%s,%s,%s,%s,%s)
                    on conflict (product_id, code) do update set meaning = excluded.meaning
                    """,
                    (product["id"], code, meaning, severity, json.dumps(steps)),
                )
                made += 1
    log.info("seed.error_codes", n=made)
    return made


async def seed_tickets(customers: List[Dict[str, Any]],
                       by_cat: Dict[str, List[Dict[str, Any]]], n: int = 200) -> int:
    made = 0
    for i in range(n):
        category, summary, resolution = random.choice(TICKET_TEMPLATES)
        pool = by_cat.get(category) or [p for ps in by_cat.values() for p in ps]
        if not pool:
            continue
        product = random.choice(pool)
        customer = random.choice(customers)
        row = await db.fetch_one(
            """
            insert into tickets (ticket_no, customer_id, product_id, priority, status,
                                 summary, verdict)
            values (%s,%s,%s,%s,'resolved',%s,null)
            on conflict (ticket_no) do nothing
            returning id::text as id
            """,
            (f"TCK-H{4000 + i}", customer["id"], product["id"],
             random.choice(["low", "normal", "normal", "high"]), summary),
        )
        if not row:
            continue
        await db.execute(
            "insert into ticket_events (ticket_id, kind, payload) values (%s,'resolved',%s)",
            (row["id"], json.dumps({"note": resolution})),
        )
        made += 1
    log.info("seed.tickets", n=made)
    return made


async def reset() -> None:
    for table in ("ticket_events", "tickets", "dealer_orders", "order_items", "orders",
                  "dealers", "customers", "troubleshooting_flows", "error_codes"):
        await db.execute(f"delete from {table} where true")
    log.info("seed.reset_done")


async def verify() -> bool:
    ok = True
    print()
    s3 = await db.fetch_one("select 1 as x from orders where order_no = 'SE-482911'")
    d3 = await db.fetch_one(
        "select dord.order_no, d.name from dealer_orders dord join dealers d on d.id = dord.dealer_id "
        "where dord.order_no = 'SE-482911'")
    if s3 is None and d3 is not None:
        print(f"PASS S3 fixture: SE-482911 is absent from `orders` and present in "
              f"`dealer_orders` ({d3['name']}) — the dealer path is exercisable.")
    else:
        print(f"FAIL S3 fixture: orders={bool(s3)} dealer_orders={bool(d3)}")
        ok = False

    counts = await db.fetch_one(
        """
        select (select count(*) from customers) as customers,
               (select count(*) from orders) as orders,
               (select count(*) from dealers) as dealers,
               (select count(*) from dealer_orders) as dealer_orders,
               (select count(*) from tickets) as tickets,
               (select count(*) from troubleshooting_flows) as flows,
               (select count(*) from error_codes) as error_codes,
               (select count(*) from products) as products
        """
    )
    for key, value in counts.items():
        print(f"  {key:16} {value}")
    if counts["flows"] == 0 or counts["error_codes"] == 0:
        print("FAIL: no troubleshooting flows or error codes — diagnostic blocks will be empty")
        ok = False
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--orders", type=int, default=120)
    parser.add_argument("--tickets", type=int, default=200)
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer(colors=False)])

    await db.init_pool()
    try:
        if args.reset:
            await reset()
        by_cat = await products_by_category()
        log.info("seed.catalog", categories=len(by_cat),
                 products=sum(len(v) for v in by_cat.values()))
        dealers = await seed_dealers()
        customers = await seed_customers()
        await seed_orders(customers, by_cat, args.orders)
        await seed_dealer_orders(dealers, by_cat)
        await seed_flows(by_cat)
        await seed_error_codes(by_cat)
        await seed_tickets(customers, by_cat, args.tickets)
        ok = await verify()
    finally:
        await db.close_pool()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
