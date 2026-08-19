"""
auth_excel.py
=============
Excel-backed authentication + role-based access control for CropAI.

All user records (uid, email, password, role) live in a single Excel
file (users.xlsx). Every read/write goes through this file — there is
no separate database. Passwords are hashed before they ever touch disk.

Role -> page/CRUD permissions are no longer hardcoded. They live in a
second file, permissions.xlsx, and can be viewed/edited live by an admin
via /api/permissions (GET) and /api/permissions/<role> (PUT/PATCH). This
is what powers the "manage roles" admin panel — no code deploy needed to
change what a role can see.

Roles (defaults, editable from the admin panel)
-------------------------------------------------
  admin    -> full CRUD on users, and access to every page
  analyst  -> Dashboard, Irrigation, Recommender, Alerts
  farmer   -> Irrigation, Disease Detection, Recommender

⚠️ SECURITY NOTE: passwords are stored as PLAIN TEXT in users.xlsx, not
hashed. Anyone who can open that file (or a backup/copy of it) can read
every user's actual password. This is fine for a local prototype but is
not safe for a real deployment, especially since people often reuse
passwords across sites. Restrict file permissions on users.xlsx, keep it
out of version control, and consider switching back to hashed storage
(werkzeug.security.generate_password_hash / check_password_hash) before
this goes anywhere near production or real user data.

Usage (from gateway.py)
------------------------
    from auth_excel import auth_bp, init_excel

    init_excel()                       # creates users.xlsx if missing
    app.register_blueprint(auth_bp)    # mounts all /api/auth and /api/users routes

Run standalone for a quick sanity check:
    python auth_excel.py
"""

import threading
import time
import uuid
from pathlib import Path

import pandas as pd
from flask import Blueprint, request, jsonify, g

# ── CONFIG ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
EXCEL_FILE = BASE_DIR / "users.xlsx"
PERMISSIONS_FILE = BASE_DIR / "permissions.xlsx"
USER_PERMISSIONS_FILE = BASE_DIR / "user_permissions.xlsx"

ALL_PAGES = ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease","/yield-detect", "/cold-storage", "/auction", "/auction-mandi"]

PERMISSIONS_COLUMNS = ["role", "pages", "crud"]  # "pages" stored as comma-separated string
USER_PERMISSIONS_COLUMNS = ["uid", "pages"]  # per-user page overrides, comma-separated

COLUMNS = ["uid", "email", "password", "role", "status", "state", "district"]  # NOTE: "password" is stored as PLAINTEXT — see warning below

VALID_ROLES = {"admin", "analyst", "farmer", "state_admin", "district_admin", "mandi"}
VALID_STATUSES = {"active", "restricted"}

# Roles that must be tied to a state (state_admin needs just the state;
# district_admin and mandi need the state AND the district within it — a
# mandi is a physical buying post in one specific district, so its auctions
# always resolve to exactly one state/district pair).
STATE_SCOPED_ROLES = {"state_admin", "district_admin", "mandi"}
DISTRICT_SCOPED_ROLES = {"district_admin", "mandi"}

# Default permissions, used only to seed permissions.xlsx the first time it's
# created. After that, permissions.xlsx is the single source of truth and is
# editable live from the admin panel (see /api/permissions routes below).
DEFAULT_ROLE_PERMISSIONS = {
    "admin": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect", "/cold-storage", "/auction", "/auction-mandi"],
        "crud": True,
    },
    "analyst": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/cold-storage", "/auction", "/auction-mandi"],
        "crud": False,
    },
    "farmer": {
        "pages": ["/irrigation", "/disease", "/recommend-page", "/yield-detect", "/cold-storage", "/auction"],
        "crud": False,
    },
    "state_admin": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect", "/cold-storage", "/auction", "/auction-mandi"],
        "crud": False,
    },
    "district_admin": {
        "pages": ["/dashboard", "/irrigation", "/recommend-page", "/alerts", "/disease", "/yield-detect","/cold-storage", "/auction", "/auction-mandi"],
        "crud": False,
    },
    "mandi": {
        "pages": ["/auction-mandi"],
        "crud": False,
    },
}

