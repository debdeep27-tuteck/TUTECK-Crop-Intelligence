"""
disease_backend.py

Crop disease detection backend using Groq VLM.

Run:
  python disease_backend.py

Endpoints:
  GET  /health
  GET  /supported_crops
  POST /detect
  POST /detect-url
"""

import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import base64
import os
import json
import re
import warnings
import requests as http_requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

PORT = int(os.environ.get("DISEASE_PORT", 5004))
MODEL_NAME = os.environ.get("MODEL", "qwen/qwen3.6-27b")

client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))


# ── STATE-SPECIFIC CROP DISEASE PROFILES ────────────────────────────────

CROP_DISEASES = {
    "tripura": {
        "Rice": ["Blast", "Brown spot", "Sheath blight", "Bacterial leaf blight", "False smut", "Hispa"],
        "Jute": ["Stem rot", "Black band", "Anthracnose", "Soft rot", "Root rot"],
        "Maize": ["Northern leaf blight", "Common rust", "Gray leaf spot", "Downy mildew", "Stalk rot"],
        "Sugarcane": ["Red rot", "Smut", "Wilt", "Ratoon stunting", "Grassy shoot"],
        "Wheat": ["Rust (leaf/stem/stripe)", "Karnal bunt", "Loose smut", "Powdery mildew"],
        "Groundnut": ["Tikka leaf spot", "Rust", "Stem rot", "Bud necrosis", "Collar rot"],
        "Banana": ["Panama wilt", "Sigatoka", "Bunchy top", "Anthracnose", "Crown rot"],
        "Potato": ["Late blight", "Early blight", "Black scurf", "Common scab", "Blackleg"],
        "Tomato": ["Early blight", "Late blight", "Leaf curl", "Bacterial wilt", "Fusarium wilt"],
        "Chilli": ["Anthracnose", "Powdery mildew", "Leaf curl", "Cercospora leaf spot"],
        "Arhar/Tur": ["Fusarium wilt", "Phytophthora blight", "Leaf spot", "Sterility mosaic"],
        "Moong(Green Gram)": ["Yellow mosaic", "Powdery mildew", "Leaf spot", "Cercospora leaf spot"],
        "Urad": ["Yellow mosaic", "Leaf crinkle", "Anthracnose", "Powdery mildew"],
        "Rapeseed &Mustard": ["White rust", "Alternaria blight", "Downy mildew", "Sclerotinia rot"],
        "Pineapple": ["Heart rot", "Root rot", "Fruitlet core rot", "Mealybug wilt"],
        "Other": [],
    },
    "meghalaya": {
        "Rice": ["Blast", "Brown spot", "Sheath blight", "Bacterial leaf blight", "False smut"],
        "Maize": ["Northern leaf blight", "Common rust", "Gray leaf spot", "Downy mildew", "Stalk rot"],
        "Potato": ["Late blight", "Early blight", "Black scurf", "Common scab", "Blackleg"],
        "Ginger": ["Soft rot", "Bacterial wilt", "Leaf spot", "Yellow disease", "Rhizome rot"],
        "Turmeric": ["Leaf blotch", "Rhizome rot", "Leaf spot", "Pythium rot"],
        "Arecanut": ["Yellow leaf disease", "Bud rot", "Mahali disease", "Fruit rot"],
        "Banana": ["Panama wilt", "Sigatoka", "Bunchy top", "Anthracnose", "Crown rot"],
        "Tomato": ["Early blight", "Late blight", "Leaf curl", "Bacterial wilt", "Fusarium wilt"],
        "Cabbage": ["Black rot", "Clubroot", "Downy mildew", "Alternaria leaf spot"],
        "Chilli": ["Anthracnose", "Powdery mildew", "Leaf curl", "Cercospora leaf spot"],
        "Onion": ["Purple blotch", "Basal rot", "Downy mildew", "Stemphylium blight"],
        "Black pepper": ["Phytophthora foot rot", "Pollu disease", "Stunt disease", "Slow decline"],
        "Cashewnut": ["Anthracnose", "Powdery mildew", "Die back", "Leaf blight"],
        "Jute": ["Stem rot", "Black band", "Anthracnose", "Soft rot"],
        "Sugarcane": ["Red rot", "Smut", "Wilt", "Ratoon stunting"],
        "Wheat": ["Rust (leaf/stem/stripe)", "Karnal bunt", "Loose smut", "Powdery mildew"],
        "Other": [],
    },
    "rajasthan": {
        "Wheat": ["Leaf rust", "Stem rust", "Stripe rust", "Loose smut", "Karnal bunt", "Powdery mildew", "Flag smut"],
        "Rice": ["Blast", "Brown spot", "Sheath blight", "Bacterial leaf blight", "False smut"],
        "Maize": ["Turcicum leaf blight", "Maydis leaf blight", "Common rust", "Downy mildew", "Charcoal rot", "Stalk rot"],
        "Bajra/Pearl Millet": ["Downy mildew", "Ergot", "Smut", "Rust", "Blast"],
        "Pearl Millet": ["Downy mildew", "Ergot", "Smut", "Rust", "Blast"],
        "Barley": ["Leaf rust", "Stripe rust", "Loose smut", "Covered smut", "Net blotch", "Powdery mildew"],
        "Gram/Chickpea": ["Fusarium wilt", "Ascochyta blight", "Botrytis gray mold", "Dry root rot", "Collar rot"],
        "Chickpea": ["Fusarium wilt", "Ascochyta blight", "Botrytis gray mold", "Dry root rot", "Collar rot"],
        "Mustard/Rapeseed": ["Alternaria blight", "White rust", "Downy mildew", "Sclerotinia stem rot", "Powdery mildew"],
        "Rapeseed &Mustard": ["Alternaria blight", "White rust", "Downy mildew", "Sclerotinia stem rot", "Powdery mildew"],
        "Groundnut": ["Early leaf spot", "Late leaf spot", "Rust", "Stem rot", "Collar rot", "Bud necrosis"],
        "Cotton": ["Bacterial blight", "Alternaria leaf spot", "Fusarium wilt", "Verticillium wilt", "Root rot", "Leaf curl virus", "Boll rot"],
        "Cotton(lint)": ["Bacterial blight", "Alternaria leaf spot", "Fusarium wilt", "Verticillium wilt", "Root rot", "Leaf curl virus", "Boll rot"],
        "Soybean": ["Yellow mosaic", "Rust", "Anthracnose", "Rhizoctonia aerial blight", "Charcoal rot", "Bacterial pustule"],
        "Moong(Green Gram)": ["Yellow mosaic", "Cercospora leaf spot", "Powdery mildew", "Anthracnose", "Root rot"],
        "Urad": ["Yellow mosaic", "Leaf crinkle", "Cercospora leaf spot", "Anthracnose", "Powdery mildew"],
        "Moth Bean": ["Yellow mosaic", "Cercospora leaf spot", "Powdery mildew", "Root rot", "Bacterial leaf spot"],
        "Guar/Cluster Bean": ["Bacterial blight", "Alternaria leaf spot", "Powdery mildew", "Root rot", "Anthracnose"],
        "Cluster Bean": ["Bacterial blight", "Alternaria leaf spot", "Powdery mildew", "Root rot", "Anthracnose"],
        "Cumin": ["Wilt", "Blight", "Powdery mildew", "Alternaria blight", "Root rot"],
        "Coriander": ["Stem gall", "Powdery mildew", "Wilt", "Blight", "Root rot"],
        "Fenugreek": ["Powdery mildew", "Downy mildew", "Leaf spot", "Root rot", "Wilt"],
        "Isabgol": ["Downy mildew", "Powdery mildew", "Leaf blight", "Root rot"],
        "Onion": ["Purple blotch", "Stemphylium blight", "Basal rot", "Downy mildew", "Twister disease"],
        "Garlic": ["Purple blotch", "Stemphylium blight", "Basal rot", "White rot", "Rust"],
        "Chilli": ["Anthracnose", "Powdery mildew", "Leaf curl", "Cercospora leaf spot", "Damping off", "Phytophthora blight"],
        "Tomato": ["Early blight", "Late blight", "Leaf curl", "Bacterial wilt", "Fusarium wilt", "Septoria leaf spot"],
        "Potato": ["Late blight", "Early blight", "Black scurf", "Common scab", "Bacterial wilt", "Blackleg"],
        "Pomegranate": ["Bacterial blight", "Anthracnose", "Cercospora leaf spot", "Fruit rot", "Wilt"],
        "Citrus": ["Citrus canker", "Gummosis", "Greening", "Tristeza", "Anthracnose", "Sooty mould"],
        "Ber": ["Powdery mildew", "Leaf spot", "Fruit rot", "Sooty mould", "Rust"],
        "Date Palm": ["Bayoud disease", "Leaf spot", "Black scorch", "Inflorescence rot", "Fruit rot"],
        "Sugarcane": ["Red rot", "Smut", "Wilt", "Grassy shoot", "Ratoon stunting"],
        "Other": [],
    },
}

