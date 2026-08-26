"""
service.py — AI Irrigation Advisory Microservice
==================================================
Standalone Flask microservice for the AI Irrigation Advisory System.
Fetches soil moisture + weather forecasts from Open-Meteo, estimates crop
water requirements by growth stage, and produces a 7-day irrigation
schedule. Designed to be run and consumed independently of the CropAI
frontend/monolith — any external app can call it over HTTP once an API
key is issued.

Local run:
    pip install -r requirements.txt
    cp .env.example .env      # then edit values
    python service.py

Production run:
    gunicorn -w 4 -b 0.0.0.0:5001 service:app

Auth:
    All endpoints except /health require an API key, sent as either:
      Header:  X-API-Key: <key>
      or       Authorization: Bearer <key>
    Valid keys are set via the IRRIGATION_API_KEYS env var (comma-separated).
    If that env var is unset, auth is disabled (open access) — fine for local
    dev, NOT recommended for a public deployment.

Versioned endpoints (preferred):
    GET  /api/v1/health    — health check (no auth)
    GET  /api/v1/crops     — list supported crops
    GET  /api/v1/districts — list districts for a state
    POST /api/v1/advise    — main irrigation schedule

Unversioned aliases (/health, /crops, /districts, /advise) are kept for
backward compatibility with existing callers and behave identically.
"""

import datetime
import logging
import os
import time
import warnings
from functools import wraps

import requests
import numpy as np
from flask import Flask, jsonify, request, g
from flask_cors import CORS

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _HAS_LIMITER = True
except ImportError:
    _HAS_LIMITER = False

warnings.filterwarnings("ignore")

# ── LOGGING ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("irrigation-service")

app = Flask(__name__)

# ── CORS ───────────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins, e.g. "https://cropai.example.com,https://partner.example.com"
# Defaults to "*" (any origin) so the service works out-of-the-box for
# third-party API consumers; tighten this in production if you only expect
# browser-based callers from known domains (server-to-server callers aren't
# affected by CORS regardless of this setting).
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
CORS(app, resources={r"/*": {"origins": _allowed_origins.split(",") if _allowed_origins != "*" else "*"}})

# ── API KEY AUTH ───────────────────────────────────────────────────────────
_raw_keys = os.environ.get("IRRIGATION_API_KEYS", "").strip()
API_KEYS = {k.strip() for k in _raw_keys.split(",") if k.strip()}
AUTH_ENABLED = len(API_KEYS) > 0

if not AUTH_ENABLED:
    log.warning(
        "IRRIGATION_API_KEYS is not set — running WITHOUT API key auth. "
        "Set IRRIGATION_API_KEYS before exposing this service publicly."
    )


def _extract_api_key():
    key = request.headers.get("X-API-Key")
    if key:
        return key.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not AUTH_ENABLED:
            return fn(*args, **kwargs)
        key = _extract_api_key()
        if not key or key not in API_KEYS:
            return jsonify({
                "error": "unauthorized",
                "message": "Missing or invalid API key. Send it as 'X-API-Key' or 'Authorization: Bearer <key>'.",
            }), 401
        return fn(*args, **kwargs)
    return wrapper


# ── RATE LIMITING ──────────────────────────────────────────────────────────
if _HAS_LIMITER:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[os.environ.get("RATE_LIMIT_DEFAULT", "60 per minute")],
        storage_uri=os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://"),
    )
