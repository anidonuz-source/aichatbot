"""
Misumi AI — shared PostgreSQL key-value store.

Every store module (admin_store, userbot_store, game_store, sticker_store,
memory_manager) used to keep its data in a local JSON file under
MEMORY_DIR. On Render's web service, the disk is EPHEMERAL — it gets
wiped on every deploy and every restart — so all of that data (connected
accounts, Pro status, saved memories, stickers, game stats) was being
lost constantly. This module replaces the file with a real Postgres
table, using the exact same "one JSON blob per key" shape each store
already expected, so the rest of each store's code barely changes.

Setup (free):
  1. Create a free Postgres database at https://neon.tech or
     https://supabase.com (either works, both have a free tier).
  2. Copy its connection string and set it as the DATABASE_URL env var
     (on Render: dashboard -> Environment). Must include sslmode=require
     (Neon/Supabase URLs already do).
  3. That's it — the table is created automatically on first use.
"""
import os
from threading import Lock

import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import SimpleConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_pool: SimpleConnectionPool | None = None
_pool_lock = Lock()
_table_lock = Lock()
_table_ready = False


def _get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not DATABASE_URL:
                    raise RuntimeError(
                        "DATABASE_URL not set. Get a free Postgres database "
                        "at https://neon.tech or https://supabase.com and "
                        "add its connection string as DATABASE_URL (see "
                        ".env.example) — required so bot data survives "
                        "redeploys."
                    )
                sslmode = {} if "sslmode=" in DATABASE_URL else {"sslmode": "require"}
                _pool = SimpleConnectionPool(1, 8, DATABASE_URL, **sslmode)
    return _pool


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        pool = _get_pool()
        conn = pool.getconn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS misumi_kv (
                        store TEXT NOT NULL,
                        key   TEXT NOT NULL,
                        value JSONB NOT NULL,
                        PRIMARY KEY (store, key)
                    )
                    """
                )
            _table_ready = True
        finally:
            pool.putconn(conn)


def load(store: str, key: str, default):
    """Return the JSON value stored at (store, key). If nothing is stored
    yet, returns `default` unchanged (caller passes a fresh literal each
    time, e.g. load(..., default={}), so there's no shared-mutable-state
    risk)."""
    _ensure_table()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM misumi_kv WHERE store = %s AND key = %s",
                (store, key),
            )
            row = cur.fetchone()
            return row[0] if row is not None else default
    finally:
        pool.putconn(conn)


def save(store: str, key: str, value) -> None:
    _ensure_table()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO misumi_kv (store, key, value)
                VALUES (%s, %s, %s)
                ON CONFLICT (store, key) DO UPDATE SET value = EXCLUDED.value
                """,
                (store, key, Json(value)),
            )
    finally:
        pool.putconn(conn)


def delete(store: str, key: str) -> None:
    _ensure_table()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM misumi_kv WHERE store = %s AND key = %s",
                (store, key),
            )
    finally:
        pool.putconn(conn)


def exists(store: str, key: str) -> bool:
    _ensure_table()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM misumi_kv WHERE store = %s AND key = %s",
                (store, key),
            )
            return cur.fetchone() is not None
    finally:
        pool.putconn(conn)
