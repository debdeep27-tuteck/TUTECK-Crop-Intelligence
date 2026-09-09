"""
shared/db.py
============
One shared Postgres connection helper for every backend service in this
project (auth, advisory, auction, credit-score, yield-detect, yield-platform).

Why this exists
----------------
Every service was originally written against sqlite3, using patterns like:

    db.execute("SELECT * FROM foo WHERE id = ?", (foo_id,)).fetchone()
    row["column_name"]
    db.commit()

Rather than rewriting every call site to psycopg2's cursor-based API,
`PGConnection` below is a thin wrapper that keeps that exact call shape
working on top of a real Postgres connection:

  * `?` placeholders are translated to psycopg2's `%s` automatically.
  * Rows come back as `RealDictRow`, which supports `row["col"]` the same
    way `sqlite3.Row` does.
  * `.execute()` returns something with `.fetchone()` / `.fetchall()` /
    `.rowcount`, same as sqlite3's connection-level `.execute()` shortcut.

Each service still calls `db.commit()` explicitly wherever it did before —
this wrapper does not add autocommit or hidden transaction behavior.

Config
------
Set DATABASE_URL in the environment (or a service's .env), e.g.:

    DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/cropai

Flask g-based per-request pattern
----------------------------------
    from shared.db import get_db, close_db

    @app.teardown_appcontext
    def _close_db(_exc):
        close_db()

    def some_route():
        db = get_db()
        row = db.execute("SELECT * FROM foo WHERE id = ?", (foo_id,)).fetchone()
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
import psycopg2.extensions
from flask import g

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Point it at your Postgres instance, e.g.\n"
        "  DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/cropai"
    )

# ── Connection pool ────────────────────────────────────────────────────
#
# Neon (and Postgres generally) is a remote server: every fresh
# psycopg2.connect() pays a full TCP + TLS handshake + auth round-trip
# before a single query runs. Sqlite never had this cost. Opening/closing
# a physical connection on every Flask request (the original pattern here)
# turns each request into an extra network round-trip on top of the
# actual query — this is what caused the login/sidebar lag after the
# sqlite -> Postgres migration.
#
# Fix: keep a small pool of already-open connections per process and
# borrow/return instead of connect()/close(). MINCONN/MAXCONN can be
# tuned via env vars if a given service needs more headroom (e.g. under
# multiple gunicorn workers, each worker gets its own pool).
MINCONN = int(os.environ.get("DB_POOL_MINCONN", "0"))
MAXCONN = int(os.environ.get("DB_POOL_MAXCONN", "10"))

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(MINCONN, MAXCONN, DATABASE_URL)
    return _pool


def closeall_pools() -> None:
    """Call on process shutdown (optional) to release every pooled
    connection cleanly."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


_QMARK_RE = re.compile(r"\?")


def _qmark_to_pct_s(sql: str) -> str:
    """Translate sqlite-style `?` placeholders to psycopg2's `%s`.

    Safe here because none of these services ever put a literal `?` inside
    a string constant in their SQL — every `?` in this codebase is a
    parameter placeholder.
    """
    return _QMARK_RE.sub("%s", sql)


class PGCursorResult:
    """Wraps a psycopg2 cursor so callers can keep using
    .fetchone()/.fetchall()/.rowcount exactly like they did against
    sqlite3's connection-level .execute() shortcut."""

    __slots__ = ("_cursor",)

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __iter__(self):
        # sqlite3's cursor (and its connection-level .execute() shortcut)
        # supports `for row in conn.execute(...)` directly, without an
        # explicit .fetchall() first. psycopg2 cursors support this too —
        # just forward to the underlying cursor's iterator.
        return iter(self._cursor)

    @property
    def lastrowid(self):
        # Postgres has no sqlite-style lastrowid. Every table in this
        # codebase uses app-generated TEXT ids (new_id()/uuid), never
        # AUTOINCREMENT/SERIAL primary keys read back this way — if this
        # ever fires, it means new code depends on a pattern that doesn't
        # translate, and should use `RETURNING id` instead.
        raise AttributeError(
            "lastrowid is not supported on Postgres — add `RETURNING id` "
            "to the INSERT and read it via .fetchone() instead"
        )

    def close(self):
        self._cursor.close()


class PGConnection:
    """Drop-in replacement for a sqlite3.Connection as used across these
    services: db.execute(sql, params), db.executescript(sql), db.commit(),
    db.rollback(), db.close(), and dict-style row access."""

    def __init__(self, raw_conn, _release=None):
        self._conn = raw_conn
        # If this connection came from the pool, `_release` puts it back
        # instead of physically closing it. One-off connect() calls (e.g.
        # migration scripts, init_db() setup outside a request) leave this
        # None and get a real close(), same as before.
        self._release = _release

    def execute(self, sql: str, params=None) -> PGCursorResult:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_qmark_to_pct_s(sql), params)
        return PGCursorResult(cur)

    def executescript(self, sql: str) -> None:
        """For schema/DDL blocks. psycopg2 supports multiple ';'-separated
        statements in a single execute() as long as no parameters are
        passed, which is all these init_db() scripts ever need."""
        cur = self._conn.cursor()
        cur.execute(sql)
        cur.close()

    def executemany(self, sql: str, seq_of_params) -> None:
        cur = self._conn.cursor()
        cur.executemany(_qmark_to_pct_s(sql), seq_of_params)
        cur.close()

    def cursor(self, *args, **kwargs):
        kwargs.setdefault("cursor_factory", psycopg2.extras.RealDictCursor)
        return self._conn.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._release is not None:
            # Guard against handing the next borrower a connection sitting
            # mid-transaction (e.g. a request errored before calling
            # .commit()). psycopg2's pool doesn't reliably clean this up
            # on its own, and a leaked open transaction on a reused pooled
            # connection is a much nastier bug than the lag we're fixing.
            try:
                if not self._conn.closed and self._conn.status != psycopg2.extensions.STATUS_READY:
                    self._conn.rollback()
            except Exception:
                pass
            self._release(self._conn)
        else:
            self._conn.close()

    @property
    def closed(self):
        return self._conn.closed

    def __enter__(self) -> "PGConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # sqlite3's `with conn:` only commits/rolls back and leaves the
        # connection open — cheap to do for a local file, but a fresh
        # Postgres connection per request is not something we want to
        # leak. Call sites that used `with get_db() as conn:` against
        # sqlite3 get the same commit/rollback behavior here, plus the
        # connection is actually closed afterward.
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


