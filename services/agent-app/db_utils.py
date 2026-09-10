import datetime
import logging
import os

import psycopg
import psycopg.rows

logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL")


def _get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=psycopg.rows.dict_row)


def init_db() -> None:
    with _get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_threads (
            thread_id UUID PRIMARY KEY,
            title TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""")


def create_thread(thread_id: str, title: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO chat_threads (thread_id, title, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (thread_id, title[:80], now, now))
    logger.info("created chat thread %s (%r)", thread_id, title[:80])


def touch_thread(thread_id: str) -> None:
    with _get_connection() as conn:
        conn.execute("UPDATE chat_threads SET updated_at = %s WHERE thread_id = %s",
                     (datetime.datetime.now(datetime.timezone.utc), thread_id))


def list_threads() -> list[dict]:
    with _get_connection() as conn:
        rows = conn.execute("SELECT * FROM chat_threads ORDER BY updated_at DESC").fetchall()
    return rows