else:
    log.warning("flask-limiter not installed — rate limiting disabled. `pip install flask-limiter` to enable it.")

    class _NoopLimiter:
        def limit(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def exempt(self, fn):
            return fn

    limiter = _NoopLimiter()

# ── REQUEST LOGGING ────────────────────────────────────────────────────────
@app.before_request
def _start_timer():
    g._t0 = time.time()


@app.after_request
def _log_request(response):
    try:
        dt_ms = (time.time() - getattr(g, "_t0", time.time())) * 1000
        log.info("%s %s -> %s (%.1fms)", request.method, request.path, response.status_code, dt_ms)
    except Exception:
        pass
    return response


# ── STANDARDIZED ERROR HANDLERS ────────────────────────────────────────────
@app.errorhandler(404)
def _not_found(e):
    return jsonify({"error": "not_found", "message": "No such endpoint. See /api/v1/health for service info."}), 404


@app.errorhandler(405)
def _method_not_allowed(e):
    return jsonify({"error": "method_not_allowed", "message": str(e)}), 405


@app.errorhandler(429)
def _rate_limited(e):
    return jsonify({"error": "rate_limited", "message": "Too many requests. Please slow down and retry later."}), 429


@app.errorhandler(500)
def _server_error(e):
    log.exception("Unhandled server error")
    return jsonify({"error": "internal_error", "message": "Something went wrong processing your request."}), 500


# ── CONFIG ─────────────────────────────────────────────────────────────────

# ── CROP BACKEND (backend_2.py) STATE → PORT MAP ────────────────────────────
# backend_2.py runs one process per state, each loading that state's own
# model_artefacts.pkl, and exposes /valid_crops (or /api/crop/valid_crops)
# scoped to whichever state it was launched with:
#   python backend_2.py --state tripura   --port 5000
#   python backend_2.py --state meghalaya --port 5002
#   python backend_2.py --state rajasthan --port 5006
# This must match CROP_BACKENDS in main.py.
CROP_BACKEND_PORTS = {
    "tripura":   int(os.environ.get("CROP_BACKEND_PORT_TRIPURA", 5000)),
    "meghalaya": int(os.environ.get("CROP_BACKEND_PORT_MEGHALAYA", 5002)),
    "rajasthan": int(os.environ.get("CROP_BACKEND_PORT_RAJASTHAN", 5006)),
}
# Host for the crop-recommender backends. Defaults to localhost, since
# historically this service ran alongside them on the same machine — but as
# an independent microservice it may now run elsewhere, so this is
# overridable (e.g. CROP_BACKEND_HOST=crop-backend.internal).
CROP_BACKEND_HOST = os.environ.get("CROP_BACKEND_HOST", "127.0.0.1")

# ── STATE-AWARE DISTRICT COORDS ────────────────────────────────────────────────
# Add new states here as needed.
ALL_DISTRICT_COORDS = {
    "tripura": {
        "Dhalai":        (24.17, 92.03),
        "Gomati":        (23.45, 91.65),
        "Khowai":        (24.07, 91.60),
        "North Tripura": (24.45, 92.02),
        "Sepahijala":    (23.57, 91.30),
        "South Tripura": (23.23, 91.73),
        "Unakoti":       (24.32, 92.08),
        "West Tripura":  (23.84, 91.28),
    },
    "meghalaya": {
        "East Garo Hills":        (25.48, 90.61),
        "East Jaintia Hills":     (25.30, 92.38),
        "East Khasi Hills":       (25.57, 91.88),
        "Eastern West Khasi Hills":(25.35, 91.45),
        "North Garo Hills":       (26.05, 90.57),
        "Ri Bhoi":                (25.73, 91.97),
        "South Garo Hills":       (25.27, 90.40),
        "South West Garo Hills":  (25.52, 89.87),
        "South West Khasi Hills": (25.07, 91.28),
        "West Garo Hills":        (25.52, 90.22),
        "West Jaintia Hills":     (25.43, 92.12),
        "West Khasi Hills":       (25.47, 91.35),
    },
    "rajasthan": {
        "Ajmer":           (26.4499, 74.6399),
        "Alwar":           (27.5665, 76.6250),
        "Banswara":        (23.5461, 74.4432),
        "Baran":           (25.1000, 76.5133),
        "Barmer":          (25.7521, 71.3967),
        "Bharatpur":       (27.2173, 77.4901),
        "Bhilwara":        (25.3463, 74.6364),
        "Bikaner":         (28.0229, 73.3119),
        "Bundi":           (25.4305, 75.6499),
        "Chittorgarh":     (24.8887, 74.6269),
        "Churu":           (28.2969, 74.9647),
        "Dausa":           (26.8940, 76.3370),
        "Dholpur":         (26.7020, 77.8930),
        "Dungarpur":       (23.8430, 73.7140),
        "Hanumangarh":     (29.5822, 74.3297),
        "Jaipur":          (26.9124, 75.7873),
        "Jaisalmer":       (26.9157, 70.9083),
        "Jalore":          (25.3450, 72.6250),
        "Jhalawar":        (24.5980, 76.1630),
        "Jhunjhunu":       (28.1290, 75.3980),
        "Jodhpur":         (26.2389, 73.0243),
        "Karauli":         (26.4980, 77.0260),
        "Kota":            (25.2138, 75.8648),
        "Nagaur":          (27.2020, 73.7340),
        "Pali":            (25.7711, 73.3234),
        "Pratapgarh":      (24.0333, 74.7833),
        "Rajsamand":       (25.0700, 73.8800),
        "Sawai Madhopur":  (26.0173, 76.3430),
        "Sikar":           (27.6094, 75.1399),
        "Sirohi":          (24.8850, 72.8580),
        "Sri Ganganagar":  (29.9094, 73.8800),
        "Tonk":            (26.1660, 75.7900),
        "Udaipur":         (24.5854, 73.7125),
    },
}

def get_district_coords(state: str = "tripura") -> dict:
    return ALL_DISTRICT_COORDS.get(state.lower(), ALL_DISTRICT_COORDS["tripura"])


@app.route("/api/v1/districts", methods=["GET"])
@app.route("/districts", methods=["GET"])
@require_api_key
def districts():
    """
    State-aware district list for API consumers / frontend dropdowns.
    GET /api/v1/districts?state=rajasthan
    """
    state = request.args.get("state", "tripura").lower().strip()
    if state not in ALL_DISTRICT_COORDS:
        return jsonify({"error": "bad_request", "message": f"Unknown state: {state}"}), 400
    return jsonify(sorted(ALL_DISTRICT_COORDS[state].keys()))

# Crop water requirements (mm/day) by growth stage
# Source: FAO Irrigation and Drainage Paper 56 + regional adaptation
CROP_WATER_NEEDS = {
    "Rice": {
        "stages": ["Transplanting", "Vegetative", "Tillering", "Flowering", "Grain Filling", "Maturity"],
        "duration_days": [15, 25, 20, 15, 20, 15],
        "kc": [1.05, 1.10, 1.15, 1.20, 1.10, 0.75],   # crop coefficient
        "critical_moisture_pct": 75,  # % of field capacity — trigger irrigation below this
    },
    "Wheat": {
        "stages": ["Germination", "Tillering", "Stem Extension", "Heading", "Grain Filling", "Maturity"],
        "duration_days": [15, 25, 20, 10, 20, 20],
        "kc": [0.4, 0.7, 1.15, 1.15, 0.75, 0.4],
        "critical_moisture_pct": 60,
    },
    "Maize": {
        "stages": ["Germination", "Vegetative", "Tasseling", "Silking", "Grain Filling", "Maturity"],
        "duration_days": [10, 30, 10, 10, 25, 15],
        "kc": [0.4, 0.8, 1.15, 1.20, 1.05, 0.6],
        "critical_moisture_pct": 65,
    },
    "Groundnut": {
        "stages": ["Germination", "Vegetative", "Flowering", "Pegging", "Pod Development", "Maturity"],
        "duration_days": [10, 25, 20, 15, 25, 15],
        "kc": [0.45, 0.75, 1.05, 1.05, 0.85, 0.6],
        "critical_moisture_pct": 60,
    },
    "Sugarcane": {
        "stages": ["Germination", "Tillering", "Grand Growth", "Ripening"],
        "duration_days": [35, 60, 150, 60],
        "kc": [0.55, 0.80, 1.25, 0.75],
        "critical_moisture_pct": 70,
    },
    "Jute": {
        "stages": ["Germination", "Vegetative", "Rapid Growth", "Maturity"],
        "duration_days": [10, 30, 60, 20],
        "kc": [0.5, 0.8, 1.15, 0.8],
        "critical_moisture_pct": 65,
    },
    "Rapeseed &Mustard": {
        "stages": ["Germination", "Rosette", "Stem Extension", "Flowering", "Pod Fill", "Maturity"],
        "duration_days": [10, 20, 25, 20, 20, 15],
        "kc": [0.35, 0.7, 1.15, 1.15, 0.75, 0.4],
        "critical_moisture_pct": 55,
    },
    "Arhar/Tur": {
        "stages": ["Germination", "Vegetative", "Flowering", "Pod Development", "Maturity"],
        "duration_days": [10, 40, 30, 30, 20],
        "kc": [0.4, 0.8, 1.05, 0.95, 0.55],
        "critical_moisture_pct": 60,
    },
    "Moong(Green Gram)": {
        "stages": ["Germination", "Vegetative", "Flowering", "Pod Fill", "Maturity"],
        "duration_days": [8, 20, 15, 15, 12],
        "kc": [0.4, 0.7, 1.05, 0.90, 0.55],
        "critical_moisture_pct": 55,
    },
    "Urad": {
        "stages": ["Germination", "Vegetative", "Flowering", "Pod Fill", "Maturity"],
        "duration_days": [8, 20, 15, 15, 12],
        "kc": [0.4, 0.7, 1.05, 0.90, 0.55],
        "critical_moisture_pct": 55,
    },
}

# Soil water retention properties
SOIL_PROPERTIES = {
    "Red Laterite":  {"field_capacity": 0.28, "wilting_point": 0.14, "max_depth_mm": 120},
    "Alluvial":      {"field_capacity": 0.35, "wilting_point": 0.18, "max_depth_mm": 150},
    "Clay":          {"field_capacity": 0.40, "wilting_point": 0.22, "max_depth_mm": 160},
    "Loam":          {"field_capacity": 0.30, "wilting_point": 0.15, "max_depth_mm": 130},
    "Sandy Loam":    {"field_capacity": 0.22, "wilting_point": 0.10, "max_depth_mm": 100},
    "Sandy":         {"field_capacity": 0.15, "wilting_point": 0.08, "max_depth_mm": 80},
    "Black Cotton":  {"field_capacity": 0.42, "wilting_point": 0.24, "max_depth_mm": 170},
}

DEFAULT_SOIL = SOIL_PROPERTIES["Red Laterite"]  # most common in Tripura

IRRIGATION_METHODS = {
    "Flood":    {"efficiency": 0.55, "label": "Flood Irrigation"},
    "Furrow":   {"efficiency": 0.65, "label": "Furrow Irrigation"},
    "Sprinkler":{"efficiency": 0.80, "label": "Sprinkler"},
    "Drip":     {"efficiency": 0.92, "label": "Drip Irrigation"},
    "Rainfed":  {"efficiency": 1.00, "label": "Rainfed (No Irrigation)"},
}


# ── HELPERS ────────────────────────────────────────────────────────────────

def get_current_stage(crop: str, sowing_date: str):
    """Determine crop growth stage from sowing date."""
    if crop not in CROP_WATER_NEEDS:
        return "Vegetative", 0, 0.8

    info     = CROP_WATER_NEEDS[crop]
    stages   = info["stages"]
    durations= info["duration_days"]
    kcs      = info["kc"]

    try:
        sown  = datetime.date.fromisoformat(sowing_date)
        today = datetime.date.today()
        days_since_sowing = (today - sown).days
        if days_since_sowing < 0:
            days_since_sowing = 0
    except Exception:
        days_since_sowing = 30  # default mid-season

    cumulative = 0
    for i, (stage, dur, kc) in enumerate(zip(stages, durations, kcs)):
        cumulative += dur
        if days_since_sowing <= cumulative:
            days_in_stage   = days_since_sowing - (cumulative - dur)
            days_remaining  = dur - days_in_stage
            return stage, days_remaining, kc

    # Past final stage
    return stages[-1], 0, kcs[-1]


def fetch_weather_forecast(lat: float, lon: float, days: int = 10):
    """Fetch forecast weather + soil moisture from Open-Meteo."""
    today     = datetime.date.today()
    end_date  = today + datetime.timedelta(days=days - 1)

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":   lat,
        "longitude":  lon,
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "et0_fao_evapotranspiration",
            "shortwave_radiation_sum",
            "windspeed_10m_max",
            "precipitation_probability_max",
        ]),
        "hourly": "soil_moisture_0_to_1cm",
        "start_date": str(today),
        "end_date":   str(end_date),
        "timezone":   "Asia/Kolkata",
    }

    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    daily  = data.get("daily", {})
    hourly = data.get("hourly", {})

    # Aggregate hourly soil moisture to daily (noon value)
    sm_hourly = hourly.get("soil_moisture_0_to_1cm", [])
    daily_sm  = []
    for d in range(days):
        noon_idx = d * 24 + 12
        val = sm_hourly[noon_idx] if noon_idx < len(sm_hourly) and sm_hourly[noon_idx] is not None else None
        daily_sm.append(val)

    dates = daily.get("time", [])
    result = []
    for i in range(min(days, len(dates))):
        def g(key, default=0.0):
            lst = daily.get(key, [])
            v   = lst[i] if i < len(lst) else None
            return float(v) if v is not None else default

        result.append({
            "date":          dates[i],
            "temp_max":      g("temperature_2m_max"),
            "temp_min":      g("temperature_2m_min"),
            "temp_mean":     round((g("temperature_2m_max") + g("temperature_2m_min")) / 2, 1),
            "rainfall":      g("precipitation_sum"),
            "et0":           g("et0_fao_evapotranspiration"),
            "solar":         g("shortwave_radiation_sum"),
            "wind":          g("windspeed_10m_max"),
            "rain_prob":     g("precipitation_probability_max"),
            "soil_moisture": daily_sm[i],
        })

    return result