STATE_LABELS = {
    "tripura": "Tripura, Northeast India",
    "meghalaya": "Meghalaya, Northeast India",
    "rajasthan": "Rajasthan, Western India",
}


# ── HELPERS ─────────────────────────────────────────────────────────────

def get_state() -> str:
    try:
        body = request.get_json(silent=True) or {}
        state = request.args.get("state") or body.get("state", "tripura")
    except Exception:
        state = "tripura"

    state = str(state).lower().strip()
    return state if state in CROP_DISEASES else "tripura"


def get_crop_diseases(state: str) -> dict:
    return CROP_DISEASES.get(state, CROP_DISEASES["tripura"])


def build_system_prompt(state: str) -> str:
    region = STATE_LABELS.get(state, "India")
    crop_profiles = get_crop_diseases(state)

    return f"""
You are an expert agricultural plant pathologist specialising in crops grown in {region}.

You analyse crop images to detect diseases, pests, and nutritional deficiencies with high accuracy.

Known crop disease profiles for this state:
{json.dumps(crop_profiles, ensure_ascii=False)}

When given an image, you MUST respond with ONLY a valid JSON object.
Do not include markdown.
Do not include explanations outside JSON.
Do not wrap JSON in ``` blocks.

The JSON must follow this exact schema:
{{
  "crop_detected": "name of the crop or Unknown",
  "health_status": "Healthy | Diseased | Pest Damage | Nutrient Deficiency | Multiple Issues | Unclear",
  "confidence": 0,
  "diseases": [
    {{
      "name": "disease/pest/deficiency name",
      "confidence": 0,
      "severity": "Mild | Moderate | Severe",
      "affected_parts": ["leaf", "stem", "root", "fruit", "flower"],
      "symptoms_observed": "brief description of visible symptoms"
    }}
  ],
  "immediate_actions": ["action 1", "action 2"],
  "treatments": [
    {{
      "type": "Chemical | Biological | Cultural | Organic",
      "product_or_method": "specific product name or practice",
      "dosage_or_details": "application rate and method",
      "timing": "when to apply"
    }}
  ],
  "preventive_measures": ["measure 1", "measure 2", "measure 3"],
  "yield_impact_estimate": "estimated % yield loss if untreated, or Negligible if healthy",
  "urgency": "Immediate | Within 3 days | Within a week | Monitoring only | None",
  "additional_notes": "any other relevant observations"
}}

Rules:
- If the image does not show a plant or crop, set health_status to "Unclear".
- diseases array can be empty [] if the plant is healthy.
- treatments should be practical and available in {region}.
- Prioritise organic/biological treatments where possible.
- confidence values must be integers from 0 to 100.
"""


