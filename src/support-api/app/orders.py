# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Order lookup: accounting tables + enrichment from the checkout trace."""

from datetime import datetime
from decimal import Decimal

from . import clients, db
from .errors import not_found

ORDER_SELECT = """
SELECT o.order_id, o.created_at, o.status, o.user_id, o.trace_id,
  COALESCE((SELECT json_agg(json_build_object(
      'product_id', i.product_id, 'product_name', p.name, 'quantity', i.quantity,
      'currency_code', i.item_cost_currency_code, 'units', i.item_cost_units, 'nanos', i.item_cost_nanos)
      ORDER BY i.product_id)
    FROM accounting.orderitem i LEFT JOIN catalog.products p ON p.id = i.product_id
    WHERE i.order_id = o.order_id), '[]') AS items,
  COALESCE((SELECT json_agg(json_build_object(
      'tracking_id', s.shipping_tracking_id, 'currency_code', s.shipping_cost_currency_code,
      'units', s.shipping_cost_units, 'nanos', s.shipping_cost_nanos,
      'street_address', s.street_address, 'city', s.city, 'state', s.state,
      'country', s.country, 'zip_code', s.zip_code))
    FROM accounting.shipping s WHERE s.order_id = o.order_id), '[]') AS shipments
FROM accounting."order" o
"""

SENSITIVE_ATTRIBUTES = {"user.email", "demo.payment.card_number", "demo.payment.card_cvv"}
SUMMARY_ATTRIBUTES = {"demo.order.id", "user.id", "http.response.status_code", "http.route",
                      "rpc.grpc.status_code", "error.type", "demo.shipping.tracking.id",
                      "demo.payment.transaction.id", "feature_flag.key", "feature_flag.variant"}


def money(currency: str, units: int, nanos: int) -> dict:
    amount = (Decimal(units) + Decimal(nanos) / Decimal(1_000_000_000)).quantize(Decimal("0.01"))
    return {"currency_code": currency, "units": units, "nanos": nanos, "amount": str(amount)}


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _address(s: dict, include_pii: bool) -> dict:
    if include_pii:
        return {k: s.get(k) for k in ("street_address", "city", "state", "country", "zip_code")}
    return {"street_address": "***", "city": s.get("city"), "state": s.get("state"),
            "country": s.get("country"), "zip_code": "***"}


async def load(order_id: str, enrich: bool = True) -> tuple[dict, dict | None]:
    """Return the raw order row and, if found, the checkout trace info. Caches trace/user ids on the order."""
    row = await db.fetch_one(ORDER_SELECT + " WHERE o.order_id = %s", order_id)
    if not row:
        raise not_found("order", order_id)
    trace = None
    if enrich:
        trace = await clients.find_order_trace(order_id)
        if trace:
            user_id = trace["checkout_attributes"].get("user.id")
            if row["trace_id"] != trace["trace_id"] or (user_id and not row["user_id"]):
                await db.execute('UPDATE accounting."order" SET trace_id = %s, user_id = COALESCE(user_id, %s) WHERE order_id = %s',
                                 trace["trace_id"], user_id, order_id)
                row["trace_id"], row["user_id"] = trace["trace_id"], row["user_id"] or user_id
    return row, trace


async def reship_tracking_ids(order_id: str) -> set[str]:
    rows = await db.fetch_all("SELECT details->>'tracking_id' AS t FROM support.order_events WHERE order_id = %s AND type = 'RESHIPPED'", order_id)
    return {r["t"] for r in rows}


def original_shipment(row: dict, reships: set[str]) -> dict | None:
    originals = [s for s in row["shipments"] if s["tracking_id"] not in reships]
    return (originals or row["shipments"] or [None])[0]


