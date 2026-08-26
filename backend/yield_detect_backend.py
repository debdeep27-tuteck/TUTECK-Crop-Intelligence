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

# ── SHARED AUTH: validate tokens against the gateway's live session store ────
# auth_excel.py keeps sessions in an in-memory dict inside the gateway
# process (port 8085 by default) — there is no separate users/sessions DB
# file this backend (a different process, port 5008) can read directly.
# So instead we just ask the gateway to verify the token for us via its
# existing /api/auth/me route, the same way any other client would.
GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

# ── YIELD PLATFORM SERVICE: generic land storage + crop-yield prediction ──
# (yield_platform_service.py — one standalone microservice, one process,
# one port, one API key). This backend used to keep its own "lands" table
# and its own soil/prediction logic; both moved out into that single
# service, which knows nothing about farmers, roles, or this app's
# gateway. This app now talks to it over HTTP for both concerns and
# translates crop-specific fields (crop, soil_type, irrigation_type, ...)
# to/from the opaque `metadata` blob on the land side, so every existing
# /api/yield/* route keeps its exact same request/response contract for
# the frontend. Same pattern as auction_backend.py -> auction_engine_service.py.
YIELD_PLATFORM_SERVICE_URL = os.environ.get("YIELD_PLATFORM_SERVICE_URL", "http://127.0.0.1:6100")
YIELD_PLATFORM_SERVICE_API_KEY = os.environ.get("YIELD_PLATFORM_SERVICE_API_KEY", "")

# Back-compat aliases: the land-storage and crop-yield clients below both
# just point at the one merged service now.
LAND_SERVICE_URL = YIELD_PLATFORM_SERVICE_URL
LAND_SERVICE_API_KEY = YIELD_PLATFORM_SERVICE_API_KEY
CROP_YIELD_SERVICE_URL = YIELD_PLATFORM_SERVICE_URL
CROP_YIELD_SERVICE_API_KEY = YIELD_PLATFORM_SERVICE_API_KEY

# ── EXTERNAL-APP AUTH: trusted-identity mode ──────────────────────────────
# Any OTHER app (one that has no idea what auth_excel.py or /api/auth/me
# are) can still use this service, as long as it verifies its own users
# itself and then tells us who the user is via trusted headers, signed
# with a shared service-to-service API key. This is separate from, and
# does not replace, the gateway-token mode above — the crop app keeps
# using that unchanged. Unset by default; set it to enable this path.
YIELD_SERVICE_API_KEY = os.environ.get("YIELD_SERVICE_API_KEY", "")

# Must match CROP_BACKENDS in main.py / gateway.py
STATE_BACKEND_PORTS = {
    "tripura": 5000,
    "meghalaya": 5002,
    "rajasthan": 5006,
}

DEFAULT_STATE = "tripura"

# Soil-type lookup and yield prediction now live in crop_yield_service.py
# (see the CROP YIELD SERVICE client section below).


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
    inside that process, not here. Returns
    {"uid","email","role","state","district"} or None if the token is
    missing/invalid/expired, or the gateway is unreachable.
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
        return {
            "uid": data.get("uid"),
            "email": data.get("email"),
            "role": data.get("role"),
            "state": data.get("state") or "",
            "district": data.get("district") or "",
        }
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not verify token against gateway (%s): %s", GATEWAY_INTERNAL_URL, exc)
        return None


