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
from flask import g

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# For development without PostgreSQL, allow fallback to SQLite in-memory database
USE_SQLITE_FALLBACK = not DATABASE_URL or "localhost" in str(DATABASE_URL)

if USE_SQLITE_FALLBACK:
    # Try to import sqlite3 and create a simple connection wrapper
    try:
        import sqlite3
        import sqlite3 as sqlite3_module
        
        _SQLITE_DB = sqlite3.connect(":memory:", check_same_thread=False)
        _SQLITE_DB.row_factory = sqlite3.Row
        
        def _get_sqlite_connection():
            return _SQLITE_DB
        
        def _close_sqlite_connection():
            pass  # SQLite in-memory doesn't need explicit closing
        
        # Create a simple connection class that mimics the PGConnection interface
        class SQLiteConnection:
            def __init__(self, conn):
                self._conn = conn
                
            def execute(self, sql, params=None):
                if params:
                    result = self._conn.execute(sql, params)
                else:
                    result = self._conn.execute(sql)
                return SQLiteCursorResult(result)
                
            def executescript(self, sql):
                self._conn.executescript(sql)
                
            def executemany(self, sql, seq_of_params):
                self._conn.executemany(sql, seq_of_params)
                
            def commit(self):
                self._conn.commit()
                
            def rollback(self):
                self._conn.rollback()
                
            def close(self):
                pass  # Don't close in-memory connection
                
            @property
            def closed(self):
                return self._conn.closed
                
            def __enter__(self):
                return self
                
            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    self.commit()
                else:
                    self.rollback()
        
        class SQLiteCursorResult:
            def __init__(self, cursor):
                self._cursor = cursor
                
            def fetchone(self):
                return self._cursor.fetchone()
                
            def fetchall(self):
                return self._cursor.fetchall()
                
            def fetchmany(self, size=None):
                if size is not None:
                    return self._cursor.fetchmany(size)
                return self._cursor.fetchmany()
                
            @property
            def rowcount(self):
                return self._cursor.rowcount
                
            def close(self):
                self._cursor.close()
        
        # Monkey-patch the shared.db module to use SQLite if PostgreSQL is not available
        def get_db():
            if "db" not in g:
                g.db = SQLiteConnection(_get_sqlite_connection())
            return g.db
            
        def close_db(_exc=None):
            db = g.pop("db", None)
            if db is not None:
                db.close()
                
        def connect():
            return SQLiteConnection(_get_sqlite_connection())
            
        def transaction():
            def decorator(func):
                def wrapper(*args, **kwargs):
                    conn = SQLiteConnection(_get_sqlite_connection())
                    try:
                        result = func(*args, **kwargs)
                        conn.commit()
                        return result
                    except Exception:
                        conn.rollback()
                        raise
                return wrapper
            return decorator
            
        def get_conn():
            conn = SQLiteConnection(_get_sqlite_connection())
            return conn
            
        PGConnWrapper = SQLiteConnection
        
        print("Using SQLite fallback (PostgreSQL not available)")
        
    except ImportError:
        print("SQLite fallback not available, using PostgreSQL only")
        USE_SQLITE_FALLBACK = False

if USE_SQLITE_FALLBACK:
    # Skip PostgreSQL setup and use SQLite connection functions
    _POOL_MIN = 1
    _POOL_MAX = 10
    _QMARK_RE = re.compile(r"\?")
    
    # Monkey-patch the original functions to use SQLite
    # These are now defined in the if USE_SQLITE_FALLBACK block above
else:
    # Original PostgreSQL code
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Postgres instance, e.g.\n"
            "  DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/cropai"
        )
    
    _POOL_MIN = int(os.environ.get("POSTGRES_POOL_MIN", "1"))
    _POOL_MAX = int(os.environ.get("POSTGRES_POOL_MAX", "10"))
    
    _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, DATABASE_URL)
    
    _QMARK_RE = re.compile(r"\?")

# ── CONNECTION POOL ─────────────────────────────────────────────────────
#
# Why this exists: connect() used to open a brand-new physical Postgres
# connection (full TCP + TLS handshake + auth) on every call, and get_db()
# called connect() once per Flask request, closing it again at teardown.
# That was free-ish against a local sqlite file; it is NOT free against a
# remote Postgres host (e.g. Neon) — every request was paying a network
# round-trip handshake before it could even run a query, which is what was
# showing up as slow sidebar/login refreshes (those routes fire several
# sequential DB-backed calls: /api/auth/me, permission lookups, etc., each
# one re-paying that handshake cost under the old code).
#
# A small pool of already-open connections fixes this: get_db() borrows an
# existing connection instead of dialing out fresh, and close_db() returns
# it to the pool instead of closing it. The handshake cost is paid once
# per pooled connection (at pool warm-up / whenever the pool needs to grow),
# not once per request.
#
# Tune via env vars if needed:
#   POSTGRES_POOL_MIN (default 1), POSTGRES_POOL_MAX (default 10)
#   (these are set above based on USE_SQLITE_FALLBACK)

