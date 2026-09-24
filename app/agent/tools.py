"""The agent's tool registry.

Each tool is an async function plus a description the planner sees. Two rules hold
everywhere:

- A tool that finds nothing returns `{"found": false, ...}` and `ok=True`. "No such
  order" is an answer; only a broken dependency is an error. Collapsing the two is how
  an agent tells a customer their purchase never happened because a socket timed out.
- Results are JSON-serialisable and small. The planner sees a trimmed view
  (`ToolResult.compact`); the composer sees the whole object.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog

from app.schemas.agent import AgentState, ToolResult
from app.services import kb, orders, products, tickets, warranty

log = structlog.get_logger(__name__)

ToolFn = Callable[..., Awaitable[Dict[str, Any]]]


class Tool:
    def __init__(self, name: str, description: str, args: str, fn: ToolFn,
                 label: str = "", needs_product: bool = False):
        self.name = name
        self.description = description
        self.args = args
        self.fn = fn
        self.label = label or name.replace("_", " ").capitalize()
        self.needs_product = needs_product

    def spec(self) -> str:
        return f"- {self.name}({self.args}) — {self.description}"


REGISTRY: Dict[str, Tool] = {}


def register(name: str, description: str, args: str, label: str = "",
             needs_product: bool = False):
    def deco(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = Tool(name, description, args, fn, label, needs_product)
        return fn
    return deco


# ── catalog ───────────────────────────────────────────────────────────────────

# The catalogue's own categories. The planner was free to invent one, or to leave it
# out, and leaving it out is what put a Fast Charging Power Strip at the top of a search
# for a fast charging power bank: name matching alone cannot tell a strip from a bank.
_CATEGORY_SET = frozenset((
    "audio", "charger", "security_camera", "accessory", "power_station", "power_bank",
    "robot_vacuum", "service", "breast_pump", "smart_lock", "cable", "tracker",
    "projector", "mower",
))
PRODUCT_CATEGORIES = ", ".join(sorted(_CATEGORY_SET))


def _normalise_category(value: Optional[str]) -> Optional[str]:
    """A category the planner typed, matched against the ones the column actually holds."""
    if not value:
        return None
    token = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    aliases = {"powerbank": "power_bank", "batteries": "power_bank", "battery": "power_bank",
               "chargers": "charger", "vacuum": "robot_vacuum", "robot_vacuums": "robot_vacuum",
               "speaker": "audio", "speakers": "audio", "headphones": "audio",
               "earbuds": "audio", "camera": "security_camera", "cameras": "security_camera",
               "power_stations": "power_station", "cables": "cable", "trackers": "tracker"}
    token = aliases.get(token, token)
    return token if token in _CATEGORY_SET else None


@register("search_products",
          "Find products by name or description. Use when the customer names a product "
          "you have not resolved yet, or asks what to buy. Pass `category` whenever the "
          "customer has said what KIND of thing they mean, because a name search alone "
          "cannot tell a power strip from a power bank. Valid categories: "
          + PRODUCT_CATEGORIES + ".",
          "query, brand?, category?, k?", label="Looking up the product")
async def search_products(state: AgentState, query: str = "", brand: Optional[str] = None,
                          category: Optional[str] = None, k: int = 8) -> Dict[str, Any]:
    # The planner sets these, so they are arguments from a model and get treated as
    # such. Asked for a fast charging power bank it answered `category="power bank"`,
    # which is the right word and the wrong token: the column holds `power_bank`, the
    # filter matched nothing, and the reply came back with no products at all.
    k = max(1, min(int(k or 8), 12))
    category = _normalise_category(category)
    rows = await products.search_products(query, brand, category, limit=k)
    # An invented category returns nothing at all, which is worse than a loose match,
    # so a categorised search that finds nothing is retried without it.
    if not rows and category:
        rows = await products.search_products(query, brand, limit=k)
    if not rows:
        rows = await kb.search_products_vector(query, k=k, category=category)
    return {"found": bool(rows), "count": len(rows),
            "products": [{"sku": r.get("sku"), "name": r.get("name"), "brand": r.get("brand"),
                          "category": r.get("category"), "price": r.get("price"),
                          "currency": r.get("currency") or "USD",
                          "url": r.get("url"), "image_url": r.get("hero_image"),
                          "status": r.get("status")} for r in rows[:k]]}


@register("get_product",
          "Full detail for one product: specs, price, manuals, known error codes. "
          "Use once you know the SKU.",
          "sku", label="Reading the product details")
async def get_product(state: AgentState, sku: str = "") -> Dict[str, Any]:
    row = await products.get_product(sku)
    if not row:
        return {"found": False, "sku": sku}
    return {"found": True, "sku": row["sku"], "name": row["name"], "brand": row["brand"],
            "category": row["category"], "price": row["price"], "url": row["url"],
            "image_url": row["hero_image"], "warranty_months": row["warranty_months"],
            "specs": row["specs"], "error_codes": row["error_codes"][:12],
            "docs": [{"title": d["title"], "url": d["url"], "kind": d["kind"]}
                     for d in row["docs"][:6]]}


@register("lookup_error_code",
          "Exact meaning and fix for an error code shown on a device or in a photo. "
          "Always try this before searching when you have a code.",
          "code, sku?", label="Looking up that error code")
async def lookup_error_code(state: AgentState, code: str = "",
                            sku: Optional[str] = None) -> Dict[str, Any]:
    product_id = state.resolved.product_id if state.resolved else None
    if sku and not product_id:
        row = await products.get_product(sku)
        product_id = row["product_id"] if row else None
    category = state.resolved.category if state.resolved else None
    rows = await products.find_error_code(code, product_id, category=category)
    return {"found": bool(rows), "code": code, "matches": rows[:3]}


# ── knowledge ─────────────────────────────────────────────────────────────────

@register("search_kb",
          "Search manuals, FAQs and troubleshooting articles. Returns passages with "
          "citations. The main tool for 'how do I' and 'why is it doing this'.",
          "query, sku?, doc_type?, k?", label="Checking the manuals")
async def search_kb(state: AgentState, query: str = "", sku: Optional[str] = None,
                    doc_type: Optional[str] = None, k: int = 6) -> Dict[str, Any]:
    sku = sku or (state.resolved.sku if state.resolved else None)
    name = state.resolved.name if state.resolved else ""
    chunks = await kb.search_kb(query, sku=sku, doc_type=doc_type, k=k, product_name=name)
    return {"found": bool(chunks), "count": len(chunks),
            "passages": [{"text": (c.get("text") or "")[:900], "title": c.get("title", ""),
                          "url": c.get("url", ""), "section": c.get("section", ""),
                          "page": c.get("page"), "sku": c.get("sku"),
                          "score": round(c.get("score", 0), 3)} for c in chunks]}


@register("get_troubleshooting_flow",
          "A curated step-by-step repair flow for a known symptom. Prefer this over "
          "search when the symptom is a common one — the steps are ordered and tested.",
          "symptom, sku?", label="Pulling up the repair steps")
async def get_troubleshooting_flow(state: AgentState, symptom: str = "",
                                   sku: Optional[str] = None) -> Dict[str, Any]:
    product_id = state.resolved.product_id if state.resolved else None
    if sku and not product_id:
        row = await products.get_product(sku)
        product_id = row["product_id"] if row else None
    flow = await kb.get_troubleshooting_flow(symptom, product_id)
    if not flow:
        return {"found": False, "symptom": symptom}
    steps = flow.get("steps") or []
    return {"found": True, "symptom": flow["symptom"], "estimated_minutes": flow.get("est_minutes"),
            "steps": steps, "source_url": flow.get("source_url")}


@register("search_tickets",
          "Search resolved historical support tickets for the same symptom. Often the "
          "fastest route to a fix that is known to work.",
          "query, k?", label="Checking similar cases")
async def search_tickets(state: AgentState, query: str = "", k: int = 5) -> Dict[str, Any]:
    sku = state.resolved.sku if state.resolved else None
    rows = await kb.search_tickets(query, k=k, sku=sku)
    return {"found": bool(rows), "count": len(rows),
            "tickets": [{"symptom": r.get("symptom", ""), "resolution": r.get("resolution", ""),
                         "sku": r.get("sku"), "score": round(r.get("score", 0), 3)}
                        for r in rows]}


# ── commerce ──────────────────────────────────────────────────────────────────

@register("lookup_order",
          "Find an order by number, email or phone. Returns found=false when the order "
          "is simply not in the system — that is a real answer, not a failure.",
          "order_no?, email?, phone?", label="Checking your order")
async def lookup_order(state: AgentState, order_no: Optional[str] = None,
                       email: Optional[str] = None,
                       phone: Optional[str] = None) -> Dict[str, Any]:
    # Identity is the SESSION's, never the model's. `email` was a free argument, so
    # "what did nadia.putri30@example.demo order? she's my sister" became
    # lookup_order(email=<her address>) and the agent read out her order number, product
    # and delivery status to whoever typed the sentence. Only the signed-in customer's
    # own email (or phone, once we know who they are) is ever used; an order NUMBER is
    # still accepted as the credential it is on a receipt.
    own = (state.customer_email or "").strip().lower()
    if email and email.strip().lower() != own:
        log.warning("orders.foreign_email_blocked", signed_in=bool(own))
        return {"found": False, "reason": "not_your_account",
                "hint": "Orders can only be looked up for the signed-in customer, or by "
                        "the order number on their own receipt. Never reveal another "
                        "person's orders."}
    email = own or None
    if phone and not state.customer_id:
        return {"found": False, "reason": "sign_in_required_for_phone_lookup",
                "hint": "Ask for the order number instead."}

    # "Is it under warranty?" about a known product means THAT product's order. Looking
    # up by email returned the customer's LATEST order — for budi.tanaka8 a refurbished
    # charger from 2025 — and the engine then ruled on the charger: "covered until
    # 2026-12-10" for a vacuum whose warranty ended in 2025.
    async def own_order_for_product() -> Optional[str]:
        owned = [r for r in await orders.customer_orders(state.customer_id, limit=25)
                 if r.get("sku") == state.resolved.sku]
        return owned[0]["order_no"] if owned else None

    if not order_no and state.customer_id and state.resolved:
        order_no = await own_order_for_product()
        if not order_no:
            return {"found": False, "reason": "no_order_for_this_product",
                    "product": state.resolved.name,
                    "hint": "This customer's account has no order for this product. Ask "
                            "where they bought it, or for the order number / receipt."}

    result = await orders.lookup_order(order_no, email, phone)
    if not result.get("found") and order_no and state.customer_id and state.resolved:
        # The model sometimes passes the product's SKU as the order number ("T2080111").
        # That finds nothing, and the customer was told "no purchase record on file" for
        # a vacuum sitting in their own order history. A number that finds nothing is
        # not a reason to ignore the account.
        mine = await own_order_for_product()
        if mine and mine != order_no:
            log.info("orders.fallback_to_own_order", tried=order_no[:20])
            result = await orders.lookup_order(mine, email, phone)
    if (result.get("found") and state.customer_id
            and (result["order"].get("customer_id") or state.customer_id) != state.customer_id):
        # Signed in as one customer, asking about another customer's order.
        log.warning("orders.foreign_order_blocked")
        return {"found": False, "reason": "not_on_your_account",
                "hint": "That order belongs to a different account. Do not reveal it."}
    if result.get("found"):
        o = result["order"]
        out = {"found": True, "order_no": o["order_no"], "channel": o["channel"],
               "purchase_date": str(o["purchase_date"]) if o.get("purchase_date") else None,
               "status": o.get("status"), "customer_name": o.get("customer_name"),
               "items": [{"sku": i.get("sku"), "name": i.get("name"), "qty": i.get("qty"),
                          "serial": i.get("serial"), "category": i.get("category"),
                          "warranty_months": i.get("warranty_months")}
                         for i in o.get("items", [])],
               "source": o.get("source", "demo")}
        await remember_purchase(state, facts_from_order(
            out, state.resolved.sku if state.resolved else None))
        return out
    return {"found": False, "reason": result.get("reason", "not_found"), "order_no": order_no,
            "hint": "If the customer bought from a reseller, try lookup_dealer_order next."}


@register("lookup_dealer_order",
          "Check the authorised-dealer directory for an order number the main system "
          "does not have. Matches the invoice format against each dealer's pattern.",
          "order_no, dealer_hint?", label="Checking the dealer directory")
async def lookup_dealer_order(state: AgentState, order_no: str = "",
                              dealer_hint: Optional[str] = None) -> Dict[str, Any]:
    result = await orders.lookup_dealer_order(order_no, dealer_hint)
    if result.get("found"):
        d = result["dealer_order"]
        out = {"found": True, "match": result["match"], "order_no": d["order_no"],
               "dealer": {"name": d["dealer_name"], "region": d["region"],
                          "contact": d["contact"], "service_path": d["service_path"],
                          "authorized": d["authorized"]},
               "purchase_date": str(d["purchase_date"]) if d.get("purchase_date") else None,
               "sku": d.get("sku"), "product_name": d.get("product_name"),
               "warranty_months": d.get("warranty_months"),
               "items": result.get("items") or []}
    else:
        out = {"found": False, "match": result.get("match", "none"),
               "reason": result.get("reason"), "dealer": result.get("dealer")}
    await remember_purchase(state, facts_from_dealer(out))
    return out


@register("check_warranty",
          "Decide warranty coverage. This is a RULE ENGINE, not a judgement call — you "
          "must call it before saying anything about coverage, and you phrase its verdict "
          "without changing it.",
          "sku?, purchase_date?, channel?, damage_class?, order_found?, dealer_matched?, "
          "proof_present?", label="Checking warranty coverage")
async def check_warranty(
    state: AgentState, sku: Optional[str] = None, purchase_date: Optional[str] = None,
    channel: Optional[str] = None, damage_class: Optional[str] = None,
    order_found: bool = False, dealer_matched: bool = False, proof_present: bool = False,
) -> Dict[str, Any]:
    # Facts come from the RECORDS, not from the model's arguments.
    #
    # The engine is deterministic, but its inputs were typed by the model on every call,
    # so the same dealer order came back needs_proof, covered_via_dealer, needs_proof on
    # three consecutive turns; a logged-in customer's 2024 order was told its warranty
    # "ended 2026-06-10" (the engine, given the real date, says 2025-04-01); and a turn
    # after correctly finding an expired order, a bare re-call said "the purchase can't
    # be verified". A rule engine is only as deterministic as what feeds it. So: this
    # turn's lookups, else the purchase this session already established, else the
    # logged-in customer's own order for this product — and the model's values only
    # fill what no record knows.
    facts = _record_facts(state)
    # A lookup THIS turn that found nothing means the customer is asking about some
    # other purchase — falling back to the last one we knew would answer the wrong order.
    looked_up_now = any(o.tool in ("lookup_order", "lookup_dealer_order")
                        for o in state.observations)
    if not facts and not looked_up_now:
        facts = dict(state.purchase or {}) or await _owned_order_facts(state, sku)
    if facts:
        typed = {"purchase_date": purchase_date, "channel": channel,
                 "order_found": order_found, "dealer_matched": dealer_matched}
        purchase_date = facts.get("purchase_date") or purchase_date
        channel = facts.get("channel") or channel
        order_found = bool(facts.get("order_found"))
        dealer_matched = bool(facts.get("dealer_matched"))
        sku = facts.get("sku") or sku
        final = {"purchase_date": purchase_date, "channel": channel,
                 "order_found": order_found, "dealer_matched": dealer_matched}
        overridden = {k: v for k, v in typed.items()
                      if v not in (None, False, "") and str(v) != str(final[k])}
        if overridden:
            log.info("warranty.args_overridden", by=facts.get("source"), model_said=overridden)
    dealer_authorized = facts.get("dealer_authorized") if facts else None

    sku = sku or (state.resolved.sku if state.resolved else None)
    # Term precedence: this product's own warranty beats its category's default.
    #
    # It used to be the other way round — the product term was read and then
    # unconditionally overwritten by the category policy, so the specific fact lost to
    # the generic one. 232 products disagree with their category: every Anker SOLIX
    # power station carries 60 months against a `power_station` default of 24, and the
    # agent quoted 24. Eight products go the other way (12 against an 18-month default),
    # which is the dangerous direction — promising a year of coverage that does not
    # exist. The category row stays the fallback for products with no term of their own,
    # and remains the only source of `covers`/`excludes`.
    category, term = None, warranty.DEFAULT_TERM_MONTHS
    product_term: Optional[int] = None
    if sku:
        row = await products.get_product(sku)
        if row:
            category = row.get("category")
            product_term = row.get("warranty_months")
    if category:
        policy = await kb.db.fetch_one(
            "select months from warranty_policies where category = %s", (category,))
        if policy:
            term = policy["months"]
    term = product_term or term

    # Damage the customer DESCRIBED ("fell in the pool yesterday"), read by perception
    # across the whole conversation. The planner rarely passed it, so water-damaged
    # earbuds reached the engine as an unknown fault and came back "can't verify the
    # purchase" — while the agent improvised a "12-month term" around the gap.
    if not damage_class and state.perception.damage in ("drop", "liquid", "crack"):
        damage_class = "physical_damage"
    elif not damage_class and state.perception.damage == "wear":
        damage_class = "wear"
    # Damage class from the photo when neither supplied one.
    if not damage_class:
        for facts in state.vlm_facts:
            dc = (facts.get("detected") or {}).get("damage_class")
            if dc and dc != "unknown":
                damage_class = dc
                break

    decision = warranty.decide(warranty.WarrantyInput(
        sku=sku, category=category,
        purchase_date=warranty.parse_date(purchase_date),
        channel=warranty.classify_channel(channel),
        damage_class=warranty.classify_damage(damage_class),
        order_found=order_found, dealer_matched=dealer_matched,
        dealer_authorized=dealer_authorized, proof_present=proof_present,
        term_months=term,
        safety_concern=state.perception.safety_concern,
    ))
    log.info("warranty.decided", verdict=decision.verdict.value, reason=decision.reason_code,
             facts_from=(facts or {}).get("source", "model_args"))
    return {"decided": True, **decision.as_payload(), "sku": sku, "category": category,
            "term_months": term, "facts_from": (facts or {}).get("source", "model_args"),
            "dealer_authorized": dealer_authorized}


def facts_from_order(data: Dict[str, Any], want_sku: Optional[str] = None) -> Dict[str, Any]:
    if not data.get("found"):
        return {}
    items = data.get("items") or []
    item = next((i for i in items if i.get("sku") == want_sku), items[0] if items else {})
    return {"source": "order", "order_no": data.get("order_no"),
            "channel": data.get("channel"), "purchase_date": data.get("purchase_date"),
            "sku": item.get("sku"), "order_found": True}


def facts_from_dealer(data: Dict[str, Any]) -> Dict[str, Any]:
    dealer = data.get("dealer") or {}
    if data.get("found"):
        return {"source": "dealer_record", "order_no": data.get("order_no"),
                "channel": "dealer", "purchase_date": data.get("purchase_date"),
                "sku": data.get("sku"), "dealer_matched": True,
                "dealer_authorized": dealer.get("authorized"), "dealer_name": dealer.get("name")}
    if dealer:
        # Only the invoice SHAPE matched a dealer: we know who, not what or when.
        return {"source": "dealer_pattern", "channel": "dealer", "dealer_matched": False,
                "dealer_authorized": dealer.get("authorized"), "dealer_name": dealer.get("name")}
    return {}


def _record_facts(state: AgentState) -> Dict[str, Any]:
    """What this turn's lookups established about the purchase, if anything."""
    o = state.observation_by_tool("lookup_order")
    if o and o.data.get("found"):
        return facts_from_order(o.data, state.resolved.sku if state.resolved else None)
    d = state.observation_by_tool("lookup_dealer_order")
    return facts_from_dealer(d.data) if d else {}


