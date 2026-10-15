# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""One-call "what is broken right now" snapshot."""

import asyncio
import re

from . import clients, db
from .config import settings

WINDOW_RE = re.compile(r"^\d+[smh]$")  # Prometheus range like 30s, 5m, 1h


async def flag_states() -> list[dict]:
    flags = await clients.read_flags()
    states = []
    for name, f in sorted(flags.items()):
        variant = clients.effective_variant(f)
        states.append({"flag": name, "variant": variant, "value": f.get("variants", {}).get(variant),
                       "variants": list(f.get("variants", {})), "state": f.get("state"),
                       "description": f.get("description"), "targeted": "targeting" in f,
                       "active": f.get("state") == "ENABLED" and variant != "off"})
    return states


async def _service_stats(window: str) -> list[dict]:
    sel = 'span_kind="SPAN_KIND_SERVER"'
    calls, errors, p95 = await asyncio.gather(
        clients.prom_query(f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{sel}}}[{window}]))"),
        clients.prom_query(f'sum by (service_name) (rate(traces_span_metrics_calls_total{{{sel},status_code="STATUS_CODE_ERROR"}}[{window}]))'),
        clients.prom_query(f"histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_milliseconds_bucket{{{sel}}}[{window}])))"),
    )

    def by_service(result):
        return {r["metric"].get("service_name"): float(r["value"][1]) for r in result}

    calls, errors, p95 = by_service(calls), by_service(errors), by_service(p95)
    stats = []
    for svc, rate in calls.items():
        err = errors.get(svc, 0.0)
        latency = p95.get(svc)
        stats.append({"service": svc, "requests_per_s": round(rate, 3), "errors_per_s": round(err, 3),
                      "error_ratio": round(err / rate, 4) if rate else 0.0,
                      "p95_ms": round(latency, 1) if latency == latency and latency is not None else None})
    return sorted(stats, key=lambda s: (-s["error_ratio"], s["service"]))


async def _db_ok() -> dict:
    try:
        await db.fetch_one("SELECT 1")
        return {"name": "postgres", "ok": True, "detail": "SELECT 1"}
    except Exception as e:
        return {"name": "postgres", "ok": False, "detail": type(e).__name__}


async def snapshot(window: str = "5m", error_threshold: float = 0.05) -> dict:
    window = window if WINDOW_RE.match(window) else "5m"
    deps, services, flags = await asyncio.gather(
        asyncio.gather(
            _db_ok(),
            clients.ping("frontend", f"{settings.frontend_url}/api/currency"),
            clients.ping("jaeger", f"{settings.jaeger_query_url}/api/v3/services"),
            clients.ping("prometheus", f"{settings.prometheus_url}/-/ready"),
            clients.ping("flagd-ui", f"{settings.flagd_ui_url}/api/read"),
        ),
        _service_stats(window),
        flag_states(),
        return_exceptions=True,
    )
    findings = []
    if isinstance(flags, list):
        findings += [f"Feature flag '{f['flag']}' is active (variant '{f['variant']}'): {f['description']}"
                     for f in flags if f["active"]]
    if isinstance(services, list):
        findings += [f"{s['service']}: {s['error_ratio']:.1%} of server spans failing over {window}"
                     for s in services if s["error_ratio"] >= error_threshold]
    if isinstance(deps, list):
        findings += [f"Dependency {d['name']} unreachable ({d['detail']})" for d in deps if not d["ok"]]
    return {
        "window": window,
        "findings": findings,
        "services": services if isinstance(services, list) else {"error": str(services)},
        "active_flags": [f for f in flags if f["active"]] if isinstance(flags, list) else {"error": str(flags)},
        "dependencies": deps if isinstance(deps, list) else {"error": str(deps)},
    }