# Soil feel → approximate % of field capacity
# Based on standard FAO field texture/feel guide adapted for Tripura soils
SOIL_FEEL_TO_MOISTURE = {
    "dry":        25,   # Dry, loose, crumbly — flows through fingers
    "slightly":   50,   # Slightly moist — forms weak ball, crumbles easily
    "moist":      75,   # Moist — forms firm ball, stains hand
    "wet":        95,   # Wet — free water visible, waterlogged
}


def estimate_moisture_from_inputs(soil_feel, forecast_days, soil_type):
    """
    Estimate current soil moisture % of field capacity from the farmer's
    tactile assessment only, with a gentle Open-Meteo sanity nudge.

    The feel answer is treated as ground truth — it describes the soil RIGHT NOW.
    This function itself does not take a rain date (see rain_recency_adjustment,
    applied by the caller) — that keeps the feel-based anchor and the
    rain-recency nudge as separate, independently testable adjustments.

    Returns estimated moisture % (0-100).
    """
    soil = SOIL_PROPERTIES.get(soil_type, DEFAULT_SOIL)
    fc   = soil["field_capacity"]
    wp   = soil["wilting_point"]

    # 1. Feel-based anchor — this is the primary signal
    feel_pct = SOIL_FEEL_TO_MOISTURE.get(soil_feel, 60)

    # 2. Open-Meteo 0-1cm as lightweight sanity nudge (±10% max, never overrides feel)
    api_nudge = 0
    first_sm = next((d["soil_moisture"] for d in forecast_days
                     if d["soil_moisture"] is not None), None)
    if first_sm is not None:
        api_pct = min(100, max(0, (first_sm - wp) / (fc - wp) * 100)) if (fc - wp) > 0 else 50
        diff = api_pct - feel_pct
        api_nudge = max(-10, min(10, diff * 0.3))

    estimated = feel_pct + api_nudge
    return round(max(5, min(100, estimated)), 1)


