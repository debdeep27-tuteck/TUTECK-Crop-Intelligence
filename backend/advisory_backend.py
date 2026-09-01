"""
advisory_backend.py
====================
Farmer AI Advisory Chatbot — orchestration microservice.

Runs on port 5013 (fits your gateway's existing port scheme). It does NOT
duplicate any of your other services' logic — it's a thin LLM layer on top
of them. When a farmer asks something, Groq decides which of your existing
microservices (mandi prices, nearest mandi, irrigation, cold storage, crop
recommender) to call as a "tool", the real data comes back from YOUR
services, and the LLM turns that into a natural-language answer. The model
never invents prices, weather, or advisory numbers — it only reports what
your services return.

Conversation history is stored per-farmer in chatbot.db (SQLite) so a
farmer can ask a follow-up question ("what about wheat?") and the model
still has context.

Real backend routes this service calls (verified against the actual files)
-----------------------------------------------------------------------------
    mandi_prices_backend.py   GET  /api/mandi-prices/prices?state=&district=&commodity=
    nearest_mandi_backend.py  GET  /api/nearest-mandi/locate?lat=&lon=&radius=
    irrigation_backend2.py    POST /advise   (JSON body: state, district, crop, ...)
    cold_storage_backend.py   GET  /api/cold-storage/districts/<district>/summary?state=
                                    (requires the farmer's own bearer token —
                                     cold_storage_backend.py verifies it against
                                     the gateway's /api/auth/me)
    crop_recommender.py       POST /recommend   (JSON body: district, top_n, Season)
                                    (state-routed: tripura=5003, meghalaya=5005,
                                     rajasthan=5007 — a SEPARATE service from the
                                     dashboard/stats backend on 5000/5002/5006)

Requirements
------------
    pip install groq flask requests

Env vars (same .env your other services already use)
------------------------------------------------------
    GROQ_API_KEY=...

Run standalone
--------------
    python advisory_backend.py
    # then:
    curl -X POST http://localhost:5013/chat \
         -H "Content-Type: application/json" \
         -d '{"farmer_id":"abc123","message":"What is the mandi price for mustard in Alwar?","lang":"en"}'

Wiring into your actual gateway.py
------------------------------------
Add ADVISORY_API = "http://127.0.0.1:5013" alongside your other *_API
constants, then a proxy route right next to the other /api/... routes:

    @app.route("/api/advisory/<path:path>", methods=["GET", "POST", "DELETE"])
    def advisory_api(path):
        return forward_request(ADVISORY_API, path)

This reuses gateway.py's existing forward_request() helper, so auth
headers, timeouts, and error handling all match your other services
automatically — no new proxy logic needed.
"""

import base64
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, request, jsonify, g, Response
from groq import Groq

# ── CONFIG ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "chatbot.db"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set. Add it to backend/.env (same key your disease-detection service uses).")

client = Groq(api_key=GROQ_API_KEY)

# Spoken replies use Sarvam AI's Bulbul TTS instead of Groq's PlayAI TTS,
# since Sarvam actually covers the Indian languages this chatbot supports
# (Groq's TTS is English/Arabic only). Get a key from dashboard.sarvam.ai.
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
if not SARVAM_API_KEY:
    raise RuntimeError("SARVAM_API_KEY is not set. Add it to backend/.env — get a key from https://dashboard.sarvam.ai")

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_TTS_MODEL = "bulbul:v3"
SARVAM_TTS_SPEAKER = "shubh"  # any bulbul:v3 speaker; see Sarvam docs for the full list

# Maps this app's SUPPORTED_LANGS codes to Sarvam's BCP-47 language_code.
# Sarvam's TTS doesn't currently cover Kokborok (kok) or Khasi (ks), so
# those are intentionally left out — /chat/speak returns a clear error
# for them instead of silently mispronouncing text in the wrong voice.
LANG_TO_SARVAM = {
    "en": "en-IN",
    "hi": "hi-IN",
    "bn": "bn-IN",
    "mr": "mr-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "gu": "gu-IN",
    "pa": "pa-IN",
}

CHAT_MODEL = "openai/gpt-oss-120b"       # Groq deprecated llama-3.3-70b-versatile;
                                          # this is the current production model
                                          # with full tool-calling support.
MAX_HISTORY_MESSAGES = 12                  # last N turns kept per farmer, to bound token usage
REQUEST_TIMEOUT = 8                        # seconds, for calls into your other microservices

