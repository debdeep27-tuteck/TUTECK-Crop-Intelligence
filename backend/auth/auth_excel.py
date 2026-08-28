"""
auth_excel.py
=============
Authentication + role-based access control for CropAI — now backed by
SQLite (users.db) instead of raw Excel files.

WHAT CHANGED: all reads/writes used to go straight to users.xlsx. Now
they go to users.db. The only thing the old .xlsx files are still used
for is a ONE-TIME AUTOMATIC MIGRATION: the very first time init_excel()
runs and users.db doesn't exist yet, this module looks for users.xlsx /
permissions.xlsx / user_permissions.xlsx in this same folder and, if
found, copies every record from them into users.db. After that, the
.xlsx files are never read or written again — they're just left on
disk untouched as a backup. Every new user created from now on goes
straight into users.db.

You do NOT need to run anything separately or change how you call this
module — same import, same function, same routes as before:

    from auth_excel import auth_bp, init_excel

    init_excel()                       # creates users.db + migrates old .xlsx data (first run only)
    app.register_blueprint(auth_bp)    # mounts all /api/auth and /api/users routes

Roles (defaults, editable from the admin panel)
-------------------------------------------------
  admin    -> full CRUD on users, and access to every page
  analyst  -> Dashboard, Irrigation, Recommender, Alerts
  farmer   -> Irrigation, Disease Detection, Recommender

⚠️ SECURITY NOTE: passwords are stored as PLAIN TEXT in users.db, not
hashed. Anyone who can open that file (or a backup/copy of it) can read
every user's actual password. This is fine for a local prototype but is
not safe for a real deployment, especially since people often reuse
passwords across sites. Restrict file permissions on users.db, keep it
out of version control, and consider switching to hashed storage
(werkzeug.security.generate_password_hash / check_password_hash) before
this goes anywhere near production or real user data.

Run standalone for a quick sanity check:
    python auth_excel.py
"""

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, request, jsonify, g

# ── CONFIG ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent

DB_FILE = BASE_DIR / "users.db"

# Old Excel files — only ever read once, during first-run auto-migration.
LEGACY_USERS_XLSX = BASE_DIR / "users.xlsx"
LEGACY_PERMISSIONS_XLSX = BASE_DIR / "permissions.xlsx"
LEGACY_USER_PERMISSIONS_XLSX = BASE_DIR / "user_permissions.xlsx"

ALL_PAGES = ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect", "/cold-storage", "/auction", "/auction-mandi", "/mandi-prices", "/nearest-mandi", "/advisory"]

VALID_ROLES = {"admin", "analyst", "farmer", "state_admin", "district_admin", "mandi"}
VALID_STATUSES = {"active", "restricted"}

# Roles that must be tied to a state (state_admin needs just the state;
# district_admin and mandi need the state AND the district within it — a
# mandi is a physical buying post in one specific district, so its auctions
# always resolve to exactly one state/district pair).
STATE_SCOPED_ROLES = {"state_admin", "district_admin", "mandi"}
DISTRICT_SCOPED_ROLES = {"district_admin", "mandi"}

# Default permissions, used only to seed role_permissions the first time
# the DB is created. After that, the table is the single source of truth
# and is editable live from the admin panel (see /api/permissions routes).
DEFAULT_ROLE_PERMISSIONS = {
    "admin": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect", "/cold-storage", "/auction", "/auction-mandi", "/mandi-prices", "/nearest-mandi", "/advisory"],
        "crud": True,
    },
    "analyst": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/cold-storage", "/auction", "/auction-mandi", "/mandi-prices", "/nearest-mandi", "/advisory"],
        "crud": False,
    },
    "farmer": {
        "pages": ["/irrigation", "/disease", "/recommend-page", "/yield-detect", "/cold-storage", "/auction", "/mandi-prices", "/nearest-mandi", "/advisory"],
        "crud": False,
    },
    "state_admin": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect", "/cold-storage", "/auction", "/auction-mandi", "/mandi-prices", "/nearest-mandi", "/advisory"],
        "crud": False,
    },
    "district_admin": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect", "/cold-storage", "/auction", "/auction-mandi", "/mandi-prices", "/nearest-mandi", "/advisory"],
        "crud": False,
    },
    "mandi": {
        "pages": ["/auction-mandi", "/nearest-mandi"],
        "crud": False,
    },
}

_db_lock = threading.RLock()