def rain_recency_adjustment(last_rain_date, reference_date=None):
    """
    Convert 'days since last significant rainfall' into a starting-moisture
    nudge (percentage points of field capacity).

    This is what actually makes the 'Last significant rainfall' date field
    do something — previously it was accepted as a parameter but never read.

    Recent rain (0-2 days ago) → soil likely retains extra moisture beyond
    what the feel test alone implies, since near-surface feel can lag behind
    a recent deep soak. Old or unknown rain → no adjustment (feel test and
    the forward ET/rainfall balance already account for depletion since then).

    Capped at ±8 points so it nudges, but never overrides, the farmer's feel
    reading (which stays the primary signal).
    """
    if not last_rain_date:
        return 0.0

    ref = reference_date or datetime.date.today()
    try:
        rain_d = datetime.date.fromisoformat(last_rain_date)
    except Exception:
        return 0.0

    days_since = (ref - rain_d).days
    if days_since < 0:
        # Rain date in the future — can't use it, ignore
        return 0.0

    if days_since <= 1:
        return 8.0
    elif days_since <= 3:
        return 5.0
    elif days_since <= 7:
        return 2.0
    else:
        return 0.0


def get_kc_for_day(crop: str, sowing_date: str, day_offset: int) -> float:
    """
    Return the correct FAO-56 crop coefficient (Kc) for a specific forecast day.
    day_offset=0 means today, day_offset=1 means tomorrow, etc.
    Walks the stage duration table so stage transitions mid-forecast are handled.
    """
    if crop not in CROP_WATER_NEEDS:
        return 0.9  # safe default

    info      = CROP_WATER_NEEDS[crop]
    stages    = info["stages"]
    durations = info["duration_days"]
    kcs       = info["kc"]

    try:
        sown = datetime.date.fromisoformat(sowing_date)
        target_day = max(0, (datetime.date.today() - sown).days + day_offset)
    except Exception:
        target_day = 30

    cumulative = 0
    for dur, kc in zip(durations, kcs):
        cumulative += dur
        if target_day <= cumulative:
            return kc

    return kcs[-1]  # past final stage