def trusted_header_identity() -> dict | None:
    """
    Alternate identity source for callers that are NOT our gateway. A caller
    app authenticates its own users however it wants, then forwards the
    request here with:

        Authorization: Bearer <YIELD_SERVICE_API_KEY>
        X-User-Email:    the already-verified user's email (required)
        X-User-Role:     e.g. "farmer", "admin"                (optional)
        X-User-State:    e.g. "rajasthan"                       (optional)
        X-User-District: e.g. "Ajmer"                           (optional)

    Only active when YIELD_SERVICE_API_KEY is set — unset (the default)
    means this path is disabled and every caller must use gateway-token
    auth, same as before. Returns None if the key doesn't match or
    X-User-Email is missing, so callers fall through to gateway auth.
    """
    if not YIELD_SERVICE_API_KEY:
        return None
    auth_header = request.headers.get("Authorization", "")
    presented_key = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if presented_key != YIELD_SERVICE_API_KEY:
        return None
    email = request.headers.get("X-User-Email", "").strip()
    if not email:
        return None
    return {
        "uid": email,
        "email": email,
        "role": request.headers.get("X-User-Role", "").strip(),
        "state": request.headers.get("X-User-State", "").strip(),
        "district": request.headers.get("X-User-District", "").strip(),
    }


def require_auth(roles: list[str] | None = None):
    """
    Route decorator: requires either (a) a trusted-service identity via the
    YIELD_SERVICE_API_KEY + X-User-* headers (any external app), or (b) a
    valid 'Authorization: Bearer <token>' verified against our own gateway
    (the crop app's existing behavior, unchanged). Sets g.user =
    {"uid","email","role","state","district"}. If `roles` is given, the
    caller's role must be in that list (case-insensitive) or 403.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = trusted_header_identity()
            if not user:
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


# ── LAND SERVICE CLIENT ─────────────────────────────────────────────────

class LandServiceError(Exception):
    pass


def _land_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if LAND_SERVICE_API_KEY:
        headers["Authorization"] = f"Bearer {LAND_SERVICE_API_KEY}"
    return headers


def _land_request(method: str, path: str, **kwargs) -> dict:
    url = f"{LAND_SERVICE_URL}{path}"
    try:
        resp = requests.request(method, url, headers=_land_headers(), timeout=10, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise LandServiceError(f"land_service unreachable at {url}: {exc}") from exc
    if resp.status_code == 404:
        return None
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise LandServiceError(f"land_service {method} {path} failed: {exc} — {resp.text[:300]}") from exc
    return resp.json() if resp.content else None


# Crop-specific fields that live inside the opaque `metadata` blob on the
# land_service side, since land_service itself has no idea what a "crop"
# or "soil type" is.
_LAND_METADATA_FIELDS = (
    "state", "district", "crop", "soil_type", "irrigation_type",
    "fertilizer_kg_per_ha", "pest_incidence", "season",
    "predicted_yield", "normal_yield", "anomaly_pct", "source",
)


def _parcel_to_land_dict(parcel: dict) -> dict:
    """Translate a generic land_service parcel back into this app's
    original land-record shape, so every existing route/frontend keeps
    seeing exactly the same fields as when this had its own `lands`
    table."""
    metadata = parcel.get("metadata") or {}
    d = {
        "id": parcel["id"],
        "property_name": parcel.get("label"),
        "user_email": parcel.get("ownerId"),
        "latitude": parcel.get("latitude"),
        "longitude": parcel.get("longitude"),
        "area_hectare": parcel.get("areaHectare"),
        "bounds": parcel.get("bounds"),
        "created_at": parcel.get("createdAt"),
        "updated_at": parcel.get("updatedAt"),
    }
    for field in _LAND_METADATA_FIELDS:
        d[field] = metadata.get(field)
    return d


def _land_dict_to_parcel_payload(body: dict, owner_email: str) -> dict:
    """Translate this app's land-record fields into a generic land_service
    parcel payload, folding crop-specific fields into `metadata`."""
    metadata = {field: body.get(field) for field in _LAND_METADATA_FIELDS if body.get(field) is not None}
    payload = {
        "ownerId": owner_email,
        "label": body.get("property_name"),
        "latitude": body.get("latitude"),
        "longitude": body.get("longitude"),
        "areaHectare": body.get("area_hectare"),
        "metadata": metadata,
    }
    if body.get("bounds") is not None:
        payload["bounds"] = body.get("bounds")
    return payload


def land_create(body: dict, owner_email: str) -> dict:
    parcel = _land_request("POST", "/parcels", json=_land_dict_to_parcel_payload(body, owner_email))
    return _parcel_to_land_dict(parcel)


def land_list(owner_email: str | None = None) -> list[dict]:
    params = {"ownerId": owner_email} if owner_email else None
    parcels = _land_request("GET", "/parcels", params=params) or []
    return [_parcel_to_land_dict(p) for p in parcels]


def land_get(land_id: int) -> dict | None:
    parcel = _land_request("GET", f"/parcels/{land_id}")
    return _parcel_to_land_dict(parcel) if parcel else None


def land_update(land_id: int, body: dict) -> dict:
    parcel = _land_request("PUT", f"/parcels/{land_id}", json=_land_dict_to_parcel_payload(body, body.get("user_email")))
    return _parcel_to_land_dict(parcel)


def land_delete(land_id: int) -> None:
    _land_request("DELETE", f"/parcels/{land_id}")


# ── CROP YIELD SERVICE CLIENT ───────────────────────────────────────────

class CropYieldServiceError(Exception):
    pass


def _crop_yield_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if CROP_YIELD_SERVICE_API_KEY:
        headers["Authorization"] = f"Bearer {CROP_YIELD_SERVICE_API_KEY}"
    return headers


def _crop_yield_request(method: str, path: str, **kwargs) -> dict:
    url = f"{CROP_YIELD_SERVICE_URL}{path}"
    try:
        resp = requests.request(method, url, headers=_crop_yield_headers(), timeout=20, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise CropYieldServiceError(f"crop_yield_service unreachable at {url}: {exc}") from exc
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise CropYieldServiceError(f"crop_yield_service {method} {path} failed: {exc} — {resp.text[:300]}") from exc
    return resp.json() if resp.content else None


def cy_valid_crops(state: str) -> dict:
    return _crop_yield_request("GET", "/valid_crops", params={"state": state})


def cy_valid_districts(state: str) -> dict:
    return _crop_yield_request("GET", "/valid_districts", params={"state": state})


def cy_soil_type(state: str, latitude=None, longitude=None, bounds=None) -> dict:
    params = {"state": state}
    if latitude is not None:
        params["lat"] = latitude
    if longitude is not None:
        params["lon"] = longitude
    if bounds:
        params.update(bounds)
    return _crop_yield_request("GET", "/soil_type", params=params)


def cy_predict(body: dict) -> dict:
    """body may include state, crop, district, season, soil_type,
    latitude/longitude or bounds, irrigation_type, area_hectare,
    fertilizer_kg_per_ha, pest_incidence — crop_yield_service auto-detects
    soil_type from lat/lng if it's not supplied."""
    return _crop_yield_request("POST", "/predict", json=body)


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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
    return jsonify({"status": "ok"})


