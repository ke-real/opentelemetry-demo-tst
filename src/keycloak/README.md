# Keycloak (demo identity provider)

OpenID Connect provider for the client MCP and the support API, started by `compose.auth.yaml`. On every
start it imports realm `astronomy` from `realm-astronomy.json`. Keycloak runs in dev mode, so changes made in
the admin UI are lost when the container is recreated: edit the JSON instead.

**Not in git:** `realm-astronomy.json` (users, passwords, client secrets) and the repo-root `.env.local`
(`KEYCLOAK_ADMIN_PASSWORD`, optional `KEYCLOAK_PUBLIC_URL`). Get them from the team, or pass them to the Azure VM
through the Terraform variables `realm_file` and `env_local_file`. Run locally with both env files:

```bash
docker compose --env-file .env --env-file .env.local -f compose.yaml -f compose.full.yaml \
  -f compose.observability.yaml -f compose.agent.yaml -f compose.auth.yaml -f compose.extras.yaml up -d
```

If the issuer is published over HTTPS (`KEYCLOAK_PUBLIC_URL=https://<fqdn>`), the URLs below that use
`http://localhost:8180` change accordingly.

| | |
|---|---|
| Admin console | http://localhost:8180/admin (admin / `KEYCLOAK_ADMIN_PASSWORD` in `.env`) |
| Issuer (`iss`) | `http://localhost:8180/realms/astronomy`, the same from the host, from containers and through the Azure SSH tunnel |
| Discovery (from the host) | http://localhost:8180/realms/astronomy/.well-known/openid-configuration |
| Discovery, JWKS, token (from containers) | `http://keycloak:8080/realms/astronomy/...`. Containers get internal endpoint URLs but the same issuer |

## Roles (realm roles, claim `realm_access.roles`)

| Role | Includes | Intended access |
|---|---|---|
| `Shop.Viewer` | – | office staff: shop API, metrics, traces, logs; anonymized data |
| `Support.Agent` | Shop.Viewer | order lookup, timelines, low/medium-risk actions (`resend_order_confirmation`, `reset_cart`, `reship_order`) |
| `Support.Approver` | Shop.Viewer | approve or reject proposed actions |
| `Support.Admin` | Support.Agent | all actions incl. `replay_order` and `set_feature_flag`; raw data |

## Demo users (test credentials)

| User | Password | Roles |
|---|---|---|
| `office` | `office-demo` | Shop.Viewer |
| `agent` | `agent-demo` | Support.Agent |
| `approver` | `approver-demo` | Support.Approver |
| `admin` | `admin-demo` | Support.Admin + Support.Approver |

## Clients

| Client | Type | Purpose |
|---|---|---|
| `client-mcp` | confidential (`client-mcp-dev-secret`) | the MCP server: OAuth proxy client if needed, and **token exchange** to `support-api` |
| `support-api` | confidential, service account | resource server; the service account is for service-to-service calls |
| `mcp-public` | public, PKCE | MCP clients that take a fixed client id; **password grant enabled for dev smoke tests only** |
| *(dynamic)* | public | MCP clients (Claude Code, Claude Desktop, MCP Inspector) **register themselves** at `/clients-registrations/openid-connect`. Anonymous registration is allowed only with `localhost` / `127.0.0.1` redirect URIs |

Every access token has `aud: client-mcp` (default scope `mcp-audience`). That is the audience the MCP server must check.

## What the client MCP does (resource server)

1. Publish protected-resource metadata pointing at the issuer above (FastMCP auth provider / JWT verifier).
2. Validate the bearer token on every request:
   - `iss` = `http://localhost:8180/realms/astronomy`
   - `aud` contains `client-mcp`
   - signature checked with keys from JWKS `http://keycloak:8080/realms/astronomy/protocol/openid-connect/certs`
3. Read roles from `realm_access.roles`, and the user from `preferred_username` / `sub`.
4. To call the support API **as the user**, exchange the token (standard token exchange):

   ```bash
   curl -u client-mcp:client-mcp-dev-secret http://keycloak:8080/realms/astronomy/protocol/openid-connect/token \
     -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
     -d subject_token="$USER_ACCESS_TOKEN" \
     -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
     -d audience=support-api -d scope=support-api-audience
   ```

   The result has `aud: support-api`, `azp: client-mcp`, and the user's `sub`, `preferred_username`, `email` and
   `realm_access.roles`. `scope=support-api-audience` is required: standard token exchange keeps only that scope's
   claims, which is why the scope also carries the identity and role mappers.

## What the support API does

- Validate the bearer token: same issuer, `aud` contains `support-api`, JWKS as above.
- Take the actor from the token (`preferred_username`), not from a header.
- Map roles to endpoints and action types. Approve requires `Support.Approver` and, optionally, an approver
  different from the requester.

## Smoke test (dev only)

```bash
curl -s -d grant_type=password -d client_id=mcp-public -d username=agent -d password=agent-demo \
  http://localhost:8180/realms/astronomy/protocol/openid-connect/token
```

Decode the `access_token` at a JWT viewer, or with `python -c` and base64, to see `aud`, `realm_access.roles` and
`preferred_username`.
