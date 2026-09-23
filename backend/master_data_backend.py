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
import re
import sys
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import openpyxl
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


# ── STATE-SCOPED XLSX HELPERS ─────────────────────────────────────────────

_PROJECT_ROOT = _BACKEND_DIR.parent

_STATE_DIRS = {
    "tripura":   _PROJECT_ROOT / "data_and_model",
    "rajasthan": _PROJECT_ROOT / "data_and_model_rajasthan",
    "meghalaya": _PROJECT_ROOT / "data_and_model_meghalaya",
}

XLSX_FILENAME = "merged_crop_enriched_features_del.xlsx"

_CROP_FEATURE_COLS = ["Crop_Category", "Crop_Water_Need", "Crop_Duration_Days"]

_VALID_CROP_CATEGORIES = {
    "Cereals", "Cereal",
    "Pulses", "Pulse",
    "Oilseeds", "Oilseed",
    "Vegetables", "Vegetable",
    "Fruits", "Fruit",
    "Spices", "Spice",
    "Fibres", "Fiber",
    "Cash Crops", "Cash",
    "Plantation Crops",
    "Medicinal",
    "Forage",
    "Unknown",
}

_NUMERIC_COLS = [
    "Area (Hectare)",
    "Production (Tonnes/Bales)",
    "Fertilizer_kg_per_ha",
    "Yield (Tonne or Bales/Hectare)",
    "Crop_Duration_Days",
]


def _get_state_xlsx_path(state: str) -> Path:
    state_lower = state.strip().lower()
    if state_lower not in _STATE_DIRS:
        raise ValueError(f"Unsupported state '{state}'. Supported: {sorted(_STATE_DIRS)}")
    return _STATE_DIRS[state_lower] / XLSX_FILENAME


def _read_crop_features(state: str) -> list:
    path = _get_state_xlsx_path(state)
    if not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    headers = [str(c).strip() if c else "" for c in rows[0]]
    data_rows = rows[1:]
    feature_col_indices = {}
    for col in _CROP_FEATURE_COLS:
        if col in headers:
            feature_col_indices[col] = headers.index(col)
    seen = set()
    results = []
    for row in data_rows:
        crop_idx = headers.index("Crop") if "Crop" in headers else -1
        if crop_idx < 0 or crop_idx >= len(row):
            continue
        crop_name = str(row[crop_idx] or "").strip()
        if not crop_name or crop_name in seen:
            continue
        seen.add(crop_name)
        entry = {"crop": crop_name}
        for col, idx in feature_col_indices.items():
            val = row[idx] if idx < len(row) else None
            entry[col] = val
        results.append(entry)
    return results


def _get_districts(state: str) -> list:
    path = _get_state_xlsx_path(state)
    if not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    headers = [str(c).strip() if c else "" for c in rows[0]]
    data_rows = rows[1:]
    if "District_Name" not in headers:
        return []
    dist_idx = headers.index("District_Name")
    seen = set()
    results = []
    for row in data_rows:
        if dist_idx < len(row):
            d = str(row[dist_idx] or "").strip()
            if d and d not in seen:
                seen.add(d)
                results.append(d)
    return sorted(results)


def _get_all_rows_from_xlsx(path: Path) -> tuple:
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows, [str(c).strip() if c else "" for c in rows[0]]


def _write_rows_to_xlsx(path: Path, headers: list, data_rows: list):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in data_rows:
        ws.append(list(row))
    wb.save(path)
    wb.close()


def _update_crop_feature_in_xlsx(state: str, crop_name: str, updates: dict):
    path = _get_state_xlsx_path(state)
    if not path.exists():
        raise FileNotFoundError(f"xlsx not found: {path}")
    rows, headers = _get_all_rows_from_xlsx(path)
    feature_indices = {col: headers.index(col) for col in _CROP_FEATURE_COLS if col in headers}
    crop_idx = headers.index("Crop") if "Crop" in headers else -1
    for i, row in enumerate(rows[1:], start=2):
        if crop_idx < len(row) and str(row[crop_idx] or "").strip() == crop_name:
            new_row = list(row)
            for col, val in updates.items():
                if col in feature_indices:
                    new_row[feature_indices[col]] = val
            rows[i - 1] = tuple(new_row)
    _write_rows_to_xlsx(path, headers, rows[1:])


