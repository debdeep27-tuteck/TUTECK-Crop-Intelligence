"""
yield_platform_service.py
==========================
ONE standalone microservice combining what used to be two separate generic
services (land_service.py + crop_yield_service.py): parcel/geofence storage
AND crop-yield prediction / soil classification. Same pattern as
auction_engine_service.py — this process knows nothing about farmers,
sessions, roles, or your gateway. yield_detect_backend.py is the single
adapter/connector app that talks to this over HTTP (like auction_backend.py
talks to auction_engine_service.py).

Why merged into one file instead of two:
  - You run/deploy them as a single unit anyway (one port, one API key).
  - Both are equally "generic" in the sense that matters here: no farmer
    auth, no gateway knowledge, no role system — just opaque owner IDs and
    stateless domain calls, gated by one shared bearer key.
  - A different agri app only needs to point at ONE URL + ONE key to get
    both land storage and yield/soil capabilities, instead of wiring up two
    services separately.

What's still genuinely reusable vs. India/state-specific (unchanged from
before the merge, just living in one file now):
  - Parcel CRUD is fully domain-agnostic (agriculture, real estate,
    logistics, anything with a geofenced area + owner).
  - Mappls geofence/geocode is regional to India — swap MAPPLS_* for
    another provider outside India.
  - Raw SoilGrids WRB classification is domain-agnostic soil science.
  - The WRB->Soil_Type mapping tables and the backend_2.py model proxy are
    tied to these three trained crop models (tripura/meghalaya/rajasthan).

Run it:
    pip install flask flask-cors requests
    export YIELD_PLATFORM_SERVICE_API_KEY="some-long-random-secret"
    export MAPPLS_CLIENT_ID="..."       # optional, for geofence routes
    export MAPPLS_CLIENT_SECRET="..."   # optional
    export MAPPLS_MAP_KEY="..."         # optional, for the tile key route
    python yield_platform_service.py --port 6100

Auth model
----------
A single shared bearer API key gates every route except /health — same as
before, just one key instead of two:

    Authorization: Bearer <YIELD_PLATFORM_SERVICE_API_KEY>

If YIELD_PLATFORM_SERVICE_API_KEY is unset, auth is skipped (local dev
only — never do this in production).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

logger = logging.getLogger("yield_platform_service")
logging.basicConfig(level=logging.INFO)

# ── CONFIG ────────────────────────────────────────────────────────────────

DEFAULT_PORT = os.environ.get("YIELD_PLATFORM_SERVICE_PORT", 6100)
DB_PATH = Path(__file__).resolve().parent / "yield_platform_service.db"
API_KEY = os.environ.get("YIELD_PLATFORM_SERVICE_API_KEY", "")

# Mappls (MapmyIndia) OAuth — optional. Geofence/geocode routes 502 with a
# clear error if these aren't set, rather than failing silently.
MAPPLS_CLIENT_ID = os.environ.get("MAPPLS_CLIENT_ID", "")
MAPPLS_CLIENT_SECRET = os.environ.get("MAPPLS_CLIENT_SECRET", "")
MAPPLS_MAP_KEY = os.environ.get("MAPPLS_MAP_KEY", "")
MAPPLS_TOKEN_URL = "https://outpost.mappls.com/api/security/oauth/token"
MAPPLS_GEOFENCE_BASE = "https://atlas.mappls.com/api/places/geofence"
MAPPLS_SEARCH_URL = "https://atlas.mappls.com/api/places/search/json"

_mappls_token_cache = {"token": None, "expires_at": 0}

# Must match CROP_BACKENDS in main.py / gateway.py — the per-state trained
# model servers (backend_2.py) this service proxies /predict etc. to.
STATE_BACKEND_PORTS = {
    "tripura": 6000,
    "meghalaya": 6002,
    "rajasthan": 6006,
}
DEFAULT_STATE = "tripura"

app = Flask(__name__)
CORS(app)


# ── AUTH ──────────────────────────────────────────────────────────────────

def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return fn(*args, **kwargs)  # auth disabled — local dev only
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {API_KEY}":
            return jsonify({"error": "invalid or missing API key"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ── DB: PARCELS + SOILGRIDS CACHE (one DB, two tables) ─────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      TEXT NOT NULL,
    label         TEXT NOT NULL,
    latitude      REAL,
    longitude     REAL,
    area_hectare  REAL,
    bounds_json   TEXT,
    metadata_json TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS soilgrids_cache (
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    fetched_at  REAL NOT NULL,
    probs_json  TEXT NOT NULL,
    PRIMARY KEY (lat, lon)
);
"""

