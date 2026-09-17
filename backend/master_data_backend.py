"""
master_data_backend.py
======================
Flask microservice on port 6015 for Master Data Configuration.
Provides CRUD endpoints for Cold Storages reference data and Role Permissions.

All endpoints require an active admin Bearer token (checked against backend/auth/active_tokens.json).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request
from flask_cors import CORS

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from shared.db import get_db, close_db, connect as db_connect
from shared.cache import ttl_cache, invalidate_prefix

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("master_data_backend")

app = Flask(__name__)
CORS(app)

@app.teardown_appcontext
def _teardown_close_db(_exc):
    close_db()

TOKENS_FILE = _BACKEND_DIR / "auth" / "active_tokens.json"

ALL_PAGES = [
    "/dashboard",
    "/irrigation",
    "/recommend-page",
    "/alerts",
    "/disease",
    "/yield-detect",
    "/cold-storage",
    "/auction",
    "/auction-mandi",
    "/mandi-prices",
    "/nearest-mandi",
    "/advisory",
    "/credit-score",
    "/storage-config",
    "/find-farmers",
    "/master-data-config",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_auction_metadata(meta_json):
    if not meta_json:
        return {}
    try:
        return json.loads(meta_json)
    except (TypeError, ValueError):
        return {}


def _get_token_from_request() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    body = request.get_json(silent=True) or {}
    return str(body.get("token") or request.args.get("token") or "").strip()


def _get_session_from_tokens_file(token: str) -> dict | None:
    if not token or not TOKENS_FILE.exists():
        return None
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            tokens = json.load(f) or {}
        session = tokens.get(token)
        if session and isinstance(session, dict):
            return session
    except Exception as e:
        logger.error(f"Error reading active_tokens.json: {e}")
    return None


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _get_token_from_request()
        if not token:
            return jsonify({"error": "Authentication token missing"}), 401

        session = _get_session_from_tokens_file(token)
        if not session:
            return jsonify({"error": "Invalid or expired session token"}), 401

        role = str(session.get("role", "")).strip().lower()
        if role != "admin":
            return jsonify({"error": "Forbidden: Admin privilege required"}), 403

        g.user = session
        return fn(*args, **kwargs)
    return wrapper


def init_db():
    """Ensure cold_storages has capacity_mt, role_permissions, and yield_config are ready."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cold_storages (
                id                  SERIAL PRIMARY KEY,
                nhb_id              TEXT,
                name                TEXT NOT NULL,
                state               TEXT NOT NULL,
                district            TEXT NOT NULL,
                block               TEXT,
                village             TEXT,
                latitude            DOUBLE PRECISION,
                longitude           DOUBLE PRECISION,
                total_capacity_mt   DOUBLE PRECISION NOT NULL DEFAULT 0,
                capacity_mt         INTEGER DEFAULT 0,
                storage_type        TEXT DEFAULT 'static',
                source              TEXT NOT NULL DEFAULT 'MANUAL',
                source_year         INTEGER,
                data_quality        TEXT NOT NULL DEFAULT 'OFFICIAL_STATIC',
                last_updated        TEXT NOT NULL,
                created_at          TEXT NOT NULL
            )
        """)
        # Safe migration for capacity_mt
        conn.execute("ALTER TABLE cold_storages ADD COLUMN IF NOT EXISTS capacity_mt INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE cold_storages ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS role_permissions (
                role TEXT PRIMARY KEY,
                pages TEXT NOT NULL DEFAULT '',
                crud INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS yield_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'general',
                description TEXT,
                last_updated TEXT NOT NULL
            )
        """)

        defaults = [
            ("anomaly_alert_threshold", "-10", "thresholds", "Anomaly % below which a land is flagged (e.g. -10 = -10% vs normal)"),
            ("default_radius_km", "5", "mandi", "Default search radius in km for mandi land lookups"),
            ("default_soil_lookup_timeout", "35", "prediction", "Timeout seconds for soil type lookup"),
            ("min_area_hectare", "0.01", "validation", "Minimum allowed area for a land parcel"),
            ("max_area_hectare", "1000", "validation", "Maximum allowed area for a land parcel"),
            ("default_state", "tripura", "general", "Default state when none specified"),
            ("supported_states", "tripura,meghalaya,rajasthan", "general", "Comma-separated list of states with trained models"),
        ]
        now = now_iso()
        for key, value, category, description in defaults:
            conn.execute(
                """INSERT INTO yield_config (key, value, category, description, last_updated)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (key) DO NOTHING""",
                (key, value, category, description, now)
            )
        conn.commit()