def _add_crop_to_xlsx(state: str, crop_name: str, features: dict):
    path = _get_state_xlsx_path(state)
    if not path.exists():
        raise FileNotFoundError(f"xlsx not found: {path}")
    rows, headers = _get_all_rows_from_xlsx(path)
    existing_crop_indices = [i for i, r in enumerate(rows[1:], start=2)
                             if len(r) > headers.index("Crop") and str(r[headers.index("Crop")] or "").strip() == crop_name]
    if existing_crop_indices:
        raise ValueError(f"Crop '{crop_name}' already exists in xlsx.")
    feature_indices = {col: headers.index(col) for col in _CROP_FEATURE_COLS if col in headers}
    dist_idx = headers.index("District_Name") if "District_Name" in headers else -1
    year_idx = headers.index("Crop_Year") if "Crop_Year" in headers else -1
    season_idx = headers.index("Season") if "Season" in headers else -1
    existing_dists = sorted({str(r[dist_idx] or "").strip() for r in rows[1:]
                             if dist_idx < len(r) and str(r[dist_idx] or "").strip()})
    existing_years = sorted({str(r[year_idx] or "").strip() for r in rows[1:]
                             if year_idx < len(r) and str(r[year_idx] or "").strip()})
    existing_seasons = sorted({str(r[season_idx] or "").strip() for r in rows[1:]
                               if season_idx < len(r) and str(r[season_idx] or "").strip()})
    new_rows = []
    for dist in existing_dists:
        for yr in existing_years:
            for season in existing_seasons:
                new_row = [""] * len(headers)
                new_row[headers.index("District_Name") if "District_Name" in headers else -1] = dist
                new_row[headers.index("Crop_Year") if "Crop_Year" in headers else -1] = yr
                new_row[headers.index("Season") if "Season" in headers else -1] = season
                new_row[headers.index("Crop") if "Crop" in headers else -1] = crop_name
                for col, val in features.items():
                    if col in feature_indices:
                        new_row[feature_indices[col]] = val
                new_rows.append(tuple(new_row))
    rows.extend(new_rows)
    _write_rows_to_xlsx(path, headers, rows[1:])


def _delete_crop_from_xlsx(state: str, crop_name: str):
    path = _get_state_xlsx_path(state)
    if not path.exists():
        raise FileNotFoundError(f"xlsx not found: {path}")
    rows, headers = _get_all_rows_from_xlsx(path)
    crop_idx = headers.index("Crop") if "Crop" in headers else -1
    filtered = [r for r in rows[1:] if crop_idx >= len(r) or str(r[crop_idx] or "").strip() != crop_name]
    _write_rows_to_xlsx(path, headers, filtered)


def _add_district_to_xlsx(state: str, district_name: str):
    path = _get_state_xlsx_path(state)
    if not path.exists():
        raise FileNotFoundError(f"xlsx not found: {path}")
    rows, headers = _get_all_rows_from_xlsx(path)
    dist_idx = headers.index("District_Name") if "District_Name" in headers else -1
    existing_dists = {str(r[dist_idx] or "").strip() for r in rows[1:]
                      if dist_idx < len(r) and str(r[dist_idx] or "").strip()}
    if district_name in existing_dists:
        raise ValueError(f"District '{district_name}' already exists in xlsx.")
    year_idx = headers.index("Crop_Year") if "Crop_Year" in headers else -1
    season_idx = headers.index("Season") if "Season" in headers else -1
    crop_idx = headers.index("Crop") if "Crop" in headers else -1
    feature_indices = {col: headers.index(col) for col in _CROP_FEATURE_COLS if col in headers}
    existing_years = sorted({str(r[year_idx] or "").strip() for r in rows[1:]
                             if year_idx < len(r) and str(r[year_idx] or "").strip()})
    existing_seasons = sorted({str(r[season_idx] or "").strip() for r in rows[1:]
                               if season_idx < len(r) and str(r[season_idx] or "").strip()})
    existing_crops = sorted({str(r[crop_idx] or "").strip() for r in rows[1:]
                             if crop_idx < len(r) and str(r[crop_idx] or "").strip()})
    new_rows = []
    for crop in existing_crops:
        for yr in existing_years:
            for season in existing_seasons:
                new_row = [""] * len(headers)
                new_row[dist_idx] = district_name
                new_row[year_idx] = yr
                new_row[season_idx] = season
                new_row[crop_idx] = crop
                crop_rows = [r for r in rows[1:]
                             if crop_idx < len(r) and str(r[crop_idx] or "").strip() == crop]
                if crop_rows:
                    ref = crop_rows[0]
                    for col, idx in feature_indices.items():
                        new_row[idx] = ref[idx] if idx < len(ref) else None
                new_rows.append(tuple(new_row))
    rows.extend(new_rows)
    _write_rows_to_xlsx(path, headers, rows[1:])


