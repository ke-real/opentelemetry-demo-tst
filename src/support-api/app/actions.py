# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Support actions: every state change is proposed (dry run), approved by a human, then executed."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from . import clients, db, orders
from .config import settings
from .errors import ApiError, not_found

SHOPPER_MCP = "astronomy-shop-mcp"


# --- Parameter models (exposed as JSON Schema via GET /v1/action-types) -----------

class Address(BaseModel):
    street_address: str
    city: str
    state: str
    country: str
    zip_code: str


class ResendConfirmationParams(BaseModel):
    order_id: str
    email: str | None = Field(None, description="Recipient. Defaults to the email recorded on the checkout trace.")


class ReshipParams(BaseModel):
    order_id: str
    address: Address | None = Field(None, description="Ship to this address instead of the original one.")


class ResetCartParams(BaseModel):
    user_id: str = Field(description="Cart owner (the shop's session/user id).")


class ReplayParams(BaseModel):
    order_id: str
    user_id: str | None = Field(None, description="Shop user to place the new order as. Defaults to a fresh 'support-replay-*' user.")
    email: str | None = Field(None, description="Defaults to the original order's email, else support-replay@example.com.")
    currency: str | None = Field(None, description="ISO 4217 code. Defaults to the original order currency.")


class SetFeatureFlagParams(BaseModel):
    flag: str = Field(description="Flag key, e.g. paymentFailure.")
    variant: str = Field(description="Variant to make the default, e.g. 'off'.")


# --- Shopper MCP replay (shared by the replay_order action and GET /orders/{id}/shopper-replay) ----

async def shopper_replay(order_id: str, user_id: str | None, email: str | None, currency: str | None) -> dict:
    row, trace = await orders.load(order_id)
    if not row["items"]:
        raise ApiError(409, "order_has_no_items", f"Order '{order_id}' has no recorded items to replay")
    attrs = (trace or {}).get("checkout_attributes", {})
    shipment = orders.original_shipment(row, await orders.reship_tracking_ids(order_id))
    user_id = user_id or f"support-replay-{uuid.uuid4().hex[:8]}"
    email = email or attrs.get("user.email") or "support-replay@example.com"
    currency = currency or attrs.get("demo.user_context.selected_currency") or row["items"][0]["currency_code"]
    address = {"streetAddress": shipment["street_address"], "city": shipment["city"], "state": shipment["state"],
               "country": shipment["country"], "zipCode": shipment["zip_code"]} if shipment else None
    checkout_person = {"userId": user_id, "userCurrency": currency, "address": address, "email": email,
                       "creditCard": settings.replay_card}
    steps = [{"tool": "empty_cart", "arguments": {"user_id": user_id}}]
    steps += [{"tool": "add_to_cart", "arguments": {"user_id": user_id, "product_id": i["product_id"], "quantity": i["quantity"]}}
              for i in row["items"]]
    steps.append({"tool": "checkout", "arguments": {"checkout_person": checkout_person}})
    return {"mcp_server": SHOPPER_MCP, "source_order_id": order_id, "user_id": user_id, "currency": currency,
            "steps": steps}


def _mask_replay(replay: dict) -> dict:
    steps = []
    for s in replay["steps"]:
        if s["tool"] == "checkout":
            person = dict(s["arguments"]["checkout_person"])
            person["email"] = orders.mask_email(person["email"])
            person["creditCard"] = {"creditCardNumber": "****-0454 (test card)"}
            s = {"tool": "checkout", "arguments": {"checkout_person": person}}
        steps.append(s)
    return replay | {"steps": steps}


# --- Planners and executors -------------------------------------------------------

async def plan_resend(p: ResendConfirmationParams) -> dict:
    row, trace = await orders.load(p.order_id)
    checkout_email = (trace or {}).get("checkout_attributes", {}).get("user.email")
    email = p.email or checkout_email
    if not email:
        raise ApiError(422, "email_unknown", "No email found on the checkout trace; pass params.email explicitly")
    shipment = orders.original_shipment(row, await orders.reship_tracking_ids(p.order_id))
    if not shipment:
        raise ApiError(409, "order_has_no_shipment", "Order has no shipping record to build the confirmation from")
    return {
        "summary": f"Re-send order confirmation for {p.order_id} to {orders.mask_email(email)}",
        "steps": [{"service": "email", "call": "POST /send_order_confirmation", "order_id": p.order_id}],
        "warnings": ["Recipient differs from the email used at checkout"] if checkout_email and email != checkout_email else [],
        "order_id": p.order_id,
        "inputs": {"email": email, "order": orders.order_result_payload(row, shipment)},
    }