def simulate_soil_moisture(forecast_days, crop, soil_type, soil_feel=None,
                            last_rain_date=None, sowing_date=None):
    """
    Simulate baseline (no-irrigation) soil moisture trajectory using FAO-56
    water balance. Used for transparency/reference values only.

    Starting moisture comes from the farmer's feel test (ground truth), lightly
    nudged by the Open-Meteo 0-1cm reading.

    NOTE: This does NOT feed irrigation water back into the balance. The
    actual trajectory driving recommendations is computed in
    build_irrigation_schedule(), which applies irrigation on days it's
    recommended so moisture recovers instead of staying pinned at the
    wilting point for the rest of the forecast.
    """
    soil  = SOIL_PROPERTIES.get(soil_type, DEFAULT_SOIL)
    fc    = soil["field_capacity"]
    wp    = soil["wilting_point"]
    depth = soil["max_depth_mm"]
    taw   = (fc - wp) * depth

    # Starting moisture: feel is ground truth, API nudges gently,
    # and recent rainfall (from last_rain_date) nudges further.
    current_moisture_pct = estimate_moisture_from_inputs(
        soil_feel or "slightly", forecast_days, soil_type
    )
    current_moisture_pct += rain_recency_adjustment(last_rain_date)
    current_moisture_pct = max(5, min(100, current_moisture_pct))
    current_sw = wp * depth + (current_moisture_pct / 100) * taw

    moisture_timeline = []
    sw = current_sw

    for i, day in enumerate(forecast_days):
        kc = get_kc_for_day(crop, sowing_date or "", i)
        etc_mm = day["et0"] * kc
        eff_rain = min(day["rainfall"], 50.0)

        sw += eff_rain - etc_mm
        sw  = max(wp * depth, min(fc * depth, sw))

        moisture_pct = (sw - wp * depth) / taw * 100 if taw > 0 else 50
        moisture_pct = max(0, min(100, moisture_pct))

        moisture_timeline.append({
            "date":         day["date"],
            "moisture_pct": round(moisture_pct, 1),
            "sw_mm":        round(sw, 1),
            "etc_mm":       round(etc_mm, 2),
            "kc_used":      round(kc, 2),
            "rainfall":     day["rainfall"],
        })

    return moisture_timeline, taw