# ── HEALTH ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
@app.route("/api/master-data/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "master-data-backend", "port": 6015}), 200


# ── COLD STORAGES CRUD ────────────────────────────────────────────────────────

@app.route("/api/master-data/cold-storages", methods=["GET"])
@require_admin
def list_cold_storages():
    db = get_db()
    state = (request.args.get("state") or "").strip().lower()
    district = (request.args.get("district") or "").strip().lower()

    sql = "SELECT * FROM cold_storages"
    params = []
    clauses = []
    if state:
        clauses.append("LOWER(state) = ?")
        params.append(state)
    if district:
        clauses.append("LOWER(district) = ?")
        params.append(district)

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id ASC"

    rows = db.execute(sql, tuple(params)).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        # normalize capacity
        cap = d.get("capacity_mt")
        if cap is None or cap == 0:
            cap = int(d.get("total_capacity_mt") or 0)
        d["capacity_mt"] = cap
        d["status"] = d.get("status") or "active"
        results.append(d)

    return jsonify(results), 200


@app.route("/api/master-data/cold-storages", methods=["POST"])
@require_admin
def create_cold_storage():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    state = str(body.get("state", "")).strip().lower()
    district = str(body.get("district", "")).strip()

    if not name or not state or not district:
        return jsonify({"error": "name, state, and district are required"}), 400

    capacity_mt = body.get("capacity_mt")
    try:
        capacity_mt = int(capacity_mt) if capacity_mt is not None else 0
    except (ValueError, TypeError):
        capacity_mt = 0

    latitude = body.get("latitude")
    longitude = body.get("longitude")
    try:
        latitude = float(latitude) if latitude is not None and str(latitude).strip() != "" else None
    except (ValueError, TypeError):
        latitude = None
    try:
        longitude = float(longitude) if longitude is not None and str(longitude).strip() != "" else None
    except (ValueError, TypeError):
        longitude = None

    status = str(body.get("status", "active")).strip().lower()
    if status not in ("active", "inactive"):
        status = "active"

    now = now_iso()
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO cold_storages (
            name, state, district, block, village, latitude, longitude,
            total_capacity_mt, capacity_mt, storage_type, source,
            data_quality, status, last_updated, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            name, state, district,
            body.get("block", ""), body.get("village", ""),
            latitude, longitude,
            float(capacity_mt), capacity_mt,
            body.get("storage_type", "static"),
            body.get("source", "MANUAL"),
            body.get("data_quality", "OFFICIAL_STATIC"),
            status,
            now, now
        )
    )
    new_id = cur.fetchone()["id"]
    db.commit()

    invalidate_prefix("cold_storage:")
    row = db.execute("SELECT * FROM cold_storages WHERE id = ?", (new_id,)).fetchone()
    d = dict(row)
    d["capacity_mt"] = d.get("capacity_mt") or int(d.get("total_capacity_mt") or 0)
    return jsonify(d), 201


@app.route("/api/master-data/cold-storages/<int:cs_id>", methods=["PATCH"])
@require_admin
def update_cold_storage(cs_id):
    db = get_db()
    row = db.execute("SELECT * FROM cold_storages WHERE id = ?", (cs_id,)).fetchone()
    if not row:
        return jsonify({"error": "Cold storage not found"}), 404

    body = request.get_json(silent=True) or {}
    fields = []
    params = []

    if "name" in body:
        fields.append("name = ?")
        params.append(str(body["name"]).strip())
    if "state" in body:
        fields.append("state = ?")
        params.append(str(body["state"]).strip().lower())
    if "district" in body:
        fields.append("district = ?")
        params.append(str(body["district"]).strip())
    if "block" in body:
        fields.append("block = ?")
        params.append(str(body["block"]).strip())
    if "village" in body:
        fields.append("village = ?")
        params.append(str(body["village"]).strip())
    if "capacity_mt" in body:
        try:
            cap = int(body["capacity_mt"])
        except (ValueError, TypeError):
            cap = 0
        fields.append("capacity_mt = ?")
        params.append(cap)
        fields.append("total_capacity_mt = ?")
        params.append(float(cap))
    if "latitude" in body:
        lat = body["latitude"]
        lat = float(lat) if lat is not None and str(lat).strip() != "" else None
        fields.append("latitude = ?")
        params.append(lat)
    if "longitude" in body:
        lon = body["longitude"]
        lon = float(lon) if lon is not None and str(lon).strip() != "" else None
        fields.append("longitude = ?")
        params.append(lon)
    if "status" in body:
        st = str(body["status"]).strip().lower()
        if st in ("active", "inactive"):
            fields.append("status = ?")
            params.append(st)
    if "storage_type" in body:
        fields.append("storage_type = ?")
        params.append(str(body["storage_type"]).strip())

    if not fields:
        return jsonify(dict(row)), 200

    fields.append("last_updated = ?")
    params.append(now_iso())

    params.append(cs_id)
    sql = f"UPDATE cold_storages SET {', '.join(fields)} WHERE id = ?"
    db.execute(sql, tuple(params))
    db.commit()

    invalidate_prefix("cold_storage:")
    updated = db.execute("SELECT * FROM cold_storages WHERE id = ?", (cs_id,)).fetchone()
    d = dict(updated)
    d["capacity_mt"] = d.get("capacity_mt") or int(d.get("total_capacity_mt") or 0)
    return jsonify(d), 200


