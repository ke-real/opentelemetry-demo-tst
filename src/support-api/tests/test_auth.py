# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest

from conftest import OTHER_KEY, bearer

FAKE_ACTION = "00000000-0000-0000-0000-000000000000"


def test_missing_credentials_is_401_with_challenge(client):
    r = client.get("/v1/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("kw", [
    {"aud": "client-mcp"},
    {"iss": "http://evil.test/realms/astronomy"},
    {"exp_in": -120},
    {"key": OTHER_KEY},
])
def test_invalid_tokens_are_rejected(client, kw):
    assert client.get("/v1/me", headers=bearer(**kw)).status_code == 401


def test_identity_and_roles_come_from_token_with_hierarchy(client):
    r = client.get("/v1/me", headers=bearer(user="admin", roles=("Support.Admin",)))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "admin" and body["kind"] == "user"
    assert set(body["roles"]) == {"Support.Admin", "Support.Agent", "Shop.Viewer"}


def test_actor_header_cannot_impersonate_token_user(client):
    headers = bearer(user="office", roles=("Shop.Viewer",)) | {"X-Support-Actor": "admin"}
    assert client.get("/v1/me", headers=headers).json()["name"] == "office"


def test_api_key_identity_is_fixed_and_actor_only_recorded(client):
    r = client.get("/v1/me", headers={"X-API-Key": "test-key", "X-Support-Actor": "mcp/jane"})
    assert r.json() == {"name": "service", "kind": "service", "on_behalf": "mcp/jane", "roles": ["Shop.Viewer"]}
    assert client.get("/v1/me", headers={"X-API-Key": "wrong"}).status_code == 401


def test_scalar_roles_claim_is_one_role(client):
    r = client.get("/v1/me", headers=bearer(user="admin", roles=(), realm_access={"roles": "Support.Admin"}))
    assert "Support.Admin" in r.json()["roles"]


def test_action_types_show_what_caller_may_propose(client):
    types = client.get("/v1/action-types", headers=bearer(user="agent", roles=("Support.Agent",))).json()["action_types"]
    allowed = {t["type"]: t["allowed"] for t in types}
    assert allowed == {"resend_order_confirmation": True, "reship_order": True, "reset_cart": True,
                       "replay_order": False, "set_feature_flag": False}


@pytest.mark.parametrize("user,roles,type_,expected", [
    ("office", ("Shop.Viewer",), "reset_cart", 403),
    ("agent", ("Support.Agent",), "set_feature_flag", 403),
    ("agent", ("Support.Agent",), "replay_order", 403),
    ("approver", ("Support.Approver",), "reset_cart", 403),
])
def test_propose_requires_role_of_action_type(client, user, roles, type_, expected):
    r = client.post("/v1/actions", headers=bearer(user=user, roles=roles), json={"type": type_, "params": {}})
    assert r.status_code == expected
    assert r.json()["error"]["code"] == "forbidden"


def test_unknown_action_type_is_422_before_role_check(client):
    r = client.post("/v1/actions", headers=bearer(roles=("Shop.Viewer",)), json={"type": "nope", "params": {}})
    assert r.status_code == 422


@pytest.mark.parametrize("path", [f"/v1/actions/{FAKE_ACTION}/approve", f"/v1/actions/{FAKE_ACTION}/reject"])
def test_only_approvers_decide(client, path):
    assert client.post(path, headers=bearer(user="agent", roles=("Support.Agent",))).status_code == 403


def test_viewer_cannot_execute_or_list_actions_or_audit(client):
    h = bearer(user="office", roles=("Shop.Viewer",))
    assert client.post(f"/v1/actions/{FAKE_ACTION}/execute", headers=h).status_code == 403
    assert client.get("/v1/actions", headers=h).status_code == 403
    assert client.get("/v1/audit", headers=bearer(user="agent", roles=("Support.Agent",))).status_code == 403


@pytest.mark.parametrize("path", ["/v1/orders/x?include_pii=true", "/v1/orders/x/timeline?include_pii=true"])
def test_pii_requires_admin(client, path):
    r = client.get(path, headers=bearer(user="agent", roles=("Support.Agent",)))
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required_roles"] == ["Support.Admin"]


def test_openapi_declares_security_schemes_not_header_params(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["components"]["securitySchemes"]) == {"HTTPBearer", "APIKeyHeader"}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            names = {p["name"].lower() for p in op.get("parameters", [])}
            assert not names & {"x-api-key", "x-support-actor", "authorization"}, f"{method} {path}"
            assert op.get("security"), f"{method} {path} has no security requirement"
