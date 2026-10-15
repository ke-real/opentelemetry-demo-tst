# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Auth tests run without the demo stack: tokens are signed with a throwaway key, JWKS is stubbed."""

import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://issuer.test/realms/astronomy"
os.environ.update({
    "OIDC_ISSUER": ISSUER,
    "OIDC_JWKS_URL": "http://issuer.test/certs",
    "OIDC_AUDIENCE": "support-api",
    "SUPPORT_API_KEY": "test-key",
    "SUPPORT_API_KEY_ROLES": "Shop.Viewer",
    "SUPPORT_AUTH_DISABLED": "false",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.main import app  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StubJwks:
    class _Key:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._Key()


@pytest.fixture(autouse=True)
def stub_jwks(monkeypatch):
    monkeypatch.setattr(auth, "_jwks", _StubJwks())


@pytest.fixture
def client():
    # No context manager: the lifespan (DB migration) is not run.
    return TestClient(app)


def token(user="agent", roles=("Support.Agent",), aud="support-api", iss=ISSUER, exp_in=300, key=KEY, **extra):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": f"sub-{user}", "preferred_username": user,
              "realm_access": {"roles": list(roles)}, "iat": now, "exp": now + exp_in, **extra}
    return jwt.encode(claims, key, algorithm="RS256")


def bearer(**kw):
    return {"Authorization": f"Bearer {token(**kw)}"}