async def _owned_order_facts(state: AgentState, sku: Optional[str]) -> Dict[str, Any]:
    """A logged-in customer's own order for the product under discussion. They should
    not have to type an order number we can already see on their account."""
    want = sku or (state.resolved.sku if state.resolved else None)
    if not (state.customer_id and want):
        return {}
    for row in await orders.customer_orders(state.customer_id, limit=25):
        if row.get("sku") == want:
            return {"source": "account_order", "order_no": row["order_no"],
                    "channel": row.get("channel"),
                    "purchase_date": str(row["purchase_date"]) if row.get("purchase_date") else None,
                    "sku": want, "order_found": True}
    return {}


async def remember_purchase(state: AgentState, facts: Dict[str, Any]) -> None:
    """Persist what the records said, so the next turn's engine call does not depend on
    the model remembering to pass it back in."""
    if facts and facts != state.purchase:
        state.purchase = facts
        from app.services import sessions as session_svc
        try:
            await session_svc.set_meta(state.session_id, "purchase", facts)
        except Exception as e:  # noqa: BLE001 — memory is an optimisation, not a gate
            log.warning("warranty.remember_failed", error=str(e))


# ── multimodal + escalation ───────────────────────────────────────────────────

@register("analyze_image",
          "Ask a specific question about a photo the customer already uploaded, when the "
          "first pass did not capture what you need.",
          "question, attachment_id?", label="Looking at your photo again")
