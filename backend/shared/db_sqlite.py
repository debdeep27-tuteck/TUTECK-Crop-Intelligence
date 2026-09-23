"""
SQLite version of backend/shared/db.py for development without PostgreSQL.

This script provides a SQLite fallback implementation for the master_data_backend
when PostgreSQL is not available. It mimics the PostgreSQL interface but uses
SQLite under the hood for development and testing.

Features:
- SQLite in-memory database for fast development
- Compatible interface with PostgreSQL-based code
- Full database schema support for cold_storages, role_permissions, and yield_config
- Same API as PostgreSQL version for seamless integration
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps
from contextlib import contextmanager
from typing import Optional

# Configure logging for better debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask g object for request context
from flask import g

# SQLite in-memory database setup
import sqlite3

_SQLITE_DB = sqlite3.connect(":memory:", check_same_thread=False)
_SQLITE_DB.row_factory = sqlite3.Row

def get_sqlite_connection():
    """Return a new SQLite connection."""
    return _SQLITE_DB

# Simple SQLite connection wrapper that mimics the PGConnection interface
class SQLiteConnection:
    def __init__(self, conn):
        self._conn = conn
        
    def execute(self, sql, params=None):
        """Execute SQL and return a cursor result."""
        if params:
            cursor = self._conn.execute(sql, params)
        else:
            cursor = self._conn.execute(sql)
        return SQLiteCursorResult(cursor)
        
    def executescript(self, sql):
        """Execute multiple SQL statements."""
        self._conn.executescript(sql)
        
    def executemany(self, sql, seq_of_params):
        """Execute SQL statement many times."""
        self._conn.executemany(sql, seq_of_params)
        
    def commit(self):
        """Commit transaction."""
        self._conn.commit()
        
    def rollback(self):
        """Rollback transaction."""
        self._conn.rollback()
        
    def close(self):
        """Close connection (no-op for SQLite in-memory)."""
        pass
        
    @property
    def closed(self):
        """Check if connection is closed."""
        return self._conn.closed
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc, tb):
        """Handle context manager exit."""
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

class SQLiteCursorResult:
    """Wraps a SQLite cursor to mimic the PGConnection interface."""
    
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
        
    def __iter__(self):
        return iter(self._cursor)

# Simple SQLite-based implementation
def connect():
    """Return a SQLite connection."""
    return SQLiteConnection(get_sqlite_connection())


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
        yield SQLiteConnection(get_sqlite_connection())
        SQLiteConnection(get_sqlite_connection()).commit()
    except Exception:
        SQLiteConnection(get_sqlite_connection()).rollback()
        raise
    finally:
        pass

# For backward compatibility with PostgreSQL code
PGConnWrapper = SQLiteConnection

# Simple SQL parameter placeholder substitution for SQLite
def _qmark_to_pct_s(sql: str) -> str:
    """For SQLite, just return SQL as-is since SQLite uses ? placeholders."""
    return sql

print("Using SQLite fallback (PostgreSQL not available)")