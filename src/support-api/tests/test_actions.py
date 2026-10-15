# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Action lifecycle authorization with the database stubbed out."""

from datetime import datetime, timedelta, timezone

import pytest

from app import actions, db
from app.auth import ADMIN, AGENT, ALL_ROLES, APPROVER, Principal, expand
from app.errors import ApiError

ACTION_ID = "11111111-1111-1111-1111-111111111111"


def user(name, *roles):
    return Principal(name, expand(roles), "user")


def row(**over):
    now = datetime.now(timezone.utc)
    base = {"id": ACTION_ID, "type": "reset_cart", "status": "PENDING_APPROVAL", "params": {}, "reason": None,
            "plan": {"summary": "s", "inputs": {"user_id": "u"}}, "requested_by": "agent", "decided_by": None,
            "decision_comment": None, "result": None, "error": None, "idempotency_key": None,
            "created_at": now, "expires_at": now + timedelta(minutes=30), "decided_at": None, "executed_at": None}
    return base | over


@pytest.fixture
def store(monkeypatch):
    state = {"row": row(), "audit": []}

    async def fake_get(action_id):
        return state["row"]

    async def fake_fetch_one(query, *params):
        if query.lstrip().startswith("UPDATE"):
            if "status = 'EXECUTING'" in query:
                state["row"] = state["row"] | {"status": "EXECUTING"}
            elif "status = 'EXECUTED'" in query:
                state["row"] = state["row"] | {"status": "EXECUTED", "result": {"ok": True}}
            else:
                state["row"] = state["row"] | {"status": params[0], "decided_by": params[1]}
            return state["row"]
        if "idempotency_key" in query:
            return state.get("idem")
        return state["row"]

    async def fake_audit(actor, event, action_id=None, details=None):
        state["audit"].append((event, actor))

    async def fake_exec(inputs, action_id):
        return {"ok": True}

    monkeypatch.setattr(actions, "_get", fake_get)
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db, "audit", fake_audit)
    monkeypatch.setitem(actions.ACTION_TYPES, "reset_cart",
                        actions.ACTION_TYPES["reset_cart"].__class__(**{**actions.ACTION_TYPES["reset_cart"].__dict__,
                                                                         "execute": fake_exec}))
    return state


async def test_requester_cannot_approve_own_action(store):
    with pytest.raises(ApiError) as e:
        await actions.decide(ACTION_ID, True, user("agent", AGENT, APPROVER), None)
    assert e.value.code == "approver_must_differ"


async def test_other_approver_can_approve(store):
    out = await actions.decide(ACTION_ID, True, user("approver", APPROVER), "ok")
    assert out["status"] == "APPROVED" and out["decided_by"] == "approver"
    assert store["audit"] == [("APPROVED", "approver")]


async def test_api_key_callers_cannot_self_approve_by_changing_actor(store):
    store["row"] = row(requested_by="service")
    svc = Principal("service", expand([AGENT, APPROVER]), "service", on_behalf="someone-else")
    with pytest.raises(ApiError) as e:
        await actions.decide(ACTION_ID, True, svc, None)
    assert e.value.code == "approver_must_differ"


async def test_anonymous_dev_mode_can_approve_own(store):
    store["row"] = row(requested_by="anonymous")
    out = await actions.decide(ACTION_ID, True, Principal("anonymous", ALL_ROLES, "anonymous"), None)
    assert out["status"] == "APPROVED"


async def test_only_requester_or_admin_executes(store):
    store["row"] = row(status="APPROVED")
    with pytest.raises(ApiError) as e:
        await actions.execute(ACTION_ID, user("other-agent", AGENT))
    assert e.value.status == 403
    assert (await actions.execute(ACTION_ID, user("agent", AGENT)))["status"] == "EXECUTED"


async def test_admin_executes_anyones_action(store):
    store["row"] = row(status="APPROVED")
    assert (await actions.execute(ACTION_ID, user("admin", ADMIN)))["status"] == "EXECUTED"
    assert store["audit"][-1] == ("EXECUTED", "admin")


async def test_unknown_type_returns_stored_outcome_or_clear_error(store):
    store["row"] = row(type="removed_type", status="EXECUTED")
    assert (await actions.execute(ACTION_ID, user("admin", ADMIN)))["status"] == "EXECUTED"
    store["row"] = row(type="removed_type", status="APPROVED")
    with pytest.raises(ApiError) as e:
        await actions.execute(ACTION_ID, user("admin", ADMIN))
    assert e.value.code == "unknown_action_type" and store["row"]["status"] == "APPROVED"


async def test_idempotency_key_of_another_requester_conflicts(store):
    store["idem"] = row(requested_by="someone-else", idempotency_key="k")
    with pytest.raises(ApiError) as e:
        await actions.propose("reset_cart", {"user_id": "u"}, None, user("agent", AGENT), "k")
    assert e.value.code == "idempotency_key_conflict"