async def analyze_image(state: AgentState, question: str = "",
                        attachment_id: Optional[str] = None) -> Dict[str, Any]:
    from app.services.vision import analyze_attachment
    att_id = attachment_id or (state.attachment_ids[0] if state.attachment_ids else None)
    if not att_id:
        return {"found": False, "reason": "no_attachment"}
    answer = await analyze_attachment(att_id, question)
    return {"found": True, "attachment_id": att_id, "answer": answer}


@register("create_ticket",
          "Open a support ticket and hand the case to a human. Use when the fixes are "
          "exhausted, the customer asks for a person, or the situation is unsafe.",
          "summary, priority?, reason?", label="Opening a ticket for you")
async def create_ticket(state: AgentState, summary: str = "", priority: str = "",
                        reason: str = "") -> Dict[str, Any]:
    # One conversation, one ticket. A second "put me through to someone" used to open a
    # second ticket, and the customer was handed two numbers for one problem.
    if state.open_ticket.get("ticket_no"):
        return {"created": True, "reused": True, **state.open_ticket}
    failed = max(sum(1 for o in state.observations if o.tool == "step_result_failed"),
                 state.failed_attempts)
    priority = priority or tickets.derive_priority(
        state.perception.emotion.value, state.perception.urgency.has_deadline,
        state.perception.safety_concern, failed,
    )
    verdict = None
    w = state.observation_by_tool("check_warranty")
    if w:
        verdict = w.data.get("verdict")
    ticket = await tickets.create_ticket(
        summary or state.perception.summary or state.user_message[:200],
        session_id=state.session_id, customer_id=state.customer_id,
        product_id=state.resolved.product_id if state.resolved else None,
        priority=priority, verdict=verdict, reason=reason,
    )
    out = {"created": True, **{k: str(v) for k, v in ticket.items()}}
    out["eta"] = tickets.ETA.get(out.get("priority", "normal"), tickets.ETA["normal"])
    state.open_ticket = {k: out.get(k) for k in ("ticket_no", "priority", "eta", "summary")}
    from app.services import sessions as session_svc
    try:
        await session_svc.set_meta(state.session_id, "ticket", state.open_ticket)
    except Exception as e:  # noqa: BLE001
        log.warning("tickets.remember_failed", error=str(e))
    return out


