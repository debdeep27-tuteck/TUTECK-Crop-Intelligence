"""
migrate_sqlite_to_postgres.py
==============================
One-time script: copies your EXISTING users.db (SQLite) data into the
new Postgres database, so you don't lose your current users/sessions
when switching auth_excel.py over to Postgres.

Run this ONCE, after:
  1. Setting DATABASE_URL in your environment (Postgres must already
     have the tables created — run your updated auth_excel.py's
     init_excel() first, e.g. by just starting the gateway once, so
     the users/role_permissions/user_permissions tables exist).
  2. Placing this script in the same folder as your old users.db.

Usage:
    DATABASE_URL="postgresql://..." python migrate_sqlite_to_postgres.py

Safe to re-run: every insert uses ON CONFLICT DO UPDATE, so running
this twice just re-syncs rather than erroring or duplicating.

This script does NOT touch or delete users.db — it's read-only against
SQLite, so your old file is untouched as a backup even after migrating.
"""

import os
import sqlite3
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE_DIR = Path(__file__).resolve().parent
SQLITE_DB = BASE_DIR / "users.db"

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL is not set. Example:")
    print('  DATABASE_URL="postgresql://user:pass@host/db?sslmode=require" python migrate_sqlite_to_postgres.py')
    sys.exit(1)

if not SQLITE_DB.exists():
    print(f"ERROR: {SQLITE_DB} not found. Run this script from the folder containing users.db.")
    sys.exit(1)


def main():
    sq_conn = sqlite3.connect(str(SQLITE_DB))
    sq_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(DATABASE_URL)
    pg_cur = pg_conn.cursor()

    counts = {"users": 0, "role_permissions": 0, "user_permissions": 0}

    # ── users ──────────────────────────────────────────────────────────
    for row in sq_conn.execute("SELECT * FROM users"):
        cols = row.keys()
        pg_cur.execute(
            """
            INSERT INTO users (uid, email, password, role, status, state, district,
                                address, latitude, longitude, storage_id)
            VALUES (%(uid)s, %(email)s, %(password)s, %(role)s, %(status)s, %(state)s,
                    %(district)s, %(address)s, %(latitude)s, %(longitude)s, %(storage_id)s)
            ON CONFLICT (uid) DO UPDATE SET
                email = EXCLUDED.email, password = EXCLUDED.password, role = EXCLUDED.role,
                status = EXCLUDED.status, state = EXCLUDED.state, district = EXCLUDED.district,
                address = EXCLUDED.address, latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude, storage_id = EXCLUDED.storage_id
            """,
            {
                "uid": row["uid"],
                "email": row["email"],
                "password": row["password"],
                "role": row["role"],
                "status": row["status"],
                "state": row["state"] if "state" in cols else "",
                "district": row["district"] if "district" in cols else "",
                "address": row["address"] if "address" in cols else "",
                "latitude": row["latitude"] if "latitude" in cols else None,
                "longitude": row["longitude"] if "longitude" in cols else None,
                "storage_id": row["storage_id"] if "storage_id" in cols else None,
            },
        )
        counts["users"] += 1

    # ── role_permissions ──────────────────────────────────────────────
    for row in sq_conn.execute("SELECT * FROM role_permissions"):
        pg_cur.execute(
            """
            INSERT INTO role_permissions (role, pages, crud)
            VALUES (%(role)s, %(pages)s, %(crud)s)
            ON CONFLICT (role) DO UPDATE SET pages = EXCLUDED.pages, crud = EXCLUDED.crud
            """,
            {"role": row["role"], "pages": row["pages"], "crud": row["crud"]},
        )
        counts["role_permissions"] += 1

    # ── user_permissions ──────────────────────────────────────────────
    for row in sq_conn.execute("SELECT * FROM user_permissions"):
        pg_cur.execute(
            """
            INSERT INTO user_permissions (uid, pages)
            VALUES (%(uid)s, %(pages)s)
            ON CONFLICT (uid) DO UPDATE SET pages = EXCLUDED.pages
            """,
            {"uid": row["uid"], "pages": row["pages"]},
        )
        counts["user_permissions"] += 1

    pg_conn.commit()
    pg_cur.close()
    pg_conn.close()
    sq_conn.close()

    print("Migration complete:")
    print(f"  users:             {counts['users']}")
    print(f"  role_permissions:  {counts['role_permissions']}")
    print(f"  user_permissions:  {counts['user_permissions']}")
    print(f"\nOriginal {SQLITE_DB.name} was NOT modified — safe to keep as a backup.")


if __name__ == "__main__":
    main()