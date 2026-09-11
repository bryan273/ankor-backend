"""Order and dealer lookups — the data half of scenario S3.

The important behaviour: a missing order is `not_found`, which is a *result*, not an
error. An agent that cannot tell "this order does not exist" from "the database is
down" will tell a customer their purchase was never made because a connection dropped.

When the order system has nothing, the dealer directory is the next question, and
it is answered by matching the order number's shape against each dealer's known
format — a reseller's invoice numbers look nothing like a store order.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import structlog

from app.clients import db

log = structlog.get_logger(__name__)


async def lookup_order(order_no: Optional[str] = None, email: Optional[str] = None,
                       phone: Optional[str] = None) -> Dict[str, Any]:
    """Returns `{"found": bool, "order": {...}}`, never raises for a miss."""
    if order_no:
        row = await db.fetch_one(
            """
            select o.id::text as order_id, o.order_no, o.channel, o.purchase_date, o.status,
                   o.total, o.currency, o.source, c.email, c.name as customer_name,
                   c.id::text as customer_id
            from orders o left join customers c on c.id = o.customer_id
            where upper(o.order_no) = upper(%s)
            """,
            (order_no.strip(),),
        )
    elif email:
        row = await db.fetch_one(
            """
            select o.id::text as order_id, o.order_no, o.channel, o.purchase_date, o.status,
                   o.total, o.currency, o.source, c.email, c.name as customer_name,
                   c.id::text as customer_id
            from orders o join customers c on c.id = o.customer_id
            where lower(c.email) = lower(%s)
            order by o.purchase_date desc limit 1
            """,
            (email.strip(),),
        )
    elif phone:
        row = await db.fetch_one(
            """
            select o.id::text as order_id, o.order_no, o.channel, o.purchase_date, o.status,
                   o.total, o.currency, o.source, c.email, c.name as customer_name,
                   c.id::text as customer_id
            from orders o join customers c on c.id = o.customer_id
            where c.phone = %s order by o.purchase_date desc limit 1
            """,
            (phone.strip(),),
        )
    else:
        return {"found": False, "reason": "no_identifier"}

    if not row:
        return {"found": False, "reason": "not_in_order_system", "order_no": order_no}

    row["items"] = await db.fetch(
        """
        select oi.qty, oi.serial, p.sku, p.name, p.category, p.warranty_months,
               p.id::text as product_id
        from order_items oi left join products p on p.id = oi.product_id
        where oi.order_id = %s
        """,
        (row["order_id"],),
    )
    return {"found": True, "order": row}


async def customer_by_email(email: str) -> Optional[Dict[str, Any]]:
    return await db.fetch_one(
        "select id::text as customer_id, email, name, phone, locale from customers "
        "where lower(email) = lower(%s)",
        (email.strip(),),
    )


async def customer_orders(customer_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    return await db.fetch(
        """
        select o.order_no, o.channel, o.purchase_date, o.status,
               p.sku, p.name, p.category, p.id::text as product_id, p.warranty_months
        from orders o
        join order_items oi on oi.order_id = o.id
        left join products p on p.id = oi.product_id
        where o.customer_id = %s
        order by o.purchase_date desc
        limit %s
        """,
        (customer_id, limit),
    )


async def lookup_dealer_order(order_no: str,
                              dealer_hint: Optional[str] = None) -> Dict[str, Any]:
    """Three passes, cheapest first: the exact invoice, the dealer named by the user,
    then the invoice-number shape."""
    order_no = (order_no or "").strip()
    if not order_no:
        return {"found": False, "reason": "no_order_no"}

    row = await db.fetch_one(
        """
        select dord.order_no, dord.purchase_date, dord.customer_ref, d.name as dealer_name,
               d.region, d.contact, d.service_path, d.authorized, d.id::text as dealer_id,
               p.sku, p.name as product_name, p.id::text as product_id, p.category,
               p.warranty_months
        from dealer_orders dord
        join dealers d on d.id = dord.dealer_id
        left join products p on p.id = dord.product_id
        where upper(dord.order_no) = upper(%s)
        """,
        (order_no,),
    )
    if row:
        return {"found": True, "match": "exact_invoice", "dealer_order": row}

    if dealer_hint:
        dealer = await db.fetch_one(
            """
            select id::text as dealer_id, name, region, contact, service_path, authorized,
                   order_no_pattern
            from dealers
            where name ilike %s or similarity(name, %s) > 0.3
            order by similarity(name, %s) desc limit 1
            """,
            (f"%{dealer_hint}%", dealer_hint, dealer_hint),
        )
        if dealer:
            return {"found": False, "match": "dealer_named", "dealer": dealer,
                    "reason": "dealer_known_invoice_unknown"}

    # Shape match: which dealer issues invoice numbers that look like this one?
    dealers = await db.fetch(
        "select id::text as dealer_id, name, region, contact, service_path, authorized, "
        "order_no_pattern from dealers where order_no_pattern is not null")
    for d in dealers:
        try:
            if re.match(d["order_no_pattern"], order_no, re.IGNORECASE):
                return {"found": False, "match": "pattern", "dealer": d,
                        "reason": "looks_like_dealer_invoice"}
        except re.error:
            log.warning("orders.bad_dealer_pattern", dealer=d["name"],
                        pattern=d["order_no_pattern"])
    return {"found": False, "match": "none", "reason": "unrecognised_format"}


async def list_dealers() -> List[Dict[str, Any]]:
    return await db.fetch(
        "select id::text as dealer_id, name, region, contact, service_path, authorized, "
        "order_no_pattern from dealers order by name")
