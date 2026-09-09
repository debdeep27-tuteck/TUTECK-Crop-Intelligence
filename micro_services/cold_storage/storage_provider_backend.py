"""
storage_provider_backend.py

Backend service for "Storage Provider Configuration": lets a storage provider
user manage their assigned cold storage facility's basic configuration
(location confirmation, free capacity, active status, notes).

This follows the same patterns as cold_storage_backend.py:
- Single-file Flask service
- Own SQLite DB table inside cold_storage.db
- Auth via gateway token verification (/api/auth/me)
- CORS enabled

Run standalone:
    pip install flask flask-cors requests
    python storage_provider_backend.py --port 5020

Wire into main.py / gateway.py:
    - main.py launches this on port 5020 alongside the other services.
    - gateway.py forwards /api/storage-provider/* -> http://127.0.0.1:5020/api/storage-provider/*
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS

# ── CONFIG ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "cold_storage.db"

GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

# yield_platform_service.py owns farmer parcel/land data (the "Yield Detect"
# database). It's a separate microservice gated by its own shared API key —
# see yield_platform_service.py's docstring for the auth model.
YIELD_PLATFORM_URL = os.environ.get("YIELD_PLATFORM_URL", "http://127.0.0.1:6100")
YIELD_PLATFORM_API_KEY = os.environ.get("YIELD_PLATFORM_SERVICE_API_KEY", "")

# Forward geocoding for facility addresses. Defaults to OpenStreetMap's public
# Nominatim endpoint (no API key needed), but can be pointed at any provider
# that accepts a `q` query param and returns a JSON array with lat/lon fields.
GEOCODING_SERVICE_URL = os.environ.get("GEOCODING_SERVICE_URL", "https://nominatim.openstreetmap.org/search")
GEOCODING_USER_AGENT = os.environ.get("GEOCODING_USER_AGENT", "cold-storage-intelligence/1.0")

app = Flask(__name__)
CORS(app)


# ── DB HELPERS ────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS storage_provider_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility_id INTEGER NOT NULL REFERENCES cold_storages(id) ON DELETE CASCADE,
            provider_email TEXT NOT NULL,
            free_capacity_mt REAL NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(facility_id, provider_email)
        );
        CREATE INDEX IF NOT EXISTS idx_spc_provider ON storage_provider_configs(provider_email);
        CREATE INDEX IF NOT EXISTS idx_spc_facility ON storage_provider_configs(facility_id);
    """)
    conn.commit()
    conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ── AUTH ──────────────────────────────────────────────────────────────────────

def verify_token(token: str) -> dict | None:
    if not token:
        return None
    try:
        resp = requests.get(
            f"{GATEWAY_INTERNAL_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.exceptions.RequestException:
        return None


def require_auth(roles: list[str] | None = None):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
            user = verify_token(token)
            if not user:
                return jsonify({"error": "Unauthorized — missing or invalid session token"}), 401
            if roles and (user.get("role") or "").lower() not in [r.lower() for r in roles]:
                return jsonify({"error": "Forbidden — this action requires role: " + ", ".join(roles)}), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapped
    return decorator


# ── ROLE-BASED ACCESS HELPERS ────────────────────────────────────────────────

def _can_access_facility(user: dict, facility_state: str, facility_district: str) -> bool:
    role = (user.get("role") or "").lower()
    if role == "admin":
        return True
    if role == "storage_provider":
        return True
    if role == "district_admin":
        user_state = (user.get("state") or "").strip().lower()
        user_district = (user.get("district") or "").strip().lower()
        return (not user_state or facility_state.lower() == user_state) and \
               (not user_district or facility_district.lower() == user_district)
    if role == "state_admin":
        user_state = (user.get("state") or "").strip().lower()
        return not user_state or facility_state.lower() == user_state
    return False


def _scope_clause(user: dict) -> tuple[str, list]:
    role = (user.get("role") or "").lower()
    clauses = []
    params = []
    if role == "storage_provider":
        provider_email = (user.get("email") or "").strip().lower()
        clauses.append("spc.provider_email = ?")
        params.append(provider_email)
    elif role == "district_admin":
        user_state = (user.get("state") or "").strip()
        user_district = (user.get("district") or "").strip()
        if user_district:
            clauses.append("cs.district = ?")
            params.append(user_district)
        if user_state:
            clauses.append("cs.state = ?")
            params.append(user_state)
    elif role == "state_admin":
        user_state = (user.get("state") or "").strip()
        if user_state:
            clauses.append("cs.state = ?")
            params.append(user_state)
    return " AND ".join(clauses) if clauses else "", params


# ── ROUTES: FACILITIES (read-only reference) ──────────────────────────────────

@app.route("/api/storage-provider/facilities", methods=["GET"])
@require_auth()
def list_facilities():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, state, district, block, village, latitude, longitude, total_capacity_mt, storage_type FROM cold_storages ORDER BY name COLLATE NOCASE"
    ).fetchall()
    db.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/storage-provider/facilities/<int:facility_id>", methods=["GET"])
@require_auth()
def get_facility(facility_id):
    db = get_db()
    row = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "facility not found"}), 404
    return jsonify(row_to_dict(row))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    r = 6371.0088  # mean Earth radius, km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


