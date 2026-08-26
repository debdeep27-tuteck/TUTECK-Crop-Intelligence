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
import time
from typing import Optional
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

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

# Simple in-memory cache. The government dataset only refreshes once a
# day, so there's no reason to hit it on every search — this keeps
# response times fast and stays well under data.gov.in's rate limits.
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_cache: dict[str, tuple[float, object]] = {}


def cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value: object) -> None:
    _cache[key] = (time.time(), value)


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

    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
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
        return jsonify({"error": "Failed to reach data.gov.in", "details": str(e)}), 502

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
    print(f"  MANDI PRICES BACKEND — Running on http://127.0.0.1:{PORT}")
    print("=" * 55)
    app.run(host="127.0.0.1", port=PORT, debug=False)