# Point these at wherever your existing services actually run — verified
# directly against mandi_prices_backend.py, nearest_mandi_backend.py,
# irrigation_backend2.py, cold_storage_backend.py, and crop_recommender.py.
# Override any of these with env vars if your ports differ per environment.
INTERNAL_SERVICES = {
    "mandi_prices":     os.environ.get("MANDI_PRICES_URL", "http://127.0.0.1:6011"),
    "nearest_mandi":    os.environ.get("NEAREST_MANDI_URL", "http://127.0.0.1:6012"),
    "irrigation":       os.environ.get("IRRIGATION_URL", "http://127.0.0.1:6001"),
    "cold_storage":     os.environ.get("COLD_STORAGE_URL", "http://127.0.0.1:6010"),
}

# Crop recommender is now its own microservice, split out of backend_2.py,
# state-routed exactly like gateway.py's RECOMMENDER_APIS (NOT the same
# ports as the dashboard/stats backend on 5000/5002/5006).
CROP_RECOMMENDER_APIS = {
    "tripura":   os.environ.get("CROP_RECOMMENDER_TRIPURA", "http://127.0.0.1:6003"),
    "meghalaya": os.environ.get("CROP_RECOMMENDER_MEGHALAYA", "http://127.0.0.1:6005"),
    "rajasthan": os.environ.get("CROP_RECOMMENDER_RAJASTHAN", "http://127.0.0.1:6007"),
}

SUPPORTED_LANGS = {"en", "hi", "bn", "mr", "kok", "ks", "ta", "te", "gu", "pa"}

# Same pattern as cold_storage_backend.py / yield_detect_backend.py: verify
# the farmer's bearer token against the gateway's own /api/auth/me, rather
# than re-implementing session storage here. This also gives us the
# farmer's role-based page permissions (auth_excel.py's ALL_PAGES/
# role_permissions) so access to this chatbot is controlled from the same
# admin panel as every other tab, instead of being wide open.
GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")
REQUIRED_PAGE_PERMISSION = "/advisory"

_db_lock = threading.RLock()

app = Flask(__name__)


# ── DB: CONVERSATION HISTORY ──────────────────────────────────────────────

