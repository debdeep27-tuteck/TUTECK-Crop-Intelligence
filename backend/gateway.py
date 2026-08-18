from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import requests
from pathlib import Path

app = Flask(__name__)
CORS(app)

# ── CONFIG ─────────────────────────────────────────────────────────────

# Tripura backends
CROP_API_TRIPURA = "http://127.0.0.1:5000"
IRR_API_TRIPURA  = "http://127.0.0.1:5001"

# Meghalaya backends
CROP_API_MEGHALAYA = "http://127.0.0.1:5002"
IRR_API_MEGHALAYA  = "http://127.0.0.1:5001"

# Rajasthan backends

CROP_API_RAJASTHAN = "http://127.0.0.1:5006"
IRR_API_RAJASTHAN  = "http://127.0.0.1:5001"

# Disease detection — single instance serves both states
DISEASE_API = "http://127.0.0.1:5004"

# Yield Detect — geofenced land yield predictions (SQLite-backed)
YIELD_API = "http://127.0.0.1:5008"

# Auction — farmer crop listings / bidding (SQLite-backed, unused_crops table)
AUCTION_API = "http://127.0.0.1:5009"

# Cold Storage — geofenced cold storage listings (SQLite-backed)
COLD_STORAGE_API = "http://127.0.0.1:5010"


# ── HELPERS ────────────────────────────────────────────────────────────

def _json_body():
    return request.get_json(silent=True) or {}


def get_state(default="tripura"):
    state = request.args.get("state") or _json_body().get("state", default)
    return str(state).lower().strip() if state else default


CROP_APIS = {
    "tripura": CROP_API_TRIPURA,
    "meghalaya": CROP_API_MEGHALAYA,
    "rajasthan": CROP_API_RAJASTHAN,
}

IRR_APIS = {
    "tripura": IRR_API_TRIPURA,
    "meghalaya": IRR_API_MEGHALAYA,
    "rajasthan": IRR_API_RAJASTHAN,
}

def get_crop_api():
    state = get_state()
    return CROP_APIS.get(state, CROP_API_TRIPURA)

def get_irr_api():
    state = get_state()
    return IRR_APIS.get(state, IRR_API_TRIPURA)


# ── FRONTEND FOLDERS ───────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = (BASE_DIR.parent / "frontend").resolve()
HTML_DIR = FRONTEND_DIR / "html"
CSS_DIR  = FRONTEND_DIR / "css"
JS_DIR   = FRONTEND_DIR / "js"
STATIC_DIR = FRONTEND_DIR / "static"

# State-specific data folders
DATA_DIR = {
    "tripura":   (BASE_DIR.parent / "data_and_model").resolve(),
    "meghalaya": (BASE_DIR.parent / "data_and_model_meghalaya").resolve(),
"rajasthan": (BASE_DIR.parent / "data_and_model_rajasthan").resolve(),
}


# ── AUTH (SIGN UP / SIGN IN / ADMIN CRUD) ─────────────────────────────────
#
# All login credentials + roles live in users.xlsx, managed entirely by
# auth_excel.py. That module also exposes /api/auth/* and /api/users/*
# routes as a Blueprint, so we just register it here.

from auth_excel import auth_bp, init_excel, get_current_session

init_excel()                    # creates users.xlsx next to this file if missing
app.register_blueprint(auth_bp) # mounts /api/auth/... and /api/users/...


# ── FRONTEND PAGE ROUTES ────────────────────────────────────────────────

@app.route("/login")
def login_page():
    return send_from_directory(str(HTML_DIR), "login.html")
@app.route("/admin")
def admin_page():
    return send_from_directory(str(HTML_DIR), "admin.html")


@app.route("/")
@app.route("/dashboard")
@app.route("/irrigation")
@app.route("/recommender")
@app.route("/recommend-page")
@app.route("/alerts")
@app.route("/disease")
@app.route("/yield-detect")
@app.route("/auction")
@app.route("/cold-storage")
def home():
    return send_from_directory(str(HTML_DIR), "index.html")


# Yield Detect's "Add Land" / "Edit Land" screen is a full standalone page
# (not wrapped in the dashboard shell/iframe) so the Google Map has room to
# breathe. It's served directly here, the same way /login and /admin are.
@app.route("/yield-detect-editor")
def yield_detect_editor_page():
    return send_from_directory(str(HTML_DIR), "yield_detect_editor.html")


# ── INTERNAL CONTENT ROUTES ─────────────────────────────────────────────

@app.route("/content/dashboard")
def content_dashboard():
    return send_from_directory(str(HTML_DIR), "crop_dashboard.html")


@app.route("/content/recommender")
def content_recommender():
    return send_from_directory(str(HTML_DIR), "crop_recommender.html")


@app.route("/content/irrigation")
def content_irrigation():
    return send_from_directory(str(HTML_DIR), "irrigation_advisory1.html")


@app.route("/content/alerts")
def content_alerts():
    return send_from_directory(str(HTML_DIR), "alert_dashboard.html")