def build_irrigation_schedule(forecast_days, moisture_timeline, crop, soil_type,
                               irrigation_method, sowing_date):
    """
    Determine day-by-day irrigation recommendations.

    Re-runs the water balance itself (rather than trusting the no-irrigation
    moisture_timeline) so that when a day's recommendation is to irrigate,
    that water is actually added back into the soil before the next day's
    balance is computed. Without this, once soil water hits the wilting
    point it stays clamped there for every subsequent day regardless of
    date/weather — which made the schedule look frozen ("irrigate now, 0%
    moisture") for the rest of the week.
    """
    crop_info  = CROP_WATER_NEEDS.get(crop, CROP_WATER_NEEDS["Rice"])
    threshold  = crop_info["critical_moisture_pct"]
    soil       = SOIL_PROPERTIES.get(soil_type, DEFAULT_SOIL)
    fc         = soil["field_capacity"]
    wp         = soil["wilting_point"]
    depth      = soil["max_depth_mm"]
    taw        = (fc - wp) * depth
    method_eff = IRRIGATION_METHODS.get(irrigation_method, IRRIGATION_METHODS["Flood"])["efficiency"]

    stage, days_to_next_stage, kc = get_current_stage(crop, sowing_date)

    # Reconstruct the same starting soil water used by simulate_soil_moisture
    # (reverse day 0's balance step to recover the pre-day-0 value).
    if moisture_timeline:
        first = moisture_timeline[0]
        sw = first["sw_mm"] - first["rainfall"] + first["etc_mm"]
        sw = max(wp * depth, min(fc * depth, sw))
    else:
        sw = wp * depth

    schedule = []
    for i, day in enumerate(forecast_days):
        kc_i       = get_kc_for_day(crop, sowing_date or "", i)
        etc_mm     = day["et0"] * kc_i
        rain_today = day["rainfall"]
        rain_prob  = day["rain_prob"]
        eff_rain   = min(rain_today, 50.0)

        # Natural water balance for today (ET loss, rainfall gain)
        sw += eff_rain - etc_mm
        sw  = max(wp * depth, min(fc * depth, sw))

        m_pct = (sw - wp * depth) / taw * 100 if taw > 0 else 50
        m_pct = max(0, min(100, m_pct))

        # How much water to bring soil back to 90% FC
        target_sw  = (wp + (fc - wp) * 0.90) * depth
        deficit_mm = max(0, target_sw - sw)
        gross_mm   = deficit_mm / method_eff if method_eff > 0 else deficit_mm

        irrigated_today = False

        if irrigation_method == "Rainfed":
            action   = "monitor"
            advice   = "Rainfed — monitor soil moisture"
            urgency  = "info"
            irr_mm   = 0

        elif m_pct <= (threshold - 15):
            action  = "irrigate_now"
            advice  = f"Irrigate immediately — moisture critically low ({m_pct:.0f}%)"
            urgency = "critical"
            irr_mm  = round(gross_mm, 1)
            irrigated_today = True

        elif m_pct <= threshold and rain_prob < 40:
            action  = "irrigate_now"
            advice  = f"Irrigate today — below threshold, low rain probability ({rain_prob:.0f}%)"
            urgency = "warning"
            irr_mm  = round(gross_mm, 1)
            irrigated_today = True

        elif m_pct <= threshold and rain_prob >= 40:
            action  = "delay"
            advice  = f"Delay irrigation — rain expected ({rain_prob:.0f}% probability)"
            urgency = "caution"
            irr_mm  = 0

        elif rain_today > 15:
            action  = "skip"
            advice  = f"Skip irrigation — significant rainfall today ({rain_today:.0f} mm)"
            urgency = "good"
            irr_mm  = 0

        elif m_pct > 80:
            action  = "skip"
            advice  = f"Soil moisture adequate ({m_pct:.0f}%) — no irrigation needed"
            urgency = "good"
            irr_mm  = 0

        else:
            action  = "monitor"
            advice  = f"Monitor — moisture OK ({m_pct:.0f}%), check again tomorrow"
            urgency = "info"
            irr_mm  = 0

        # Today's reported moisture % is the pre-irrigation state — this is
        # the number that justifies today's advice text, so it must match
        # what's shown for "moisture_pct" on this day.
        reported_pct = round(m_pct, 1)

        # Feed applied irrigation (net of efficiency losses) into the
        # balance so TOMORROW starts from recovered moisture, not the floor.
        # This must not change today's own reported percentage above.
        if irrigated_today and irr_mm > 0:
            net_applied_mm = irr_mm * method_eff
            sw = min(fc * depth, sw + net_applied_mm)

        schedule.append({
            "date":           day["date"],
            "day_label":      _day_label(day["date"]),
            "action":         action,
            "advice":         advice,
            "urgency":        urgency,
            "irrigation_mm":  irr_mm,
            "moisture_pct":   reported_pct,
            "rainfall_mm":    round(rain_today, 1),
            "rain_prob_pct":  int(rain_prob),
            "et0_mm":         round(day["et0"], 2),
            "temp_mean":      day["temp_mean"],
            "crop_stage":     stage,
        })

    return schedule