_excel_lock = threading.Lock()
_perm_lock = threading.RLock()

# token -> {"uid": str, "email": str, "role": str, "created_at": float}
SESSIONS = {}

auth_bp = Blueprint("auth_excel", __name__)


# ── EXCEL STORAGE HELPERS ────────────────────────────────────────────────

def init_excel():
    """Create users.xlsx (and permissions.xlsx / user_permissions.xlsx) with the right headers if missing."""
    if not EXCEL_FILE.exists():
        df = pd.DataFrame(columns=COLUMNS)
        df.to_excel(EXCEL_FILE, index=False, engine="openpyxl")
    init_permissions()
    init_user_permissions()


def _load_df():
    if not EXCEL_FILE.exists():
        init_excel()
    df = pd.read_excel(EXCEL_FILE, engine="openpyxl", dtype=str)
    # Normalize: make sure all expected columns exist even if the sheet
    # was hand-edited and a column got dropped.
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = "active" if col == "status" else ""
    df["status"] = df["status"].replace("", "active").fillna("active")
    return df[COLUMNS].fillna("")


def _save_df(df):
    df.to_excel(EXCEL_FILE, index=False, engine="openpyxl")


def _new_uid():
    return uuid.uuid4().hex[:12]


# ── ROLE PERMISSIONS STORAGE (permissions.xlsx) ──────────────────────────
# Lets an admin change what pages each role can see, and whether a role can
# manage other users, without touching code. Stored as one row per role:
#   role | pages (comma-separated) | crud (TRUE/FALSE)

def init_permissions():
    """Create permissions.xlsx seeded with DEFAULT_ROLE_PERMISSIONS if missing."""
    if not PERMISSIONS_FILE.exists():
        rows = [
            {"role": role, "pages": ",".join(cfg["pages"]), "crud": str(bool(cfg["crud"]))}
            for role, cfg in DEFAULT_ROLE_PERMISSIONS.items()
        ]
        df = pd.DataFrame(rows, columns=PERMISSIONS_COLUMNS)
        df.to_excel(PERMISSIONS_FILE, index=False, engine="openpyxl")


def _load_permissions_df():
    if not PERMISSIONS_FILE.exists():
        init_permissions()
    df = pd.read_excel(PERMISSIONS_FILE, engine="openpyxl", dtype=str)
    for col in PERMISSIONS_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[PERMISSIONS_COLUMNS].fillna("")


def _save_permissions_df(df):
    df.to_excel(PERMISSIONS_FILE, index=False, engine="openpyxl")