def extract_json_object(text: str) -> dict:
    """
    Extract and parse JSON object from model output.
    """
    if not text:
        raise ValueError("Empty response from model")

    cleaned = text.strip()

    # Remove markdown fences if model accidentally uses them
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to extract from first { to last }
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("Model did not return valid JSON")


def default_unclear_response(message: str, state: str):
    return {
        "crop_detected": "Unknown",
        "health_status": "Unclear",
        "confidence": 0,
        "diseases": [],
        "immediate_actions": [],
        "treatments": [],
        "preventive_measures": [],
        "yield_impact_estimate": "Unknown",
        "urgency": "None",
        "additional_notes": message,
        "state": state,
        "model_used": MODEL_NAME,
        "provider": "Groq"
    }


def normalize_result(result: dict, state: str) -> dict:
    """
    Ensure frontend always receives required keys.
    """
    required = {
        "crop_detected": "Unknown",
        "health_status": "Unclear",
        "confidence": 0,
        "diseases": [],
        "immediate_actions": [],
        "treatments": [],
        "preventive_measures": [],
        "yield_impact_estimate": "Unknown",
        "urgency": "None",
        "additional_notes": ""
    }

    for key, value in required.items():
        if key not in result or result[key] is None:
            result[key] = value

    if not isinstance(result.get("diseases"), list):
        result["diseases"] = []

    if not isinstance(result.get("immediate_actions"), list):
        result["immediate_actions"] = []

    if not isinstance(result.get("treatments"), list):
        result["treatments"] = []

    if not isinstance(result.get("preventive_measures"), list):
        result["preventive_measures"] = []

    try:
        result["confidence"] = int(result.get("confidence", 0))
    except Exception:
        result["confidence"] = 0

    result["confidence"] = max(0, min(100, result["confidence"]))

    result["state"] = state
    result["model_used"] = MODEL_NAME
    result["provider"] = "Groq"

    return result


# ── ROUTES ──────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    state = get_state()
    key_set = bool(os.environ.get("GROQ_API_KEY", ""))

    return jsonify({
        "status": "ok",
        "service": f"{state}-disease-detection",
        "state": state,
        "groq_key_set": key_set,
        "model": MODEL_NAME
    })


@app.route("/supported_crops", methods=["GET"])
def supported_crops():
    state = get_state()
    crops = get_crop_diseases(state)

    return jsonify({
        "state": state,
        "crops": list(crops.keys()),
        "diseases": crops
    })