@app.route("/api/storage-provider/facilities/<int:facility_id>/nearby-farmers", methods=["GET"])
@require_auth(roles=["storage_provider", "admin"])
def nearby_farmers(facility_id):
    """Find farmer land parcels (from yield_platform_service's parcels table)
    within a given radius of this facility's location."""
    radius_km = request.args.get("radius_km", type=float)
    if radius_km is None or radius_km <= 0:
        return jsonify({"error": "radius_km must be a positive number"}), 400

    db = get_db()
    facility = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    db.close()
    if not facility:
        return jsonify({"error": "facility not found"}), 404

    facility = row_to_dict(facility)
    f_lat, f_lng = facility.get("latitude"), facility.get("longitude")
    if f_lat is None or f_lng is None:
        return jsonify({"error": "This facility does not have a location set yet."}), 400

    try:
        headers = {}
        if YIELD_PLATFORM_API_KEY:
            headers["Authorization"] = f"Bearer {YIELD_PLATFORM_API_KEY}"
        resp = requests.get(f"{YIELD_PLATFORM_URL}/parcels", headers=headers, timeout=10)
        resp.raise_for_status()
        parcels = resp.json()
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach the Yield Detect service: {exc}"}), 502

    results = []
    for p in parcels:
        p_lat, p_lng = p.get("latitude"), p.get("longitude")
        if p_lat is None or p_lng is None:
            continue
        try:
            distance_km = haversine_km(float(f_lat), float(f_lng), float(p_lat), float(p_lng))
        except (TypeError, ValueError):
            continue
        if distance_km <= radius_km:
            results.append({
                "parcel_id": p.get("id"),
                "owner_id": p.get("ownerId"),
                "label": p.get("label"),
                "latitude": p_lat,
                "longitude": p_lng,
                "area_hectare": p.get("areaHectare"),
                "distance_km": round(distance_km, 2),
            })

    results.sort(key=lambda r: r["distance_km"])
    return jsonify({
        "facility_id": facility_id,
        "facility_name": facility.get("name"),
        "center": {"latitude": f_lat, "longitude": f_lng},
        "radius_km": radius_km,
        "count": len(results),
        "farmers": results,
    })