@register("web_search",
          "Search Anker's public sites for something the knowledge base does not have. "
          "Restricted to official domains.",
          "query", label="Checking Anker's site")
async def web_search(state: AgentState, query: str = "") -> Dict[str, Any]:
    from app.services.websearch import search_official
    results = await search_official(query)
    return {"found": bool(results), "results": results[:5]}


# ── dispatch ──────────────────────────────────────────────────────────────────

def tool_specs(exclude: Optional[List[str]] = None) -> str:
    exclude = exclude or []
    return "\n".join(t.spec() for name, t in REGISTRY.items() if name not in exclude)


async def run_tool(state: AgentState, name: str, args: Dict[str, Any],
                   call_id: Optional[str] = None) -> ToolResult:
    # The caller owns the id: it already announced this call to the client, and a
    # result carrying a different id can never be paired with it.
    call_id = call_id or f"call_{uuid.uuid4().hex[:8]}"
    tool = REGISTRY.get(name)
    if tool is None:
        return ToolResult(call_id=call_id, tool=name, ok=False,
                          summary=f"no such tool: {name}",
                          data={"error": "unknown_tool",
                                "available": sorted(REGISTRY)})
    started = time.perf_counter()
    try:
        clean = {k: v for k, v in (args or {}).items() if v is not None}
        data = await tool.fn(state, **clean)
        ms = int((time.perf_counter() - started) * 1000)
        return ToolResult(call_id=call_id, tool=name, ok=True, ms=ms,
                          summary=summarise(name, data), data=data)
    except TypeError as e:
        # A hallucinated argument name is a planner error, not an outage — tell the
        # planner precisely what went wrong so the retry can be correct.
        ms = int((time.perf_counter() - started) * 1000)
        log.warning("tool.bad_args", tool=name, args=args, error=str(e)[:160])
        return ToolResult(call_id=call_id, tool=name, ok=False, ms=ms,
                          summary="wrong arguments",
                          data={"error": "bad_arguments", "detail": str(e)[:200],
                                "expected": tool.args})
    except Exception as e:  # noqa: BLE001
        ms = int((time.perf_counter() - started) * 1000)
        log.exception("tool.failed", tool=name)
        return ToolResult(call_id=call_id, tool=name, ok=False, ms=ms,
                          summary=f"{type(e).__name__}", data={"error": str(e)[:300]})


