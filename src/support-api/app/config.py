# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


def _url(value: str) -> str:
    return value if value.startswith("http") else f"http://{value}"


@dataclass(frozen=True)
class Settings:
    port: int = int(os.getenv("SUPPORT_API_PORT", "8020"))
    api_key: str = os.getenv("SUPPORT_API_KEY", "")
    db_url: str = os.getenv("SUPPORT_DB_URL", "postgresql://support_user:support_password@localhost:5444/astronomy_db")
    db_password: str = os.getenv("SUPPORT_DB_PASSWORD", "support_password")
    db_admin_url: str = os.getenv("SUPPORT_DB_ADMIN_URL", "")
    frontend_url: str = _url(os.getenv("APPLICATION_ENDPOINT", "localhost:8080"))
    email_url: str = _url(os.getenv("EMAIL_ADDR", "http://email:6060"))
    shipping_url: str = _url(os.getenv("SHIPPING_ADDR", "http://shipping:50050"))
    jaeger_query_url: str = _url(os.getenv("JAEGER_QUERY_URL", "http://localhost:8080/jaeger/ui"))
    jaeger_public_url: str = _url(os.getenv("JAEGER_PUBLIC_URL", "http://localhost:8080/jaeger/ui"))
    prometheus_url: str = _url(os.getenv("PROMETHEUS_ADDR", "localhost:9090"))
    flagd_ui_url: str = _url(os.getenv("FLAGD_UI_URL", "http://flagd-ui:4000"))
    trace_lookback_hours: int = int(os.getenv("SUPPORT_TRACE_LOOKBACK_HOURS", "72"))
    action_ttl_minutes: int = int(os.getenv("SUPPORT_ACTION_TTL_MINUTES", "30"))
    require_distinct_approver: bool = _bool("SUPPORT_REQUIRE_DISTINCT_APPROVER", False)
    # Test card from src/load-generator/people.json, used when replaying orders.
    replay_card: dict = field(default_factory=lambda: {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardExpirationMonth": 1,
        "creditCardExpirationYear": 2039,
        "creditCardCvv": 672,
    })


settings = Settings()