async def exec_resend(inputs: dict, action_id) -> dict:
    await clients.send_order_confirmation(inputs["email"], inputs["order"])
    order_id = inputs["order"]["order_id"]
    await db.order_event(order_id, "CONFIRMATION_RESENT", {"email": orders.mask_email(inputs["email"])}, action_id)
    return {"order_id": order_id, "sent_to": orders.mask_email(inputs["email"])}


async def plan_reship(p: ReshipParams) -> dict:
    row, _ = await orders.load(p.order_id, enrich=False)
    original = orders.original_shipment(row, await orders.reship_tracking_ids(p.order_id))
    if not original and not p.address:
        raise ApiError(409, "order_has_no_shipment", "Order has no shipping address; pass params.address")
    address = p.address.model_dump() if p.address else {k: original[k] for k in ("street_address", "city", "state", "country", "zip_code")}
    items = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in row["items"]]
    return {
        "summary": f"Create a replacement shipment for {p.order_id} ({sum(i['quantity'] for i in items)} items) to {address['city']}, {address['country']}",
        "steps": [{"service": "shipping", "call": "POST /ship-order", "items": items},
                  {"service": "postgres", "call": "INSERT accounting.shipping (zero cost) + set order status RESHIPPED"}],
        "warnings": ["Creates a new tracking id; the customer is not charged again"]
                    + (["Shipping to a different address than the original"] if p.address else []),
        "order_id": p.order_id,
        "inputs": {"order_id": p.order_id, "address": address, "items": items,
                   "currency": original["currency_code"] if original else "USD"},
    }


async def exec_reship(inputs: dict, action_id) -> dict:
    tracking_id = await clients.ship_order(inputs["address"], inputs["items"])
    a = inputs["address"]
    await db.execute(
        "INSERT INTO accounting.shipping (shipping_tracking_id, shipping_cost_currency_code, shipping_cost_units, shipping_cost_nanos,"
        " street_address, city, state, country, zip_code, order_id) VALUES (%s, %s, 0, 0, %s, %s, %s, %s, %s, %s)",
        tracking_id, inputs["currency"], a["street_address"], a["city"], a["state"], a["country"], a["zip_code"], inputs["order_id"])
    await db.execute('UPDATE accounting."order" SET status = %s WHERE order_id = %s', "RESHIPPED", inputs["order_id"])
    await db.order_event(inputs["order_id"], "RESHIPPED", {"tracking_id": tracking_id}, action_id)
    return {"order_id": inputs["order_id"], "tracking_id": tracking_id}


async def plan_reset_cart(p: ResetCartParams) -> dict:
    cart = await clients.get_cart(p.user_id)
    items = [{"product_id": i.get("productId"), "quantity": i.get("quantity")} for i in cart.get("items", [])]
    return {
        "summary": f"Empty the cart of user {p.user_id} ({len(items)} line items)",
        "steps": [{"service": "frontend", "call": "DELETE /api/cart", "current_items": items}],
        "warnings": [] if items else ["Cart is already empty"],
        "inputs": {"user_id": p.user_id},
    }


async def exec_reset_cart(inputs: dict, action_id) -> dict:
    await clients.empty_cart(inputs["user_id"])
    return {"user_id": inputs["user_id"], "cart_emptied": True}


async def plan_replay(p: ReplayParams) -> dict:
    replay = await shopper_replay(p.order_id, p.user_id, p.email, p.currency)
    warnings = ["Places a NEW real order (checkout, payment with the demo test card, shipping, email, Kafka)"]
    if p.user_id and not p.user_id.startswith("support-replay-"):
        warnings.append(f"Empties the current cart of user {p.user_id} first")
    return {
        "summary": f"Replay order {p.order_id} as user {replay['user_id']} via the shopper flow ({len(replay['steps']) - 2} products)",
        "steps": _mask_replay(replay)["steps"],
        "warnings": warnings,
        "order_id": p.order_id,
        "inputs": replay,
    }