# token -> {"uid": str, "email": str, "role": str, "created_at": float}
SESSIONS = {}

auth_bp = Blueprint("auth_excel", __name__)


# ── DB CONNECTION / SCHEMA ───────────────────────────────────────────────

@contextmanager
def _conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_excel():
    """
    Create users.db (with the right tables) if missing, seed default role
    permissions, and — the first time only — migrate any existing
    users.xlsx / permissions.xlsx / user_permissions.xlsx records into it.
    Safe to call on every app startup; migration only runs once (when
    users.db doesn't exist yet).
    """
    db_is_new = not DB_FILE.exists()

    with _db_lock, _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                state TEXT NOT NULL DEFAULT '',
                district TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS role_permissions (
                role TEXT PRIMARY KEY,
                pages TEXT NOT NULL DEFAULT '',
                crud INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_permissions (
                uid TEXT PRIMARY KEY,
                pages TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (uid) REFERENCES users(uid) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                key TEXT PRIMARY KEY,
                applied_at REAL NOT NULL
            )
        """)
        existing_roles = {r["role"] for r in conn.execute("SELECT role FROM role_permissions")}
        for role, cfg in DEFAULT_ROLE_PERMISSIONS.items():
            if role not in existing_roles:
                conn.execute(
                    "INSERT INTO role_permissions (role, pages, crud) VALUES (?, ?, ?)",
                    (role, ",".join(cfg["pages"]), int(bool(cfg["crud"]))),
                )

        # One-time backfill: "/advisory" was added to ALL_PAGES/DEFAULT_ROLE_PERMISSIONS
        # after some installs already had a populated role_permissions table, so those
        # existing rows never picked it up from the seed step above. Runs exactly once
        # (tracked in schema_migrations) — so if an admin later deliberately removes
        # "/advisory" from a role via the admin panel, it stays removed on restart
        # instead of silently coming back.
        already_migrated = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE key = ?", ("advisory_page_backfill_v1",)
        ).fetchone()
        if not already_migrated:
            for role, cfg in DEFAULT_ROLE_PERMISSIONS.items():
                if "/advisory" not in cfg["pages"]:
                    continue
                row = conn.execute("SELECT pages FROM role_permissions WHERE role = ?", (role,)).fetchone()
                if row is None:
                    continue
                current_pages = [p.strip() for p in row["pages"].split(",") if p.strip()]
                if "/advisory" not in current_pages:
                    current_pages.append("/advisory")
                    conn.execute(
                        "UPDATE role_permissions SET pages = ? WHERE role = ?",
                        (",".join(current_pages), role),
                    )
            conn.execute(
                "INSERT INTO schema_migrations (key, applied_at) VALUES (?, ?)",
                ("advisory_page_backfill_v1", time.time()),
            )

    if db_is_new:
        _migrate_legacy_excel_files()


def _migrate_legacy_excel_files():
    """One-time import of records from the old .xlsx files into users.db,
    if those files exist. Never modifies or deletes the .xlsx files."""
    if not (LEGACY_USERS_XLSX.exists() or LEGACY_PERMISSIONS_XLSX.exists() or LEGACY_USER_PERMISSIONS_XLSX.exists()):
        return  # fresh install, nothing to migrate

    try:
        import pandas as pd
    except ImportError:
        print("auth_excel: found legacy .xlsx files but pandas isn't installed, "
              "so they could not be auto-migrated. Install pandas + openpyxl and "
              "restart, or delete the .xlsx files if you don't need them.")
        return

    migrated = {"users": 0, "role_permissions": 0, "user_permissions": 0}

    with _db_lock, _conn() as conn:
        if LEGACY_USERS_XLSX.exists():
            df = pd.read_excel(LEGACY_USERS_XLSX, engine="openpyxl", dtype=str).fillna("")
            for _, row in df.iterrows():
                uid = str(row.get("uid", "")).strip()
                email = str(row.get("email", "")).strip().lower()
                if not uid or not email:
                    continue
                conn.execute(
                    """
                    INSERT INTO users (uid, email, password, role, status, state, district)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET
                        email=excluded.email, password=excluded.password, role=excluded.role,
                        status=excluded.status, state=excluded.state, district=excluded.district
                    """,
                    (
                        uid, email, str(row.get("password", "")),
                        str(row.get("role", "")).strip().lower(),
                        str(row.get("status", "active")).strip().lower() or "active",
                        str(row.get("state", "")), str(row.get("district", "")),
                    ),
                )
                migrated["users"] += 1

        if LEGACY_PERMISSIONS_XLSX.exists():
            df = pd.read_excel(LEGACY_PERMISSIONS_XLSX, engine="openpyxl", dtype=str).fillna("")
            for _, row in df.iterrows():
                role = str(row.get("role", "")).strip().lower()
                if not role:
                    continue
                crud = str(row.get("crud", "")).strip().lower() in ("true", "1", "yes")
                conn.execute(
                    """
                    INSERT INTO role_permissions (role, pages, crud) VALUES (?, ?, ?)
                    ON CONFLICT(role) DO UPDATE SET pages=excluded.pages, crud=excluded.crud
                    """,
                    (role, str(row.get("pages", "")), int(crud)),
                )
                migrated["role_permissions"] += 1

        if LEGACY_USER_PERMISSIONS_XLSX.exists():
            df = pd.read_excel(LEGACY_USER_PERMISSIONS_XLSX, engine="openpyxl", dtype=str).fillna("")
            for _, row in df.iterrows():
                uid = str(row.get("uid", "")).strip()
                if not uid:
                    continue
                conn.execute(
                    """
                    INSERT INTO user_permissions (uid, pages) VALUES (?, ?)
                    ON CONFLICT(uid) DO UPDATE SET pages=excluded.pages
                    """,
                    (uid, str(row.get("pages", ""))),
                )
                migrated["user_permissions"] += 1

    print(f"auth_excel: migrated legacy Excel data into {DB_FILE.name} -> "
          f"{migrated['users']} user(s), {migrated['role_permissions']} role permission row(s), "
          f"{migrated['user_permissions']} user permission row(s). "
          f"The original .xlsx files were left untouched.")


def _new_uid():
    return uuid.uuid4().hex[:12]


# ── ROLE PERMISSIONS ──────────────────────────────────────────────────────

def get_role_permissions():
    """Return the ROLE_PERMISSIONS dict shape, read live from the DB."""
    with _db_lock, _conn() as conn:
        rows = conn.execute("SELECT role, pages, crud FROM role_permissions").fetchall()
    result = {}
    for row in rows:
        role = row["role"].strip().lower()
        if not role:
            continue
        pages = [p.strip() for p in row["pages"].split(",") if p.strip()]
        result[role] = {"pages": pages, "crud": bool(row["crud"])}
    for role in VALID_ROLES:
        result.setdefault(role, {"pages": [], "crud": False})
    return result


def update_role_permissions(role, pages=None, crud=None):
    """Update the pages/crud for a single role. Creates the row if missing."""
    role = role.strip().lower()
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of {sorted(VALID_ROLES)}.")

    if pages is not None:
        bad = [p for p in pages if p not in ALL_PAGES]
        if bad:
            raise ValueError(f"Unknown page(s): {bad}. Valid pages are {ALL_PAGES}.")

    with _db_lock, _conn() as conn:
        existing = conn.execute("SELECT role FROM role_permissions WHERE role = ?", (role,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO role_permissions (role, pages, crud) VALUES (?, ?, ?)",
                (role, ",".join(pages) if pages is not None else "", int(bool(crud)) if crud is not None else 0),
            )
        else:
            if pages is not None:
                conn.execute("UPDATE role_permissions SET pages = ? WHERE role = ?", (",".join(pages), role))
            if crud is not None:
                conn.execute("UPDATE role_permissions SET crud = ? WHERE role = ?", (int(bool(crud)), role))

    return get_role_permissions()[role]


# ── PER-USER PERMISSIONS ──────────────────────────────────────────────────

def get_user_permissions(uid, role=None):
    """
    Return {"pages": [...]} for this specific user. If the user has no row
    yet in user_permissions, seed it from their role's default pages
    (looked up via `role`, or from the users table if not passed).
    """
    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT pages FROM user_permissions WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            if role is None:
                user = get_user(uid)
                role = user["role"] if user else None
            default_pages = get_role_permissions().get(role, {}).get("pages", [])
            conn.execute(
                "INSERT INTO user_permissions (uid, pages) VALUES (?, ?)",
                (uid, ",".join(default_pages)),
            )
            return {"pages": default_pages}

        pages = [p.strip() for p in row["pages"].split(",") if p.strip()]
        return {"pages": pages}


def update_user_permissions(uid, pages):
    """Set the exact list of pages a specific user can access."""
    bad = [p for p in pages if p not in ALL_PAGES]
    if bad:
        raise ValueError(f"Unknown page(s): {bad}. Valid pages are {ALL_PAGES}.")

    with _db_lock, _conn() as conn:
        existing = conn.execute("SELECT uid FROM user_permissions WHERE uid = ?", (uid,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO user_permissions (uid, pages) VALUES (?, ?)", (uid, ",".join(pages)))
        else:
            conn.execute("UPDATE user_permissions SET pages = ? WHERE uid = ?", (",".join(pages), uid))

    return {"pages": pages}


def delete_user_permissions(uid):
    with _db_lock, _conn() as conn:
        conn.execute("DELETE FROM user_permissions WHERE uid = ?", (uid,))


# ── USER OPERATIONS ───────────────────────────────────────────────────────

def _validate_state_district(role, state, district):
    """Enforce that state_admin/district_admin carry the right location fields,
    and that other roles don't. Returns the normalized (state, district)."""
    state = (state or "").strip()
    district = (district or "").strip()

    if role in STATE_SCOPED_ROLES and not state:
        raise ValueError(f"'{role}' accounts require a state.")
    if role in DISTRICT_SCOPED_ROLES and not district:
        raise ValueError(f"'{role}' accounts require a district.")

    if role not in STATE_SCOPED_ROLES:
        state = ""
    if role not in DISTRICT_SCOPED_ROLES:
        district = ""

    return state, district


def create_user(email, password, role, state="", district=""):
    email = email.strip().lower()
    role = role.strip().lower()

    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of {sorted(VALID_ROLES)}.")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")

    state, district = _validate_state_district(role, state, district)

    with _db_lock, _conn() as conn:
        dupe = conn.execute("SELECT 1 FROM users WHERE lower(email) = ?", (email,)).fetchone()
        if dupe:
            raise ValueError("An account with this email already exists.")

        uid = _new_uid()
        conn.execute(
            "INSERT INTO users (uid, email, password, role, status, state, district) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, email, password, role, "active", state, district),
        )

    # Seed this user's individual page permissions from their role's defaults.
    get_user_permissions(uid, role=role)

    return {"uid": uid, "email": email, "role": role, "status": "active", "state": state, "district": district}