@app.route("/api/master-data/cold-storages/<int:cs_id>", methods=["DELETE"])
@require_admin
def delete_cold_storage(cs_id):
    db = get_db()
    row = db.execute("SELECT id FROM cold_storages WHERE id = ?", (cs_id,)).fetchone()
    if not row:
        return jsonify({"error": "Cold storage not found"}), 404

    db.execute("DELETE FROM cold_storages WHERE id = ?", (cs_id,))
    db.commit()
    invalidate_prefix("cold_storage:")
    return jsonify({"success": True, "deleted_id": cs_id}), 200


# ── ROLE PERMISSIONS CRUD ───────────────────────────────────────────────────

@app.route("/api/master-data/role-permissions", methods=["GET"])
@require_admin
def get_role_permissions():
    db = get_db()
    rows = db.execute("SELECT role, pages, crud FROM role_permissions ORDER BY role ASC").fetchall()
    results = {}
    for r in rows:
        role = r["role"]
        pages_raw = r["pages"] or ""
        pages = [p.strip() for p in pages_raw.split(",") if p.strip()]
        results[role] = {
            "pages": pages,
            "crud": bool(r["crud"])
        }
    return jsonify({
        "permissions": results,
        "all_pages": ALL_PAGES
    }), 200


@app.route("/api/master-data/role-permissions/<role>", methods=["PUT", "PATCH"])
@require_admin
def update_role_permission(role):
    role = str(role).strip().lower()
    body = request.get_json(silent=True) or {}
    pages = body.get("pages")
    crud = body.get("crud")

    db = get_db()
    row = db.execute("SELECT * FROM role_permissions WHERE role = ?", (role,)).fetchone()
    if not row:
        return jsonify({"error": f"Role '{role}' not found"}), 404

    current_pages = [p.strip() for p in (row["pages"] or "").split(",") if p.strip()]
    current_crud = bool(row["crud"])

    if pages is not None:
        if not isinstance(pages, list):
            return jsonify({"error": "'pages' must be a list of page strings"}), 400
        current_pages = [str(p).strip() for p in pages if str(p).strip()]

    if crud is not None:
        current_crud = bool(crud)

    pages_str = ",".join(current_pages)
    db.execute(
        "UPDATE role_permissions SET pages = ?, crud = ? WHERE role = ?",
        (pages_str, 1 if current_crud else 0, role)
    )
    db.commit()

    invalidate_prefix("auth:")

    return jsonify({
        "role": role,
        "pages": current_pages,
        "crud": current_crud
    }), 200


# ── YIELD DETECT CONFIG CRUD ─────────────────────────────────────────────────

@app.route("/api/master-data/yield-config", methods=["GET"])
@require_admin
def list_yield_config():
    db = get_db()
    rows = db.execute("SELECT * FROM yield_config ORDER BY category, key").fetchall()
    return jsonify([dict(r) for r in rows]), 200


@app.route("/api/master-data/yield-config/<path:key>", methods=["PUT"])
@require_admin
def update_yield_config(key):
    key = str(key).strip()
    if not key:
        return jsonify({"error": "Config key is required"}), 400

    body = request.get_json(silent=True) or {}
    value = str(body.get("value", "")).strip()
    category = str(body.get("category", "general")).strip()
    description = str(body.get("description", "")).strip()
    now = now_iso()

    db = get_db()
    db.execute(
        """INSERT INTO yield_config (key, value, category, description, last_updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET
               value = ?,
               category = ?,
               description = ?,
               last_updated = ?""",
        (key, value, category, description, now, value, category, description, now)
    )
    db.commit()
    invalidate_prefix(f"yield_config:{key}")

    row = db.execute("SELECT * FROM yield_config WHERE key = ?", (key,)).fetchone()
    return jsonify(dict(row)), 200