def _delete_district_from_xlsx(state: str, district_name: str):
    path = _get_state_xlsx_path(state)
    if not path.exists():
        raise FileNotFoundError(f"xlsx not found: {path}")
    rows, headers = _get_all_rows_from_xlsx(path)
    dist_idx = headers.index("District_Name") if "District_Name" in headers else -1
    filtered = [r for r in rows[1:] if dist_idx >= len(r) or str(r[dist_idx] or "").strip() != district_name]
    _write_rows_to_xlsx(path, headers, filtered)


def _validate_crop_features(data: dict) -> tuple:
    errors = []
    category = str(data.get("Crop_Category", "")).strip()
    if category and category not in _VALID_CROP_CATEGORIES:
        errors.append(f"Invalid Crop_Category '{category}'. Must be one of: {sorted(_VALID_CROP_CATEGORIES)}")
    water_need = data.get("Crop_Water_Need")
    if water_need is not None:
        try:
            wn = int(water_need)
            if wn not in (1, 2, 3):
                errors.append("Crop_Water_Need must be 1 (Low), 2 (Medium), or 3 (High)")
        except (ValueError, TypeError):
            errors.append("Crop_Water_Need must be a number (1, 2, or 3)")
    duration = data.get("Crop_Duration_Days")
    if duration is not None:
        try:
            d = int(duration)
            if d <= 0:
                errors.append("Crop_Duration_Days must be a positive integer")
        except (ValueError, TypeError):
            errors.append("Crop_Duration_Days must be a positive integer")
    return errors


def _normalize_feature_val(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return int(val)
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return str(val).strip() if str(val).strip() != "" else None



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


# ── STATE SCOPING HELPER ─────────────────────────────────────────────────────

def _user_visible_states() -> list:
    user_state = str(g.user.get("state", "") or "").strip().lower()
    allowed = set(_STATE_DIRS.keys())
    if user_state and user_state in allowed:
        return [user_state]
    return sorted(allowed)


def _check_state_access(state: str):
    state_lower = state.strip().lower()
    if state_lower not in _STATE_DIRS:
        return jsonify({"error": f"Unsupported state '{state}'. Supported: {sorted(_STATE_DIRS)}"}), 400
    user_state = str(g.user.get("state", "") or "").strip().lower()
    if user_state and user_state != state_lower:
        return jsonify({"error": f"Forbidden: you are scoped to state '{user_state}', not '{state}'"}), 403


# ── HEALTH ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
@app.route("/api/master-data/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "master-data-backend", "port": 6015}), 200


# ── CROP FEATURES CRUD ──────────────────────────────────────────────────────

@app.route("/api/master-data/crop-features", methods=["GET"])
@require_admin
def list_crop_features():
    state = (request.args.get("state") or "").strip().lower()
    if not state:
        return jsonify({"error": "state query parameter is required"}), 400
    err = _check_state_access(state)
    if err:
        return err
    try:
        data = _read_crop_features(state)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"state": state, "crops": data}), 200


@app.route("/api/master-data/crop-features", methods=["POST"])
@require_admin
def create_crop_feature():
    body = request.get_json(silent=True) or {}
    state = str(body.get("state", "")).strip().lower()
    crop_name = str(body.get("crop", "")).strip()

    if not state or not crop_name:
        return jsonify({"error": "state and crop are required"}), 400

    err = _check_state_access(state)
    if err:
        return err

    features = {}
    for col in _CROP_FEATURE_COLS:
        if col in body and body[col] is not None and str(body[col]).strip() != "":
            features[col] = _normalize_feature_val(body[col])

    validation_errors = _validate_crop_features(features)
    if validation_errors:
        return jsonify({"error": "; ".join(validation_errors)}), 400

    try:
        _add_crop_to_xlsx(state, crop_name, features)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to add crop: {e}"}), 500

    invalidate_prefix("yp:valid_crops")
    updated = _read_crop_features(state)
    entry = next((c for c in updated if c["crop"] == crop_name), {"crop": crop_name, **features})
    return jsonify({"state": state, **entry}), 201