@app.route("/content/disease")
def content_disease():
    return send_from_directory(str(HTML_DIR), "disease_detection.html")


@app.route("/content/yield-detect")
def content_yield_detect():
    return send_from_directory(str(HTML_DIR), "yield_detect.html")


@app.route("/content/auction")
def content_auction():
    return send_from_directory(str(HTML_DIR), "auction_farmer.html")

@app.route("/content/cold-storage")
def content_cold_storage():
    return send_from_directory(str(HTML_DIR), "cold_storage.html")


# ── STATIC ASSET ROUTES ─────────────────────────────────────────────────

@app.route("/css/<path:filename>")
def serve_css(filename):
    return send_from_directory(str(CSS_DIR), filename)


@app.route("/js/<path:filename>")
def serve_js(filename):
    return send_from_directory(str(JS_DIR), filename)


@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(STATIC_DIR), filename)


# ── DATA ROUTES ─────────────────────────────────────────────────────────

@app.route("/predictions.json")
def serve_predictions():
    """
    Serves the alerts feed for the dashboard's Alerts tab.

    Same server-side scoping as /api/crop: a logged-in state_admin/
    district_admin gets their own session's state (ignoring ?state= from
    the client), and a district_admin additionally gets the JSON response
    filtered down to just their district before it's sent — so the raw
    file is never exposed to them, not even via devtools network tab.
    Unscoped roles / logged-out requests behave exactly as before.
    """
    session = get_current_session()
    forced_state = None
    forced_district = None

    if session:
        role = (session.get("role") or "").lower().strip()
        if role in ("state_admin", "district_admin"):
            forced_state = (session.get("state") or "").lower().strip() or None
        if role == "district_admin":
            forced_district = (session.get("district") or "").strip() or None

    state = forced_state or request.args.get("state", "tripura").lower()
    data_dir = DATA_DIR.get(state, DATA_DIR["tripura"])

    if not forced_district:
        return send_from_directory(str(data_dir), "predictions.json")

    predictions_file = data_dir / "predictions.json"
    if not predictions_file.exists():
        return jsonify({"error": "predictions.json not found", "state": state}), 404

    import json
    with open(predictions_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    payload["predictions"] = [
        r for r in payload.get("predictions", [])
        if str(r.get("district", "")).strip().lower() == forced_district.lower()
    ]
    return jsonify(payload)


# ── PROXY HELPER ────────────────────────────────────────────────────────

def forward_request(base_url, path, override_params=None, override_json=None):
    """
    Forward gateway request to backend service.

    override_params / override_json let a route force specific query-string
    or JSON-body values (e.g. district scoping for a district_admin) that
    win over whatever the client actually sent — this is how server-side
    enforcement happens, since the client's own values can't be trusted.

    Examples:
      /api/disease/health          -> http://127.0.0.1:5004/health
      /api/disease/supported_crops -> http://127.0.0.1:5004/supported_crops
      /api/disease/detect          -> http://127.0.0.1:5004/detect
      /api/yield/lands             -> http://127.0.0.1:5008/api/yield/lands
    """
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    try:
        headers = {}

        # Forward content-type for JSON requests
        if request.content_type:
            headers["Content-Type"] = request.content_type

        # Forward the caller's auth token so backend services (e.g.
        # yield_detect_backend.py) can verify who's making the request —
        # previously this was dropped entirely, so every proxied request
        # looked anonymous and got 401'd by anything requiring auth.
        if request.headers.get("Authorization"):
            headers["Authorization"] = request.headers["Authorization"]

        params = request.args.to_dict(flat=True)
        if override_params:
            params.update(override_params)

        if request.method == "GET":
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=30
            )

        elif request.method == "POST":
            if request.is_json:
                body = request.get_json(silent=True) or {}
                if override_json:
                    body.update(override_json)
                resp = requests.post(
                    url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=180
                )
            else:
                resp = requests.post(
                    url,
                    params=params,
                    data=request.get_data(),
                    headers=headers,
                    timeout=180
                )

        elif request.method == "PUT":
            if request.is_json:
                body = request.get_json(silent=True) or {}
                if override_json:
                    body.update(override_json)
                resp = requests.put(
                    url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=180
                )
            else:
                resp = requests.put(
                    url,
                    params=params,
                    data=request.get_data(),
                    headers=headers,
                    timeout=180
                )

        elif request.method == "DELETE":
            resp = requests.delete(
                url,
                params=params,
                headers=headers,
                timeout=30
            )

        else:
            return jsonify({"error": "Method not allowed"}), 405

        excluded_headers = {
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection"
        }

        response_headers = [
            (name, value)
            for name, value in resp.headers.items()
            if name.lower() not in excluded_headers
        ]

        return Response(
            resp.content,
            status=resp.status_code,
            headers=response_headers,
            content_type=resp.headers.get("Content-Type")
        )

    except requests.exceptions.ConnectionError:
        return jsonify({
            "error": "Backend service unavailable",
            "backend": base_url,
            "path": path
        }), 503

    except requests.exceptions.Timeout:
        return jsonify({
            "error": "Backend service timeout",
            "backend": base_url,
            "path": path
        }), 504

    except Exception as e:
        return jsonify({
            "error": "Gateway proxy error",
            "details": str(e),
            "backend": base_url,
            "path": path
        }), 500