def summarise(tool: str, data: Dict[str, Any]) -> str:
    """A short human line for the `tool_result` event. The customer sees this."""
    if tool == "check_warranty":
        until = f" until {data['warranty_until']}" if data.get("warranty_until") else ""
        return f"verdict: {data.get('verdict', '?')}{until}"
    if tool in ("lookup_order", "lookup_dealer_order"):
        if data.get("found"):
            dealer = (data.get("dealer") or {}).get("name")
            # Whole names, with the SKU. Cut at 40 characters, "2× SOLIX F3800 Plus + 2×
            # Expansion Battery + Smart Home Power Panel…" lost the power panel and the SKU,
            # and the reply that mentioned both read as invented to anyone checking it.
            items = [f"{i['name'][:120]}" + (f" ({i['sku']})" if i.get("sku") else "")
                     for i in (data.get("items") or []) if i.get("name")]
            return (f"found {data.get('order_no', '')}"
                    + (f" · dealer {dealer}" if dealer else "")
                    + (f" · items: {'; '.join(items[:3])}" if items else "")
                    + (f" · bought {data['purchase_date']}" if data.get("purchase_date") else ""))
        if data.get("reason") in ("not_your_account", "not_on_your_account"):
            return "not on this customer's account"
        return "no matching order"
    if tool == "search_kb":
        return f"{data.get('count', 0)} passages"
    if tool == "search_products":
        return f"{data.get('count', 0)} products"
    if tool == "get_troubleshooting_flow":
        return f"{len(data.get('steps', []))} steps" if data.get("found") else "no flow"
    if tool == "lookup_error_code":
        if data.get("found"):
            meaning = ((data.get("matches") or [{}])[0].get("meaning") or "").strip()
            return f"code recognised: {meaning}" if meaning else "code recognised"
        return "code not in the database"
    if tool == "search_tickets":
        return f"{data.get('count', 0)} similar resolved cases"
    if tool == "create_ticket":
        # Priority and ETA ride in the summary: the pipeline strip shows the customer
        # this line, and "ticket TCK-1897" alone left them asking how long it takes.
        bits = [f"ticket {data.get('ticket_no', '')}"]
        if data.get("priority"):
            bits.append(f"{data['priority']} priority")
        if data.get("eta"):
            bits.append(f"a person picks it up {data['eta']}")
        if data.get("reused"):
            bits.append("already open for this conversation")
        return " · ".join(bits)
    return "ok" if data.get("found", True) else "nothing found"
