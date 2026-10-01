"""
mandi_prices_backend.py

Standalone microservice that serves daily mandi (market) commodity prices
for farmers on the Crop Analytics site.

Data source: data.gov.in — "Current Daily Price of Various Commodities
from Various Markets (Mandi)" (Ministry of Agriculture and Farmers
Welfare). Resource id: 9ef84268-d588-465a-a308-a864a43d0070

This is the SAME government dataset commercial sites like commodityonline
and Agriwatch build their own price pages on top of — going straight to
the source here means no scraping, no ToS risk, and no breakage when a
third-party site changes its HTML.

Follows the same conventions as auction_backend.py / cold_storage_backend.py:
  • runs as its own process on its own port
  • mounts its own routes under /api/mandi-prices/...
  • gateway.py just proxies to it (see forward_request in gateway.py)

NOTE on districts/commodities: this service does NOT derive its own
district/commodity lists from data.gov.in. The dropdowns on the frontend
are populated straight from the site's existing crop backend
(backend_2.py's /api/crop/valid_districts and /api/crop/valid_crops,
already proxied by gateway.py) — that's the same district/crop list
every other tab on the dashboard already uses, so it stays consistent
site-wide and needs no extra network hop through this service. This
backend's only job is the actual price lookup once the farmer searches.

Routes exposed:
  GET  /health
  GET  /api/mandi-prices/states
  GET  /api/mandi-prices/prices?state=Rajasthan&district=Jaipur&commodity=Wheat&limit=100&offset=0
       (state required; district and commodity are optional filters)

Env vars:
  DATA_GOV_API_KEY   Your data.gov.in API key (required — get one free at
                      data.gov.in after registering, on the dataset's API
                      page). Falls back to the public demo key baked into
                      the dataset ("579b464db66ec23bdd0000...") which is
                      heavily rate-limited — replace it for production use.
  MANDI_PRICES_PORT  Port to listen on (default 5011)
"""

from __future__ import annotations

import os
import json
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from shared.db import get_db, close_db

app = Flask(__name__)
CORS(app)

# ── CONFIG ──────────────────────────────────────────────────────────────

RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
DATA_GOV_BASE_URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"

# data.gov.in silently drops (never responds — hangs until client timeout)
# requests carrying Python's default "python-requests/x.x" User-Agent.
# A browser-style UA gets served normally, so every outbound call to this
# API must include one — see fetch_records() below.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Replace via env var in production — this is the shared public demo key
# shown on the dataset's API page and is rate-limited.
DATA_GOV_API_KEY = os.environ.get(
    "DATA_GOV_API_KEY",
    "579b464db66ec23bdd000001334da536d6b6428c545587ef32f8e086",
)

PORT = int(os.environ.get("MANDI_PRICES_PORT", 5011))

# The three states this Crop Analytics site currently serves. Kept in
# sync with CROP_BACKENDS in main.py / gateway.py. The government dataset
# uses its own state-name spellings (e.g. "Keralam" not "Kerala" — seen
# in the raw sample), so this is a display-name -> dataset-name map. Add
# entries here as more states come online.
SUPPORTED_STATES = {
    "tripura": "Tripura",
    "meghalaya": "Meghalaya",
    "rajasthan": "Rajasthan",
}

# Persistent database cache for successful responses. The source refreshes
# daily, so stored results also reduce repeat calls to data.gov.in.
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
PRICE_CACHE_TABLE = "mandi_price_cache"


