# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .config import settings

logger = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).parent / "migrations"

pool = AsyncConnectionPool(settings.db_url, open=False, min_size=1, max_size=5,
                           kwargs={"row_factory": dict_row, "autocommit": True})


async def migrate() -> None:
    if not settings.db_admin_url:
        logger.warning("SUPPORT_DB_ADMIN_URL not set, skipping migrations")
        return
    async with await psycopg.AsyncConnection.connect(settings.db_admin_url, autocommit=True) as conn:
        exists = await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'support_user'")).fetchone()
        verb = "ALTER" if exists else "CREATE"
        await conn.execute(sql.SQL(verb + " ROLE support_user WITH LOGIN PASSWORD {}").format(
            sql.Literal(settings.db_password)))
        for path in sorted(MIGRATIONS.glob("*.sql")):
            await conn.execute(path.read_text(encoding="utf-8"))
            logger.info("Applied migration %s", path.name)


async def fetch_all(query: str, *params) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(query, params)).fetchall()


async def fetch_one(query: str, *params) -> dict | None:
    async with pool.connection() as conn:
        return await (await conn.execute(query, params)).fetchone()


async def execute(query: str, *params) -> None:
    async with pool.connection() as conn:
        await conn.execute(query, params)


async def audit(actor: str, event: str, action_id=None, details: dict | None = None) -> None:
    await execute("INSERT INTO support.audit_log (actor, action_id, event, details) VALUES (%s, %s, %s, %s)",
                  actor, action_id, event, Jsonb(details or {}))


async def order_event(order_id: str, type_: str, details: dict, action_id=None) -> None:
    await execute("INSERT INTO support.order_events (order_id, type, details, action_id) VALUES (%s, %s, %s, %s)",
                  order_id, type_, Jsonb(details), action_id)