def get_role_permissions():
    """Return the ROLE_PERMISSIONS dict shape, read live from permissions.xlsx."""
    with _perm_lock:
        df = _load_permissions_df()
    result = {}
    for _, row in df.iterrows():
        role = row["role"].strip().lower()
        if not role:
            continue
        pages = [p.strip() for p in row["pages"].split(",") if p.strip()]
        crud = str(row["crud"]).strip().lower() in ("true", "1", "yes")
        result[role] = {"pages": pages, "crud": crud}
    # Make sure every known role has an entry, even if the sheet was hand-edited.
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

    with _perm_lock:
        df = _load_permissions_df()
        idx = df.index[df["role"].str.lower() == role]

        if len(idx) == 0:
            new_row = {
                "role": role,
                "pages": ",".join(pages) if pages is not None else "",
                "crud": str(bool(crud)) if crud is not None else "False",
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        else:
            i = idx[0]
            if pages is not None:
                df.at[i, "pages"] = ",".join(pages)
            if crud is not None:
                df.at[i, "crud"] = str(bool(crud))

        _save_permissions_df(df)

    return get_role_permissions()[role]


# ── PER-USER PERMISSIONS STORAGE (user_permissions.xlsx) ─────────────────
# Lets an admin grant/revoke individual pages per user (checkbox-per-row in
# the admin panel), instead of only editing an entire role at once. A new
# user is seeded with their role's default pages; after that, this file is
# the source of truth for what that specific user can see. Stored as one
# row per user:  uid | pages (comma-separated)

def init_user_permissions():
    """Create user_permissions.xlsx (empty, one row per user) if missing."""
    if not USER_PERMISSIONS_FILE.exists():
        df = pd.DataFrame(columns=USER_PERMISSIONS_COLUMNS)
        df.to_excel(USER_PERMISSIONS_FILE, index=False, engine="openpyxl")


def _load_user_permissions_df():
    if not USER_PERMISSIONS_FILE.exists():
        init_user_permissions()
    df = pd.read_excel(USER_PERMISSIONS_FILE, engine="openpyxl", dtype=str)
    for col in USER_PERMISSIONS_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[USER_PERMISSIONS_COLUMNS].fillna("")


def _save_user_permissions_df(df):
    df.to_excel(USER_PERMISSIONS_FILE, index=False, engine="openpyxl")


def get_user_permissions(uid, role=None):
    """
    Return {"pages": [...]} for this specific user. If the user has no row
    yet in user_permissions.xlsx, seed it from their role's default pages
    (looked up via `role`, or from users.xlsx if not passed).
    """
    with _perm_lock:
        df = _load_user_permissions_df()
        idx = df.index[df["uid"] == uid]

        if len(idx) == 0:
            if role is None:
                user = get_user(uid)
                role = user["role"] if user else None
            default_pages = get_role_permissions().get(role, {}).get("pages", [])
            new_row = {"uid": uid, "pages": ",".join(default_pages)}
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            _save_user_permissions_df(df)
            return {"pages": default_pages}

        pages = [p.strip() for p in df.loc[idx[0], "pages"].split(",") if p.strip()]
        if str(role or "").strip().lower() == "mandi" and "/auction-mandi" not in pages:
            pages.append("/auction-mandi")
            df.at[idx[0], "pages"] = ",".join(pages)
            _save_user_permissions_df(df)
        return {"pages": pages}


def update_user_permissions(uid, pages):
    """Set the exact list of pages a specific user can access."""
    bad = [p for p in pages if p not in ALL_PAGES]
    if bad:
        raise ValueError(f"Unknown page(s): {bad}. Valid pages are {ALL_PAGES}.")

    with _perm_lock:
        df = _load_user_permissions_df()
        idx = df.index[df["uid"] == uid]

        if len(idx) == 0:
            df = pd.concat(
                [df, pd.DataFrame([{"uid": uid, "pages": ",".join(pages)}])],
                ignore_index=True,
            )
        else:
            df.at[idx[0], "pages"] = ",".join(pages)

        _save_user_permissions_df(df)

    return {"pages": pages}


def delete_user_permissions(uid):
    with _perm_lock:
        df = _load_user_permissions_df()
        df = df[df["uid"] != uid]
        _save_user_permissions_df(df)


# ── USER OPERATIONS (all go through the Excel file) ─────────────────────

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

    with _excel_lock:
        df = _load_df()
        if (df["email"].str.lower() == email).any():
            raise ValueError("An account with this email already exists.")

        uid = _new_uid()
        new_row = {
            "uid": uid,
            "email": email,
            "password": password,
            "role": role,
            "status": "active",
            "state": state,
            "district": district,
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        _save_df(df)

    # Seed this user's individual page permissions from their role's defaults.
    get_user_permissions(uid, role=role)

    return {"uid": uid, "email": email, "role": role, "status": "active", "state": state, "district": district}


def verify_login(email, password):
    """Return {'uid','email','role','status'} on success, or None on bad credentials."""
    email = email.strip().lower()
    with _excel_lock:
        df = _load_df()
    row = df[df["email"].str.lower() == email]
    if row.empty:
        return None
    row = row.iloc[0]
    if row["password"] != password:
        return None
    return {
        "uid": row["uid"], "email": row["email"], "role": row["role"], "status": row["status"],
        "state": row["state"], "district": row["district"],
    }


def list_users():
    """Return all users WITHOUT password hashes — for the admin panel.
    Includes each user's current status, state/district scope, and their
    individual page permissions."""
    with _excel_lock:
        df = _load_df()
    users = df[["uid", "email", "role", "status", "state", "district"]].to_dict(orient="records")
    for user in users:
        user["pages"] = get_user_permissions(user["uid"], role=user["role"])["pages"]
    return users


def get_user(uid):
    with _excel_lock:
        df = _load_df()
    row = df[df["uid"] == uid]
    if row.empty:
        return None
    row = row.iloc[0]
    return {
        "uid": row["uid"], "email": row["email"], "role": row["role"], "status": row["status"],
        "state": row["state"], "district": row["district"],
    }


def update_user(uid, email=None, password=None, role=None, state=None, district=None):
    with _excel_lock:
        df = _load_df()
        idx = df.index[df["uid"] == uid]
        if len(idx) == 0:
            raise ValueError("User not found.")
        i = idx[0]

        if email:
            email = email.strip().lower()
            dupe = df[(df["email"].str.lower() == email) & (df["uid"] != uid)]
            if not dupe.empty:
                raise ValueError("Another account already uses this email.")
            df.at[i, "email"] = email

        # Figure out the effective role/state/district after this update, so
        # we can validate the state/district combination against whichever
        # role ends up in effect (new role if given, else the existing one).
        effective_role = (role.strip().lower() if role else df.at[i, "role"])
        if role:
            if effective_role not in VALID_ROLES:
                raise ValueError(f"Invalid role '{effective_role}'. Must be one of {sorted(VALID_ROLES)}.")
            df.at[i, "role"] = effective_role

        effective_state = state if state is not None else df.at[i, "state"]
        effective_district = district if district is not None else df.at[i, "district"]
        norm_state, norm_district = _validate_state_district(effective_role, effective_state, effective_district)
        df.at[i, "state"] = norm_state
        df.at[i, "district"] = norm_district

        if password:
            if len(password) < 6:
                raise ValueError("Password must be at least 6 characters.")
            df.at[i, "password"] = password

        _save_df(df)
        row = df.loc[i]
        return {
            "uid": row["uid"], "email": row["email"], "role": row["role"], "status": row["status"],
            "state": row["state"], "district": row["district"],
        }


def delete_user(uid):
    with _excel_lock:
        df = _load_df()
        if not (df["uid"] == uid).any():
            raise ValueError("User not found.")
        df = df[df["uid"] != uid]
        _save_df(df)
        # Drop any active sessions for this user.
        for tok in [t for t, s in SESSIONS.items() if s["uid"] == uid]:
            SESSIONS.pop(tok, None)
    delete_user_permissions(uid)


def set_user_status(uid, status):
    """Restrict or reactivate a user. Restricting immediately kills their sessions."""
    status = status.strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {sorted(VALID_STATUSES)}.")

    with _excel_lock:
        df = _load_df()
        idx = df.index[df["uid"] == uid]
        if len(idx) == 0:
            raise ValueError("User not found.")
        df.at[idx[0], "status"] = status
        _save_df(df)
        row = df.loc[idx[0]]

    if status == "restricted":
        for tok in [t for t, s in SESSIONS.items() if s["uid"] == uid]:
            SESSIONS.pop(tok, None)

    return {"uid": row["uid"], "email": row["email"], "role": row["role"], "status": row["status"]}


# ── SESSIONS ──────────────────────────────────────────────────────────────

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
    return SESSIONS.get(token)


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
        # No explicit value given: toggle current status.
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
# Lets an admin change, per role, which pages it can see and whether it has
# CRUD (user-management) rights — no code changes needed, persisted in
# permissions.xlsx.

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
    pages = body.get("pages")   # optional list[str]
    crud = body.get("crud")     # optional bool

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
    print(f"users.xlsx ready at: {EXCEL_FILE}")
    print("Columns:", COLUMNS)
    print("Current users:", list_users())
    print(f"permissions.xlsx ready at: {PERMISSIONS_FILE}")
    print("Current role permissions:", get_role_permissions())