_conn = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ownerId": row["owner_id"],
        "label": row["label"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "areaHectare": row["area_hectare"],
        "bounds": json.loads(row["bounds_json"]) if row["bounds_json"] else None,
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


# ── ROUTES: HEALTH ────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "yield_platform_service", "db": str(DB_PATH)})


# ── ROUTES: PARCEL CRUD (formerly land_service.py) ─────────────────────────

@app.route("/parcels", methods=["POST"])
@require_api_key
def create_parcel():
    body = request.get_json(force=True) or {}
    owner_id = body.get("ownerId")
    label = body.get("label")
    if not owner_id or not label:
        return jsonify({"error": "ownerId and label are required"}), 400

    ts = now_iso()
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO parcels (owner_id, label, latitude, longitude, area_hectare,
                              bounds_json, metadata_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            owner_id,
            label,
            body.get("latitude"),
            body.get("longitude"),
            body.get("areaHectare"),
            json.dumps(body.get("bounds")) if body.get("bounds") is not None else None,
            json.dumps(body.get("metadata")) if body.get("metadata") is not None else None,
            ts,
            ts,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM parcels WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.route("/parcels", methods=["GET"])
@require_api_key
def list_parcels():
    """Optional filter: ownerId. Deliberately does NOT filter on metadata
    contents — this service doesn't interpret metadata, so any
    domain-specific filtering (state/district/crop/etc.) is the calling
    app's job, done client-side after fetching."""
    db = get_db()
    owner_id = request.args.get("ownerId")
    if owner_id:
        rows = db.execute(
            "SELECT * FROM parcels WHERE owner_id = ? ORDER BY updated_at DESC", (owner_id,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM parcels ORDER BY updated_at DESC").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/parcels/<int:parcel_id>", methods=["GET"])
@require_api_key
def get_parcel(parcel_id):
    db = get_db()
    row = db.execute("SELECT * FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(row))


@app.route("/parcels/<int:parcel_id>", methods=["PUT"])
@require_api_key
def update_parcel(parcel_id):
    db = get_db()
    existing = db.execute("SELECT * FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
    if not existing:
        return jsonify({"error": "not found"}), 404

    body = request.get_json(force=True) or {}
    merged = row_to_dict(existing)
    for key in ("label", "latitude", "longitude", "areaHectare", "bounds", "metadata"):
        if key in body and body[key] is not None:
            merged[key] = body[key]

    ts = now_iso()
    db.execute(
        """
        UPDATE parcels SET label = ?, latitude = ?, longitude = ?, area_hectare = ?,
                            bounds_json = ?, metadata_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            merged["label"],
            merged["latitude"],
            merged["longitude"],
            merged["areaHectare"],
            json.dumps(merged["bounds"]) if merged.get("bounds") is not None else None,
            json.dumps(merged["metadata"]) if merged.get("metadata") is not None else None,
            ts,
            parcel_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/parcels/<int:parcel_id>", methods=["DELETE"])
@require_api_key
def delete_parcel(parcel_id):
    db = get_db()
    existing = db.execute("SELECT id FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
    if not existing:
        return jsonify({"error": "not found"}), 404
    db.execute("DELETE FROM parcels WHERE id = ?", (parcel_id,))
    db.commit()
    return jsonify({"deleted": parcel_id})


# ── MAPPLS OAUTH TOKEN HANDLING (formerly land_service.py) ─────────────────

def get_mappls_token() -> str | None:
    now = time.time()
    if _mappls_token_cache["token"] and _mappls_token_cache["expires_at"] > now + 30:
        return _mappls_token_cache["token"]

    if not MAPPLS_CLIENT_ID or not MAPPLS_CLIENT_SECRET:
        return None

    try:
        resp = requests.post(
            MAPPLS_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": MAPPLS_CLIENT_ID,
                "client_secret": MAPPLS_CLIENT_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 3600))
        if not token:
            return None
        _mappls_token_cache["token"] = token
        _mappls_token_cache["expires_at"] = now + expires_in
        return token
    except requests.exceptions.RequestException:
        return None


def mappls_auth_header() -> dict | None:
    token = get_mappls_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


# ── ROUTES: MAP TILE KEY / GEOCODE / GEOFENCE (formerly land_service.py) ───

@app.route("/mappls_key", methods=["GET"])
@require_api_key
def mappls_key():
    return jsonify({"key": MAPPLS_MAP_KEY, "configured": bool(MAPPLS_MAP_KEY)})


def _search_mappls(query: str):
    headers = mappls_auth_header()
    if not headers:
        return None
    try:
        resp = requests.get(
            MAPPLS_SEARCH_URL, params={"query": query, "region": "ind"}, headers=headers, timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        return None

    raw_locations = data.get("suggestedLocations") or []
    if not raw_locations:
        return None

    results = []
    for item in raw_locations:
        lat, lng = item.get("latitude"), item.get("longitude")
        if lat is None or lng is None:
            continue
        label_parts = [item.get("placeName"), item.get("placeAddress")]
        display_name = ", ".join(p for p in label_parts if p) or item.get("placeName", query)
        results.append({"display_name": display_name, "lat": lat, "lon": lng, "source": "mappls"})
    return results


def _search_nominatim(query: str):
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": 6, "q": query},
            headers={"Accept": "application/json", "User-Agent": "yield-platform-service/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        return []
    return [
        {"display_name": item.get("display_name", query), "lat": item.get("lat"),
         "lon": item.get("lon"), "source": "nominatim"}
        for item in data
    ]


@app.route("/geocode/search", methods=["GET"])
@require_api_key
def geocode_search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 3:
        return jsonify({"results": []})
    results = _search_mappls(query)
    if not results:
        results = _search_nominatim(query)
    return jsonify({"results": results[:8]})


@app.route("/geofence/status", methods=["GET"])
@require_api_key
def geofence_status():
    configured = bool(MAPPLS_CLIENT_ID and MAPPLS_CLIENT_SECRET)
    token_ok = bool(get_mappls_token()) if configured else False
    return jsonify({"configured": configured, "token_ok": token_ok})


@app.route("/geofence", methods=["POST"])
@require_api_key
def create_geofence():
    headers = mappls_auth_header()
    if not headers:
        return jsonify({"error": "Mappls geofencing not configured or token unavailable"}), 502

    body = request.get_json(force=True) or {}
    name = body.get("name")
    bounds = body.get("bounds") or {}
    if not name or not all(k in bounds for k in ("north", "south", "east", "west")):
        return jsonify({"error": "name and bounds{north,south,east,west} are required"}), 400

    ring = [
        [bounds["west"], bounds["north"]],
        [bounds["east"], bounds["north"]],
        [bounds["east"], bounds["south"]],
        [bounds["west"], bounds["south"]],
        [bounds["west"], bounds["north"]],
    ]
    payload = {"name": name, "geo_json": {"type": "Polygon", "coordinates": [ring]}}

    try:
        resp = requests.post(f"{MAPPLS_GEOFENCE_BASE}/save", json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Mappls geofence create failed: {exc}"}), 502


@app.route("/geofence/<fence_id>", methods=["DELETE"])
@require_api_key
def delete_geofence(fence_id):
    headers = mappls_auth_header()
    if not headers:
        return jsonify({"error": "Mappls geofencing not configured or token unavailable"}), 502
    try:
        resp = requests.delete(f"{MAPPLS_GEOFENCE_BASE}/{fence_id}", headers=headers, timeout=15)
        resp.raise_for_status()
        return jsonify({"deleted": fence_id})
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Mappls geofence delete failed: {exc}"}), 502


@app.route("/geofence/check", methods=["POST"])
@require_api_key
def check_geofence_point():
    headers = mappls_auth_header()
    if not headers:
        return jsonify({"error": "Mappls geofencing not configured or token unavailable"}), 502

    body = request.get_json(force=True) or {}
    fence_id = body.get("fence_id")
    lat = body.get("latitude")
    lng = body.get("longitude")
    if not fence_id or lat is None or lng is None:
        return jsonify({"error": "fence_id, latitude, longitude are required"}), 400

    try:
        resp = requests.get(
            f"{MAPPLS_GEOFENCE_BASE}/{fence_id}/contains",
            params={"lat": lat, "lng": lng},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Mappls geofence check failed: {exc}"}), 502


# ── SOILGRIDS CACHE (formerly crop_yield_service.py) ────────────────────────

SOILGRIDS_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 1 week


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 5), round(lon, 5))


def _soilgrids_cache_get(lat: float, lon: float) -> list[tuple[str, float]] | None:
    key_lat, key_lon = _cache_key(lat, lon)
    row = get_db().execute(
        "SELECT fetched_at, probs_json FROM soilgrids_cache WHERE lat = ? AND lon = ?",
        (key_lat, key_lon),
    ).fetchone()
    if not row:
        return None
    if (time.time() - row["fetched_at"]) >= SOILGRIDS_CACHE_TTL_SECONDS:
        return None
    try:
        return [tuple(item) for item in json.loads(row["probs_json"])]
    except (TypeError, ValueError):
        return None


def _soilgrids_cache_set(lat: float, lon: float, probs: list[tuple[str, float]]) -> None:
    key_lat, key_lon = _cache_key(lat, lon)
    db = get_db()
    db.execute(
        """
        INSERT INTO soilgrids_cache (lat, lon, fetched_at, probs_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lat, lon) DO UPDATE SET fetched_at = excluded.fetched_at, probs_json = excluded.probs_json
        """,
        (key_lat, key_lon, time.time(), json.dumps(probs)),
    )
    db.commit()


# ── SOILGRIDS (ISRIC) SOIL TYPE LOOKUP (formerly crop_yield_service.py) ────
SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/classification/query"
SOILGRIDS_TIMEOUTS_SECONDS = [8, 20]
SOILGRIDS_NUMBER_CLASSES = 3


def query_soilgrids_wrb(lat: float, lon: float) -> list[tuple[str, float]]:
    cached = _soilgrids_cache_get(lat, lon)
    if cached is not None:
        return cached

    last_exc: Exception | None = None
    for attempt, timeout in enumerate(SOILGRIDS_TIMEOUTS_SECONDS, start=1):
        try:
            resp = requests.get(
                SOILGRIDS_URL,
                params={"lon": lon, "lat": lat, "number_classes": SOILGRIDS_NUMBER_CLASSES},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            probs = data.get("wrb_class_probability") or []
            result = [(cls, pct) for cls, pct in probs if pct is not None]
            _soilgrids_cache_set(lat, lon, result)
            return result
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < len(SOILGRIDS_TIMEOUTS_SECONDS):
                logger.warning(
                    "SoilGrids query attempt %d/%d failed for (lat=%s, lon=%s): %s — retrying",
                    attempt, len(SOILGRIDS_TIMEOUTS_SECONDS), lat, lon, exc,
                )
                continue

    logger.warning("SoilGrids query failed for (lat=%s, lon=%s): %s", lat, lon, last_exc)
    return []


# ── STATE-SPECIFIC WRB -> Soil_Type MAPPING (crop-model domain knowledge) ──

RAJASTHAN_WRB_TO_SOIL = {
    "Calcisols": "Desert", "Leptosols": "Desert", "Solonchaks": "Desert",
    "Arenosols": "Sandy", "Regosols": "Sandy",
    "Cambisols": "Alluvial", "Fluvisols": "Alluvial",
    "Vertisols": "Black Cotton", "Luvisols": "Black Cotton", "Chernozems": "Black Cotton",
}

TRIPURA_WRB_TO_SOIL = {
    "Acrisols": "Red Laterite", "Ferralsols": "Red Laterite",
    "Plinthosols": "Red Laterite", "Nitisols": "Red Laterite",
    "Lixisols": "Red Laterite", "Alisols": "Red Laterite",
    "Fluvisols": "Alluvial", "Cambisols": "Alluvial",
    "Gleysols": "Alluvial", "Regosols": "Alluvial", "Umbrisols": "Alluvial",
}

MEGHALAYA_WRB_TO_SOIL = {
    "Acrisols": "Red Laterite", "Ferralsols": "Red Laterite",
    "Plinthosols": "Red Laterite", "Nitisols": "Red Laterite",
    "Lixisols": "Red Laterite", "Alisols": "Red Laterite",
    "Umbrisols": "Red Laterite",
    "Arenosols": "Sandy Loam", "Regosols": "Sandy Loam",
    "Fluvisols": "Alluvial", "Cambisols": "Alluvial", "Gleysols": "Alluvial",
}

STATE_WRB_TO_SOIL = {
    "rajasthan": RAJASTHAN_WRB_TO_SOIL,
    "tripura": TRIPURA_WRB_TO_SOIL,
    "meghalaya": MEGHALAYA_WRB_TO_SOIL,
}


def lookup_soil_type(lat: float, lon: float, state: str) -> str | None:
    wrb_map = STATE_WRB_TO_SOIL.get((state or "").lower().strip())
    if not wrb_map:
        return None

    probs = query_soilgrids_wrb(lat, lon)
    if not probs:
        return None

    scores: dict[str, float] = {}
    for wrb_class, pct in probs:
        mapped = wrb_map.get(wrb_class)
        if mapped:
            scores[mapped] = scores.get(mapped, 0) + pct

    if not scores:
        return None

    return max(scores, key=scores.get)


def geofence_centroid(body: dict) -> tuple[float, float] | None:
    lat, lon = body.get("latitude"), body.get("longitude")
    if lat is not None and lon is not None:
        return float(lat), float(lon)

    bounds = body.get("bounds") or {}
    if all(k in bounds for k in ("north", "south", "east", "west")):
        centroid_lat = (float(bounds["north"]) + float(bounds["south"])) / 2
        centroid_lon = (float(bounds["east"]) + float(bounds["west"])) / 2
        return centroid_lat, centroid_lon

    return None


# ── PER-STATE MODEL BACKEND PROXY (backend_2.py instances) ─────────────────

def state_port(state: str) -> int:
    return STATE_BACKEND_PORTS.get((state or "").lower().strip(), STATE_BACKEND_PORTS[DEFAULT_STATE])


def call_predict(state: str, payload: dict) -> dict:
    port = state_port(state)
    url = f"http://127.0.0.1:{port}/predict"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        return {"error": f"Could not reach crop backend for state '{state}' on port {port}: {exc}"}


def call_valid_crops(state: str) -> list:
    port = state_port(state)
    url = f"http://127.0.0.1:{port}/valid_crops"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("crops", data.get("valid_crops", []))
    except requests.exceptions.RequestException:
        return []


def call_valid_districts(state: str) -> list:
    port = state_port(state)
    url = f"http://127.0.0.1:{port}/valid_districts"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("valid_districts", [])
    except requests.exceptions.RequestException:
        return []


def build_predict_payload(body: dict, soil_type: str | None) -> dict:
    return {
        "crop": body.get("crop", ""),
        "district": body.get("district", "Dhalai"),
        "Season": body.get("season", "Kharif"),
        "Soil_Type": soil_type or "Alluvial",
        "Irrigation_Type": body.get("irrigation_type", "Canal"),
        "Area (Hectare)": float(body.get("area_hectare") or 0) or 500,
        "Fertilizer_kg_per_ha": float(body.get("fertilizer_kg_per_ha") or 70),
        "Pest_Disease_Incidence": body.get("pest_incidence", "Low"),
    }


# ── ROUTES: CROP YIELD (formerly crop_yield_service.py) ────────────────────

@app.route("/valid_crops", methods=["GET"])
@require_api_key
def valid_crops():
    state = request.args.get("state", DEFAULT_STATE)
    return jsonify({"state": state, "crops": call_valid_crops(state)})


@app.route("/valid_districts", methods=["GET"])
@require_api_key
def valid_districts():
    state = request.args.get("state", DEFAULT_STATE)
    return jsonify({"state": state, "districts": call_valid_districts(state)})


@app.route("/soil_type", methods=["GET"])
@require_api_key
def soil_type_route():
    """Query params: state, lat, lon (or north/south/east/west bounds)."""
    state = request.args.get("state", DEFAULT_STATE)
    body = {
        "latitude": request.args.get("lat", type=float),
        "longitude": request.args.get("lon", type=float),
        "bounds": {
            k: request.args.get(k, type=float)
            for k in ("north", "south", "east", "west")
            if request.args.get(k) is not None
        } or None,
    }
    point = geofence_centroid(body)
    if not point:
        return jsonify({"error": "latitude/longitude (or bounds) are required"}), 400

    soil_type = lookup_soil_type(point[0], point[1], state)
    return jsonify({
        "state": state,
        "latitude": point[0],
        "longitude": point[1],
        "soil_type": soil_type,
        "configured": (state or "").lower().strip() in STATE_WRB_TO_SOIL,
    })


@app.route("/predict", methods=["POST"])
@require_api_key
def predict():
    """
    Body: { state, crop, district, season, soil_type (optional),
            latitude/longitude or bounds (optional, used to auto-detect
            soil_type if not supplied), irrigation_type, area_hectare,
            fertilizer_kg_per_ha, pest_incidence }
    Returns: { state, yield, normal, anomaly, source, soil_type_used }
    """
    body = request.get_json(force=True) or {}
    state = body.get("state", DEFAULT_STATE)

    if not body.get("crop"):
        return jsonify({"error": "crop is required"}), 400

    soil_type = body.get("soil_type")
    if not soil_type:
        point = geofence_centroid(body)
        if point:
            soil_type = lookup_soil_type(point[0], point[1], state)

    payload = build_predict_payload(body, soil_type)
    result = call_predict(state, payload)

    if "error" in result:
        return jsonify(result), 502

    return jsonify({
        "state": state,
        "yield": result.get("yield"),
        "normal": result.get("normal"),
        "anomaly": result.get("anomaly"),
        "source": result.get("source"),
        "soil_type_used": soil_type,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    get_db()  # ensures DB/tables exist before serving
    app.run(host="0.0.0.0", port=args.port, debug=False)