@app.route("/detect", methods=["POST"])
def detect():
    state = get_state()

    if not os.environ.get("GROQ_API_KEY", ""):
        return jsonify({
            "error": "GROQ_API_KEY is not set. Please set it in your environment or .env file."
        }), 500

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "No JSON body received"}), 400

    image_base64 = data.get("image_base64")
    media_type = data.get("media_type", "image/jpeg")
    crop_hint = data.get("crop_hint", "")
    notes = data.get("notes", "")

    if not image_base64:
        return jsonify({"error": "image_base64 is required"}), 400

    if not media_type.startswith("image/"):
        media_type = "image/jpeg"

    try:
        data_url = f"data:{media_type};base64,{image_base64}"

        user_text = f"""
Analyse this crop image.

State: {state}
Region: {STATE_LABELS.get(state, "India")}
Crop hint: {crop_hint or "None"}
Farmer/image notes: {notes or "None"}

Return only valid JSON using the required schema.
"""

        completion = client.chat.completions.create(
            model=MODEL_NAME,
            temperature=0.1,
            max_completion_tokens=1800,
            reasoning_effort="none",
            messages=[
                {
                    "role": "system",
                    "content": build_system_prompt(state)
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_url
                            }
                        }
                    ]
                }
            ]
        )

        raw = completion.choices[0].message.content

        print("=" * 70)
        print("[disease] RAW MODEL OUTPUT:")
        print(raw)
        print("=" * 70)

        result = extract_json_object(raw)
        result = normalize_result(result, state)

        return jsonify(result)

    except Exception as e:
        import traceback
        print("=" * 70)
        print("[disease] EXCEPTION during /detect:")
        traceback.print_exc()
        print("=" * 70)

        return jsonify({
            "error": "Disease detection failed",
            "details": str(e),
            "state": state
        }), 500


@app.route("/detect-url", methods=["POST"])
def detect_url():
    """
    Accepts a JSON body with an ``image_url`` field, downloads the image,
    and runs it through the same Groq VLM pipeline as /detect.

    Request body (JSON):
      {
        "image_url": "https://…",   # required – publicly accessible image URL
        "state": "tripura",          # optional – state context (default: tripura)
        "crop_hint": "Rice",         # optional
        "notes": "Yellowing leaves"  # optional
      }

    Response: same JSON schema as /detect.
    """
    state = get_state()

    if not os.environ.get("GROQ_API_KEY", ""):
        return jsonify({
            "error": "GROQ_API_KEY is not set. Please set it in your environment or .env file."
        }), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body received"}), 400

    image_url = data.get("image_url", "").strip()
    crop_hint = data.get("crop_hint", "")
    notes = data.get("notes", "")

    if not image_url:
        return jsonify({"error": "image_url is required"}), 400

    # ── Download image from URL ───────────────────────────────────────────
    try:
        resp = http_requests.get(image_url, timeout=15, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        image_bytes = resp.content
    except http_requests.exceptions.Timeout:
        return jsonify({"error": "Timed out while downloading image from URL"}), 408
    except http_requests.exceptions.RequestException as exc:
        return jsonify({"error": "Failed to download image", "details": str(exc)}), 400

    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    media_type = content_type

    # ── Run through VLM pipeline (identical to /detect) ───────────────────
    try:
        data_url = f"data:{media_type};base64,{image_base64}"

        user_text = f"""
Analyse this crop image.

State: {state}
Region: {STATE_LABELS.get(state, "India")}
Crop hint: {crop_hint or "None"}
Farmer/image notes: {notes or "None"}
Source URL: {image_url}

Return only valid JSON using the required schema.
"""

        completion = client.chat.completions.create(
            model=MODEL_NAME,
            temperature=0.1,
            max_completion_tokens=1800,
            reasoning_effort="none",
            messages=[
                {
                    "role": "system",
                    "content": build_system_prompt(state)
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_url
                            }
                        }
                    ]
                }
            ]
        )

        raw = completion.choices[0].message.content

        print("=" * 70)
        print("[disease/detect-url] RAW MODEL OUTPUT:")
        print(raw)
        print("=" * 70)

        result = extract_json_object(raw)
        result = normalize_result(result, state)
        result["source_url"] = image_url

        return jsonify(result)

    except Exception as e:
        import traceback
        print("=" * 70)
        print("[disease] EXCEPTION during /detect-url:")
        traceback.print_exc()
        print("=" * 70)

        return jsonify({
            "error": "Disease detection failed",
            "details": str(e),
            "state": state
        }), 500


# ── MAIN ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  DISEASE DETECTION BACKEND")
    print(f"  Running at: http://0.0.0.0:{PORT}")
    print(f"  Model: {MODEL_NAME}")
    print(f"  GROQ_API_KEY: {'SET [OK]' if os.environ.get('GROQ_API_KEY') else 'NOT SET [FAIL]'}")
    print("=" * 55)

    app.run(host="0.0.0.0", port=PORT, debug=False)