def connect() -> PGConnection:
    """Open a brand-new Postgres connection (for one-off scripts / init_db()
    style setup that runs outside a Flask request context, e.g. the
    migration script). Not pooled on purpose — these are short-lived
    processes where a pool would just add overhead for no reuse benefit."""
    raw = psycopg2.connect(DATABASE_URL)
    return PGConnection(raw)


def _connect_pooled() -> PGConnection:
    """Borrow a connection from the shared pool instead of opening a new
    physical connection. `.close()` on the returned PGConnection returns
    it to the pool rather than tearing down the TCP/TLS session.

    Neon (and other serverless/managed Postgres) will silently drop
    connections that sit idle for a while — the socket dies server-side
    but the pool has no way to know unless something checks. Handing out
    a dead connection produces exactly the "SSL connection has been closed
    unexpectedly" / "connection already closed" loop seen in production:
    once one dead connection lands in the pool, every request borrowing
    it fails the same way, forever, since nothing ever replaces it.

    So: ping with a trivial query before handing the connection back to
    the caller. If it's dead, tell the pool to discard it (closed=True —
    this frees the slot without returning it to the free list) and try
    again; the pool opens a fresh physical connection to fill that slot.
    """
    pool = _get_pool()
    for _attempt in range(MAXCONN + 1):
        raw = pool.getconn()
        try:
            if raw.closed:
                raise psycopg2.InterfaceError("connection already closed")
            with raw.cursor() as probe:
                probe.execute("SELECT 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            pool.putconn(raw, close=True)
            continue
        return PGConnection(raw, _release=pool.putconn)
    # Every pooled slot was dead (e.g. Neon suspended/reset all of them at
    # once) — fall back to a brand-new, unpooled connection rather than
    # failing the request outright.
    raw = psycopg2.connect(DATABASE_URL)
    return PGConnection(raw)


def get_db() -> PGConnection:
    """Per-request connection, cached on flask.g — same call pattern every
    service already used for sqlite3, but now borrowed from a pool instead
    of opening a fresh physical connection (with its TCP + TLS + auth
    handshake to Neon) on every single request."""
    if "db" not in g:
        g.db = _connect_pooled()
    return g.db


def close_db(_exc=None) -> None:
    """Call this from @app.teardown_appcontext in each service. Returns
    the connection to the pool rather than closing it outright."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ── Scoped-connection helpers (non-Flask call sites) ─────────────────────
#
# get_db()/close_db() above assume a Flask app context (flask.g). Some
# services (auth_excel.py) instead want a connection scoped to a plain
# `with` block, outside any request — these two cover that.

@contextmanager
def transaction():
    """`with transaction() as conn:` — yields a PGConnection (same
    `?`-or-`%s`-placeholder `.execute()` shape as get_db()). Commits on a
    clean exit, rolls back on exception, always returns the connection to
    the pool (rather than opening/closing a fresh physical connection on
    every call, which is the same handshake cost get_db() used to pay)."""
    conn = _connect_pooled()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_conn():
    """`with get_conn() as conn, conn.cursor() as cur:` — yields the raw
    psycopg2 connection for call sites that want native cursor control
    instead of PGConnection's wrapped `.execute()`. Rows come back as
    RealDictCursor by default, so `row["col"]` still works. Commits on a
    clean exit, rolls back on exception, and returns the connection to the
    pool rather than closing it outright."""
    pool = _get_pool()
    for _attempt in range(MAXCONN + 1):
        raw = pool.getconn()
        try:
            if raw.closed:
                raise psycopg2.InterfaceError("connection already closed")
            with raw.cursor() as probe:
                probe.execute("SELECT 1")
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            pool.putconn(raw, close=True)
    else:
        raw = psycopg2.connect(DATABASE_URL)
        pool = None  # signal below to just close(), not putconn()
    raw.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        yield raw
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        try:
            if not raw.closed and raw.status != psycopg2.extensions.STATUS_READY:
                raw.rollback()
        except Exception:
            pass
        if pool is not None:
            pool.putconn(raw)
        else:
            raw.close()


# auth_excel.py imports this name (originally written against a slightly
# different shared/db.py draft) — it's the same class as PGConnection,
# kept as an alias so that import doesn't need to change.
PGConnWrapper = PGConnection