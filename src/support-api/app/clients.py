# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Thin async clients for the demo services support-api talks to."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings
from .errors import upstream_failed

FLAGD_SCHEMA = "https://flagd.dev/schema/v0/flags.json"

http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))


async def _call(service: str, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        res = await http.request(method, url, **kwargs)
    except httpx.HTTPError as e:
        raise upstream_failed(service, f"{type(e).__name__}: {e}") from e
    if res.status_code >= 400:
        raise upstream_failed(service, f"HTTP {res.status_code}: {res.text[:300]}")
    return res


# --- Jaeger (v3 query API, OTLP JSON) ---------------------------------------

def _attr_value(v: dict):
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def _attrs(items: list | None) -> dict:
    return {a["key"]: _attr_value(a.get("value", {})) for a in items or []}


def _parse_otlp(body: str) -> list[dict]:
    """Flatten a (possibly streamed, newline-delimited) v3 response into span dicts."""
    chunks = []
    try:
        chunks = [json.loads(body)]
    except json.JSONDecodeError:
        chunks = [json.loads(line) for line in body.splitlines() if line.strip()]
    spans = []
    for chunk in chunks:
        for rs in (chunk.get("result") or chunk).get("resourceSpans", []):
            service = _attrs(rs.get("resource", {}).get("attributes")).get("service.name")
            for ss in rs.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    start, end = int(s.get("startTimeUnixNano", 0)), int(s.get("endTimeUnixNano", 0))
                    kind = s.get("kind")
                    status = s.get("status") or {}
                    spans.append({
                        "trace_id": s.get("traceId"),
                        "span_id": s.get("spanId"),
                        "parent_span_id": s.get("parentSpanId") or None,
                        "service": service,
                        "name": s.get("name"),
                        "kind": kind if isinstance(kind, str) else {1: "SPAN_KIND_INTERNAL", 2: "SPAN_KIND_SERVER", 3: "SPAN_KIND_CLIENT", 4: "SPAN_KIND_PRODUCER", 5: "SPAN_KIND_CONSUMER"}.get(kind, str(kind)),
                        "start": datetime.fromtimestamp(start / 1e9, tz=timezone.utc).isoformat(),
                        "start_ns": start,
                        "duration_ms": round((end - start) / 1e6, 2),
                        "error": status.get("code") in (2, "STATUS_CODE_ERROR"),
                        "status_message": status.get("message"),
                        "attributes": _attrs(s.get("attributes")),
                        "events": [e.get("name") for e in s.get("events", [])],
                    })
    return spans


async def find_order_trace(order_id: str) -> dict | None:
    """Locate the checkout trace for an order via the demo.order.id span attribute."""
    now = datetime.now(timezone.utc)
    params = {
        "query.service_name": "checkout",
        "query.attributes": json.dumps({"demo.order.id": order_id}),
        "query.start_time_min": (now - timedelta(hours=settings.trace_lookback_hours)).isoformat(),
        "query.start_time_max": now.isoformat(),
        "query.search_depth": 1,
    }
    try:
        res = await http.get(f"{settings.jaeger_query_url}/api/v3/traces", params=params)
    except httpx.HTTPError:
        return None
    if res.status_code != 200:
        return None
    spans = _parse_otlp(res.text)
    if not spans:
        return None
    checkout = {}
    for s in spans:
        if s["service"] == "checkout":
            checkout.update(s["attributes"])
    return {"trace_id": spans[0]["trace_id"], "spans": spans, "checkout_attributes": checkout}


async def get_trace(trace_id: str) -> list[dict]:
    res = await http.get(f"{settings.jaeger_query_url}/api/v3/traces/{trace_id}")
    if res.status_code == 404:
        return []
    if res.status_code >= 400:
        raise upstream_failed("jaeger", f"HTTP {res.status_code}")
    return _parse_otlp(res.text)


def trace_link(trace_id: str | None) -> str | None:
    return f"{settings.jaeger_public_url}/trace/{trace_id}" if trace_id else None


# --- Prometheus ---------------------------------------------------------------

async def prom_query(query: str) -> list[dict]:
    res = await _call("prometheus", "GET", f"{settings.prometheus_url}/api/v1/query", params={"query": query})
    return res.json().get("data", {}).get("result", [])


# --- Feature flags (flagd-ui storage API) --------------------------------------

async def read_flags() -> dict:
    res = await _call("flagd-ui", "GET", f"{settings.flagd_ui_url}/api/read")
    return res.json().get("flags", {})


def effective_variant(flag: dict) -> str | None:
    """Variant the flag serves. Mirrors flagd-ui Storage.update_flag: a 3-arm targeting
    `if` carries the switchable variant at index 1, otherwise it is defaultVariant."""
    rule = (flag.get("targeting") or {}).get("if")
    return rule[1] if isinstance(rule, list) and len(rule) == 3 else flag.get("defaultVariant")


async def write_flag(name: str, variant: str) -> None:
    """Prefer flagd-ui's single-flag write (keeps the file pretty-printed); fall back to full replace."""
    res = await http.post(f"{settings.flagd_ui_url}/api/write-flag", json={"flag": name, "variant": variant})
    if res.status_code < 400:
        return
    if res.status_code != 404:
        raise upstream_failed("flagd-ui", f"HTTP {res.status_code}: {res.text[:300]}")
    flags = await read_flags()
    rule = (flags[name].get("targeting") or {}).get("if")
    if isinstance(rule, list) and len(rule) == 3:
        rule[1] = variant
    else:
        flags[name]["defaultVariant"] = variant
    await _call("flagd-ui", "POST", f"{settings.flagd_ui_url}/api/write",
                json={"data": {"$schema": FLAGD_SCHEMA, "flags": flags}})


# --- Shop services ---------------------------------------------------------------

async def get_cart(user_id: str) -> dict:
    res = await _call("frontend", "GET", f"{settings.frontend_url}/api/cart", params={"sessionId": user_id})
    return res.json()


async def empty_cart(user_id: str) -> None:
    await _call("frontend", "DELETE", f"{settings.frontend_url}/api/cart", json={"userId": user_id})


async def add_to_cart(user_id: str, product_id: str, quantity: int) -> None:
    await _call("frontend", "POST", f"{settings.frontend_url}/api/cart",
                json={"userId": user_id, "item": {"productId": product_id, "quantity": quantity}})


async def checkout(body: dict, currency: str) -> dict:
    res = await _call("frontend", "POST", f"{settings.frontend_url}/api/checkout",
                      params={"currencyCode": currency}, json=body)
    return res.json()


async def send_order_confirmation(email: str, order: dict) -> None:
    await _call("email", "POST", f"{settings.email_url}/send_order_confirmation", json={"email": email, "order": order})


async def ship_order(address: dict, items: list[dict]) -> str:
    res = await _call("shipping", "POST", f"{settings.shipping_url}/ship-order", json={"address": address, "items": items})
    return res.json()["tracking_id"]


async def ping(name: str, url: str) -> dict:
    started = datetime.now(timezone.utc)
    try:
        res = await http.get(url, timeout=5.0)
        ok = res.status_code < 500
        detail = f"HTTP {res.status_code}"
    except httpx.HTTPError as e:
        ok, detail = False, type(e).__name__
    return {"name": name, "ok": ok, "detail": detail,
            "latency_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)}