# ── API: VALID CROPS (proxy, for populating the editor's crop dropdown) ───────

@app.route("/api/yield/valid_crops", methods=["GET"])
def valid_crops():
    state = request.args.get("state", DEFAULT_STATE)
    try:
        return jsonify(cy_valid_crops(state))
    except CropYieldServiceError as exc:
        logger.error("valid_crops: %s", exc)
        return jsonify({"error": "crop yield service unavailable"}), 502


# ── API: VALID DISTRICTS (proxy, for populating the editor's district dropdown) ─
# District_Name is a one-hot trained feature — a district string typed into
# free text that doesn't exactly match a trained value gets silently
# dropped to the baseline district at prediction time. This exposes the
# real trained list so the frontend can offer a constrained dropdown.

@app.route("/api/yield/valid_districts", methods=["GET"])
def valid_districts():
    state = request.args.get("state", DEFAULT_STATE)
    try:
        return jsonify(cy_valid_districts(state))
    except CropYieldServiceError as exc:
        logger.error("valid_districts: %s", exc)
        return jsonify({"error": "crop yield service unavailable"}), 502


# ── API: SOIL TYPE LOOKUP (proxy to crop_yield_service, by geofence lat/lng) ──
# Frontend calls this right after a geofence is drawn/dragged so it can
# auto-fill the soil dropdown in the editor before the user hits "Analyze".
# Accepts either an explicit lat/lon, or a bounds{north,south,east,west}
# rectangle (its centroid is used) — mirrors what /analyze and /lands accept.

