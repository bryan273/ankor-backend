"""Who the customer is — the demo sign-in behind the storefront.

Deliberately **not** authentication. There is no password here and no token: the demo
data has no credentials to check against, and pretending otherwise would be a login
screen that lies about what it does. What this gives the rest of the system is the one
thing identity is actually for in this agent: `customer_id` comes from the session, and
never from anything the model can type. `lookup_order` refuses a foreign email on the
strength of it.

A real deployment replaces this with the shop's own sign-in and keeps the same contract:
the server decides who is asking, and the agent is told.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from app.clients import db
from app.deps import require_api_key

router = APIRouter(tags=["customers"])


_PROFILE_SQL = """
    select c.id::text as customer_id, c.email, c.name, c.locale,
           count(o.id) as orders,
           max(o.created_at)::date as last_order_at
    from customers c
    left join orders o on o.customer_id = c.id
    where lower(c.email) = lower(%s)
    group by c.id, c.email, c.name, c.locale
"""


@router.get("/customers/lookup")
async def lookup_customer(email: str = "",
                          _: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Sign in as an existing customer. An unknown address is `found: false`, not a 404:
    the storefront lets anyone browse and ask, signed in or not."""
    if not email.strip():
        return {"found": False, "reason": "no_email"}
    row = await db.fetch_one(_PROFILE_SQL, (email.strip(),))
    if not row:
        return {"found": False, "reason": "unknown_email", "email": email.strip()}
    return {"found": True, **row}


@router.get("/customers/demo")
async def demo_customers(limit: int = 4,
                         _: str = Depends(require_api_key)) -> Dict[str, Any]:
    """The accounts worth demonstrating with: the ones that actually own things.

    An account with no orders proves nothing on stage — the interesting behaviour
    (resolving "my power station" from purchase history, refusing another person's
    order) only shows on a customer who has bought something.
    """
    rows: List[Dict[str, Any]] = await db.fetch(
        """
        select c.id::text as customer_id, c.email, c.name,
               count(o.id) as orders,
               (array_agg(p.name order by o.created_at desc))[1] as latest_product
        from customers c
        join orders o on o.customer_id = c.id
        join order_items oi on oi.order_id = o.id
        join products p on p.id = oi.product_id
        group by c.id, c.email, c.name
        having count(o.id) >= 5
        order by count(o.id) desc, c.name
        limit %s
        """,
        (min(limit, 12),),
    )
    return {"count": len(rows), "customers": rows}
