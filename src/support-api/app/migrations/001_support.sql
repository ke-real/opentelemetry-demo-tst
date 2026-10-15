-- Copyright The OpenTelemetry Authors
-- SPDX-License-Identifier: Apache-2.0
--
-- Idempotent: applied by support-api on every start (as the postgres superuser).
-- The support_user role itself is created by support-api before this file runs.

-- Accounting: support-relevant order metadata. The accounting service only
-- inserts order_id, so the new columns rely on defaults or are filled by support-api.
ALTER TABLE accounting."order" ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE accounting."order" ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE accounting."order" ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'RECORDED';
ALTER TABLE accounting."order" ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE accounting."order" ADD COLUMN IF NOT EXISTS trace_id TEXT;
CREATE INDEX IF NOT EXISTS order_created_at_idx ON accounting."order" (created_at DESC);
CREATE INDEX IF NOT EXISTS order_user_id_idx ON accounting."order" (user_id);

CREATE SCHEMA IF NOT EXISTS support;

CREATE TABLE IF NOT EXISTS support.actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    params JSONB NOT NULL,
    plan JSONB NOT NULL,
    reason TEXT,
    requested_by TEXT NOT NULL,
    decided_by TEXT,
    decision_comment TEXT,
    decided_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    result JSONB,
    error TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_status_idx ON support.actions (status, created_at DESC);

CREATE TABLE IF NOT EXISTS support.audit_log (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action_id UUID,
    event TEXT NOT NULL,
    details JSONB
);

CREATE TABLE IF NOT EXISTS support.order_events (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    type TEXT NOT NULL,
    details JSONB,
    action_id UUID
);
CREATE INDEX IF NOT EXISTS order_events_order_idx ON support.order_events (order_id, at);

GRANT USAGE ON SCHEMA accounting, catalog, support TO support_user;
GRANT SELECT ON ALL TABLES IN SCHEMA accounting TO support_user;
GRANT UPDATE (status, user_id, trace_id) ON accounting."order" TO support_user;
GRANT INSERT ON accounting.shipping TO support_user;
GRANT SELECT ON catalog.products TO support_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA support TO support_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA support TO support_user;