def _day_label(date_str: str) -> str:
    try:
        d    = datetime.date.fromisoformat(date_str)
        today = datetime.date.today()
        delta = (d - today).days
        if delta == 0:   return "Today"
        if delta == 1:   return "Tomorrow"
        return d.strftime("%a, %d %b")
    except Exception:
        return date_str


# ── ROUTES ─────────────────────────────────────────────────────────────────

@app.route("/api/v1/health", methods=["GET"])
@app.route("/health", methods=["GET"])
@limiter.exempt
def health():
    """Unauthenticated health check — used by load balancers / orchestrators."""
    return jsonify({
        "status": "ok",
        "service": "irrigation-advisor",
        "version": "1.0.0",
        "auth_enabled": AUTH_ENABLED,
        "crops": list(CROP_WATER_NEEDS.keys()),
    })


@app.route("/", methods=["GET"])
def index():
    """Root info page so a curious caller (or health probe) hitting '/' gets something useful."""
    return jsonify({
        "service": "AI Irrigation Advisory Microservice",
        "docs": "See README.md for full usage.",
        "endpoints": {
            "GET  /api/v1/health": "Health check (no auth)",
            "GET  /api/v1/crops": "List supported crops (?state=optional)",
            "GET  /api/v1/districts": "List districts (?state=required)",
            "POST /api/v1/advise": "Get an irrigation schedule",
        },
        "auth": "Send API key via 'X-API-Key' header or 'Authorization: Bearer <key>'." if AUTH_ENABLED else "No auth required (IRRIGATION_API_KEYS not set).",
    })


def _fetch_valid_crops_for_state(state: str):
    """
    Ask backend_2.py's instance for this state which crops its model was
    actually trained on. Returns a list of crop names, or None if the
    crop backend for this state is unreachable/unknown (caller should
    fall back to serving every crop we have water-need data for).
    """
    port = CROP_BACKEND_PORTS.get(state)
    if not port:
        return None
    for path in ("/api/crop/valid_crops", "/valid_crops"):
        try:
            resp = requests.get(f"http://{CROP_BACKEND_HOST}:{port}{path}", timeout=3)
            if not resp.ok:
                continue
            data = resp.json()
            crops = data.get("valid_crops")
            if crops:
                return crops
        except Exception:
            continue
    return None


@app.route("/api/v1/crops", methods=["GET"])
@app.route("/crops", methods=["GET"])
@require_api_key
def crops():
    """
    State-aware crop list for the frontend dropdown.
    GET /crops?state=rajasthan

    Only returns crops that are BOTH:
      1. valid for the requested state's yield model (backend_2.py), and
      2. have FAO-56 water-need data here, since only those can actually
         produce an irrigation schedule via /advise.

    If backend_2.py isn't reachable for this state (not running yet, wrong
    port, etc.), falls back to every crop this service has water-need data
    for, so the dropdown still works standalone.
    """
    state = request.args.get("state", "").lower().strip()
    valid_for_state = _fetch_valid_crops_for_state(state) if state else None

    if valid_for_state:
        # Match case/whitespace-insensitively against our own crop keys so
        # small naming differences between the two backends don't silently
        # drop a crop from the dropdown.
        by_norm = {c.strip().lower(): c for c in CROP_WATER_NEEDS.keys()}
        crop_names = []
        for c in valid_for_state:
            match = by_norm.get(str(c).strip().lower())
            if match and match not in crop_names:
                crop_names.append(match)
        # If none of the state's valid crops overlap our water-need table,
        # fall back rather than returning an empty dropdown.
        if not crop_names:
            crop_names = list(CROP_WATER_NEEDS.keys())
    else:
        crop_names = list(CROP_WATER_NEEDS.keys())

    out = []
    for crop in crop_names:
        info = CROP_WATER_NEEDS[crop]
        out.append({
            "name":   crop,
            "stages": info["stages"],
            "total_duration": sum(info["duration_days"]),
        })
    return jsonify(out)