def verify_login(email, password):
    """Return {'uid','email','role','status','state','district'} on success, or None on bad credentials."""
    email = email.strip().lower()
    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
    if row is None or row["password"] != password:
        return None
    return {
        "uid": row["uid"], "email": row["email"], "role": row["role"], "status": row["status"],
        "state": row["state"], "district": row["district"],
    }


def list_users():
    """Return all users WITHOUT password hashes — for the admin panel.
    Includes each user's current status, state/district scope, and their
    individual page permissions."""
    with _db_lock, _conn() as conn:
        rows = conn.execute("SELECT uid, email, role, status, state, district FROM users").fetchall()
    users = [dict(row) for row in rows]
    for user in users:
        user["pages"] = get_user_permissions(user["uid"], role=user["role"])["pages"]
    return users


def get_user(uid):
    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT uid, email, role, status, state, district FROM users WHERE uid = ?", (uid,)).fetchone()
    return dict(row) if row else None


def update_user(uid, email=None, password=None, role=None, state=None, district=None):
    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            raise ValueError("User not found.")

        if email:
            email = email.strip().lower()
            dupe = conn.execute(
                "SELECT 1 FROM users WHERE lower(email) = ? AND uid != ?", (email, uid)
            ).fetchone()
            if dupe:
                raise ValueError("Another account already uses this email.")
            conn.execute("UPDATE users SET email = ? WHERE uid = ?", (email, uid))

        effective_role = (role.strip().lower() if role else row["role"])
        if role:
            if effective_role not in VALID_ROLES:
                raise ValueError(f"Invalid role '{effective_role}'. Must be one of {sorted(VALID_ROLES)}.")
            conn.execute("UPDATE users SET role = ? WHERE uid = ?", (effective_role, uid))

        effective_state = state if state is not None else row["state"]
        effective_district = district if district is not None else row["district"]
        norm_state, norm_district = _validate_state_district(effective_role, effective_state, effective_district)
        conn.execute("UPDATE users SET state = ?, district = ? WHERE uid = ?", (norm_state, norm_district, uid))

        if password:
            if len(password) < 6:
                raise ValueError("Password must be at least 6 characters.")
            conn.execute("UPDATE users SET password = ? WHERE uid = ?", (password, uid))

        updated = conn.execute(
            "SELECT uid, email, role, status, state, district FROM users WHERE uid = ?", (uid,)
        ).fetchone()
        return dict(updated)


