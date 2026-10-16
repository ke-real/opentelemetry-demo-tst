# Support API

Internal support API for the Astronomy Shop. It answers support questions (orders, what
happened to an order, carts, flags, "what's broken right now") and performs support actions
**only after a human approves them**. It's built to be wrapped by a support MCP server.

It is **not** routed through `frontend-proxy` and is invisible to the shop UI.

| | |
|---|---|
| From the host | `http://localhost:8020` |
| From the Docker network | `http://support-api:8020` |
| OpenAPI | `GET /openapi.json` (also committed: [`openapi.json`](openapi.json)), Swagger UI at `/docs` |
| Auth (users) | `Authorization: Bearer <access token>` from Keycloak with `aud: support-api`. Identity and roles come from the token; see [Authentication and roles](#authentication-and-roles) |
| Auth (services) | `X-API-Key: <SUPPORT_API_KEY>` → roles from `SUPPORT_API_KEY_ROLES` (default read-only `Shop.Viewer`) |
| Shopper MCP (customer flows) | `http://localhost:8011/mcp` (streamable HTTP) |

Start it with the agent and auth layers (Keycloak needs `.env.local` and the realm file, see `src/keycloak/README.md`):

```bash
docker compose --env-file .env --env-file .env.local -f compose.yaml -f compose.full.yaml -f compose.observability.yaml \
  -f compose.agent.yaml -f compose.auth.yaml -f compose.extras.yaml up -d --build support-api
```

## Authentication and roles

Callers send an OIDC access token. The API checks:
- the signature, against keys from `OIDC_JWKS_URL`
- `iss` = `OIDC_ISSUER`
- `aud` contains `support-api`
- expiry

It then reads the user from `preferred_username` and the roles from `realm_access.roles` (configurable with
`OIDC_USERNAME_CLAIM` / `OIDC_ROLES_CLAIM`, so Entra or Auth0 is a config change).

A support MCP gets such a token by **exchanging the user's MCP token** at Keycloak (`client-mcp` → `audience=support-api`,
`scope=support-api-audience`). The call is then made *as the user*. A token issued for `client-mcp` itself is rejected (401).

| Role | Can |
|---|---|
| `Shop.Viewer` | all reads, PII redacted; `listActionTypes` |
| `Support.Agent` (includes Viewer) | propose `resend_order_confirmation`, `reship_order`, `reset_cart`; list/get actions; execute **own** approved actions |
| `Support.Approver` (includes Viewer) | approve / reject others' actions; list/get actions; audit log |
| `Support.Admin` (includes Agent) | propose `replay_order`, `set_feature_flag`; `include_pii=true`; unmasked shopper replay; approve / reject any action, **including their own**; execute anyone's approved action; audit log |

- **Four eyes:** with `SUPPORT_REQUIRE_DISTINCT_APPROVER=true` (default), nobody approves their own proposal, except
  `Support.Admin`: admins may approve (and reject) any action, including their own.
- The role hierarchy is applied by the API too, so it also holds for providers without composite roles.
- `GET /v1/me` shows the caller's name and effective roles. `listActionTypes` marks each type `allowed` for the caller.
- API-key callers all act as the identity `service`. Their optional `X-Support-Actor` is recorded in the audit
  log (`service (on behalf of <value>)`) but never changes who they are: it can't get around the four-eyes or
  requester checks, and it never overrides a token's user.
- With `SUPPORT_AUTH_DISABLED=true`, the four-eyes rule is skipped, since every caller is the same anonymous user.
- `SUPPORT_AUTH_DISABLED=true` (local development only) makes every caller an anonymous user with all roles.

On start it migrates the database (idempotent; see `app/migrations/`):
- adds `created_at`, `status`, `user_id`, `trace_id` to `accounting."order"`
- creates schema `support` (`actions`, `audit_log`, `order_events`)
- creates DB role `support_user` with least-privilege grants

## Endpoints and suggested MCP tools

`operationId`s are stable; use them as tool names or map them yourself.

### Read-only (safe for the agent to call freely)

| operationId | Method + path | Use |
|---|---|---|
| `searchOrders` | `GET /v1/orders?limit&offset&status&user_id&product_id&since` | Find orders, newest first |
| `getOrder` | `GET /v1/orders/{order_id}?include_pii=false` | Items, totals, shipments, customer, Jaeger trace link |
| `getOrderTimeline` | `GET /v1/orders/{order_id}/timeline?detail=summary\|full` | Checkout trace spans + support events, chronological, with `error_count` |
| `getShopperReplay` | `GET /v1/orders/{order_id}/shopper-replay?user_id&email&currency` | **Exact shopper MCP tool calls** that re-create the order (see below) |
| `getCart` | `GET /v1/carts/{user_id}` | A user's current cart |
| `listFlags` | `GET /v1/flags?active_only=false` | Feature flags with their *effective* variant (targeting-aware) |
| `getDiagnostics` | `GET /v1/diagnostics?window=5m&error_threshold=0.05` | `findings[]` (plain sentences), per-service error ratio / p95, active flags, dependency health. **Good first call in an incident** |
| `listActionTypes` | `GET /v1/action-types` | Available actions, risk level, params JSON Schema |
| `listActions` / `getAction` | `GET /v1/actions`, `GET /v1/actions/{id}` | Action status, e.g. waiting for approval |
| `listAudit` | `GET /v1/audit?action_id` | Who proposed, approved and executed what |

### Actions (state-changing, human in the loop)

