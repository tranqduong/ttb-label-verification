"""
Persistence for the Application Queue.

Serverless functions are stateless between invocations — there's no
in-memory list or local file that would survive from one request to the
next (Vercel's function filesystem is also read-only outside /tmp), so the
queue (past submissions, their review status, and reviewer notes) needs a
real database.

This targets Postgres specifically because Vercel's own "Storage" tab can
provision a Postgres database (Neon under the hood) with zero manual
credential entry: it wires up a connection string env var for you across
Production/Preview/Development automatically. That matters here in
particular, since neither the end user nor this assistant should be typing
a raw database credential into a config field by hand — provisioning it
through Vercel's dashboard sidesteps that entirely.

Connections are opened fresh per call rather than pooled at module scope:
serverless functions get frozen and thawed between invocations in ways
that make a long-lived connection/pool unreliable across invocations, and
this app's traffic volume doesn't need one. Each function call pays one
connection setup, which is a fine trade for a prototype.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

# Different Postgres integrations on Vercel (native Postgres, Neon, Supabase)
# land the connection string under different env var names. Try them in
# order rather than requiring the user to know which one their storage
# integration used.
_ENV_VAR_CANDIDATES = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_URL_NON_POOLING",
    "POSTGRES_PRISMA_URL",
)


def _dsn() -> str:
    for name in _ENV_VAR_CANDIDATES:
        value = os.environ.get(name)
        if value:
            # asyncpg wants the postgresql:// scheme; some providers hand out postgres://
            return value.replace("postgres://", "postgresql://", 1)
    raise RuntimeError(
        "No Postgres connection string found (checked "
        + ", ".join(_ENV_VAR_CANDIDATES)
        + "). Add Postgres storage to this project from the Vercel dashboard's "
        "Storage tab — that sets one of these automatically, no manual "
        "credential entry needed."
    )


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            application_data JSONB NOT NULL,
            image_bytes BYTEA,
            extracted JSONB,
            field_results JSONB,
            overall_status TEXT,
            label_filename TEXT,
            note TEXT
        )
        """
    )
    # Submission and analysis used to happen in the same request, so
    # extracted/field_results/overall_status were always populated at
    # insert time and image bytes were never kept around. Now a record can
    # sit in `pending_analysis` with none of those filled in yet, and the
    # analyze step needs the original image bytes to run extraction later
    # -- these statements backfill both changes onto any table created by
    # an earlier deployment. ADD COLUMN IF NOT EXISTS and DROP NOT NULL are
    # both no-ops on a table that already matches, so this is safe (and
    # cheap) to run on every connection rather than needing a real
    # migration step.
    await conn.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS image_bytes BYTEA")
    await conn.execute("ALTER TABLE applications ALTER COLUMN extracted DROP NOT NULL")
    await conn.execute("ALTER TABLE applications ALTER COLUMN field_results DROP NOT NULL")
    await conn.execute("ALTER TABLE applications ALTER COLUMN overall_status DROP NOT NULL")


def _row_to_record(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"].isoformat(),
        "status": row["status"],
        "application": json.loads(row["application_data"]),
        "extracted": json.loads(row["extracted"]) if row["extracted"] else None,
        "fields": json.loads(row["field_results"]) if row["field_results"] else [],
        "overall_status": row["overall_status"],
        "label_filename": row["label_filename"],
        "note": row["note"],
    }


async def insert_pending_application(
    application_data: dict,
    image_bytes: bytes,
    label_filename: Optional[str],
) -> dict:
    """Saves the label image + filed application data to the queue without
    running analysis. The record starts in `pending_analysis`; call
    complete_analysis() once extraction/comparison has run against the
    image (fetched back via get_application_image())."""
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await _ensure_schema(conn)
        record_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        await conn.execute(
            """
            INSERT INTO applications
                (id, created_at, status, application_data, image_bytes, extracted, field_results, overall_status, label_filename, note)
            VALUES ($1, $2, 'pending_analysis', $3, $4, NULL, NULL, NULL, $5, NULL)
            """,
            record_id,
            created_at,
            json.dumps(application_data),
            image_bytes,
            label_filename,
        )
    finally:
        await conn.close()
    return {
        "id": record_id,
        "created_at": created_at.isoformat(),
        "status": "pending_analysis",
        "application": application_data,
        "extracted": None,
        "fields": [],
        "overall_status": None,
        "label_filename": label_filename,
        "note": None,
    }


async def get_application_image(app_id: str) -> Optional[tuple[bytes, Optional[str]]]:
    """Returns (image_bytes, label_filename) for an application's stored
    label photo, or None if the application (or its image) doesn't exist.
    Used both to serve the image back to the frontend and to re-run
    extraction against it in complete_analysis()'s caller."""
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await _ensure_schema(conn)
        row = await conn.fetchrow("SELECT image_bytes, label_filename FROM applications WHERE id = $1", app_id)
    finally:
        await conn.close()
    if not row or row["image_bytes"] is None:
        return None
    return row["image_bytes"], row["label_filename"]


async def complete_analysis(
    app_id: str,
    extracted: dict,
    field_results: list,
    overall_status: str,
) -> Optional[dict]:
    """Fills in the analysis fields for a `pending_analysis` record and
    moves it to `needs_review` -- the same "nobody has acted on this yet"
    starting point the old one-shot insert_application() used, just
    reached one step later now that submission and analysis are split."""
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await _ensure_schema(conn)
        row = await conn.fetchrow(
            """
            UPDATE applications
            SET extracted = $2, field_results = $3, overall_status = $4, status = 'needs_review'
            WHERE id = $1
            RETURNING *
            """,
            app_id,
            json.dumps(extracted),
            json.dumps(field_results),
            overall_status,
        )
    finally:
        await conn.close()
    return _row_to_record(row) if row else None


async def list_applications() -> list[dict]:
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await _ensure_schema(conn)
        rows = await conn.fetch("SELECT * FROM applications ORDER BY created_at DESC")
    finally:
        await conn.close()
    return [_row_to_record(r) for r in rows]


async def get_application(app_id: str) -> Optional[dict]:
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await _ensure_schema(conn)
        row = await conn.fetchrow("SELECT * FROM applications WHERE id = $1", app_id)
    finally:
        await conn.close()
    return _row_to_record(row) if row else None


async def update_application_status(app_id: str, status: str, note: Optional[str]) -> Optional[dict]:
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await _ensure_schema(conn)
        row = await conn.fetchrow(
            "UPDATE applications SET status = $2, note = $3 WHERE id = $1 RETURNING *",
            app_id,
            status,
            note,
        )
    finally:
        await conn.close()
    return _row_to_record(row) if row else None