def delete_user(uid):
    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            raise ValueError("User not found.")
        conn.execute("DELETE FROM users WHERE uid = ?", (uid,))
        conn.execute("DELETE FROM user_permissions WHERE uid = ?", (uid,))
        for tok in [t for t, s in SESSIONS.items() if s["uid"] == uid]:
            SESSIONS.pop(tok, None)


def set_user_status(uid, status):
    """Restrict or reactivate a user. Restricting immediately kills their sessions."""
    status = status.strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {sorted(VALID_STATUSES)}.")

    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            raise ValueError("User not found.")
        conn.execute("UPDATE users SET status = ? WHERE uid = ?", (status, uid))
        updated = conn.execute("SELECT uid, email, role, status FROM users WHERE uid = ?", (uid,)).fetchone()

    if status == "restricted":
        for tok in [t for t, s in SESSIONS.items() if s["uid"] == uid]:
            SESSIONS.pop(tok, None)

    return dict(updated)


# ── SESSIONS (unchanged: in-memory) ───────────────────────────────────────

def issue_session(uid, email, role, state="", district=""):
    token = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars
    SESSIONS[token] = {
        "uid": uid, "email": email, "role": role,
        "state": state or "", "district": district or "",
        "created_at": time.time(),
    }
    return token


def _get_token_from_request():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    body = request.get_json(silent=True) or {}
    return body.get("token") or request.args.get("token")