def cache_get(key: str, *, allow_stale: bool = False):
    row = get_db().execute(
        f"SELECT payload, fetched_at FROM {PRICE_CACHE_TABLE} WHERE cache_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    ts = int(row["fetched_at"])
    age = time.time() - ts
    if age > CACHE_TTL_SECONDS and not allow_stale:
        return None
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return (ts, value) if allow_stale else value


def cache_set(key: str, value: object) -> None:
    db = get_db()
    db.execute(
        f"""INSERT INTO {PRICE_CACHE_TABLE} (cache_key, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT (cache_key) DO UPDATE SET
                payload = EXCLUDED.payload,
                fetched_at = EXCLUDED.fetched_at""",
        (key, json.dumps(value, ensure_ascii=False), int(time.time())),
    )
    db.commit()


def init_db() -> None:
    db = get_db()
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS {PRICE_CACHE_TABLE} (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at BIGINT NOT NULL
        )"""
    )
    db.commit()


@app.teardown_appcontext
def _close_db(_exc):
    close_db(_exc)


# ── DATA.GOV.IN CLIENT ─────────────────────────────────────────────────

def fetch_records(
    state: Optional[str] = None,
    district: Optional[str] = None,
    commodity: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """
    Calls the data.gov.in resource endpoint with the filters[...] query
    param format shown on the dataset's own API docs page, e.g.:
      ?api-key=...&format=json&filters[state.keyword]=Rajasthan
       &filters[district]=Jaipur&filters[commodity]=Wheat&limit=100

    IMPORTANT: this API expects the "filters[state.keyword]" style keys
    LITERALLY in the query string (unencoded brackets), the way curl or a
    browser address bar sends them. requests' params=dict percent-encodes
    "[" and "]" to %5B/%5D, which this API doesn't handle the same way —
    in practice that causes the request to hang until it times out rather
    than erroring cleanly. So the URL is built manually here: keys are
    left as-is, only the values are percent-encoded.
    """
    params = {
        "api-key": DATA_GOV_API_KEY,
        "format": "json",
        "limit": limit,
        "offset": offset,
    }
    if state:
        params["filters[state.keyword]"] = state
    if district:
        params["filters[district]"] = district
    if commodity:
        params["filters[commodity]"] = commodity

    query_string = "&".join(
        f"{key}={quote(str(value), safe='')}" for key, value in params.items()
    )
    url = f"{DATA_GOV_BASE_URL}?{query_string}"

    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=(4, 12))
    resp.raise_for_status()
    return resp.json()


def normalize_record(r: dict) -> dict:
    """Reshape a raw data.gov.in record into what the frontend table wants."""
    def to_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "state": r.get("state", ""),
        "district": r.get("district", ""),
        "market": r.get("market", ""),
        "commodity": r.get("commodity", ""),
        "variety": r.get("variety", ""),
        "grade": r.get("grade", ""),
        "arrival_date": r.get("arrival_date", ""),
        "min_price": to_num(r.get("min_price")),
        "max_price": to_num(r.get("max_price")),
        "modal_price": to_num(r.get("modal_price")),
    }


# ── ROUTES ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "mandi-prices-backend",
        "source": "data.gov.in (Agmarknet)",
        "resource_id": RESOURCE_ID,
        "supported_states": list(SUPPORTED_STATES.keys()),
    })


@app.route("/api/mandi-prices/states")
def list_states():
    """States this site currently supports (matches the crop dashboard's own states)."""
    return jsonify({
        "states": [
            {"id": key, "name": name} for key, name in SUPPORTED_STATES.items()
        ]
    })


@app.route("/api/mandi-prices/prices")
def get_prices():
    """
    Main table endpoint. A farmer must pick a state; district and
    commodity are optional filters (both come from the site's existing
    /api/crop/valid_districts and /api/crop/valid_crops dropdowns on the
    frontend, not from this service).

      GET /api/mandi-prices/prices?state=rajasthan                     (state only)
      GET /api/mandi-prices/prices?state=rajasthan&district=Jaipur     (+ district)
      GET /api/mandi-prices/prices?state=rajasthan&commodity=Wheat     (+ commodity)
    """
    state_id = (request.args.get("state") or "").lower().strip()
    district = (request.args.get("district") or "").strip()
    commodity = (request.args.get("commodity") or "").strip()

    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    except ValueError:
        limit = 100
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0

    if not state_id:
        return jsonify({"error": "state is required",
                         "supported_states": list(SUPPORTED_STATES.keys())}), 400

    dataset_state = SUPPORTED_STATES.get(state_id)
    if not dataset_state:
        return jsonify({"error": f"Unsupported state '{state_id}'",
                         "supported_states": list(SUPPORTED_STATES.keys())}), 400

    cache_key = f"prices:{state_id}:{district}:{commodity}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        data = fetch_records(
            state=dataset_state,
            district=district or None,
            commodity=commodity or None,
            limit=limit,
            offset=offset,
        )
    except requests.RequestException as e:
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None:
            cached_at, payload = stale
            response = dict(payload)
            response["stale"] = True
            response["cache_age_seconds"] = max(0, int(time.time() - cached_at))
            response["warning"] = "Live mandi data is temporarily unavailable; showing the most recently fetched prices."
            return jsonify(response)
        return jsonify({
            "error": "Live mandi prices are temporarily unavailable. Please try again shortly.",
            # requests' exception includes the full URL, including api-key.
            # Return only safe diagnostics to clients; never expose the key.
            "details": (
                f"data.gov.in returned HTTP {e.response.status_code}."
                if isinstance(e, requests.HTTPError) and e.response is not None
                else "Could not connect to data.gov.in or the request timed out."
            ),
        }), 502

    records = [normalize_record(r) for r in data.get("records", [])]
    payload = {
        "state": state_id,
        "district": district or None,
        "commodity": commodity or None,
        "total": data.get("total"),
        "count": len(records),
        "limit": limit,
        "offset": offset,
        "updated": data.get("updated_date"),
        "records": records,
    }
    cache_set(cache_key, payload)
    return jsonify(payload)


# ── MAIN ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print(f"  MANDI PRICES BACKEND — Running on http://0.0.0.0:{PORT}")
    print("=" * 55)
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=PORT, debug=False)
