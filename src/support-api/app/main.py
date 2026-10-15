# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import actions, clients, db, diagnostics, orders
from .config import settings
from .errors import ApiError

logging.basicConfig(level=logging.INFO)

DESCRIPTION = """
Internal support API for the Astronomy Shop. Not routed through frontend-proxy; meant for support
tooling such as a support MCP server.

* **Reads** (orders, timelines, carts, flags, diagnostics) are safe to call freely. PII is redacted
  unless `include_pii=true`.
* **Writes** go through actions with a human in the loop: `proposeAction` (dry run with plan) →
  `approveAction` / `rejectAction` → `executeAction`. Only the approved plan is executed.

Auth: `X-API-Key` header. Attribution: `X-Support-Actor` header (who is calling: user or agent id).
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.migrate()
    await db.pool.open()
    yield
    await db.pool.close()
    await clients.http.aclose()


def require_api_key(x_api_key: str | None = Header(None, description="Shared API key (SUPPORT_API_KEY).")):
    if settings.api_key and not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key")


def actor(x_support_actor: str | None = Header(None, description="Who is acting, e.g. 'mcp:support/jane.doe'.")) -> str:
    return x_support_actor or "anonymous"


app = FastAPI(title="Astronomy Shop Support API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan,
              generate_unique_id_function=lambda route: route.name)


@app.exception_handler(ApiError)
async def api_error(_: Request, e: ApiError):
    return JSONResponse(status_code=e.status, content={"error": {"code": e.code, "message": e.message, "details": e.details}})


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, e: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Request validation failed",
                                                            "details": {"errors": e.errors()}}})


@app.get("/healthz", tags=["meta"], include_in_schema=False)
async def healthz():
    return {"status": "ok"}


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


# --- Orders --------------------------------------------------------------------

@v1.get("/orders", tags=["orders"], summary="Search orders, newest first")
async def searchOrders(limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
                       status: str | None = Query(None, description="RECORDED, RESHIPPED, ..."),
                       user_id: str | None = None, product_id: str | None = None,
                       since: datetime | None = Query(None, description="ISO 8601; orders created at or after")):
    return await orders.search(limit, offset, status, user_id, product_id, since)


@v1.get("/orders/{order_id}", tags=["orders"], summary="Order details: items, totals, shipments, customer, trace link")
async def getOrder(order_id: str, include_pii: bool = False):
    return await orders.get(order_id, include_pii)


@v1.get("/orders/{order_id}/timeline", tags=["orders"],
        summary="What happened to this order: checkout trace spans merged with support events")
async def getOrderTimeline(order_id: str, detail: Literal["summary", "full"] = "summary", include_pii: bool = False):
    return await orders.timeline(order_id, detail, include_pii)


@v1.get("/orders/{order_id}/shopper-replay", tags=["orders"],
        summary="Shopper MCP tool calls that would re-create this order (for executing via the shopper MCP)")
async def getShopperReplay(order_id: str, user_id: str | None = None, email: str | None = None, currency: str | None = None):
    return await actions.shopper_replay(order_id, user_id, email, currency)


# --- Carts, flags, diagnostics ------------------------------------------------------

@v1.get("/carts/{user_id}", tags=["carts"], summary="Current cart of a shop user")
async def getCart(user_id: str):
    return await clients.get_cart(user_id)


@v1.get("/flags", tags=["flags"], summary="All feature flags with their current variant")
async def listFlags(active_only: bool = False):
    flags = await diagnostics.flag_states()
    return {"flags": [f for f in flags if f["active"]] if active_only else flags}


@v1.get("/diagnostics", tags=["diagnostics"],
        summary="Snapshot: active failure flags, per-service error ratio and p95, dependency health")
async def getDiagnostics(window: str = Query("5m", description="Prometheus range, e.g. 5m, 15m, 1h"),
                         error_threshold: float = Query(0.05, ge=0, le=1)):
    return await diagnostics.snapshot(window, error_threshold)


# --- Actions (human in the loop) -------------------------------------------------------

class ProposeActionRequest(BaseModel):
    type: str = Field(description="One of GET /v1/action-types")
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(None, description="Why; shown to the approver, e.g. ticket key")


class DecisionRequest(BaseModel):
    comment: str | None = None


@v1.get("/action-types", tags=["actions"], summary="Available actions with risk level and params JSON schema")
async def listActionTypes():
    return {"action_types": actions.describe_types()}


@v1.post("/actions", tags=["actions"], status_code=201,
         summary="Propose an action: validates and dry-runs it, returns the plan in PENDING_APPROVAL")
async def proposeAction(body: ProposeActionRequest, who: str = Depends(actor),
                        idempotency_key: str | None = Header(None, description="Repeat-safe proposals: same key returns the same action")):
    action, created = await actions.propose(body.type, body.params, body.reason, who, idempotency_key)
    return action if created else JSONResponse(status_code=200, content=action)


@v1.get("/actions", tags=["actions"], summary="List actions")
async def listActions(status: str | None = Query(None, description="PENDING_APPROVAL, APPROVED, REJECTED, EXECUTING, EXECUTED, FAILED, EXPIRED"),
                      type: str | None = None, limit: int = Query(50, ge=1, le=200)):
    return {"actions": await actions.search(status, type, limit)}


@v1.get("/actions/{action_id}", tags=["actions"], summary="Get an action")
async def getAction(action_id: str):
    return await actions.get(action_id)


@v1.post("/actions/{action_id}/approve", tags=["actions"], summary="Approve a proposed action (human decision)")
async def approveAction(action_id: str, body: DecisionRequest | None = None, who: str = Depends(actor)):
    return await actions.decide(action_id, True, who, body.comment if body else None)


@v1.post("/actions/{action_id}/reject", tags=["actions"], summary="Reject a proposed action")
async def rejectAction(action_id: str, body: DecisionRequest | None = None, who: str = Depends(actor)):
    return await actions.decide(action_id, False, who, body.comment if body else None)


@v1.post("/actions/{action_id}/execute", tags=["actions"],
         summary="Execute an APPROVED action. Idempotent: re-calling returns the stored outcome")
async def executeAction(action_id: str, who: str = Depends(actor)):
    return await actions.execute(action_id, who)


@v1.get("/audit", tags=["actions"], summary="Audit log of support actions")
async def listAudit(action_id: str | None = None, limit: int = Query(100, ge=1, le=500)):
    if action_id:
        rows = await db.fetch_all("SELECT * FROM support.audit_log WHERE action_id = %s ORDER BY at DESC LIMIT %s", action_id, limit)
    else:
        rows = await db.fetch_all("SELECT * FROM support.audit_log ORDER BY at DESC LIMIT %s", limit)
    return {"entries": [r | {"at": r["at"].isoformat(), "action_id": str(r["action_id"]) if r["action_id"] else None} for r in rows]}


app.include_router(v1)
