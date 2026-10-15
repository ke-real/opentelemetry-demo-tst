# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY src/support-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/support-api/app app

ARG SUPPORT_API_PORT=8020
EXPOSE ${SUPPORT_API_PORT}

CMD ["sh", "-c", "opentelemetry-instrument uvicorn app.main:app --host 0.0.0.0 --port ${SUPPORT_API_PORT:-8020}"]