| Action `type` | Risk | Params | Effect |
|---|---|---|---|
| `resend_order_confirmation` | low | `order_id`, `email?` | Calls email service `/send_order_confirmation` |
| `reship_order` | medium | `order_id`, `address?` | Shipping `/ship-order`, adds a zero-cost shipment row, sets order `RESHIPPED` |
| `reset_cart` | medium | `user_id` | Empties the user's cart |
| `replay_order` | high | `order_id`, `user_id?`, `email?`, `currency?` | Places a **new real order** with the same items through the shopper flow (test card) |
| `set_feature_flag` | high | `flag`, `variant` | Switches a flag, e.g. `paymentFailure` → `off` |

Lifecycle:

```
POST /v1/actions                    → 201 PENDING_APPROVAL  (dry run: plan.summary, plan.steps, plan.warnings)
POST /v1/actions/{id}/approve       → APPROVED              (or /reject → REJECTED)
POST /v1/actions/{id}/execute       → EXECUTED | FAILED     (result / error)
```

- **Only the approved plan runs.** Inputs are resolved at proposal time and stored; execute does not re-read params.
  `set_feature_flag` additionally refuses (`409 flag_changed`) if the flag changed since the proposal.
- Proposals expire after `SUPPORT_ACTION_TTL_MINUTES` (default 30) → `EXPIRED`.
- `Idempotency-Key` header on propose: the same key returns the existing action (HTTP 200 instead of 201).
  Reusing another requester's key returns `409 idempotency_key_conflict`.
- `execute` is idempotent: calling it again on `EXECUTED`/`FAILED` returns the stored outcome.
- `SUPPORT_REQUIRE_DISTINCT_APPROVER=true` (default) → the requester can't approve their own action (`403 approver_must_differ`), unless they are `Support.Admin`.

### Recommended pattern in the support MCP

1. Tool `propose_<action>` → `proposeAction` with the user's exchanged token; return `plan.summary` + `plan.warnings`.
2. A **different person with `Support.Approver`** approves, outside the chat (support API, approval UI, Teams/Jira link).
   **Do not expose `approveAction` as a tool the LLM can call**: the four-eyes rule would block the requester anyway,
   but the approval should not run through the requester's chat.
3. Once approved, the requester (or an admin) calls `executeAction`.
4. Return `result` (e.g. new tracking id or new order id) and the trace link.

## Repeating shopper actions

`GET /v1/orders/{id}/shopper-replay` returns ready-to-run calls for the existing **shopper MCP** (`astronomy-shop-mcp`):

```json
{
  "mcp_server": "astronomy-shop-mcp",
  "source_order_id": "cfe9…",
  "user_id": "support-replay-1a2b3c4d",
  "currency": "USD",
  "steps": [
    {"tool": "empty_cart",  "arguments": {"user_id": "support-replay-1a2b3c4d"}},
    {"tool": "add_to_cart", "arguments": {"user_id": "…", "product_id": "0PUK6V6EV0", "quantity": 2}},
    {"tool": "checkout",    "arguments": {"checkout_person": {"userId": "…", "userCurrency": "USD", "address": {…}, "email": "…", "creditCard": {…}}}}
  ]
}
```

Two ways to use it:
- **Through the support API** (approved, audited, linked to the source order): the `replay_order` action.
- **Through the shopper MCP directly** (your MCP is a client of `http://localhost:8011/mcp`): run `steps` in order.
  Treat this as an action too and ask the human first: `checkout` creates a real order.

Shopper MCP tools: `list_products`, `get_product`, `get_ads`, `get_recommendations`, `add_to_cart`,
`get_cart`, `empty_cart`, `checkout`, `get_supported_currencies`, `get_shipping_quote`.
(`get_cart` was fixed in this fork: it previously sent `user_id` instead of `sessionId` and always returned an empty cart.)

## Errors

Every error has the same shape:

```json
{"error": {"code": "order_not_found", "message": "Order 'x' not found", "details": {}}}
```

| HTTP | Typical codes |
|---|---|
| 401 | `unauthorized` |
| 404 | `order_not_found`, `action_not_found`, `flag_not_found` |
| 409 | `invalid_state`, `flag_changed`, `order_has_no_items`, `order_has_no_shipment` |
| 422 | `invalid_request`, `invalid_params`, `unknown_action_type`, `unknown_variant`, `email_unknown` |
| 502 | `upstream_error` (`details.service` names the failing dependency) |

## Data notes and PII

- PII (email, street, zip) is **redacted by default**; `include_pii=true` returns it. Card data from traces is never returned.
- `customer.email` is only known when checkout emits it on the trace, which requires the `emitRawPii` flag.
  Otherwise `resend_order_confirmation` needs `params.email` (`422 email_unknown`).
- `user_id` and `trace_id` are filled in lazily: the first `getOrder`/timeline looks up the checkout trace in Jaeger
  (by `demo.order.id`, last 72 h) and stores them on the order.
- `created_at` is `null` for orders recorded before the migration.

## Quick check

Reads with the service key:

```bash
curl -s -H "X-API-Key: dev-support-key" localhost:8020/v1/diagnostics
```

As a user: log in as `agent` (dev-only password grant), exchange the token for `support-api` like the MCP does,
then propose:

```bash
KC=http://localhost:8180/realms/astronomy/protocol/openid-connect/token
USER_TOKEN=$(curl -s -d grant_type=password -d client_id=mcp-public -d username=agent -d password=agent-demo $KC | jq -r .access_token)
TOKEN=$(curl -s -u client-mcp:client-mcp-dev-secret -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d subject_token=$USER_TOKEN -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d audience=support-api -d scope=support-api-audience $KC | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8020/v1/me
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -X POST localhost:8020/v1/actions \
  -d '{"type":"reset_cart","params":{"user_id":"some-user"},"reason":"INC-1"}'
```

Tests (no stack needed): `cd src/support-api && uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest`.
