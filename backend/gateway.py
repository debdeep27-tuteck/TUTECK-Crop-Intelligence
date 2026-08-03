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

from auth_excel import auth_bp, init_excel

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
def home():
    return send_from_directory(str(HTML_DIR), "index.html")


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
    state = request.args.get("state", "tripura").lower()
    data_dir = DATA_DIR.get(state, DATA_DIR["tripura"])
    return send_from_directory(str(data_dir), "predictions.json")


# ── PROXY HELPER ────────────────────────────────────────────────────────

def forward_request(base_url, path):
    """
    Forward gateway request to backend service.

    Examples:
      /api/disease/health          -> http://127.0.0.1:5004/health
      /api/disease/supported_crops -> http://127.0.0.1:5004/supported_crops
      /api/disease/detect          -> http://127.0.0.1:5004/detect
    """
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    try:
        headers = {}

        # Forward content-type for JSON requests
        if request.content_type:
            headers["Content-Type"] = request.content_type

        if request.method == "GET":
            resp = requests.get(
                url,
                params=request.args,
                timeout=30
            )

        elif request.method == "POST":
            if request.is_json:
                resp = requests.post(
                    url,
                    params=request.args,
                    json=request.get_json(silent=True),
                    timeout=180
                )
            else:
                resp = requests.post(
                    url,
                    params=request.args,
                    data=request.get_data(),
                    headers=headers,
                    timeout=180
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
    return forward_request(get_crop_api(), path)


@app.route("/api/irrigation/<path:path>", methods=["GET", "POST"])
def irrigation_api(path):
    return forward_request(get_irr_api(), path)


@app.route("/api/disease/<path:path>", methods=["GET", "POST"])
def disease_api(path):
    return forward_request(DISEASE_API, path)


# Optional direct checks
@app.route("/api/disease-health")
def disease_health_direct():
    return forward_request(DISEASE_API, "health")


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
                "disease": DISEASE_API
            }
        }
    })


# ── MAIN ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  API GATEWAY — Running on http://localhost:8085")
    print("=" * 55)
    app.run(host="0.0.0.0", port=8085, debug=False)