def get_current_session():
    token = _get_token_from_request()
    if not token:
        return None
    session = SESSIONS.get(token)
    # TEMP DEBUG — remove once the 401 is diagnosed.
    print(f"[get_current_session] pid={os.getpid()} token_received={token!r} "
          f"found={session is not None} sessions_count={len(SESSIONS)} "
          f"known_tokens={list(SESSIONS.keys())}")
    return session


def require_auth(roles=None):
    """
    Decorator for routes. Rejects with 401 if not logged in, 403 if the
    session's role isn't in `roles` (when provided). On success, stashes
    the session on flask.g.user for the view to use.
    """
    def decorator(fn):
        def wrapper(*args, **kwargs):
            session = get_current_session()
            if not session:
                return jsonify({"error": "Not authenticated"}), 401
            user = get_user(session["uid"])
            if not user or user.get("status") == "restricted":
                SESSIONS.pop(_get_token_from_request(), None)
                return jsonify({"error": "This account has been restricted."}), 403
            if roles and session["role"] not in roles:
                return jsonify({"error": "Forbidden — insufficient role"}), 403
            g.user = session
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


# ── ROUTES: SIGNUP / LOGIN / ME / LOGOUT ─────────────────────────────────

@auth_bp.route("/api/auth/signup", methods=["POST"])
def signup():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    role = str(body.get("role", "")).strip().lower()

    if "@" not in email or "." not in email:
        return jsonify({"error": "Please enter a valid email address."}), 400

    try:
        user = create_user(email, password, role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    token = issue_session(user["uid"], user["email"], user["role"], user.get("state"), user.get("district"))
    return jsonify({"token": token, **user, "permissions": get_user_permissions(user["uid"], role=user["role"])}), 201


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))

    user = verify_login(email, password)
    if not user:
        return jsonify({"error": "Invalid email or password."}), 401

    if user.get("status") == "restricted":
        return jsonify({"error": "This account has been restricted. Contact an admin."}), 403

    token = issue_session(user["uid"], user["email"], user["role"], user.get("state"), user.get("district"))
    return jsonify({"token": token, **user, "permissions": get_user_permissions(user["uid"], role=user["role"])}), 200


@auth_bp.route("/api/auth/me")
@require_auth()
def me():
    user = g.user
    return jsonify({**user, "permissions": get_user_permissions(user["uid"], role=user["role"])}), 200


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    token = _get_token_from_request()
    SESSIONS.pop(token, None)
    return jsonify({"status": "logged out"}), 200


# ── ROUTES: ADMIN CRUD ON USERS ──────────────────────────────────────────
# Only role="admin" may list, create-as-admin, update, or delete accounts.