@app.route("/api/v1/advise", methods=["POST"])
@app.route("/advise", methods=["POST"])
@require_api_key
@limiter.limit(os.environ.get("RATE_LIMIT_ADVISE", "20 per minute"))
def advise():
    """
    Main endpoint. Expects JSON:
    {
      "district":          "Dhalai",
      "crop":              "Rice",
      "sowing_date":       "2025-06-15",
      "soil_type":         "Red Laterite",
      "irrigation_method": "Flood",
      "area_ha":           5.0,
      "soil_feel":         "slightly",   // 'dry' | 'slightly' | 'moist' | 'wet'
      "last_rain_date":    "2025-06-10"  // ISO date of last significant rainfall
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "bad_request", "message": "Expected a JSON body."}), 400

    state           = data.get("state", "tripura").lower().strip()
    district        = data.get("district")
    crop            = data.get("crop", "Rice")
    sowing          = data.get("sowing_date", str(datetime.date.today() - datetime.timedelta(days=30)))
    soil_type       = data.get("soil_type", "Red Laterite")
    irr_method      = data.get("irrigation_method", "Flood")
    area_ha         = float(data.get("area_ha", 1.0))
    soil_feel       = data.get("soil_feel", "slightly")
    last_rain_date  = data.get("last_rain_date", None)

    if state not in ALL_DISTRICT_COORDS:
        return jsonify({"error": "bad_request", "message": f"Unknown state: {state}"}), 400

    if not district:
        return jsonify({"error": "bad_request", "message": "district is required"}), 400

    district_coords = get_district_coords(state)
    coords = district_coords.get(district)
    if not coords:
        return jsonify({"error": "bad_request", "message": f"Unknown district: {district} for state: {state}"}), 400

    try:
        forecast = fetch_weather_forecast(coords[0], coords[1], days=10)
    except Exception as e:
        log.warning("Weather fetch failed: %s", e)
        return jsonify({"error": "upstream_error", "message": f"Weather fetch failed: {str(e)}"}), 502

    moisture_timeline, taw = simulate_soil_moisture(forecast, crop, soil_type,
                                                      soil_feel, last_rain_date, sowing)
    schedule = build_irrigation_schedule(forecast, moisture_timeline, crop, soil_type,
                                          irr_method, sowing)

    # Summary stats
    total_irr_mm  = sum(s["irrigation_mm"] for s in schedule)
    total_irr_m3  = round(total_irr_mm / 1000 * area_ha * 10000, 1)  # mm→m³ for given area
    irr_events    = sum(1 for s in schedule if s["action"] == "irrigate_now")
    total_rain    = round(sum(d["rainfall"] for d in forecast[:7]), 1)
    avg_et0       = round(np.mean([d["et0"] for d in forecast[:7]]), 2)

    stage, days_to_next, kc = get_current_stage(crop, sowing)
    try:
        sown_d        = datetime.date.fromisoformat(sowing)
        days_in_field = (datetime.date.today() - sown_d).days
    except Exception:
        days_in_field = 0

    soil_props = SOIL_PROPERTIES.get(soil_type, DEFAULT_SOIL)

    # Compute estimated starting moisture for transparency
    estimated_moisture_pct = estimate_moisture_from_inputs(
        soil_feel or "slightly", forecast, soil_type
    )

    return jsonify({
        "district":               district,
        "crop":                   crop,
        "sowing_date":            sowing,
        "soil_type":              soil_type,
        "irrigation_method":      irr_method,
        "area_ha":                area_ha,
        "soil_feel":              soil_feel,
        "last_rain_date":         last_rain_date,
        "estimated_moisture_pct": estimated_moisture_pct,
        "current_stage":          stage,
        "days_in_field":          days_in_field,
        "days_to_next_stage":     days_to_next,
        "crop_kc":                round(kc, 2),
        "field_capacity_pct":     round(soil_props["field_capacity"] * 100, 1),
        "taw_mm":                 round(taw, 1),
        "summary": {
            "total_irrigation_mm":  round(total_irr_mm, 1),
            "total_irrigation_m3":  total_irr_m3,
            "irrigation_events_7d": irr_events,
            "total_rainfall_7d_mm": total_rain,
            "avg_et0_mm_day":       avg_et0,
            "next_action":          schedule[0]["action"] if schedule else "monitor",
            "next_advice":          schedule[0]["advice"] if schedule else "",
            "next_urgency":         schedule[0]["urgency"] if schedule else "info",
        },
        "schedule":          schedule,
        "weather_forecast":  forecast,
    })


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("DEBUG", "false").lower() == "true"

    print("=" * 60)
    print("  AI IRRIGATION ADVISORY — Independent Microservice")
    print(f"  Listening on http://{host}:{port}")
    print(f"  API key auth: {'ENABLED' if AUTH_ENABLED else 'DISABLED (set IRRIGATION_API_KEYS)'}")
    print(f"  Rate limiting: {'enabled' if _HAS_LIMITER else 'disabled (pip install flask-limiter)'}")
    print("  NOTE: for production, run via gunicorn instead of this dev server:")
    print(f"    gunicorn -w 4 -b {host}:{port} service:app")
    print("=" * 60)
    app.run(host=host, port=port, debug=debug)