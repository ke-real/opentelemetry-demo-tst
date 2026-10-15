# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Who is calling, and may they? Bearer JWTs from the OIDC provider, or the service API key."""

import asyncio
import secrets
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .errors import ApiError

VIEWER, AGENT, APPROVER, ADMIN = "Shop.Viewer", "Support.Agent", "Support.Approver", "Support.Admin"

IMPLIES = {AGENT: {VIEWER}, APPROVER: {VIEWER}, ADMIN: {AGENT, VIEWER}}
ALL_ROLES = frozenset({VIEWER, AGENT, APPROVER, ADMIN})

bearer_scheme = HTTPBearer(auto_error=False, description="OIDC access token (aud: support-api)")
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service-to-service key")

_jwks = jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True, lifespan=3600) if settings.oidc_jwks_url else None


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset[str]
    kind: str
    on_behalf: str | None = None

    def has(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))

    @property
    def label(self) -> str:
        return f"{self.name} (on behalf of {self.on_behalf})" if self.on_behalf else self.name


def expand(roles) -> frozenset[str]:
    out = set(roles)
    for r in list(out):
        out |= IMPLIES.get(r, set())
    return frozenset(out)


def _claim(claims: dict, path: str):
    value = claims
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": 'Bearer realm="support-api"'})


async def _verify(token: str) -> Principal:
    if not _jwks:
        raise _unauthorized("Bearer tokens are not accepted: OIDC_ISSUER is not configured")
    try:
        key = await asyncio.to_thread(_jwks.get_signing_key_from_jwt, token)
    except jwt.PyJWKClientConnectionError as e:
        raise ApiError(503, "auth_unavailable", f"Cannot reach the identity provider's keys: {e}")
    except jwt.PyJWTError as e:
        raise _unauthorized(f"Invalid token: {e}")
    try:
        claims = jwt.decode(token, key.key, algorithms=["RS256", "RS384", "RS512", "ES256", "PS256"],
                            audience=settings.oidc_audience, issuer=settings.oidc_issuer, leeway=30,
                            options={"require": ["exp", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise _unauthorized(f"Invalid token: {e}")
    roles = _claim(claims, settings.oidc_roles_claim) or []
    if isinstance(roles, str):
        roles = [roles]
    name = _claim(claims, settings.oidc_username_claim) or claims["sub"]
    return Principal(str(name), expand(r for r in roles if isinstance(r, str)), "user")


async def authenticate(request: Request,
                       bearer: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                       api_key: str | None = Depends(api_key_scheme)) -> Principal:
    if bearer:
        return await _verify(bearer.credentials)
    if api_key and settings.api_key and secrets.compare_digest(api_key, settings.api_key):
        return Principal("service", expand(settings.api_key_roles), "service",
                         on_behalf=request.headers.get("x-support-actor"))
    if api_key:
        raise _unauthorized("Invalid X-API-Key")
    if settings.auth_disabled:
        return Principal("anonymous", ALL_ROLES, "anonymous")
    raise _unauthorized("Missing credentials: send 'Authorization: Bearer <token>'")


def require(*roles: str):
    """Dependency: the caller must hold at least one of `roles`."""

    async def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.has(*roles):
            raise forbidden(principal, roles)
        return principal

    return dependency


def forbidden(principal: Principal, roles, what: str = "this operation") -> ApiError:
    return ApiError(403, "forbidden", f"{what} requires one of the roles: {', '.join(roles)}",
                    {"required_roles": list(roles), "caller": principal.name})