async def exec_replay(inputs: dict, action_id) -> dict:
    user_id, results = inputs["user_id"], []
    for step in inputs["steps"]:
        args = step["arguments"]
        if step["tool"] == "empty_cart":
            await clients.empty_cart(user_id)
        elif step["tool"] == "add_to_cart":
            await clients.add_to_cart(user_id, args["product_id"], args["quantity"])
        elif step["tool"] == "checkout":
            res = await clients.checkout(args["checkout_person"], inputs["currency"])
            results.append(res)
    new_order = results[-1] if results else {}
    new_order_id = new_order.get("orderId")
    await db.order_event(inputs["source_order_id"], "REPLAYED", {"new_order_id": new_order_id, "user_id": user_id}, action_id)
    return {"source_order_id": inputs["source_order_id"], "new_order_id": new_order_id, "user_id": user_id,
            "shipping_tracking_id": new_order.get("shippingTrackingId")}


async def plan_set_flag(p: SetFeatureFlagParams) -> dict:
    flags = await clients.read_flags()
    flag = flags.get(p.flag)
    if not flag:
        raise not_found("flag", p.flag)
    if p.variant not in flag.get("variants", {}):
        raise ApiError(422, "unknown_variant", f"Flag '{p.flag}' has no variant '{p.variant}'",
                       {"variants": list(flag.get("variants", {}))})
    current = clients.effective_variant(flag)
    return {
        "summary": f"Set feature flag {p.flag}: {current} -> {p.variant}",
        "steps": [{"service": "flagd-ui", "call": "POST /api/write-flag", "flag": p.flag, "from": current, "to": p.variant,
                   "value": flag["variants"][p.variant]}],
        "warnings": ["No change: flag already has this variant"] if current == p.variant else [],
        "inputs": {"flag": p.flag, "from": current, "to": p.variant},
    }


async def exec_set_flag(inputs: dict, action_id) -> dict:
    flags = await clients.read_flags()
    current = clients.effective_variant(flags.get(inputs["flag"], {}))
    if current != inputs["from"]:
        raise ApiError(409, "flag_changed", f"Flag changed since approval (now '{current}', approved from '{inputs['from']}'); propose again")
    await clients.write_flag(inputs["flag"], inputs["to"])
    return {"flag": inputs["flag"], "from": inputs["from"], "to": inputs["to"]}


@dataclass(frozen=True)
class ActionType:
    name: str
    description: str
    risk: str
    params: type[BaseModel]
    plan: Callable[[BaseModel], Awaitable[dict]]
    execute: Callable[[dict, object], Awaitable[dict]]


ACTION_TYPES = {a.name: a for a in [
    ActionType("resend_order_confirmation", "Re-send the order confirmation email.", "low",
               ResendConfirmationParams, plan_resend, exec_resend),
    ActionType("reship_order", "Create a replacement shipment (new tracking id, no charge).", "medium",
               ReshipParams, plan_reship, exec_reship),
    ActionType("reset_cart", "Empty a user's shopping cart (e.g. stuck or corrupted cart).", "medium",
               ResetCartParams, plan_reset_cart, exec_reset_cart),
    ActionType("replay_order", "Place a new order with the same items through the shopper flow (reproduce an issue or re-create a lost order).", "high",
               ReplayParams, plan_replay, exec_replay),
    ActionType("set_feature_flag", "Change a feature flag's default variant (e.g. disable a failure flag).", "high",
               SetFeatureFlagParams, plan_set_flag, exec_set_flag),
]}


def describe_types() -> list[dict]:
    return [{"type": a.name, "description": a.description, "risk": a.risk, "params_schema": a.params.model_json_schema()}
            for a in ACTION_TYPES.values()]


# --- Lifecycle ---------------------------------------------------------------------

def public(row: dict) -> dict:
    plan = {k: v for k, v in (row["plan"] or {}).items() if k != "inputs"}
    out = {k: row[k] for k in ("type", "status", "params", "reason", "requested_by", "decided_by", "decision_comment",
                               "result", "error", "idempotency_key")}
    for k in ("created_at", "expires_at", "decided_at", "executed_at"):
        out[k] = row[k].isoformat() if row[k] else None
    return {"id": str(row["id"]), **out, "plan": plan}


async def _get(action_id: str) -> dict:
    try:
        uuid.UUID(action_id)
    except ValueError:
        raise not_found("action", action_id)
    row = await db.fetch_one("SELECT * FROM support.actions WHERE id = %s", action_id)
    if not row:
        raise not_found("action", action_id)
    if row["status"] in ("PENDING_APPROVAL", "APPROVED") and row["expires_at"] < datetime.now(timezone.utc):
        row = await db.fetch_one("UPDATE support.actions SET status = 'EXPIRED' WHERE id = %s RETURNING *", action_id)
    return row