@app.route("/api/yield/soil_lookup", methods=["GET", "POST"])
def soil_lookup():
    if request.method == "GET":
        state = request.args.get("state", DEFAULT_STATE)
        lat = request.args.get("lat", type=float)
        lon = request.args.get("lon", type=float)
        bounds = None
    else:
        body = request.get_json(force=True) or {}
        state = body.get("state", DEFAULT_STATE)
        lat = body.get("latitude")
        lon = body.get("longitude")
        bounds = body.get("bounds")

    if lat is None and lon is None and not bounds:
        return jsonify({"error": "latitude/longitude (or bounds) are required"}), 400

    try:
        return jsonify(cy_soil_type(state, latitude=lat, longitude=lon, bounds=bounds))
    except CropYieldServiceError as exc:
        logger.error("soil_lookup: %s", exc)
        return jsonify({"error": "crop yield service unavailable"}), 502


# ── API: ANALYZE (does NOT persist — used by the "Analyze" button) ────────────

@app.route("/api/yield/analyze", methods=["POST"])
def analyze():
    body = request.get_json(force=True) or {}
    state = body.get("state", DEFAULT_STATE)

    if not body.get("crop"):
        return jsonify({"error": "crop is required"}), 400
    if body.get("latitude") is None or body.get("longitude") is None:
        return jsonify({"error": "latitude/longitude are required (geofence the land first)"}), 400

    try:
        result = cy_predict(body)
    except CropYieldServiceError as exc:
        logger.error("analyze: %s", exc)
        return jsonify({"error": "crop yield service unavailable"}), 502

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
    role = (g.user.get("role") or "").lower()

    # land_service only filters by ownerId server-side (it doesn't
    # understand state/district — those live in opaque metadata). So we
    # fetch the relevant owner scope and filter the rest in Python here.
    try:
        if role == "farmer":
            lands = land_list(owner_email=g.user["email"])
        else:
            lands = land_list()
    except LandServiceError as exc:
        logger.error("list_lands: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502

    if state:
        lands = [l for l in lands if (l.get("state") or "").lower() == state.lower()]
    if role == "district_admin":
        if g.user.get("district"):
            lands = [l for l in lands if l.get("district") == g.user["district"]]
        if g.user.get("state"):
            lands = [l for l in lands if l.get("state") == g.user["state"]]
    elif role == "state_admin":
        if g.user.get("state"):
            lands = [l for l in lands if l.get("state") == g.user["state"]]

    lands.sort(key=lambda l: l.get("updated_at") or "", reverse=True)
    return jsonify(lands)


@app.route("/api/yield/internal/production", methods=["GET"])
def internal_production():
    """
    Internal, service-to-service endpoint (no auth — mirrors
    auction_backend.py's /api/unused-crops) so other backends on the same
    host (e.g. cold_storage_backend.py) can pull expected crop production
    for a state+district from the geofenced Yield Detect lands, instead of
    from Auction-floor listings.

    Production per land is estimated as predicted_yield * area_hectare
    (falls back to normal_yield if a prediction hasn't been run yet for
    that land), summed per crop. Land records themselves now live in
    land_service.py — this fetches all parcels and filters/aggregates
    here, since land_service doesn't interpret the crop/state/district
    fields tucked inside metadata.

    Also returns "by_farmer": a per-land breakdown (crop + user_email +
    production_mt) so callers that want a per-farmer view (e.g. the cold
    storage dashboard's Yield Detect table) don't have to guess an
    identity — user_email is the only farmer-identifying field this
    service (or the auth gateway) actually stores; there is no separate
    display-name field anywhere in the stack.
    """
    state = request.args.get("state")
    district = request.args.get("district")
    try:
        lands = land_list()
    except LandServiceError as exc:
        logger.error("internal_production: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502

    if state:
        lands = [l for l in lands if (l.get("state") or "").lower() == state.lower()]
    if district:
        lands = [l for l in lands if (l.get("district") or "").lower() == district.lower()]

    totals: dict[str, float] = {}
    by_farmer: list[dict] = []
    for land in lands:
        crop = land.get("crop")
        area = land.get("area_hectare") or 0
        if not crop or not area:
            continue
        yield_rate = land.get("predicted_yield") if land.get("predicted_yield") is not None else land.get("normal_yield")
        if not yield_rate:
            continue
        production = float(yield_rate) * float(area)
        totals[crop] = totals.get(crop, 0.0) + production
        by_farmer.append({
            "crop": crop,
            "user_email": land.get("user_email"),
            "production_mt": production,
        })

    return jsonify({"state": state, "district": district, "production_mt": totals, "by_farmer": by_farmer})


@app.route("/api/yield/lands/<int:land_id>", methods=["GET"])
@require_auth()
def get_land(land_id):
    try:
        land = land_get(land_id)
    except LandServiceError as exc:
        logger.error("get_land: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502
    if not land:
        return jsonify({"error": "land not found"}), 404

    role = (g.user.get("role") or "").lower()
    if role == "farmer" and land["user_email"] != g.user["email"]:
        return jsonify({"error": "Forbidden — not your land record"}), 403
    if role == "district_admin":
        if g.user.get("district") and land["district"] != g.user["district"]:
            return jsonify({"error": "Forbidden — outside your assigned district"}), 403
        if g.user.get("state") and land["state"] != g.user["state"]:
            return jsonify({"error": "Forbidden — outside your assigned state"}), 403
    if role == "state_admin" and g.user.get("state") and land["state"] != g.user["state"]:
        return jsonify({"error": "Forbidden — outside your assigned state"}), 403

    return jsonify(land)


@app.route("/api/yield/lands", methods=["POST"])
@require_auth(roles=["farmer"])
def create_land():
    body = request.get_json(force=True) or {}

    if not body.get("property_name"):
        return jsonify({"error": "property_name is required"}), 400
    if body.get("latitude") is None or body.get("longitude") is None:
        return jsonify({"error": "latitude/longitude are required — geofence the land on the map first"}), 400

    state = body.get("state", DEFAULT_STATE)
    soil_type = body.get("soil_type")

    # If the caller hasn't already analyzed (no predicted_yield passed in),
    # run the prediction now so a land is never saved without a yield.
    # crop_yield_service auto-detects soil_type from lat/lng if it's not
    # already supplied — no separate soil-lookup call needed here.
    predicted_yield = body.get("predicted_yield")
    normal_yield = body.get("normal_yield")
    anomaly_pct = body.get("anomaly_pct")
    source = body.get("source")

    if predicted_yield is None and body.get("crop"):
        try:
            result = cy_predict(body)
        except CropYieldServiceError as exc:
            logger.error("create_land predict: %s", exc)
            result = {"error": str(exc)}
        if "error" not in result:
            predicted_yield = result.get("yield")
            normal_yield = result.get("normal")
            anomaly_pct = result.get("anomaly")
            source = result.get("source")
            soil_type = soil_type or result.get("soil_type_used")

    if not soil_type:
        # No prediction ran (e.g. crop not supplied yet) — still auto-fill
        # soil_type via the standalone soil lookup so it's on the record.
        try:
            soil_type = cy_soil_type(state, latitude=body.get("latitude"), longitude=body.get("longitude"),
                                      bounds=body.get("bounds")).get("soil_type")
        except CropYieldServiceError as exc:
            logger.warning("create_land soil lookup: %s", exc)

    to_store = {
        **body,
        "state": state,
        "soil_type": soil_type,
        "predicted_yield": predicted_yield,
        "normal_yield": normal_yield,
        "anomaly_pct": anomaly_pct,
        "source": source,
    }
    try:
        land = land_create(to_store, g.user["email"])
    except LandServiceError as exc:
        logger.error("create_land: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502
    return jsonify(land), 201


@app.route("/api/yield/lands/<int:land_id>", methods=["PUT"])
@require_auth(roles=["farmer"])
def update_land(land_id):
    try:
        existing = land_get(land_id)
    except LandServiceError as exc:
        logger.error("update_land: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502
    if not existing:
        return jsonify({"error": "land not found"}), 404
    if (g.user.get("role") or "").lower() == "farmer" and existing["user_email"] != g.user["email"]:
        return jsonify({"error": "Forbidden — not your land record"}), 403

    body = request.get_json(force=True) or {}
    merged = {**existing, **{k: v for k, v in body.items() if v is not None}}
    state = merged.get("state", DEFAULT_STATE)

    # Re-analyze on edit unless the caller explicitly supplied fresh
    # results. crop_yield_service auto-detects soil_type from lat/lng if
    # it's genuinely missing on the record — otherwise it keeps whatever's
    # already there rather than silently overwriting a manual override
    # (same behavior as before, just enforced by build_predict_payload's
    # default inside crop_yield_service).
    predicted_yield = body.get("predicted_yield")
    normal_yield = body.get("normal_yield")
    anomaly_pct = body.get("anomaly_pct")
    source = body.get("source")

    if predicted_yield is None and merged.get("crop"):
        try:
            result = cy_predict(merged)
        except CropYieldServiceError as exc:
            logger.error("update_land predict: %s", exc)
            result = {"error": str(exc)}
        if "error" not in result:
            predicted_yield = result.get("yield")
            normal_yield = result.get("normal")
            anomaly_pct = result.get("anomaly")
            source = result.get("source")
            if not merged.get("soil_type"):
                merged["soil_type"] = result.get("soil_type_used")
        else:
            predicted_yield = existing["predicted_yield"]
            normal_yield = existing["normal_yield"]
            anomaly_pct = existing["anomaly_pct"]
            source = existing["source"]

    merged.update({
        "state": state,
        "predicted_yield": predicted_yield,
        "normal_yield": normal_yield,
        "anomaly_pct": anomaly_pct,
        "source": source,
    })

    try:
        land = land_update(land_id, merged)
    except LandServiceError as exc:
        logger.error("update_land: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502
    return jsonify(land)


@app.route("/api/yield/lands/<int:land_id>", methods=["DELETE"])
@require_auth(roles=["farmer", "admin", "state_admin", "district_admin"])
def delete_land(land_id):
    try:
        existing = land_get(land_id)
    except LandServiceError as exc:
        logger.error("delete_land: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502
    if not existing:
        return jsonify({"error": "land not found"}), 404
    # Farmers may only delete their own land record; admin/state_admin/
    # district_admin can delete any land record.
    if (g.user.get("role") or "").lower() == "farmer" and existing["user_email"] != g.user["email"]:
        return jsonify({"error": "Forbidden — not your land record"}), 403
    try:
        land_delete(land_id)
    except LandServiceError as exc:
        logger.error("delete_land: %s", exc)
        return jsonify({"error": "land service unavailable"}), 502
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

    print("=" * 55)
    print("  YIELD DETECT BACKEND (adapter)")
    print(f"  Yield platform service: {YIELD_PLATFORM_SERVICE_URL}")
    print(f"  Running at http://127.0.0.1:{args.port}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=args.port, debug=False)