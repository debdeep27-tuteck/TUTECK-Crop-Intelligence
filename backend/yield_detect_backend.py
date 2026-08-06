"""
yield_detect_backend.py

Backend service for the "Yield Detect" feature: farmers/agronomists draw a
geofence (rectangle) on Google Maps around a plot of land, give it a name,
and get a yield prediction + supporting stats for that land. Every analyzed
land is stored in a local SQLite database (yield_lands.db) and listed in the
"Yield Detect" tab, with Edit / Delete actions.

This backend does NOT re-implement the ML model. It reuses the existing
per-state crop backends (backend_2.py) that main.py already launches on:
    tripura   -> 127.0.0.1:5000
    meghalaya -> 127.0.0.1:5002
    rajasthan -> 127.0.0.1:5006
and simply proxies a /predict call to the right one with the fields derived
from the geofenced land (area, district, soil, irrigation, fertilizer, etc).

Run standalone:
    pip install flask flask-cors requests
    export MAPPLS_CLIENT_ID="your-client-id"
    export MAPPLS_CLIENT_SECRET="your-client-secret"
    python yield_detect_backend.py --port 5008

Mappls geofencing (optional):
    Set MAPPLS_CLIENT_ID / MAPPLS_CLIENT_SECRET as environment variables
    (never hardcode them in this file or commit them to source control).
    This backend exchanges them server-side for a short-lived OAuth bearer
    token and proxies /api/yield/geofence/* calls to Mappls so the secret
    never reaches the browser.

Wire into main.py / gateway.py:
    - main.py should launch this on port 5008 alongside the other services.
    - gateway.py should forward:
        /content/yield-detect         -> http://127.0.0.1:5008/content/yield-detect
        /content/yield-detect-editor  -> http://127.0.0.1:5008/content/yield-detect-editor
        /api/yield/*                  -> http://127.0.0.1:5008/api/yield/*
      (See main.py / index.html changes shipped alongside this file — the
      nav tab and iframe route are already wired to these paths.)
"""

from __future__ import annotations

import os
import time

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS
from functools import wraps
import logging

logger = logging.getLogger("yield_detect")
logging.basicConfig(level=logging.INFO)

# ── CONFIG ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = (BASE_DIR / "../frontend").resolve()
HTML_DIR = FRONTEND_DIR / "html"
CSS_DIR = FRONTEND_DIR / "css"
JS_DIR = FRONTEND_DIR / "js"

DB_PATH = BASE_DIR / "yield_lands.db"

# ── SHARED AUTH: validate tokens against the gateway's live session store ────
# auth_excel.py keeps sessions in an in-memory dict inside the gateway
# process (port 8085 by default) — there is no separate users/sessions DB
# file this backend (a different process, port 5008) can read directly.
# So instead we just ask the gateway to verify the token for us via its
# existing /api/auth/me route, the same way any other client would.
GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

# Must match CROP_BACKENDS in main.py / gateway.py
STATE_BACKEND_PORTS = {
    "tripura": 5000,
    "meghalaya": 5002,
    "rajasthan": 5006,
}

DEFAULT_STATE = "tripura"

# ── SOILGRIDS (ISRIC) SOIL TYPE LOOKUP ────────────────────────────────────────
# Given a lat/lng from a geofenced plot, queries ISRIC SoilGrids' WRB
# classification endpoint (no auth required) and maps the returned WRB
# classes onto the Soil_Type categories each state's crop-yield model was
# actually trained on. This is looked up by state because the training-data
# categories differ per state model — extend STATE_SOIL_CLASSES /
# STATE_WRB_TO_SOIL as more state models are confirmed.
SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/classification/query"

# SoilGrids is a public, best-effort service and is occasionally slow
# (multi-second responses aren't unusual). First attempt fails fast so a
# broken/very-slow request doesn't block the UI for too long; if it times
# out, the retry gets a longer window in case it was just transient.
SOILGRIDS_TIMEOUTS_SECONDS = [8, 20]

# Only request the top 3 WRB candidate classes — plenty for a weighted vote
# against a 4-category mapping table, and a smaller/faster response than
# the default 5.
SOILGRIDS_NUMBER_CLASSES = 3

# Cache is backed by SQLite (same DB file as the lands table) so it survives
# backend restarts — the same villages/plots get analyzed repeatedly across
# sessions, and there's no reason to re-hit SoilGrids for a point we've
# already resolved recently. Keyed on lat/lon rounded to ~1m precision.
SOILGRIDS_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 1 week — soil classification doesn't change day to day


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 5), round(lon, 5))