async def propose(type_: str, params: dict, reason: str | None, actor: str, idempotency_key: str | None) -> tuple[dict, bool]:
    if idempotency_key:
        existing = await db.fetch_one("SELECT * FROM support.actions WHERE idempotency_key = %s", idempotency_key)
        if existing:
            return public(existing), False
    action_type = ACTION_TYPES.get(type_)
    if not action_type:
        raise ApiError(422, "unknown_action_type", f"Unknown action type '{type_}'", {"known": list(ACTION_TYPES)})
    try:
        parsed = action_type.params.model_validate(params)
    except ValidationError as e:
        raise ApiError(422, "invalid_params", "Invalid action params", {"errors": e.errors(include_url=False)})
    plan = await action_type.plan(parsed)
    row = await db.fetch_one(
        "INSERT INTO support.actions (type, status, params, plan, reason, requested_by, idempotency_key, expires_at)"
        " VALUES (%s, 'PENDING_APPROVAL', %s, %s, %s, %s, %s, %s) RETURNING *",
        type_, Jsonb(parsed.model_dump(mode="json")), Jsonb(plan), reason, actor, idempotency_key,
        datetime.now(timezone.utc) + timedelta(minutes=settings.action_ttl_minutes))
    await db.audit(actor, "PROPOSED", row["id"], {"type": type_, "summary": plan["summary"]})
    return public(row), True


async def decide(action_id: str, approve: bool, actor: str, comment: str | None) -> dict:
    row = await _get(action_id)
    if row["status"] != "PENDING_APPROVAL":
        raise ApiError(409, "invalid_state", f"Action is {row['status']}, expected PENDING_APPROVAL", {"status": row["status"]})
    if approve and settings.require_distinct_approver and actor == row["requested_by"]:
        raise ApiError(403, "approver_must_differ", "The requester cannot approve their own action")
    status = "APPROVED" if approve else "REJECTED"
    row = await db.fetch_one(
        "UPDATE support.actions SET status = %s, decided_by = %s, decision_comment = %s, decided_at = now()"
        " WHERE id = %s AND status = 'PENDING_APPROVAL' RETURNING *", status, actor, comment, action_id)
    if not row:
        raise ApiError(409, "invalid_state", "Action was decided concurrently")
    await db.audit(actor, status, row["id"], {"comment": comment})
    return public(row)


async def execute(action_id: str, actor: str) -> dict:
    row = await _get(action_id)
    if row["status"] in ("EXECUTED", "FAILED"):
        return public(row)
    claimed = await db.fetch_one(
        "UPDATE support.actions SET status = 'EXECUTING' WHERE id = %s AND status = 'APPROVED' RETURNING *", action_id)
    if not claimed:
        raise ApiError(409, "invalid_state", f"Action is {row['status']}, expected APPROVED", {"status": row["status"]})
    action_type = ACTION_TYPES[row["type"]]
    try:
        result = await action_type.execute(row["plan"]["inputs"], row["id"])
        row = await db.fetch_one("UPDATE support.actions SET status = 'EXECUTED', executed_at = now(), result = %s"
                                 " WHERE id = %s RETURNING *", Jsonb(result), action_id)
        await db.audit(actor, "EXECUTED", row["id"], result)
    except Exception as e:
        message = f"{e.code}: {e.message}" if isinstance(e, ApiError) else f"{type(e).__name__}: {e}"
        row = await db.fetch_one("UPDATE support.actions SET status = 'FAILED', executed_at = now(), error = %s"
                                 " WHERE id = %s RETURNING *", message, action_id)
        await db.audit(actor, "FAILED", row["id"], {"error": message})
    return public(row)


async def get(action_id: str) -> dict:
    return public(await _get(action_id))


async def search(status: str | None, type_: str | None, limit: int) -> list[dict]:
    where, params = [], []
    if status:
        where.append("status = %s"); params.append(status)
    if type_:
        where.append("type = %s"); params.append(type_)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = await db.fetch_all("SELECT * FROM support.actions" + clause + " ORDER BY created_at DESC LIMIT %s", *params, limit)
    return [public(r) for r in rows]