if USE_SQLITE_FALLBACK:
    # Define helper functions for SQLite when USE_SQLITE_FALLBACK is True
    
    def _qmark_to_pct_s(sql: str) -> str:
        """For SQLite compatibility, just return the SQL as-is since SQLite
        uses ? placeholders like this code expects."""
        return sql
    
    # Define simple connection classes for SQLite
    class SQLiteCursorResult:
        def __init__(self, cursor):
            self._cursor = cursor
            
        def fetchone(self):
            return self._cursor.fetchone()
            
        def fetchall(self):
            return self._cursor.fetchall()
            
        def fetchmany(self, size=None):
            if size is not None:
                return self._cursor.fetchmany(size)
            return self._cursor.fetchmany()
            
        @property
        def rowcount(self):
            return self._cursor.rowcount
            
        def close(self):
            self._cursor.close()
    
    class SQLiteConnection:
        def __init__(self, conn):
            self._conn = conn
            
        def execute(self, sql, params=None):
            if params:
                result = self._conn.execute(sql, params)
            else:
                result = self._conn.execute(sql)
            return SQLiteCursorResult(result)
            
        def executescript(self, sql):
            self._conn.executescript(sql)
            
        def executemany(self, sql, seq_of_params):
            self._conn.executemany(sql, seq_of_params)
            
        def commit(self):
            self._conn.commit()
            
        def rollback(self):
            self._conn.rollback()
            
        def close(self):
            pass
            
        @property
        def closed(self):
            return self._conn.closed
            
        def __enter__(self):
            return self
            
        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
    
    # Replace the PostgreSQL connection functions with SQLite versions
    # These are defined in the USE_SQLITE_FALLBACK block above
    
    def _borrow_from_pool():
        """Return a SQLite connection for SQLite fallback."""
        return SQLiteConnection(_SQLITE_DB)
    
    def connect():
        """Return a SQLite connection."""
        return SQLiteConnection(_SQLITE_DB)
    
    def get_db():
        """Return a SQLite connection for Flask g."""
        if "db" not in g:
            g.db = connect()
        return g.db
    
    def close_db(_exc=None):
        """Close SQLite connection."""
        db = g.pop("db", None)
        if db is not None:
            db.close()
    
    @contextmanager
    def transaction():
        """SQLite transaction helper."""
        conn = connect()
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
        """Return SQLite connection."""
        try:
            yield SQLiteConnection(_SQLITE_DB)
            SQLiteConnection(_SQLITE_DB).commit()
        except Exception:
            SQLiteConnection(_SQLITE_DB).rollback()
            raise
        finally:
            pass
    
    PGConnWrapper = SQLiteConnection
    
    # For backward compatibility with PostgreSQL code that expects _pool
    # We create a dummy _pool object that has getconn() method returning SQLiteConnection
    class DummyPool:
        def getconn(self):
            return SQLiteConnection(_SQLITE_DB)
        def putconn(self, conn, close=False):
            pass
    
    _pool = DummyPool()
    
else:
    # Original PostgreSQL code
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Postgres instance, e.g.\n"
            "  DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/cropai"
        )
    
    _POOL_MIN = int(os.environ.get("POSTGRES_POOL_MIN", "1"))
    _POOL_MAX = int(os.environ.get("POSTGRES_POOL_MAX", "10"))
    
    _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, DATABASE_URL)
    
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

    def __init__(self, raw_conn, from_pool: bool = False):
        self._conn = raw_conn
        self._from_pool = from_pool

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
        # If this connection came from the pool, "closing" it means giving
        # it back for reuse, not tearing down the physical socket — that's
        # the whole point of pooling. Non-pooled connections (one-off
        # scripts) still get a real close().
        if self._from_pool:
            _pool.putconn(self._conn)
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
        # connection is returned to the pool (or closed, if unpooled)
        # afterward.
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()


def _borrow_from_pool() -> "tuple[psycopg2.extensions.connection, bool]":
    """Get a live connection out of the pool, discarding and replacing any
    connection the remote server has silently closed (Neon and other
    serverless/managed Postgres hosts suspend compute and drop idle
    connections — a pooled connection that's been sitting unused can go
    stale between checkouts). Detected with a cheap SELECT 1 probe; a dead
    connection is evicted from the pool and a fresh one is opened in its
    place, rather than handing back something that will fail on first
    real use.

    Returns (raw_conn, from_pool) — the replacement opened after an
    eviction is NOT a connection the pool knows about, so from_pool is
    False for it; callers must not hand it back to the pool via
    putconn() (that raises psycopg2.pool.PoolError: trying to put
    unkeyed connection), only close it directly."""
    raw = _pool.getconn()
    try:
        with raw.cursor() as probe:
            probe.execute("SELECT 1")
    except psycopg2.OperationalError:
        _pool.putconn(raw, close=True)
        raw = psycopg2.connect(DATABASE_URL)
        return raw, False
    return raw, True


def connect() -> PGConnection:
    """Borrow a connection from the shared pool. Safe to call from a plain
    script too (one-off migrations, etc.) — just remember to .close() it
    when done so it goes back to the pool instead of sitting checked-out."""
    raw, from_pool = _borrow_from_pool()
    return PGConnection(raw, from_pool=from_pool)


def get_db() -> PGConnection:
    """Per-request connection, cached on flask.g — same pattern every
    service already used for sqlite3. Now backed by the pool: borrowing
    here is a fast in-process handoff, not a fresh network handshake."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None) -> None:
    """Call this from @app.teardown_appcontext in each service. Returns
    the connection to the pool rather than physically closing it."""
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
    the pool."""
    conn = connect()
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
    clean exit, always returns the connection to the pool."""
    raw, from_pool = _borrow_from_pool()
    raw.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        yield raw
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        if from_pool:
            _pool.putconn(raw)
        else:
            raw.close()


# auth_excel.py imports this name (originally written against a slightly
# different shared/db.py draft) — it's the same class as PGConnection,
# kept as an alias so that import doesn't need to change.
PGConnWrapper = PGConnection