def _soilgrids_cache_get(lat: float, lon: float) -> list[tuple[str, float]] | None:
    key_lat, key_lon = _cache_key(lat, lon)
    db = get_db()
    row = db.execute(
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


# The exact 4 strings baked into the Rajasthan model's one-hot encoder
# (confirmed against merged_crop_enriched_features_del.xlsx and the
# Soil_Type_* columns inside model_artefacts.pkl — "Alluvial" is the
# dropped/reference category, "Black Cotton" is spelled out in full).
RAJASTHAN_SOIL_CLASSES = ["Alluvial", "Sandy", "Desert", "Black Cotton"]

# WRB (SoilGrids) class -> nearest Rajasthan Soil_Type category.
# Only used for Rajasthan right now; add per-state tables here once the
# other state models' training categories are confirmed the same way.
RAJASTHAN_WRB_TO_SOIL = {
    "Calcisols": "Desert", "Leptosols": "Desert", "Solonchaks": "Desert",
    "Arenosols": "Sandy", "Regosols": "Sandy",
    "Cambisols": "Alluvial", "Fluvisols": "Alluvial",
    "Vertisols": "Black Cotton", "Luvisols": "Black Cotton", "Chernozems": "Black Cotton",
}

# The exact 2 strings baked into the Tripura model's one-hot encoder
# (confirmed against merged_crop_enriched_features_del.xlsx — only two
# Soil_Type values ever appear in the training data — and against
# feat_cols inside model_artefacts.pkl, which carries a single
# "Soil_Type_Red Laterite" dummy column, meaning "Alluvial" is the
# dropped/reference category).
TRIPURA_SOIL_CLASSES = ["Alluvial", "Red Laterite"]

# WRB (SoilGrids) class -> nearest Tripura Soil_Type category. Tripura is a
# hilly, high-rainfall North-East state, so its dominant WRB classes split
# roughly into: heavily-weathered, iron/aluminium-oxide-rich upland soils
# (the "Red Laterite" bucket) vs. younger, river/valley-deposited soils
# (the "Alluvial" bucket). Unlike the Rajasthan table, this hasn't been
# validated against ground-truth soil surveys for Tripura specifically —
# treat it as a reasonable best-effort default, not a confirmed mapping,
# and let a manual dropdown override take precedence.
TRIPURA_WRB_TO_SOIL = {
    "Acrisols": "Red Laterite", "Ferralsols": "Red Laterite",
    "Plinthosols": "Red Laterite", "Nitisols": "Red Laterite",
    "Lixisols": "Red Laterite", "Alisols": "Red Laterite",
    "Fluvisols": "Alluvial", "Cambisols": "Alluvial",
    "Gleysols": "Alluvial", "Regosols": "Alluvial", "Umbrisols": "Alluvial",
}

# The exact 3 strings baked into the Meghalaya model's one-hot encoder
# (confirmed against merged_crop_enriched_features_del.xlsx — Soil_Type
# takes 3 values in the training data — and against feat_cols inside
# model_artefacts.pkl, which carries two dummy columns,
# "Soil_Type_Red Laterite" and "Soil_Type_Sandy Loam", meaning "Alluvial"
# is the dropped/reference category, same as it is for Tripura).
# Training-data counts: Red Laterite 6882, Sandy Loam 788, Alluvial 275 —
# Red Laterite dominates heavily, consistent with Meghalaya's hilly,
# high-rainfall plateau terrain; the small Alluvial slice likely comes from
# the Garo Hills lowlands near the Brahmaputra plains.
MEGHALAYA_SOIL_CLASSES = ["Alluvial", "Red Laterite", "Sandy Loam"]

# WRB (SoilGrids) class -> nearest Meghalaya Soil_Type category. Like the
# Tripura table, this is a pedologically-reasoned best-effort default (not
# validated against a ground-truth Meghalaya soil survey): heavily-weathered
# upland iron/aluminium-oxide soils and high-altitude humus-rich soils map
# to "Red Laterite" (the dominant class here); coarser, less-structured
# classes map to "Sandy Loam"; younger river/valley-deposited soils map to
# "Alluvial". Treat as a starting point and let a manual dropdown override
# take precedence when it looks wrong for a given spot.
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


def query_soilgrids_wrb(lat: float, lon: float) -> list[tuple[str, float]]:
    """
    Queries ISRIC SoilGrids' classification endpoint for a point and returns
    the raw list of (wrb_class_name, probability_pct) pairs, ordered highest
    to lowest, or [] if the service is unreachable / the point has no data.
    NOTE: SoilGrids takes lon then lat (GeoJSON point order) — the params
    below are passed by name so there's no ambiguity at the call site.

    Cached in SQLite for SOILGRIDS_CACHE_TTL_SECONDS per (lat, lon) rounded
    to ~1m, surviving backend restarts. On a cache miss, tries a fast
    timeout first and a more patient one on retry — SoilGrids is a public
    best-effort service and occasionally takes a while to respond.
    """
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
            # Filter out null probabilities (SoilGrids returns nulls for
            # points outside its coverage, e.g. open ocean or bad coordinate
            # order).
            result = [(cls, pct) for cls, pct in probs if pct is not None]
            _soilgrids_cache_set(lat, lon, result)
            return result
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < len(SOILGRIDS_TIMEOUTS_SECONDS):
                logger.warning(
                    "SoilGrids query attempt %d/%d (timeout=%ss) failed for (lat=%s, lon=%s): %s — retrying",
                    attempt, len(SOILGRIDS_TIMEOUTS_SECONDS), timeout, lat, lon, exc,
                )
                continue

    logger.warning(
        "SoilGrids query failed for (lat=%s, lon=%s) after %d attempt(s): %s",
        lat, lon, len(SOILGRIDS_TIMEOUTS_SECONDS), last_exc,
    )
    return []


def lookup_soil_type(lat: float, lon: float, state: str) -> str | None:
    """
    Returns the Soil_Type string a given state's model expects, derived from
    a weighted vote over SoilGrids' returned WRB probability classes (not
    just the single top class, since SoilGrids confidence is often spread
    thin across several candidates). Returns None if unconfigured for this
    state, or if SoilGrids has no usable data for the point — callers should
    fall back to a manual default/dropdown in that case, never block on it.
    """
    wrb_map = STATE_WRB_TO_SOIL.get((state or "").lower().strip())
    if not wrb_map:
        logger.info("No WRB->Soil_Type mapping configured for state '%s' yet.", state)
        return None

    probs = query_soilgrids_wrb(lat, lon)
    if not probs:
        logger.info("SoilGrids returned no usable classification for (lat=%s, lon=%s).", lat, lon)
        return None

    scores: dict[str, float] = {}
    for wrb_class, pct in probs:
        mapped = wrb_map.get(wrb_class)
        if mapped:
            scores[mapped] = scores.get(mapped, 0) + pct

    if not scores:
        logger.info(
            "SoilGrids classes for (lat=%s, lon=%s) had no overlap with the '%s' mapping table: %s",
            lat, lon, state, probs,
        )
        return None

    best = max(scores, key=scores.get)
    logger.info("Soil type for (lat=%s, lon=%s) -> %s (weighted scores: %s)", lat, lon, best, scores)
    return best


def geofence_centroid(body: dict) -> tuple[float, float] | None:
    """
    Resolves the lat/lng to run the soil lookup against. Prefers an explicit
    latitude/longitude on the body (e.g. a marker the user placed); falls
    back to the centroid of the geofence bounds{north,south,east,west} if
    only a rectangle was drawn. Returns None if neither is present.
    """
    lat, lon = body.get("latitude"), body.get("longitude")
    if lat is not None and lon is not None:
        return float(lat), float(lon)

    bounds = body.get("bounds") or {}
    if all(k in bounds for k in ("north", "south", "east", "west")):
        centroid_lat = (float(bounds["north"]) + float(bounds["south"])) / 2
        centroid_lon = (float(bounds["east"]) + float(bounds["west"])) / 2
        return centroid_lat, centroid_lon

    return None


# ── MAPPLS (MapmyIndia) OAUTH CONFIG ──────────────────────────────────────────
# Read from environment variables so the secret never lives in source control.
# Set these before running:
#   export MAPPLS_CLIENT_ID="..."
#   export MAPPLS_CLIENT_SECRET="..."
MAPPLS_CLIENT_ID = os.environ.get("MAPPLS_CLIENT_ID", "")
MAPPLS_CLIENT_SECRET = os.environ.get("MAPPLS_CLIENT_SECRET", "")
MAPPLS_TOKEN_URL = "https://outpost.mappls.com/api/security/oauth/token"
MAPPLS_GEOFENCE_BASE = "https://atlas.mappls.com/api/places/geofence"
MAPPLS_SEARCH_URL = "https://atlas.mappls.com/api/places/search/json"

# Separate, referrer-restricted "map key" for raster tile requests. This is
# NOT the OAuth client_id/secret above — Mappls issues a distinct key for
# embedding directly in client-side tile URLs, so it's safe to hand to the
# browser (unlike the client_secret, which must never leave the server).
# Set it before running:
#   export MAPPLS_MAP_KEY="..."
MAPPLS_MAP_KEY = os.environ.get("MAPPLS_MAP_KEY", "")

_mappls_token_cache = {"token": None, "expires_at": 0}

app = Flask(__name__)
CORS(app)

logger.info(
    "Mappls env check on startup — MAPPLS_CLIENT_ID: %s, MAPPLS_CLIENT_SECRET: %s, MAPPLS_MAP_KEY: %s",
    "set" if MAPPLS_CLIENT_ID else "MISSING",
    "set" if MAPPLS_CLIENT_SECRET else "MISSING",
    "set" if MAPPLS_MAP_KEY else "MISSING",
)


# ── MAPPLS OAUTH TOKEN HANDLING ────────────────────────────────────────────────

def get_mappls_token() -> str | None:
    """
    Returns a cached, valid Mappls bearer token, fetching/refreshing it from
    Mappls' OAuth endpoint when missing or expired. Returns None if the
    client_id/secret aren't configured or the request fails — logs which,
    with detail, so a misconfigured key doesn't get silently swallowed.
    """
    now = time.time()
    if _mappls_token_cache["token"] and _mappls_token_cache["expires_at"] > now + 30:
        return _mappls_token_cache["token"]

    if not MAPPLS_CLIENT_ID or not MAPPLS_CLIENT_SECRET:
        logger.info("Mappls OAuth token not requested: MAPPLS_CLIENT_ID/MAPPLS_CLIENT_SECRET not set in this process's environment.")
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
            logger.warning("Mappls OAuth call succeeded (HTTP 200) but no access_token in response: %s", data)
            return None
        _mappls_token_cache["token"] = token
        _mappls_token_cache["expires_at"] = now + expires_in
        return token
    except requests.exceptions.RequestException as exc:
        # Log the actual HTTP response body when there is one — Mappls' OAuth
        # endpoint returns a JSON error (invalid_client, invalid credentials,
        # etc.) that tells you exactly why auth failed, e.g. a truncated or
        # copy-pasted-wrong client_id/secret.
        body = getattr(exc.response, "text", None) if getattr(exc, "response", None) is not None else None
        logger.warning("Mappls OAuth token request failed: %s%s", exc, f" | response body: {body}" if body else "")
        return None


def mappls_auth_header() -> dict | None:
    token = get_mappls_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


# ── AUTH: verify session token against the gateway's /api/auth/me ────────────
# Mirrors the "Authorization: Bearer <session.token>" pattern admin.html and
# index.html use. Every /api/yield/lands* route below requires a valid
# session, and farmers are restricted to their own records.

def verify_token(token: str) -> dict | None:
    """
    Asks the gateway process to resolve a bearer token to a user, since the
    actual session store (auth_excel.py's in-memory SESSIONS dict) lives
    inside that process, not here. Returns {"uid","email","role"} or None
    if the token is missing/invalid/expired, or the gateway is unreachable.
    """
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
        data = resp.json()
        if not data.get("email"):
            return None
        return {"uid": data.get("uid"), "email": data.get("email"), "role": data.get("role")}
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not verify token against gateway (%s): %s", GATEWAY_INTERNAL_URL, exc)
        return None


def require_auth(roles: list[str] | None = None):
    """
    Route decorator: requires a valid 'Authorization: Bearer <token>' header.
    Sets g.user = {"uid","email","role"}. If `roles` is given, the caller's
    role must be in that list (case-insensitive) or the request gets 403.
    """
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


# ── DB HELPERS ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS lands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_name       TEXT NOT NULL,
    state               TEXT NOT NULL,
    district            TEXT,
    user_email          TEXT,
    crop                TEXT,
    soil_type           TEXT,
    irrigation_type     TEXT,
    fertilizer_kg_per_ha REAL,
    pest_incidence      TEXT,
    season              TEXT,
    latitude            REAL,
    longitude           REAL,
    area_hectare        REAL,
    bounds_json         TEXT,
    predicted_yield     REAL,
    normal_yield        REAL,
    anomaly_pct         REAL,
    source              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS soilgrids_cache (
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    fetched_at  REAL NOT NULL,
    probs_json  TEXT NOT NULL,
    PRIMARY KEY (lat, lon)
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    # Migration for pre-existing DBs created before user_email existed
    # (CREATE TABLE IF NOT EXISTS won't add columns to an already-existing
    # table).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(lands)")}
    if "user_email" not in existing_cols:
        conn.execute("ALTER TABLE lands ADD COLUMN user_email TEXT")
    conn.commit()
    conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("bounds_json"):
        try:
            d["bounds"] = json.loads(d["bounds_json"])
        except (TypeError, ValueError):
            d["bounds"] = None
    else:
        d["bounds"] = None
    d.pop("bounds_json", None)
    return d


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── STATE / MODEL BACKEND PROXY ────────────────────────────────────────────────

def state_port(state: str) -> int:
    return STATE_BACKEND_PORTS.get((state or "").lower().strip(), STATE_BACKEND_PORTS[DEFAULT_STATE])


def call_predict(state: str, payload: dict) -> dict:
    """
    Proxy a prediction request to the running per-state crop backend
    (backend_2.py) started by main.py. Returns dict with yield/normal/anomaly/source,
    or an 'error' key if the backend is unreachable / crop invalid.
    """
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
        # backend_2.py /valid_crops may return a list or {"crops": [...]}
        if isinstance(data, list):
            return data
        return data.get("crops", data.get("valid_crops", []))
    except requests.exceptions.RequestException:
        return []


def build_predict_payload(body: dict) -> dict:
    """
    Map a land record / form body to the fields backend_2.py's /predict
    expects. If soil_type wasn't supplied by the caller (frontend didn't
    auto-detect it, or the user's manual dropdown was left unset), try the
    SoilGrids lookup against the geofenced lat/lng before falling back to
    the "Alluvial" default.
    """
    soil_type = body.get("soil_type")
    if not soil_type:
        state = body.get("state", DEFAULT_STATE)
        point = geofence_centroid(body)
        if point:
            soil_type = lookup_soil_type(point[0], point[1], state)

    return {
        "crop": body.get("crop", ""),
        "district": body.get("district", "Dhalai"),
        "Soil_Type": soil_type or "Alluvial",
        "Irrigation_Type": body.get("irrigation_type", "Canal"),
        "Area (Hectare)": float(body.get("area_hectare") or 0) or 500,
        "Fertilizer_kg_per_ha": float(body.get("fertilizer_kg_per_ha") or 70),
        "Pest_Disease_Incidence": body.get("pest_incidence", "Low"),
    }


# ── FRONTEND PAGE ROUTES (served directly; gateway.py can proxy these) ────────

@app.route("/content/yield-detect", methods=["GET"])
def serve_yield_detect_page():
    # NOTE: this is a plain page navigation (window.top.location.href /
    # iframe src) so there's no Authorization header to check here — the
    # cropai_session lives in localStorage, not a cookie. Enforcement for
    # this page happens two ways instead: (1) client-side in the page's own
    # <script>, which checks the session role and bounces non-farmers
    # straight back out, and (2) every /api/yield/* call the page makes is
    # still hard-enforced server-side via @require_auth above. If you'd
    # rather have real page-level enforcement, switch session storage to an
    # HttpOnly cookie and this route can use @require_auth(roles=["farmer"]).
    return send_from_directory(str(HTML_DIR), "yield_detect.html")


@app.route("/content/yield-detect-editor", methods=["GET"])
def serve_yield_detect_editor_page():
    return send_from_directory(str(HTML_DIR), "yield_detect_editor.html")


@app.route("/css/<path:filename>", methods=["GET"])
def serve_css(filename):
    return send_from_directory(str(CSS_DIR), filename)


@app.route("/js/<path:filename>", methods=["GET"])
def serve_js(filename):
    return send_from_directory(str(JS_DIR), filename)


# ── API: HEALTH ─────────────────────────────────────────────────────────────────

@app.route("/api/yield/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "db": str(DB_PATH)})


# ── API: VALID CROPS (proxy, for populating the editor's crop dropdown) ───────

@app.route("/api/yield/valid_crops", methods=["GET"])
def valid_crops():
    state = request.args.get("state", DEFAULT_STATE)
    return jsonify({"state": state, "crops": call_valid_crops(state)})


# ── API: SOIL TYPE LOOKUP (SoilGrids, by geofence lat/lng) ────────────────────
# Frontend calls this right after a geofence is drawn/dragged so it can
# auto-fill the soil dropdown in the editor before the user hits "Analyze".
# Accepts either an explicit lat/lon, or a bounds{north,south,east,west}
# rectangle (its centroid is used) — mirrors what /analyze and /lands accept.

@app.route("/api/yield/soil_lookup", methods=["GET", "POST"])
def soil_lookup():
    if request.method == "GET":
        state = request.args.get("state", DEFAULT_STATE)
        body = {
            "latitude": request.args.get("lat", type=float),
            "longitude": request.args.get("lon", type=float),
        }
    else:
        body = request.get_json(force=True) or {}
        state = body.get("state", DEFAULT_STATE)

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


# ── API: ANALYZE (does NOT persist — used by the "Analyze" button) ────────────

@app.route("/api/yield/analyze", methods=["POST"])
def analyze():
    body = request.get_json(force=True) or {}
    state = body.get("state", DEFAULT_STATE)

    if not body.get("crop"):
        return jsonify({"error": "crop is required"}), 400
    if body.get("latitude") is None or body.get("longitude") is None:
        return jsonify({"error": "latitude/longitude are required (geofence the land first)"}), 400

    payload = build_predict_payload(body)
    result = call_predict(state, payload)

    if "error" in result:
        return jsonify(result), 502

    return jsonify({
        "state": state,
        "predicted_yield": result.get("yield"),
        "normal_yield": result.get("normal"),
        "anomaly_pct": result.get("anomaly"),
        "source": result.get("source"),
    })


# ── API: LANDS CRUD ─────────────────────────────────────────────────────────────

@app.route("/api/yield/lands", methods=["GET"])
@require_auth()
def list_lands():
    state = request.args.get("state")
    db = get_db()

    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    # Farmers only ever see their own lands; other roles (admin, analyst,
    # state/district/central-admin) see everything (optionally filtered by
    # ?state=).
    if (g.user.get("role") or "").lower() == "farmer":
        clauses.append("user_email = ?")
        params.append(g.user["email"])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(f"SELECT * FROM lands {where} ORDER BY updated_at DESC", params).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/yield/lands/<int:land_id>", methods=["GET"])
@require_auth()
def get_land(land_id):
    db = get_db()
    row = db.execute("SELECT * FROM lands WHERE id = ?", (land_id,)).fetchone()
    if not row:
        return jsonify({"error": "land not found"}), 404
    if (g.user.get("role") or "").lower() == "farmer" and row["user_email"] != g.user["email"]:
        return jsonify({"error": "Forbidden — not your land record"}), 403
    return jsonify(row_to_dict(row))


@app.route("/api/yield/lands", methods=["POST"])
@require_auth(roles=["farmer"])
def create_land():
    body = request.get_json(force=True) or {}

    if not body.get("property_name"):
        return jsonify({"error": "property_name is required"}), 400
    if body.get("latitude") is None or body.get("longitude") is None:
        return jsonify({"error": "latitude/longitude are required — geofence the land on the map first"}), 400

    state = body.get("state", DEFAULT_STATE)

    # Auto-detect soil type from the geofence before persisting, if the
    # caller (frontend) didn't already supply one via the dropdown.
    soil_type = body.get("soil_type")
    if not soil_type:
        point = geofence_centroid(body)
        if point:
            soil_type = lookup_soil_type(point[0], point[1], state)

    # If the caller hasn't already analyzed (no predicted_yield passed in),
    # run the prediction now so a land is never saved without a yield.
    predicted_yield = body.get("predicted_yield")
    normal_yield = body.get("normal_yield")
    anomaly_pct = body.get("anomaly_pct")
    source = body.get("source")

    if predicted_yield is None and body.get("crop"):
        predict_body = {**body, "soil_type": soil_type}
        result = call_predict(state, build_predict_payload(predict_body))
        if "error" not in result:
            predicted_yield = result.get("yield")
            normal_yield = result.get("normal")
            anomaly_pct = result.get("anomaly")
            source = result.get("source")

    ts = now_iso()
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO lands (
            property_name, state, district, user_email, crop, soil_type, irrigation_type,
            fertilizer_kg_per_ha, pest_incidence, season, latitude, longitude,
            area_hectare, bounds_json, predicted_yield, normal_yield, anomaly_pct,
            source, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            body.get("property_name"),
            state,
            body.get("district"),
            g.user["email"],
            body.get("crop"),
            soil_type,
            body.get("irrigation_type"),
            body.get("fertilizer_kg_per_ha"),
            body.get("pest_incidence"),
            body.get("season"),
            body.get("latitude"),
            body.get("longitude"),
            body.get("area_hectare"),
            json.dumps(body.get("bounds")) if body.get("bounds") else None,
            predicted_yield,
            normal_yield,
            anomaly_pct,
            source,
            ts,
            ts,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM lands WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.route("/api/yield/lands/<int:land_id>", methods=["PUT"])
@require_auth(roles=["farmer"])
def update_land(land_id):
    db = get_db()
    existing = db.execute("SELECT * FROM lands WHERE id = ?", (land_id,)).fetchone()
    if not existing:
        return jsonify({"error": "land not found"}), 404
    if (g.user.get("role") or "").lower() == "farmer" and existing["user_email"] != g.user["email"]:
        return jsonify({"error": "Forbidden — not your land record"}), 403

    body = request.get_json(force=True) or {}
    merged = {**row_to_dict(existing), **{k: v for k, v in body.items() if v is not None}}
    state = merged.get("state", DEFAULT_STATE)

    # Re-detect soil type only if it's genuinely missing (e.g. this land
    # predates the SoilGrids wiring) — otherwise keep whatever's already on
    # the record rather than silently overwriting a user's manual override.
    if not merged.get("soil_type"):
        point = geofence_centroid(merged)
        if point:
            merged["soil_type"] = lookup_soil_type(point[0], point[1], state)

    # Re-analyze on edit unless the caller explicitly supplied fresh results.
    predicted_yield = body.get("predicted_yield")
    normal_yield = body.get("normal_yield")
    anomaly_pct = body.get("anomaly_pct")
    source = body.get("source")

    if predicted_yield is None and merged.get("crop"):
        result = call_predict(state, build_predict_payload(merged))
        if "error" not in result:
            predicted_yield = result.get("yield")
            normal_yield = result.get("normal")
            anomaly_pct = result.get("anomaly")
            source = result.get("source")
        else:
            predicted_yield = existing["predicted_yield"]
            normal_yield = existing["normal_yield"]
            anomaly_pct = existing["anomaly_pct"]
            source = existing["source"]

    ts = now_iso()
    db.execute(
        """
        UPDATE lands SET
            property_name = ?, state = ?, district = ?, crop = ?, soil_type = ?,
            irrigation_type = ?, fertilizer_kg_per_ha = ?, pest_incidence = ?,
            season = ?, latitude = ?, longitude = ?, area_hectare = ?,
            bounds_json = ?, predicted_yield = ?, normal_yield = ?, anomaly_pct = ?,
            source = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            merged.get("property_name"),
            state,
            merged.get("district"),
            merged.get("crop"),
            merged.get("soil_type"),
            merged.get("irrigation_type"),
            merged.get("fertilizer_kg_per_ha"),
            merged.get("pest_incidence"),
            merged.get("season"),
            merged.get("latitude"),
            merged.get("longitude"),
            merged.get("area_hectare"),
            json.dumps(merged.get("bounds")) if merged.get("bounds") else None,
            predicted_yield,
            normal_yield,
            anomaly_pct,
            source,
            ts,
            land_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM lands WHERE id = ?", (land_id,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/yield/lands/<int:land_id>", methods=["DELETE"])
@require_auth(roles=["farmer"])
def delete_land(land_id):
    db = get_db()
    existing = db.execute("SELECT id, user_email FROM lands WHERE id = ?", (land_id,)).fetchone()
    if not existing:
        return jsonify({"error": "land not found"}), 404
    if (g.user.get("role") or "").lower() == "farmer" and existing["user_email"] != g.user["email"]:
        return jsonify({"error": "Forbidden — not your land record"}), 403
    db.execute("DELETE FROM lands WHERE id = ?", (land_id,))
    db.commit()
    return jsonify({"deleted": land_id})


# ── API: MAPPLS MAP KEY (for tile layer) ─────────────────────────────────────
# Hands the browser the referrer-restricted tile key so it can build the
# Mappls raster tile URL itself. Unlike the OAuth client_id/secret, this key
# is designed by Mappls to live in client-side requests.

@app.route("/api/yield/mappls_key", methods=["GET"])
def mappls_key():
    return jsonify({"key": MAPPLS_MAP_KEY, "configured": bool(MAPPLS_MAP_KEY)})


# ── API: PLACE SEARCH (Mappls, with Nominatim fallback) ──────────────────────
# Mappls' India-focused index covers small villages, hamlets, and farmland far
# better than OpenStreetMap/Nominatim, which is what the search box needs for
# a crop-yield tool. The frontend never calls either geocoder directly — this
# keeps the Mappls bearer token server-side and gives us one place to fall
# back to Nominatim if Mappls isn't configured or the request fails.

def _search_mappls(query: str):
    headers = mappls_auth_header()
    if not headers:
        # The real reason (missing env vars vs. a failed/rejected OAuth call)
        # was already logged inside get_mappls_token() above.
        return None
    try:
        resp = requests.get(
            MAPPLS_SEARCH_URL,
            params={"query": query, "region": "ind"},
            headers=headers,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        logger.warning("Mappls search request failed for %r: %s", query, exc)
        return None

    raw_locations = data.get("suggestedLocations") or []
    if not raw_locations:
        # Either Mappls returned zero matches, or the response shape doesn't
        # match what this code expects — log the raw payload so the field
        # names can be checked/adjusted against what your account actually
        # returns.
        logger.warning(
            "Mappls search for %r returned no 'suggestedLocations'. Raw response: %s",
            query, json.dumps(data)[:2000],
        )
        return None

    results = []
    for item in raw_locations:
        lat, lng = item.get("latitude"), item.get("longitude")
        if lat is None or lng is None:
            continue
        label_parts = [
            item.get("placeName"), item.get("placeAddress"),
        ]
        display_name = ", ".join(p for p in label_parts if p) or item.get("placeName", query)
        results.append({
            "display_name": display_name,
            "lat": lat,
            "lon": lng,
            "source": "mappls",
        })

    if not results:
        logger.warning(
            "Mappls search for %r: 'suggestedLocations' present but no usable lat/lng. Raw response: %s",
            query, json.dumps(data)[:2000],
        )
    return results


def _search_nominatim(query: str):
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": 6, "q": query},
            headers={"Accept": "application/json", "User-Agent": "yield-detect/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        return []

    return [
        {
            "display_name": item.get("display_name", query),
            "lat": item.get("lat"),
            "lon": item.get("lon"),
            "source": "nominatim",
        }
        for item in data
    ]


@app.route("/api/yield/geocode/search", methods=["GET"])
def geocode_search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 3:
        return jsonify({"results": []})

    results = _search_mappls(query)
    if not results:
        logger.info("Falling back to Nominatim for %r", query)
        results = _search_nominatim(query)

    return jsonify({"results": results[:8]})


# ── API: MAPPLS GEOFENCE PROXY ──────────────────────────────────────────────────
# The frontend never talks to Mappls directly for these — it calls this backend,
# which attaches the OAuth bearer token (fetched server-side from client_id/secret)
# to every request. This keeps the client_secret out of the browser entirely.

@app.route("/api/yield/geofence/status", methods=["GET"])
def geofence_status():
    """Lets the frontend check whether geofencing is configured/reachable."""
    configured = bool(MAPPLS_CLIENT_ID and MAPPLS_CLIENT_SECRET)
    token_ok = bool(get_mappls_token()) if configured else False
    return jsonify({"configured": configured, "token_ok": token_ok})


@app.route("/api/yield/geofence", methods=["POST"])
def create_geofence():
    """
    Create a Mappls geofence for a land plot.
    Expects: { "name": str, "bounds": {"north","south","east","west"} }
    """
    headers = mappls_auth_header()
    if not headers:
        return jsonify({"error": "Mappls geofencing not configured or token unavailable"}), 502

    body = request.get_json(force=True) or {}
    name = body.get("name")
    bounds = body.get("bounds") or {}
    if not name or not all(k in bounds for k in ("north", "south", "east", "west")):
        return jsonify({"error": "name and bounds{north,south,east,west} are required"}), 400

    # Mappls geofence create expects a polygon ring of [lng, lat] pairs.
    ring = [
        [bounds["west"], bounds["north"]],
        [bounds["east"], bounds["north"]],
        [bounds["east"], bounds["south"]],
        [bounds["west"], bounds["south"]],
        [bounds["west"], bounds["north"]],
    ]
    payload = {
        "name": name,
        "geo_json": {"type": "Polygon", "coordinates": [ring]},
    }

    try:
        resp = requests.post(f"{MAPPLS_GEOFENCE_BASE}/save", json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Mappls geofence create failed: {exc}"}), 502


@app.route("/api/yield/geofence/<fence_id>", methods=["DELETE"])
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


@app.route("/api/yield/geofence/check", methods=["POST"])
def check_geofence_point():
    """
    Checks whether a lat/lng point falls inside a saved Mappls geofence.
    Expects: { "fence_id": str, "latitude": float, "longitude": float }
    """
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


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Yield Detect backend")
    parser.add_argument("--port", type=int, default=5008)
    args = parser.parse_args()

    init_db()
    print("=" * 55)
    print("  YIELD DETECT BACKEND")
    print(f"  DB:  {DB_PATH}")
    print(f"  Running at http://127.0.0.1:{args.port}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=args.port, debug=False)