@contextmanager
def _conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db_lock, _conn() as conn:
        # One row per user_id. `content` holds the full message array as JSON:
        # [{"role": "user"|"assistant", "content": "...", "created_at": 173...}, ...]
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                user_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        # Migrate old schema (farmer_id, one row per message) if it's still present.
        try:
            old_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        except sqlite3.OperationalError:
            old_cols = set()
        if "farmer_id" in old_cols and "user_id" not in old_cols:
            conn.execute("ALTER TABLE messages RENAME TO messages_legacy")
            conn.execute("""
                CREATE TABLE messages (
                    user_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            legacy_rows = conn.execute(
                "SELECT farmer_id, role, content, created_at FROM messages_legacy ORDER BY farmer_id, id"
            ).fetchall()
            grouped = {}
            for r in legacy_rows:
                grouped.setdefault(r["farmer_id"], []).append({
                    "role": r["role"], "content": r["content"], "created_at": r["created_at"],
                })
            for uid, msgs in grouped.items():
                conn.execute(
                    "INSERT INTO messages (user_id, content, updated_at) VALUES (?, ?, ?)",
                    (uid, json.dumps(msgs), time.time()),
                )
            conn.execute("DROP TABLE messages_legacy")


def load_history(user_id):
    """Return the last MAX_HISTORY_MESSAGES turns as Groq-format messages."""
    with _db_lock, _conn() as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return []
    msgs = json.loads(row["content"])
    trimmed = msgs[-MAX_HISTORY_MESSAGES:]
    return [{"role": m["role"], "content": m["content"]} for m in trimmed]


def append_messages(user_id, new_messages):
    """Append one or more {'role', 'content'} messages to this user's array."""
    now = time.time()
    with _db_lock, _conn() as conn:
        row = conn.execute("SELECT content FROM messages WHERE user_id = ?", (user_id,)).fetchone()
        existing = json.loads(row["content"]) if row else []
        for m in new_messages:
            existing.append({"role": m["role"], "content": m["content"], "created_at": now})
        conn.execute(
            """
            INSERT INTO messages (user_id, content, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(existing), now),
        )


def save_message(user_id, role, content):
    append_messages(user_id, [{"role": role, "content": content}])


def clear_history(user_id):
    with _db_lock, _conn() as conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))


# ── TOOL DEFINITIONS (what the LLM is allowed to call) ────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_mandi_price",
            "description": "Get the current mandi (market) price for a crop in a given district.",
            "parameters": {
                "type": "object",
                "properties": {
                    "crop": {"type": "string", "description": "Crop name, e.g. 'mustard', 'wheat'"},
                    "district": {"type": "string", "description": "District name, e.g. 'Alwar'"},
                    "state": {"type": "string", "description": "State name, e.g. 'Rajasthan'"},
                },
                "required": ["crop", "district"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearest_mandi",
            "description": (
                "Find real marketplaces/mandis near a GPS location, sorted by distance. "
                "Requires the farmer's latitude and longitude — if you don't have them "
                "yet (they aren't in the conversation), ask the farmer to share their "
                "location instead of guessing coordinates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "radius_m": {"type": "number", "description": "Search radius in meters. Defaults to 5000 (5km)."},
                },
                "required": ["latitude", "longitude"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_irrigation_advice",
            "description": "Get today's irrigation recommendation (whether/how much to irrigate) for a crop and field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "crop": {"type": "string"},
                    "district": {"type": "string"},
                    "state": {"type": "string"},
                    "field_id": {"type": "string", "description": "Optional geofenced field/parcel ID if the farmer has one on record."},
                },
                "required": ["crop", "district"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cold_storage_availability",
            "description": "Check nearby cold storage facilities and available capacity for a crop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "crop": {"type": "string"},
                    "district": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["crop", "district"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_crop_recommendation",
            "description": "Get the top recommended crops to plant this season for a district, ranked by suitability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "district": {"type": "string"},
                    "state": {"type": "string"},
                    "season": {"type": "string", "description": "e.g. 'Kharif', 'Rabi', 'Zaid'"},
                },
                "required": ["district", "state"],
            },
        },
    },
]


# ── TOOL DISPATCH: map LLM tool calls -> real HTTP calls into your services ─

def _request(method, base_url, path, service_label, params=None, json_body=None, extra_headers=None):
    try:
        resp = requests.request(
            method,
            f"{base_url.rstrip('/')}/{path.lstrip('/')}",
            params=params,
            json=json_body,
            headers=extra_headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"Could not reach {service_label} service: {e}"}


def call_internal_service(tool_name, args, auth_header=None):
    """
    Dispatch a tool call to the matching internal microservice, calling each
    backend directly (not through the gateway) using its own real routes —
    verified against mandi_prices_backend.py, nearest_mandi_backend.py,
    irrigation_backend2.py, cold_storage_backend.py, and crop_recommender.py.

    `auth_header` is the farmer's own "Bearer <token>" value, forwarded from
    the /chat request. cold_storage_backend.py requires it (it verifies the
    token against the gateway's /api/auth/me) — without it, every cold
    storage lookup will 401.
    """
    if tool_name == "get_mandi_price":
        params = {
            "state": str(args.get("state") or "").lower().strip(),
            "district": args.get("district"),
            "commodity": args.get("crop"),
        }
        return _request("GET", INTERNAL_SERVICES["mandi_prices"], "api/mandi-prices/prices",
                         "mandi_prices", params={k: v for k, v in params.items() if v})

    if tool_name == "get_nearest_mandi":
        params = {
            "lat": args.get("latitude"),
            "lon": args.get("longitude"),
            "radius": args.get("radius_m", 5000),
        }
        return _request("GET", INTERNAL_SERVICES["nearest_mandi"], "api/nearest-mandi/locate",
                         "nearest_mandi", params=params)

    if tool_name == "get_irrigation_advice":
        # irrigation_backend2.py's /advise is a POST with a JSON body, and
        # (if IRRIGATION_API_KEYS is configured on that service) needs its
        # own API key — a farmer's login session token does NOT satisfy it.
        # Set IRRIGATION_API_KEYS to empty/unset for this integration to
        # work without a separate key, or add ADVISORY_IRRIGATION_API_KEY
        # to this service's env and it'll be sent automatically below.
        body = {
            "state": str(args.get("state") or "tripura").lower().strip(),
            "district": args.get("district"),
            "crop": args.get("crop", "Rice"),
        }
        headers = {}
        irrigation_key = os.environ.get("ADVISORY_IRRIGATION_API_KEY")
        if irrigation_key:
            headers["X-API-Key"] = irrigation_key
        return _request("POST", INTERNAL_SERVICES["irrigation"], "advise", "irrigation",
                         json_body=body, extra_headers=headers or None)

    if tool_name == "get_cold_storage_availability":
        state = str(args.get("state") or "").strip()
        district = args.get("district")
        if not state or not district:
            return {"error": "Both 'state' and 'district' are required for a cold storage lookup."}
        headers = {"Authorization": auth_header} if auth_header else None
        if not auth_header:
            return {"error": "Cold storage lookup requires the farmer to be logged in (no auth token available)."}
        return _request("GET", INTERNAL_SERVICES["cold_storage"], f"api/cold-storage/districts/{district}/summary",
                         "cold_storage", params={"state": state}, extra_headers=headers)

    if tool_name == "get_crop_recommendation":
        state = str(args.get("state", "tripura")).lower().strip()
        base = CROP_RECOMMENDER_APIS.get(state, CROP_RECOMMENDER_APIS["tripura"])
        body = {"district": args.get("district", "Dhalai"), "top_n": 5}
        if args.get("season"):
            body["Season"] = args["season"]
        return _request("POST", base, "recommend", "crop_recommender", json_body=body)

    return {"error": f"Unknown tool '{tool_name}'"}


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────

def build_system_prompt(lang):
    return (
        "You are a farm advisory assistant for Indian farmers using the CropAI/TUTECK platform. "
        f"Respond in language code '{lang}', in simple, plain language a farmer with limited "
        "literacy can understand — short sentences, no jargon. "
        "You have tools that fetch REAL, live data (mandi prices, irrigation advice, cold storage "
        "availability, crop recommendations, nearest mandi). "
        "Rules:\n"
        "1. NEVER guess or make up a price, weather figure, or advisory number. If you need data to "
        "answer, call the matching tool first.\n"
        "2. If a tool returns an error or no data, tell the farmer plainly that the information isn't "
        "available right now instead of inventing an answer.\n"
        "3. Do not give financial, legal, or medical advice. For those, tell the farmer to consult the "
        "relevant local authority or professional.\n"
        "4. When you use a tool's data in your answer, briefly say where it came from (e.g. "
        "\"based on today's mandi prices\") so the farmer knows it isn't a guess.\n"
        "5. Keep answers short — 2-4 sentences unless the farmer asks for more detail."
    )


# ── CORE CHAT LOGIC ────────────────────────────────────────────────────────

def run_chat_turn(user_id, message, lang, auth_header=None):
    history = load_history(user_id)

    messages = [
        {"role": "system", "content": build_system_prompt(lang)},
        *history,
        {"role": "user", "content": message},
    ]

    response = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        temperature=0.3,
    )

    msg = response.choices[0].message
    tools_used = []

    if msg.tool_calls:
        # Feed the assistant's tool-call message back into the conversation...
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # ...then run each tool and feed the results back as tool messages.
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = call_internal_service(tc.function.name, args, auth_header=auth_header)
            tools_used.append({"tool": tc.function.name, "args": args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })

        # Second pass: let the model turn the tool results into a real answer.
        final = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            temperature=0.3,
        )
        reply_text = final.choices[0].message.content
    else:
        reply_text = msg.content

    append_messages(user_id, [
        {"role": "user", "content": message},
        {"role": "assistant", "content": reply_text},
    ])

    return reply_text, tools_used


# ── AUTH: verify session token + "/advisory" page permission against the
#    gateway, same pattern as cold_storage_backend.py's verify_token/require_auth ──

def verify_token(token):
    if not token:
        print("[verify_token] no token provided in Authorization header")
        return None
    try:
        resp = requests.get(
            f"{GATEWAY_INTERNAL_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        # TEMP DEBUG — remove once the 401 is diagnosed.
        print(f"[verify_token] GET {GATEWAY_INTERNAL_URL}/api/auth/me -> {resp.status_code} body={resp.text[:300]!r}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("email"):
            print(f"[verify_token] 200 but no email in response: {data!r}")
            return None
        pages = (data.get("permissions") or {}).get("pages", [])
        return {
            "uid": data.get("uid"),
            "email": data.get("email"),
            "role": data.get("role"),
            "state": data.get("state") or "",
            "district": data.get("district") or "",
            "pages": pages,
        }
    except requests.exceptions.RequestException as e:
        # TEMP DEBUG — remove once the 401 is diagnosed.
        print(f"[verify_token] request to gateway failed: {e!r}")
        return None


def require_advisory_access(fn):
    """Rejects with 401 if not logged in, 403 if the farmer's role hasn't
    been granted the '/advisory' page by an admin (see auth_excel.py's
    role_permissions / per-user permissions, editable from the admin panel)."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        user = verify_token(token)
        if not user:
            return jsonify({"error": "Unauthorized — please log in."}), 401
        if REQUIRED_PAGE_PERMISSION not in user["pages"]:
            return jsonify({"error": "Forbidden — you don't have access to the Advisory chat."}), 403
        g.user = user
        return fn(*args, **kwargs)
    return wrapped


# ── ROUTES ─────────────────────────────────────────────────────────────────

@app.route("/chat", methods=["POST"])
@require_advisory_access
def chat():
    body = request.get_json(silent=True) or {}
    # Use the verified identity from the token, not whatever the client
    # body claims, so a user can't read/write another user's history
    # by passing a different user_id.
    user_id = g.user["uid"]
    message = str(body.get("message", "")).strip()
    lang = str(body.get("lang", "en")).strip().lower()

    if not message:
        return jsonify({"error": "'message' is required."}), 400
    if lang not in SUPPORTED_LANGS:
        lang = "en"

    try:
        reply, tools_used = run_chat_turn(user_id, message, lang, auth_header=request.headers.get("Authorization"))
    except Exception as e:
        return jsonify({"error": f"Advisory service error: {e}"}), 500

    return jsonify({
        "reply": reply,
        "tools_used": [t["tool"] for t in tools_used],  # names only; keep raw args/results out of the public response
    }), 200


@app.route("/chat/history", methods=["GET"])
@require_advisory_access
def get_history():
    return jsonify({"user_id": g.user["uid"], "messages": load_history(g.user["uid"])}), 200


@app.route("/chat/history", methods=["DELETE"])
@require_advisory_access
def reset_history():
    clear_history(g.user["uid"])
    return jsonify({"status": "cleared", "user_id": g.user["uid"]}), 200


@app.route("/chat/transcribe", methods=["POST"])
@require_advisory_access
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "'audio' file is required."}), 400
    audio_file = request.files["audio"]
    lang = str(request.form.get("lang", "en")).strip().lower()
    if lang not in SUPPORTED_LANGS:
        lang = "en"

    audio_bytes = audio_file.read()
    # A clip under ~2KB is almost certainly silence, a misfire (click with
    # no speech), or a permissions glitch rather than real audio — catching
    # it here avoids sending noise to Whisper and getting garbage back.
    if len(audio_bytes) < 2000:
        return jsonify({"error": "That recording was too short — please try again and speak for a moment before stopping."}), 400

    try:
        result = client.audio.transcriptions.create(
            file=(audio_file.filename or "audio.webm", audio_bytes),
            # Full model, not the "turbo" variant — turbo trades accuracy
            # for speed, which shows up as exactly the kind of unclear/
            # garbled transcriptions you're seeing.
            model="whisper-large-v3",
            language=lang,
            # Nudges Whisper toward the vocabulary it'll actually hear —
            # crop/mandi/irrigation terms it might otherwise mishear as
            # similar-sounding common words.
            prompt="Farming advisory conversation about mandi prices, irrigation, crop recommendations, cold storage, and weather for Indian farmers.",
            response_format="text",
            temperature=0.0,
        )
        # response_format="text" returns a plain string; some SDK versions
        # instead return an object with a .text attribute — handle both.
        text = result if isinstance(result, str) else getattr(result, "text", "")
    except Exception as e:
        return jsonify({"error": f"Transcription failed: {e}"}), 500

    text = text.strip()
    if not text:
        return jsonify({"error": "Didn't catch any speech in that recording — please try again."}), 400

    return jsonify({"text": text}), 200


@app.route("/chat/speak", methods=["POST"])
@require_advisory_access
def speak():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    lang = str(body.get("lang", "en")).strip().lower()
    if not text:
        return jsonify({"error": "'text' is required."}), 400
    # bulbul:v3 caps input at 2500 characters; trim defensively so a long
    # reply doesn't error out instead of just speaking the first part.
    text = text[:2500]

    language_code = LANG_TO_SARVAM.get(lang)
    if not language_code:
        return jsonify({"error": "Spoken replies aren't available in this language yet."}), 400

    try:
        resp = requests.post(
            SARVAM_TTS_URL,
            headers={
                "api-subscription-key": SARVAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "language_code": language_code,
                "model": SARVAM_TTS_MODEL,
                "speaker": SARVAM_TTS_SPEAKER,
                "speech_sample_rate": 24000,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Sarvam TTS request failed ({resp.status_code}): {resp.text[:300]}"}), 502

        data = resp.json()
        audios = data.get("audios") or []
        if not audios:
            return jsonify({"error": "No audio returned by Sarvam TTS."}), 502
        audio_bytes = base64.b64decode(audios[0])
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Couldn't reach Sarvam TTS: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"Speech generation failed: {e}"}), 500

    return Response(audio_bytes, mimetype="audio/wav")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "advisory_backend", "model": CHAT_MODEL}), 200


# ── ENTRYPOINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(port=6013, debug=True)