"""Presentation-layer chat history: the durable, user-facing transcript.

Deliberately separate from LangGraph's checkpointed `messages` state in
agent_graph.py. That state is the agent's internal working memory (tool
calls, tool outputs, retries, regenerated drafts) -- optimized for
reasoning continuity, not for display. This table is the one and only
source of truth for "what the chat UI shows", so a page reload always
reproduces exactly what the user saw live: no intermediate/discarded
drafts, no raw tool-call JSON, and any structured artifact (e.g. a
chart) generated for a turn survives reload too.
"""
import json
import logging

import psycopg.rows
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


async def init_chat_store(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_turns (
                id BIGSERIAL PRIMARY KEY,
                thread_id UUID NOT NULL,
                turn_index INT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                chart_data JSONB,
                faithfulness_score DOUBLE PRECISION,
                completeness_score DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Migration for databases created before thinking_log existed:
        # CREATE TABLE IF NOT EXISTS is a no-op on an already-existing
        # table, so adding the column to the CREATE statement above does
        # NOT retroactively add it to a database that already has this
        # table from a previous run. ADD COLUMN IF NOT EXISTS is the
        # actual migration step that makes it appear on existing tables,
        # and is a no-op (safe) on a freshly created table too.
        await conn.execute("""
            ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS thinking_log JSONB
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS chat_turns_thread_idx
            ON chat_turns (thread_id, turn_index)
        """)


async def next_turn_index(pool: AsyncConnectionPool, thread_id: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(turn_index), -1) + 1 FROM chat_turns WHERE thread_id = %s",
            (thread_id,))
        row = await cur.fetchone()
    return row[0]


async def save_turn(pool: AsyncConnectionPool, thread_id: str, turn_index: int, role: str,
                     content: str, chart_data: dict | None = None,
                     faithfulness_score: float | None = None,
                     completeness_score: float | None = None,
                     thinking_log: list[str] | None = None) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO chat_turns
                   (thread_id, turn_index, role, content, chart_data,
                    faithfulness_score, completeness_score, thinking_log)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)""",
                (thread_id, turn_index, role, content,
                 json.dumps(chart_data) if chart_data is not None else None,
                 faithfulness_score, completeness_score,
                 json.dumps(thinking_log) if thinking_log is not None else None))
    except Exception:
        logger.exception("failed to persist chat turn (thread_id=%s, role=%s)", thread_id, role)


async def get_turns(pool: AsyncConnectionPool, thread_id: str) -> list[dict]:
    async with pool.connection() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """SELECT role, content, chart_data, faithfulness_score, completeness_score, thinking_log
                   FROM chat_turns WHERE thread_id = %s ORDER BY turn_index ASC, id ASC""",
            (thread_id,))
        rows = await cur.fetchall()
    return rows