@auth_bp.route("/api/users", methods=["GET"])
@require_auth(roles=["admin"])
def admin_list_users():
    return jsonify(list_users()), 200


@auth_bp.route("/api/users", methods=["POST"])
@require_auth(roles=["admin"])
def admin_create_user():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    role = str(body.get("role", "")).strip().lower()
    state = str(body.get("state", "")).strip()
    district = str(body.get("district", "")).strip()

    if "@" not in email or "." not in email:
        return jsonify({"error": "Please enter a valid email address."}), 400
    try:
        user = create_user(email, password, role, state=state, district=district)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(user), 201


@auth_bp.route("/api/users/<uid>", methods=["GET"])
@require_auth(roles=["admin"])
def admin_get_user(uid):
    user = get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404
    return jsonify(user), 200


@auth_bp.route("/api/users/<uid>", methods=["PUT", "PATCH"])
@require_auth(roles=["admin"])
def admin_update_user(uid):
    body = request.get_json(silent=True) or {}
    try:
        user = update_user(
            uid,
            email=body.get("email"),
            password=body.get("password"),
            role=body.get("role"),
            state=body.get("state"),
            district=body.get("district"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(user), 200


@auth_bp.route("/api/users/<uid>", methods=["DELETE"])
@require_auth(roles=["admin"])
def admin_delete_user(uid):
    if g.user["uid"] == uid:
        return jsonify({"error": "You can't delete your own account while logged in."}), 400
    try:
        delete_user(uid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"status": "deleted", "uid": uid}), 200


@auth_bp.route("/api/users/<uid>/restrict", methods=["POST", "PATCH"])
@require_auth(roles=["admin"])
def admin_restrict_user(uid):
    if g.user["uid"] == uid:
        return jsonify({"error": "You can't restrict your own account while logged in."}), 400

    body = request.get_json(silent=True) or {}
    restricted = body.get("restricted")

    user = get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404

    if restricted is None:
        new_status = "active" if user["status"] == "restricted" else "restricted"
    else:
        new_status = "restricted" if restricted else "active"

    try:
        updated = set_user_status(uid, new_status)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(updated), 200


@auth_bp.route("/api/users/<uid>/permissions", methods=["GET"])
@require_auth(roles=["admin"])
def admin_get_user_permissions(uid):
    user = get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404
    return jsonify({
        "uid": uid,
        **get_user_permissions(uid, role=user["role"]),
        "all_pages": ALL_PAGES,
    }), 200


@auth_bp.route("/api/users/<uid>/permissions", methods=["PUT", "PATCH"])
@require_auth(roles=["admin"])
def admin_update_user_permissions(uid):
    user = get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404

    body = request.get_json(silent=True) or {}
    pages = body.get("pages")

    if not isinstance(pages, list):
        return jsonify({"error": "'pages' must be a list of page paths."}), 400

    try:
        updated = update_user_permissions(uid, pages)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"uid": uid, **updated}), 200


@auth_bp.route("/api/roles", methods=["GET"])
def get_roles():
    """Public: lets the frontend know what pages each role can see, for nav filtering."""
    return jsonify(get_role_permissions()), 200


# ── ROUTES: ADMIN — EDIT ROLE PERMISSIONS ────────────────────────────────

@auth_bp.route("/api/permissions", methods=["GET"])
@require_auth(roles=["admin"])
def admin_get_permissions():
    return jsonify({
        "permissions": get_role_permissions(),
        "all_pages": ALL_PAGES,
        "valid_roles": sorted(VALID_ROLES),
    }), 200


@auth_bp.route("/api/permissions/<role>", methods=["PUT", "PATCH"])
@require_auth(roles=["admin"])
def admin_update_permissions(role):
    body = request.get_json(silent=True) or {}
    pages = body.get("pages")
    crud = body.get("crud")

    if pages is not None and not isinstance(pages, list):
        return jsonify({"error": "'pages' must be a list of page paths."}), 400

    try:
        updated = update_role_permissions(role, pages=pages, crud=crud)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"role": role.strip().lower(), **updated}), 200


# ── STANDALONE SANITY CHECK ──────────────────────────────────────────────

if __name__ == "__main__":
    init_excel()
    print(f"users.db ready at: {DB_FILE}")
    print("Current users:", list_users())
    print("Current role permissions:", get_role_permissions())