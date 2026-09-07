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


# ── ROUTES: STORAGE PROVIDER CONFIGS ──────────────────────────────────────────

@app.route("/api/storage-provider/config", methods=["GET"])
@require_auth(roles=["storage_provider", "admin"])
def list_my_configs():
    provider_email = (g.user.get("email") or "").strip().lower()
    db = get_db()
    rows = db.execute("""
        SELECT spc.*, cs.name as facility_name, cs.state, cs.district, cs.block, cs.village
        FROM storage_provider_configs spc
        JOIN cold_storages cs ON cs.id = spc.facility_id
        WHERE spc.provider_email = ?
        ORDER BY spc.updated_at DESC
    """, (provider_email,)).fetchall()
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

    if (g.user.get("role") or "").lower() != "admin" and row["provider_email"] != provider_email:
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

    updated = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
    db.close()
    return jsonify(row_to_dict(updated))


@app.route("/api/storage-provider/config/<int:config_id>", methods=["DELETE"])
@require_auth(roles=["storage_provider", "admin"])
def delete_config(config_id):
    provider_email = (g.user.get("email") or "").strip().lower()
    db = get_db()
    row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Config not found"}), 404

    if (g.user.get("role") or "").lower() != "admin" and row["provider_email"] != provider_email:
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