def shape(row: dict, trace: dict | None, reships: set[str], include_pii: bool = False) -> dict:
    items, currency, subtotal = [], None, Decimal(0)
    for i in row["items"]:
        unit = money(i["currency_code"], i["units"], i["nanos"])
        line = (Decimal(unit["amount"]) * i["quantity"]).quantize(Decimal("0.01"))
        subtotal += line
        currency = currency or i["currency_code"]
        items.append({"product_id": i["product_id"], "product_name": i["product_name"], "quantity": i["quantity"],
                      "unit_price": unit, "line_total": str(line)})
    original = original_shipment(row, reships)
    shipping_cost = money(original["currency_code"], original["units"], original["nanos"]) if original else None
    total = subtotal + (Decimal(shipping_cost["amount"]) if shipping_cost else 0)
    attrs = (trace or {}).get("checkout_attributes", {})
    email = attrs.get("user.email")
    return {
        "order_id": row["order_id"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else row["created_at"],
        "customer": {
            "user_id": row["user_id"],
            "email": email if include_pii else mask_email(email),
            "currency": attrs.get("demo.user_context.selected_currency"),
        },
        "items": items,
        "shipping_cost": shipping_cost,
        "total": {"currency_code": currency, "amount": str(total.quantize(Decimal("0.01")))},
        "shipments": [{"tracking_id": s["tracking_id"], "is_reship": s["tracking_id"] in reships,
                       "address": _address(s, include_pii)} for s in row["shipments"]],
        "trace": {"trace_id": row["trace_id"], "link": clients.trace_link(row["trace_id"])},
        "pii_redacted": not include_pii,
    }


async def get(order_id: str, include_pii: bool = False) -> dict:
    row, trace = await load(order_id)
    return shape(row, trace, await reship_tracking_ids(order_id), include_pii)


async def search(limit: int, offset: int, status: str | None, user_id: str | None,
                 product_id: str | None, since: datetime | None) -> dict:
    where, params = [], []
    if status:
        where.append("o.status = %s"); params.append(status)
    if user_id:
        where.append("o.user_id = %s"); params.append(user_id)
    if since:
        where.append("o.created_at >= %s"); params.append(since)
    if product_id:
        where.append("EXISTS (SELECT 1 FROM accounting.orderitem i WHERE i.order_id = o.order_id AND i.product_id = %s)")
        params.append(product_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = (await db.fetch_one('SELECT count(*) AS n FROM accounting."order" o' + clause, *params))["n"]
    rows = await db.fetch_all(ORDER_SELECT + clause + " ORDER BY o.created_at DESC NULLS LAST, o.order_id LIMIT %s OFFSET %s",
                              *params, limit, offset)
    orders = []
    for r in rows:
        shaped = shape(r, None, set())
        orders.append({k: shaped[k] for k in ("order_id", "status", "created_at", "total", "trace")}
                      | {"user_id": r["user_id"], "item_count": sum(i["quantity"] for i in r["items"])})
    return {"total": total, "limit": limit, "offset": offset, "orders": orders}


def order_result_payload(row: dict, shipment: dict) -> dict:
    """OrderResult in the snake_case JSON shape checkout sends to the email service."""
    return {
        "order_id": row["order_id"],
        "shipping_tracking_id": shipment["tracking_id"],
        "shipping_cost": {"currency_code": shipment["currency_code"], "units": shipment["units"], "nanos": shipment["nanos"]},
        "shipping_address": {k: shipment[k] for k in ("street_address", "city", "state", "country", "zip_code")},
        "items": [{"item": {"product_id": i["product_id"], "quantity": i["quantity"]},
                   "cost": {"currency_code": i["currency_code"], "units": i["units"], "nanos": i["nanos"]}}
                  for i in row["items"]],
    }


async def timeline(order_id: str, detail: str, include_pii: bool) -> dict:
    row, _ = await load(order_id)
    spans = await clients.get_trace(row["trace_id"]) if row["trace_id"] else []
    entries = []
    for s in spans:
        if detail == "summary" and not (s["kind"] in ("SPAN_KIND_SERVER", "SPAN_KIND_CONSUMER") or s["error"]):
            continue
        keep = (lambda k: True) if detail == "full" else (lambda k: k in SUMMARY_ATTRIBUTES)
        attrs = {k: v for k, v in s["attributes"].items()
                 if keep(k) and (include_pii or k not in SENSITIVE_ATTRIBUTES)}
        entries.append({"at": s["start"], "sort": s["start_ns"], "source": "trace", "service": s["service"],
                        "name": s["name"], "kind": s["kind"], "duration_ms": s["duration_ms"], "error": s["error"],
                        "status_message": s["status_message"], "events": s["events"], "attributes": attrs,
                        "span_id": s["span_id"]})
    for e in await db.fetch_all("SELECT at, type, details, action_id FROM support.order_events WHERE order_id = %s", order_id):
        entries.append({"at": e["at"].isoformat(), "sort": int(e["at"].timestamp() * 1e9), "source": "support",
                        "service": "support-api", "name": e["type"], "details": e["details"],
                        "action_id": str(e["action_id"]) if e["action_id"] else None, "error": False})
    entries.sort(key=lambda x: x.pop("sort"))
    return {
        "order_id": order_id,
        "trace_id": row["trace_id"],
        "trace_link": clients.trace_link(row["trace_id"]),
        "trace_found": bool(spans),
        "detail": detail,
        "error_count": sum(1 for e in entries if e["error"]),
        "services": sorted({e["service"] for e in entries if e["service"]}),
        "entries": entries,
    }