@app.route("/api/storage-provider/facilities/<int:facility_id>/geofence", methods=["POST"])
@require_auth(roles=["storage_provider", "admin"])
def update_facility_geofence(facility_id):
    body = request.get_json(silent=True) or {}
    latitude = body.get("latitude")
    longitude = body.get("longitude")

    if latitude is None or longitude is None:
        return jsonify({"error": "latitude and longitude are required"}), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be valid numbers"}), 400

    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return jsonify({"error": "latitude must be between -90 and 90, longitude between -180 and 180"}), 400

    db = get_db()
    row = db.execute("SELECT id FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "facility not found"}), 404

    db.execute(
        "UPDATE cold_storages SET latitude = ?, longitude = ? WHERE id = ?",
        (latitude, longitude, facility_id),
    )
    db.commit()
    updated = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    db.close()
    return jsonify(row_to_dict(updated))


@app.route("/api/storage-provider/facilities/<int:facility_id>/geocode-address", methods=["POST"])
@require_auth(roles=["storage_provider", "admin"])
def geocode_facility_address(facility_id):
    """Resolve an address (or the facility's state/district/block/village
    fields, if no address is given) to lat/lng via GEOCODING_SERVICE_URL,
    then store the result on the facility — same effect as calling
    /geofence directly, but starting from an address instead of coordinates."""
    db = get_db()
    row = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "facility not found"}), 404
    facility = row_to_dict(row)

    body = request.get_json(silent=True) or {}
    address = (body.get("address") or "").strip()
    if not address:
        parts = [facility.get("village"), facility.get("block"), facility.get("district"), facility.get("state")]
        address = ", ".join(p for p in parts if p)
    if not address:
        db.close()
        return jsonify({"error": "address is required (facility has no state/district/block/village set to fall back on)"}), 400

    try:
        resp = requests.get(
            GEOCODING_SERVICE_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": GEOCODING_USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.exceptions.RequestException as exc:
        db.close()
        return jsonify({"error": f"Geocoding service unavailable: {exc}"}), 502

    if not results:
        db.close()
        return jsonify({"error": f"No location found for address: {address}"}), 404

    try:
        latitude = float(results[0]["lat"])
        longitude = float(results[0]["lon"])
    except (KeyError, TypeError, ValueError):
        db.close()
        return jsonify({"error": "Geocoding service returned an unexpected response"}), 502

    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        db.close()
        return jsonify({"error": "Geocoding service returned out-of-range coordinates"}), 502

    db.execute(
        "UPDATE cold_storages SET latitude = ?, longitude = ? WHERE id = ?",
        (latitude, longitude, facility_id),
    )
    db.commit()
    updated = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    db.close()
    return jsonify({
        "facility": row_to_dict(updated),
        "geocoded_address": address,
        "matched_display_name": results[0].get("display_name"),
    })


# ── ROUTES: STORAGE PROVIDER CONFIGS ──────────────────────────────────────────

@app.route("/api/storage-provider/config", methods=["GET"])
@require_auth(roles=["storage_provider", "admin", "state_admin", "district_admin"])
def list_my_configs():
    db = get_db()
    where_clause, params = _scope_clause(g.user)
    query = """
        SELECT spc.*, cs.name as facility_name, cs.state, cs.district, cs.block, cs.village
        FROM storage_provider_configs spc
        JOIN cold_storages cs ON cs.id = spc.facility_id
    """
    if where_clause:
        query += " WHERE " + where_clause
    query += " ORDER BY spc.updated_at DESC"
    rows = db.execute(query, params).fetchall()
    db.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/storage-provider/config", methods=["POST"])
@require_auth(roles=["storage_provider", "admin"])
def create_or_update_config():
    provider_email = (g.user.get("email") or "").strip().lower()
    body = request.get_json(silent=True) or {}
    facility_id = body.get("facility_id")
    free_capacity_mt = body.get("free_capacity_mt")
    is_active = body.get("is_active")
    notes = body.get("notes")

    if not facility_id:
        return jsonify({"error": "facility_id is required"}), 400

    try:
        facility_id = int(facility_id)
    except (TypeError, ValueError):
        return jsonify({"error": "facility_id must be an integer"}), 400

    db = get_db()
    facility = db.execute("SELECT id FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    if not facility:
        db.close()
        return jsonify({"error": "Facility not found"}), 404

    now = datetime.now(timezone.utc).isoformat()
    free_capacity_mt = float(free_capacity_mt) if free_capacity_mt is not None else 0.0
    is_active = 1 if is_active else 0
    notes = notes if notes is not None else ""

    existing = db.execute(
        "SELECT id FROM storage_provider_configs WHERE facility_id = ? AND provider_email = ?",
        (facility_id, provider_email),
    ).fetchone()

    if existing:
        db.execute("""
            UPDATE storage_provider_configs
            SET free_capacity_mt = ?, is_active = ?, notes = ?, updated_at = ?
            WHERE id = ?
        """, (free_capacity_mt, is_active, notes, now, existing["id"]))
        config_id = existing["id"]
    else:
        cursor = db.execute("""
            INSERT INTO storage_provider_configs (facility_id, provider_email, free_capacity_mt, is_active, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (facility_id, provider_email, free_capacity_mt, is_active, notes, now))
        config_id = cursor.lastrowid

    db.commit()

    db.execute(
        "UPDATE cold_storages SET total_capacity_mt = ?, last_updated = ? WHERE id = ?",
        (free_capacity_mt, now, facility_id),
    )
    db.commit()

    row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
    db.close()
    return jsonify(row_to_dict(row)), 200


@app.route("/api/storage-provider/config/<int:config_id>", methods=["PATCH"])
@require_auth(roles=["storage_provider", "admin"])
def update_config(config_id):
    provider_email = (g.user.get("email") or "").strip().lower()
    body = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Config not found"}), 404

    role = (g.user.get("role") or "").lower()
    if role == "storage_provider" and row["provider_email"] != provider_email:
        db.close()
        return jsonify({"error": "Forbidden — not your config"}), 403

    updates = {}
    if "free_capacity_mt" in body:
        updates["free_capacity_mt"] = float(body["free_capacity_mt"])
    if "is_active" in body:
        updates["is_active"] = 1 if body["is_active"] else 0
    if "notes" in body:
        updates["notes"] = body["notes"]
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [config_id]
    db.execute(f"UPDATE storage_provider_configs SET {set_clause} WHERE id = ?", values)
    db.commit()

    if "free_capacity_mt" in updates:
        db.execute(
            "UPDATE cold_storages SET total_capacity_mt = ?, last_updated = ? WHERE id = ?",
            (updates["free_capacity_mt"], datetime.now(timezone.utc).isoformat(), row["facility_id"]),
        )
        db.commit()

    updated = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
    db.close()
    return jsonify(row_to_dict(updated))


@app.route("/api/storage-provider/config/<int:config_id>", methods=["DELETE"])
@require_auth(roles=["storage_provider", "admin", "state_admin", "district_admin"])
def delete_config(config_id):
    provider_email = (g.user.get("email") or "").strip().lower()
    db = get_db()
    row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Config not found"}), 404

    role = (g.user.get("role") or "").lower()
    if role not in ("admin", "storage_provider"):
        facility = db.execute("SELECT state, district FROM cold_storages WHERE id = ?", (row["facility_id"],)).fetchone()
        db.close()
        if not facility or not _can_access_facility(g.user, facility["state"], facility["district"]):
            return jsonify({"error": "Forbidden — facility outside your scope"}), 403
    elif role == "storage_provider" and row["provider_email"] != provider_email:
        db.close()
        return jsonify({"error": "Forbidden — not your config"}), 403

    db.execute("DELETE FROM storage_provider_configs WHERE id = ?", (config_id,))
    db.commit()
    db.close()
    return jsonify({"status": "deleted", "id": config_id})


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("=" * 55)
    print("  STORAGE PROVIDER BACKEND — Running on http://localhost:5020")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5020, debug=False)