@app.route("/api/master-data/crop-features/<path:crop_name>", methods=["PATCH"])
@require_admin
def update_crop_feature(crop_name):
    crop_name = str(crop_name).strip()
    if not crop_name:
        return jsonify({"error": "crop name is required"}), 400

    body = request.get_json(silent=True) or {}
    state = str(body.get("state", "")).strip().lower()
    if not state:
        return jsonify({"error": "state is required in request body"}), 400

    err = _check_state_access(state)
    if err:
        return err

    features = {}
    for col in _CROP_FEATURE_COLS:
        if col in body and body[col] is not None and str(body[col]).strip() != "":
            features[col] = _normalize_feature_val(body[col])

    if not features:
        return jsonify({"error": "No feature fields provided to update"}), 400

    validation_errors = _validate_crop_features(features)
    if validation_errors:
        return jsonify({"error": "; ".join(validation_errors)}), 400

    try:
        existing = _read_crop_features(state)
        if not any(c["crop"] == crop_name for c in existing):
            return jsonify({"error": f"Crop '{crop_name}' not found in xlsx for state '{state}'"}), 404
        _update_crop_feature_in_xlsx(state, crop_name, features)
    except FileNotFoundError:
        return jsonify({"error": f"xlsx not found for state '{state}'"}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to update crop: {e}"}), 500

    invalidate_prefix("yp:valid_crops")
    invalidate_prefix("yp:valid_districts")
    updated = _read_crop_features(state)
    entry = next((c for c in updated if c["crop"] == crop_name), {"crop": crop_name, **features})
    return jsonify({"state": state, **entry}), 200


@app.route("/api/master-data/crop-features/<path:crop_name>", methods=["DELETE"])
@require_admin
def delete_crop_feature(crop_name):
    crop_name = str(crop_name).strip()
    if not crop_name:
        return jsonify({"error": "crop name is required"}), 400

    body = request.get_json(silent=True) or {}
    state = str(body.get("state", "")).strip().lower()
    if not state:
        return jsonify({"error": "state is required in request body"}), 400

    err = _check_state_access(state)
    if err:
        return err

    try:
        existing = _read_crop_features(state)
        if not any(c["crop"] == crop_name for c in existing):
            return jsonify({"error": f"Crop '{crop_name}' not found in xlsx for state '{state}'"}), 404
        _delete_crop_from_xlsx(state, crop_name)
    except FileNotFoundError:
        return jsonify({"error": f"xlsx not found for state '{state}'"}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to delete crop: {e}"}), 500

    invalidate_prefix("yp:valid_crops")
    invalidate_prefix("yp:valid_districts")
    return jsonify({"success": True, "state": state, "deleted_crop": crop_name}), 200


# ── DISTRICTS CRUD ──────────────────────────────────────────────────────────

@app.route("/api/master-data/districts", methods=["GET"])
@require_admin
def list_districts():
    state = (request.args.get("state") or "").strip().lower()
    if not state:
        return jsonify({"error": "state query parameter is required"}), 400
    err = _check_state_access(state)
    if err:
        return err
    try:
        data = _get_districts(state)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"state": state, "districts": data}), 200


@app.route("/api/master-data/districts", methods=["POST"])
@require_admin
def create_district():
    body = request.get_json(silent=True) or {}
    state = str(body.get("state", "")).strip().lower()
    district_name = str(body.get("district", "")).strip()

    if not state or not district_name:
        return jsonify({"error": "state and district are required"}), 400

    err = _check_state_access(state)
    if err:
        return err

    try:
        existing = _get_districts(state)
        if district_name in existing:
            return jsonify({"error": f"District '{district_name}' already exists in xlsx for state '{state}'"}), 409
        _add_district_to_xlsx(state, district_name)
    except FileNotFoundError:
        return jsonify({"error": f"xlsx not found for state '{state}'"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": f"Failed to add district: {e}"}), 500

    invalidate_prefix("yp:valid_districts")
    invalidate_prefix("yp:valid_crops")
    return jsonify({"state": state, "district": district_name}), 201


@app.route("/api/master-data/districts/<path:district_name>", methods=["DELETE"])
@require_admin
def delete_district(district_name):
    district_name = str(district_name).strip()
    if not district_name:
        return jsonify({"error": "district name is required"}), 400

    body = request.get_json(silent=True) or {}
    state = str(body.get("state", "")).strip().lower()
    if not state:
        return jsonify({"error": "state is required in request body"}), 400

    err = _check_state_access(state)
    if err:
        return err

    try:
        existing = _get_districts(state)
        if district_name not in existing:
            return jsonify({"error": f"District '{district_name}' not found in xlsx for state '{state}'"}), 404
        _delete_district_from_xlsx(state, district_name)
    except FileNotFoundError:
        return jsonify({"error": f"xlsx not found for state '{state}'"}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to delete district: {e}"}), 500

    invalidate_prefix("yp:valid_districts")
    invalidate_prefix("yp:valid_crops")
    return jsonify({"success": True, "state": state, "deleted_district": district_name}), 200


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