# ── API PROXY ROUTES ────────────────────────────────────────────────────

@app.route("/api/crop/<path:path>", methods=["GET", "POST"])
def crop_api(path):
    """
    Enforces district/state scoping server-side using the caller's verified
    session (from auth_excel's SESSIONS store, looked up via their bearer
    token) rather than trusting whatever ?state=/district the client sent.

      - state_admin / district_admin: their session's own `state` always
        wins over the client-supplied state, so they can never point their
        dashboard at another state's backend by editing the URL.
      - district_admin additionally has their session's `district` forced
        into both the query params (GET, e.g. /stats) and the JSON body
        (POST, e.g. /predict), overriding any district the client sent —
        this is what actually closes the "remove district= in devtools"
        gap, since the override happens after the request leaves the
        browser and can't be edited by the caller.
      - Anyone not logged in (no/invalid token), or logged in as a role
        without a state/district scope (admin, analyst, farmer), is
        forwarded exactly as before — unscoped, full access.
    """
    session = get_current_session()
    override_params = {}
    override_json = {}
    forced_state = None

    if session:
        role = (session.get("role") or "").lower().strip()
        if role in ("state_admin", "district_admin"):
            forced_state = (session.get("state") or "").lower().strip() or None
        if role == "district_admin":
            district = (session.get("district") or "").strip()
            if district:
                override_params["district"] = district
                override_json["district"] = district

    state = forced_state or get_state()
    base_url = CROP_APIS.get(state, CROP_API_TRIPURA)

    return forward_request(
        base_url,
        path,
        override_params=override_params or None,
        override_json=override_json or None,
    )


@app.route("/api/irrigation/<path:path>", methods=["GET", "POST"])
def irrigation_api(path):
    return forward_request(get_irr_api(), path)


@app.route("/api/disease/<path:path>", methods=["GET", "POST"])
def disease_api(path):
    return forward_request(DISEASE_API, path)


@app.route("/api/yield/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def yield_api(path):
    # yield_detect_backend.py mounts its routes under /api/yield/... itself,
    # so we forward the full path (unlike /api/crop -> /path, this one keeps
    # the "yield" segment) — see YIELD_API + forward_request below.
    return forward_request(YIELD_API, f"api/yield/{path}")


@app.route("/api/unused-crops", methods=["GET", "POST"])
@app.route("/api/unused-crops/<path:path>", methods=["GET", "PATCH", "DELETE", "POST"])
def auction_crops_api(path=""):
    # auction_backend.py mounts its own routes under /api/unused-crops/...,
    # same convention as /api/yield above — forward the full path so
    # /api/unused-crops/<id>/sell reaches the backend unchanged.
    full_path = f"api/unused-crops/{path}".rstrip("/") if path else "api/unused-crops"
    return forward_request(AUCTION_API, full_path)


@app.route("/api/bids", methods=["GET", "POST"])
@app.route("/api/bids/<path:path>", methods=["GET", "PATCH"])
def auction_bids_api(path=""):
    full_path = f"api/bids/{path}".rstrip("/") if path else "api/bids"
    return forward_request(AUCTION_API, full_path)


# Optional direct checks
@app.route("/api/disease-health")
def disease_health_direct():
    return forward_request(DISEASE_API, "health")


@app.route("/api/yield-health")
def yield_health_direct():
    return forward_request(YIELD_API, "api/yield/health")


@app.route("/api/auction-health")
def auction_health_direct():
    return forward_request(AUCTION_API, "health")

@app.route("/api/cold-storage-health")
def cold_storage_health_direct():
    return forward_request(COLD_STORAGE_API, "api/cold-storage/health")


@app.route("/api/cold-storage/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def cold_storage_api(path):
    # cold_storage_backend.py mounts its own routes under /api/cold-storage/...
    # itself, same convention as /api/yield above — forward the full path so
    # e.g. /api/cold-storage/districts/<d>/summary reaches the backend unchanged.
    # (This general proxy was missing — only the /api/cold-storage-health
    # shortcut existed, which is why every real cold-storage call 404'd.)
    return forward_request(COLD_STORAGE_API, f"api/cold-storage/{path}")


# ── HEALTH CHECK ────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "api-gateway",
        "services": {
            "tripura": {
                "crop": CROP_API_TRIPURA,
                "irrigation": IRR_API_TRIPURA
            },
            "meghalaya": {
                "crop": CROP_API_MEGHALAYA,
                "irrigation": IRR_API_MEGHALAYA
            },
            "shared": {
                "disease": DISEASE_API,
                "yield_detect": YIELD_API,
                "cold_storage": COLD_STORAGE_API,
                "auction": AUCTION_API
            }
        }
    })


# ── MAIN ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  API GATEWAY — Running on http://localhost:8085")
    print("=" * 55)
    app.run(host="0.0.0.0", port=8085, debug=False)