# ── MAIN RUNNER ─────────────────────────────────────────────────────────────

@app.route("/api/master-data/auctions", methods=["GET"])
@require_admin
def admin_list_auctions():
    db = get_db()
    status = (request.args.get("status") or "").strip().lower()
    crop_type = (request.args.get("cropType") or "").strip().lower()
    owner_id = (request.args.get("ownerId") or "").strip().lower()

    sql = "SELECT * FROM auctions"
    params = []
    clauses = []
    if status:
        clauses.append("LOWER(status) = ?")
        params.append(status)
    if crop_type:
        clauses.append("LOWER(category) = ?")
        params.append(crop_type)
    if owner_id:
        clauses.append("LOWER(owner_id) = ?")
        params.append(owner_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"

    rows = db.execute(sql, tuple(params)).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["metadata"] = _parse_auction_metadata(d.get("metadata"))
        results.append(d)
    return jsonify(results), 200


@app.route("/api/master-data/auctions", methods=["POST"])
@require_admin
def admin_create_auction():
    body = request.get_json(silent=True) or {}
    item_label = str(body.get("itemLabel", "")).strip()
    category = str(body.get("category", "")).strip() or None
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    metadata = str(metadata).strip() if metadata else None
    target_quantity = body.get("targetQuantity")
    base_price = body.get("basePrice")
    auction_type = str(body.get("auctionType", "forward")).strip().lower()
    duration_minutes = body.get("durationMinutes")

    if not item_label or target_quantity is None or base_price is None or duration_minutes is None:
        return jsonify({"error": "itemLabel, targetQuantity, basePrice, and durationMinutes are required"}), 400

    try:
        target_quantity = float(target_quantity)
        base_price = float(base_price)
        duration_minutes = int(duration_minutes)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric field"}), 400

    if auction_type not in ("forward", "reverse"):
        auction_type = "forward"

    counter_gap = body.get("counterGap")
    try:
        counter_gap = float(counter_gap) if counter_gap is not None else 0
    except (TypeError, ValueError):
        counter_gap = 0

    extension_minutes = body.get("extensionMinutes")
    try:
        extension_minutes = int(extension_minutes) if extension_minutes is not None else 0
    except (TypeError, ValueError):
        extension_minutes = 0

    starts_at = body.get("startsAt")
    try:
        starts_at = int(starts_at) if starts_at is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        starts_at = int(time.time() * 1000)

    ends_at = starts_at + duration_minutes * 60_000
    remaining_quantity = body.get("remainingQuantity")
    try:
        remaining_quantity = float(remaining_quantity) if remaining_quantity is not None else target_quantity
    except (TypeError, ValueError):
        remaining_quantity = target_quantity

    owner_price = body.get("ownerPrice")
    try:
        owner_price = float(owner_price) if owner_price is not None and str(owner_price).strip() != "" else None
    except (TypeError, ValueError):
        owner_price = None

    now = int(time.time() * 1000)
    auction_id = f"auc_{uuid.uuid4().hex[:12]}"

    db.execute(
        """
        INSERT INTO auctions (
            id, owner_id, item_label, category, metadata,
            target_quantity, remaining_quantity, base_price, owner_price,
            counter_gap, auction_type, duration_minutes, extension_minutes,
            starts_at, ends_at, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?)
        """,
        (
            auction_id,
            body.get("ownerId", "admin"),
            item_label,
            category,
            metadata,
            target_quantity,
            remaining_quantity,
            base_price,
            owner_price,
            counter_gap,
            auction_type,
            duration_minutes,
            extension_minutes,
            starts_at,
            ends_at,
            now,
        ),
    )
    db.commit()

    row = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
    d = dict(row)
    d["metadata"] = _parse_auction_metadata(d.get("metadata"))
    return jsonify(d), 201


@app.route("/api/master-data/auctions/<auction_id>", methods=["GET"])
@require_admin
def admin_get_auction(auction_id):
    db = get_db()
    row = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if not row:
        return jsonify({"error": "Auction not found"}), 404
    d = dict(row)
    d["metadata"] = _parse_auction_metadata(d.get("metadata"))
    return jsonify(d), 200


@app.route("/api/master-data/auctions/<auction_id>", methods=["PATCH"])
@require_admin
def admin_update_auction(auction_id):
    db = get_db()
    row = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if not row:
        return jsonify({"error": "Auction not found"}), 404

    body = request.get_json(silent=True) or {}
    fields = []
    params = []

    if "itemLabel" in body:
        fields.append("item_label = ?")
        params.append(str(body["itemLabel"]).strip())
    if "category" in body:
        fields.append("category = ?")
        params.append(str(body["category"]).strip() or None)
    if "metadata" in body:
        raw = body["metadata"]
        if isinstance(raw, dict):
            raw = json.dumps(raw)
        fields.append("metadata = ?")
        params.append(str(raw).strip() or None)
    if "targetQuantity" in body:
        try:
            tq = float(body["targetQuantity"])
        except (TypeError, ValueError):
            return jsonify({"error": "targetQuantity must be a number"}), 400
        fields.append("target_quantity = ?")
        params.append(tq)
    if "remainingQuantity" in body:
        try:
            rq = float(body["remainingQuantity"])
        except (TypeError, ValueError):
            return jsonify({"error": "remainingQuantity must be a number"}), 400
        fields.append("remaining_quantity = ?")
        params.append(rq)
    if "basePrice" in body:
        try:
            bp = float(body["basePrice"])
        except (TypeError, ValueError):
            return jsonify({"error": "basePrice must be a number"}), 400
        fields.append("base_price = ?")
        params.append(bp)
    if "ownerPrice" in body:
        val = body["ownerPrice"]
        try:
            op = float(val) if val is not None and str(val).strip() != "" else None
        except (TypeError, ValueError):
            return jsonify({"error": "ownerPrice must be a number"}), 400
        fields.append("owner_price = ?")
        params.append(op)
    if "counterGap" in body:
        try:
            cg = float(body["counterGap"])
        except (TypeError, ValueError):
            return jsonify({"error": "counterGap must be a number"}), 400
        fields.append("counter_gap = ?")
        params.append(cg)
    if "auctionType" in body:
        at = str(body["auctionType"]).strip().lower()
        if at not in ("forward", "reverse"):
            return jsonify({"error": "auctionType must be forward or reverse"}), 400
        fields.append("auction_type = ?")
        params.append(at)
    if "durationMinutes" in body:
        try:
            dm = int(body["durationMinutes"])
        except (TypeError, ValueError):
            return jsonify({"error": "durationMinutes must be an integer"}), 400
        fields.append("duration_minutes = ?")
        params.append(dm)
    if "extensionMinutes" in body:
        try:
            em = int(body["extensionMinutes"])
        except (TypeError, ValueError):
            return jsonify({"error": "extensionMinutes must be an integer"}), 400
        fields.append("extension_minutes = ?")
        params.append(em)
    if "startsAt" in body:
        try:
            sa = int(body["startsAt"])
        except (TypeError, ValueError):
            return jsonify({"error": "startsAt must be an integer epoch-ms"}), 400
        fields.append("starts_at = ?")
        params.append(sa)
    if "endsAt" in body:
        try:
            ea = int(body["endsAt"])
        except (TypeError, ValueError):
            return jsonify({"error": "endsAt must be an integer epoch-ms"}), 400
        fields.append("ends_at = ?")
        params.append(ea)
    if "status" in body:
        st = str(body["status"]).strip().lower()
        if st not in ("scheduled", "active", "closed"):
            return jsonify({"error": "status must be scheduled, active, or closed"}), 400
        fields.append("status = ?")
        params.append(st)

    if not fields:
        d = dict(row)
        d["metadata"] = _parse_auction_metadata(d.get("metadata"))
        return jsonify(d), 200

    params.append(auction_id)
    db.execute(f"UPDATE auctions SET {', '.join(fields)} WHERE id=?", tuple(params))
    db.commit()

    updated = db.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
    d = dict(updated)
    d["metadata"] = _parse_auction_metadata(d.get("metadata"))
    return jsonify(d), 200


@app.route("/api/master-data/auctions/<auction_id>", methods=["DELETE"])
@require_admin
def admin_delete_auction(auction_id):
    db = get_db()
    row = db.execute("SELECT id FROM auctions WHERE id=?", (auction_id,)).fetchone()
    if not row:
        return jsonify({"error": "Auction not found"}), 404

    db.execute("DELETE FROM bids WHERE auction_id=?", (auction_id,))
    db.execute("DELETE FROM invitations WHERE auction_id=?", (auction_id,))
    db.execute("DELETE FROM auctions WHERE id=?", (auction_id,))
    db.commit()
    return jsonify({"success": True, "deletedId": auction_id}), 200

def parse_args():
    parser = argparse.ArgumentParser(description="Master Data Backend Microservice")
    parser.add_argument("--port", type=int, default=6015, help="Port to listen on (default 6015)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default 0.0.0.0)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    init_db()
    logger.info(f"Master Data microservice starting on {args.host}:{args.port}")
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        app.run(host=args.host, port=args.port, debug=False)

