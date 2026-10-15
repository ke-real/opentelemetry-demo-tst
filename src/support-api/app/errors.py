# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0


class ApiError(Exception):
    """Rendered as {"error": {"code", "message", "details"}} with the given HTTP status."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(404, f"{what}_not_found", f"{what.replace('_', ' ').capitalize()} '{ident}' not found")


def upstream_failed(service: str, detail: str) -> ApiError:
    return ApiError(502, "upstream_error", f"Call to {service} failed: {detail}", {"service": service})
