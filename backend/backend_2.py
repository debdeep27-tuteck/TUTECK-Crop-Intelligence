"""
Crop Recommender — Updated Backend
====================================
Uses model_artefacts.pkl from crop_yield_with_weather.py (33 features).
Serves yield predictions AND full ranked recommendations to the HTML frontend.

Run:
    pip install flask flask-cors pandas xgboost scikit-learn openpyxl
    python backend.py

Then open crop_recommender.html in your browser.
"""

import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────


BASE_DIR = Path(__file__).resolve().parent

# ── STATE-AWARE DATA PATHS ───────────────────────────────────────────────────
# main.py starts two copies of this backend:
#   Tripura   -> python backend_2.py --state tripura   --port 5000
#   Meghalaya -> python backend_2.py --state meghalaya --port 5002
# These paths must be selected BEFORE model_artefacts.pkl is loaded.
import sys
STATE = "tripura"
if "--state" in sys.argv:
    _idx = sys.argv.index("--state")
    if _idx + 1 < len(sys.argv):
        STATE = sys.argv[_idx + 1].lower().strip()

DATA_DIRS = {
    "tripura":   (BASE_DIR / "../data_and_model").resolve(),
    "meghalaya": (BASE_DIR / "../data_and_model_meghalaya").resolve(),
    "rajasthan": (BASE_DIR / "../data_and_model_rajasthan").resolve(),
}
DATA_DIR = DATA_DIRS.get(STATE, DATA_DIRS["tripura"])

ARTEFACTS_FILE = DATA_DIR / "model_artefacts.pkl"
WEATHER_FILE   = DATA_DIR / "merged_crop_enriched_features_del.xlsx"


YIELD_COL = "Yield (Tonne or Bales/Hectare)"

WEATHER_FEATURES = [
    "weather_temp_mean", "weather_rain_total", "weather_rain_days",
    "weather_et0_total", "weather_wind_mean", "weather_solarrad_total",
]

PEST_MAP = {"Low": 0, "Medium": 1, "High": 2}
PEST_MAP_INV = {0: "Low", 1: "Medium", 2: "High"}

import logging
import math

logger = logging.getLogger("crop_backend")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

# ── DEFENSIVE PEST_DISEASE_INCIDENCE HELPERS ─────────────────────────────────
# Pest_Disease_Incidence is the single most inconsistent column across state
# datasets: Tripura/Meghalaya artefacts store it as numeric 0/1/2, while the
# Rajasthan Excel source stores it as free-text "Low"/"Medium"/"High" (with
# occasional case/whitespace variance, blanks, or stray numeric-as-string
# values like "1"). These helpers normalize any of the following into a safe
# numeric code (0/1/2) without ever raising, logging anything unrecognized:
#   - Python int / numpy int
#   - Python float / numpy float (including NaN)
#   - numeric strings ("0", "1.0", "2")
#   - label strings ("Low", "medium", " High ", case/space-insensitive)
#   - None / NaN / pd.NA / empty string
#   - already-categorical dtype columns

_PEST_LABELS_CI = {"low": 0, "medium": 1, "med": 1, "high": 2}


def normalize_pest_value(x, default=1):
    """
    Convert a single Pest_Disease_Incidence cell to an int code (0/1/2).
    Never raises. Falls back to `default` (Medium) for anything unrecognized,
    and logs the offending raw value (once per distinct bad value per call
    site is left to the caller via normalize_pest_series' aggregated log).
    """
    if x is None:
        return default
    try:
        if isinstance(x, float) and math.isnan(x):
            return default
    except (TypeError, ValueError):
        pass
    try:
        if pd.isna(x):
            return default
    except (TypeError, ValueError):
        pass

    # Already a clean int/float code.
    if isinstance(x, (int, np.integer)):
        return int(x) if int(x) in (0, 1, 2) else default
    if isinstance(x, (float, np.floating)):
        xi = int(round(x))
        return xi if xi in (0, 1, 2) else default

    # String (label or numeric-as-string).
    s = str(x).strip().lower()
    if s == "":
        return default
    if s in _PEST_LABELS_CI:
        return _PEST_LABELS_CI[s]
    try:
        xi = int(round(float(s)))
        return xi if xi in (0, 1, 2) else default
    except (TypeError, ValueError):
        return default


def normalize_pest_series(series, default=1, context=""):
    """
    Vectorized, crash-proof normalization of a Pest_Disease_Incidence column.
    Handles numeric dtype, object dtype, pandas 'string' extension dtype,
    and 'category' dtype uniformly by operating on raw Python values.
    Logs a concise summary of any unrecognized raw values instead of
    crashing, so /stats can never 500 on this column again.
    """
    if series is None:
        return series

    raw_values = series.tolist() if hasattr(series, "tolist") else list(series)
    bad_values = []
    out = []
    for v in raw_values:
        code = normalize_pest_value(v, default=default)
        out.append(code)
        # Flag values that weren't a recognized label, numeric code, or null.
        is_known_null = v is None
        try:
            is_known_null = is_known_null or (isinstance(v, float) and math.isnan(v))
        except (TypeError, ValueError):
            pass
        try:
            is_known_null = is_known_null or bool(pd.isna(v))
        except (TypeError, ValueError):
            pass
        if not is_known_null:
            s = str(v).strip().lower()
            if s not in _PEST_LABELS_CI and s not in ("0", "1", "2", "0.0", "1.0", "2.0"):
                bad_values.append(v)

    if bad_values:
        sample = list(dict.fromkeys(bad_values))[:10]
        logger.warning(
            "Pest_Disease_Incidence: %d unrecognized value(s) coerced to default=%s "
            "(%s). Sample raw values: %r",
            len(bad_values), default, context or "unspecified context", sample,
        )

    return pd.Series(out, index=series.index, dtype="int64")

# Importance-derived suitability weights (from final model run)
# Only features the user can provide that differentiate between crops
# Pest and Area excluded — they don't vary per crop in suitability scoring
# Re-normalised to sum to 1.0
SUITABILITY_WEIGHTS = {
    "weather_rain_days":    0.189,
    "Fertilizer_kg_per_ha": 0.140,
    "weather_et0_total":    0.119,
    "weather_temp_mean":    0.098,
    "weather_rain_total":   0.082,
    "weather_solarrad_total": 0.071,
    "weather_wind_mean":    0.054,
}
# Normalise so they sum to 1
_wsum = sum(SUITABILITY_WEIGHTS.values())
SUITABILITY_WEIGHTS = {k: v / _wsum for k, v in SUITABILITY_WEIGHTS.items()}

# ── LOAD ──────────────────────────────────────────────────────────────────────

print(f"\nLoading model artefacts for state={STATE} from {ARTEFACTS_FILE}...")
if not Path(ARTEFACTS_FILE).exists():
    print(f"\nERROR: {ARTEFACTS_FILE} not found.")
    print("Run crop_yield_with_weather.py first to generate it.")
    raise SystemExit(1)

with open(ARTEFACTS_FILE, "rb") as f:
    art = pickle.load(f)

model      = art["model"]
feat_cols  = art["feat_cols"]
scaler     = art["scaler"]
crop_stats = art["crop_stats"]
df_history = art["df_history"]
valid_crops = crop_stats.index.tolist()

print(f"  Model loaded — {len(valid_crops)} crops, {len(feat_cols)} features")
print(f"  Valid crops: {valid_crops}\n")

# ── LOAD FULL DATASET (for stats/correlations — df_history only has weather+yield) ──
print(f"Loading full dataset for stats endpoints from {WEATHER_FILE}...")
_full_df = None
if Path(WEATHER_FILE).exists():
    _full_df = pd.read_excel(WEATHER_FILE)
    _full_df.columns = [str(c).strip() for c in _full_df.columns]

    # Clean labels for state files where Excel values may include serial prefixes
    # such as "1. Rajasthan" or "1. Ajmer". Rajasthan needs this for weather merge.
    def _strip_serial_prefix(v):
        import re
        s = str(v).strip()
        s = re.sub(r"^\s*\d+\s*[.)-]\s*", "", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    for _label_col in ["State_Name", "District_Name", "Season", "Crop", "Crop_Year"]:
        if _label_col in _full_df.columns:
            if _label_col in ["State_Name", "District_Name"]:
                _full_df[_label_col] = _full_df[_label_col].apply(_strip_serial_prefix)
            else:
                _full_df[_label_col] = _full_df[_label_col].astype(str).str.strip()

    # Merge weather from df_history only when needed.
    # If the Excel already contains weather_* columns, keep them and avoid
    # weather_*_x / weather_*_y suffixes that break dashboard stats.
    _dh = df_history.copy()
    _dh.columns = [str(c).strip() for c in _dh.columns]

    if "District_Name" in _dh.columns:
        _dh["District_Name"] = _dh["District_Name"].apply(_strip_serial_prefix)
    if "Crop" in _dh.columns:
        _dh["Crop"] = _dh["Crop"].astype(str).str.strip()

    # backend/dashboard expects Crop_Year. Some artifacts store Year as offset from 2004.
    if "Crop_Year" not in _dh.columns:
        if "Year" in _dh.columns:
            _dh["Crop_Year"] = _dh["Year"].apply(lambda y: f"{int(y)+2004} - {int(y)+2005}")
        else:
            print("  WARNING: df_history has neither Year nor Crop_Year; weather merge may be skipped.")

    _weather_cols = [c for c in _dh.columns if c.startswith("weather_")]
    # merged_crop_enriched_features_del.xlsx (Rajasthan) does not carry
    # Pest_Disease_Incidence at all — it only lives in the model artefact's
    # df_history. Pull it in via the same left-merge as the weather columns
    # so /stats can build the Pest Impact chart instead of returning {}.
    for _extra_col in ["Pest_Disease_Incidence"]:
        if _extra_col in _dh.columns and _extra_col not in _weather_cols:
            _weather_cols.append(_extra_col)

    # If Excel already has duplicate suffix columns from a previous merge, normalize them first.
    for _col in list(_full_df.columns):
        if _col.startswith("weather_") and (_col.endswith("_x") or _col.endswith("_y")):
            _base = _col[:-2]
            if _base not in _full_df.columns:
                _full_df[_base] = _full_df[_col]
            else:
                _full_df[_base] = _full_df[_base].fillna(_full_df[_col])
            _full_df = _full_df.drop(columns=[_col])

    if _weather_cols:
        _merge_keys = ["District_Name", "Crop_Year", "Crop"]
        _can_merge = all(k in _dh.columns for k in _merge_keys) and all(k in _full_df.columns for k in _merge_keys)

        # Merge from artifact only if any required weather column is absent or entirely empty in Excel.
        _needs_merge = False
        for _wc in _weather_cols:
            if _wc not in _full_df.columns or _full_df[_wc].isna().all():
                _needs_merge = True
                break

        if _needs_merge and _can_merge:
            _wx = _dh[_merge_keys + _weather_cols].drop_duplicates(subset=_merge_keys)
            _full_df = _full_df.merge(_wx, on=_merge_keys, how="left", suffixes=("", "_artifact"))

            for _wc in _weather_cols:
                _artifact_col = f"{_wc}_artifact"
                if _wc not in _full_df.columns and _artifact_col in _full_df.columns:
                    _full_df[_wc] = _full_df[_artifact_col]
                elif _wc in _full_df.columns and _artifact_col in _full_df.columns:
                    _full_df[_wc] = _full_df[_wc].fillna(_full_df[_artifact_col])
                if _artifact_col in _full_df.columns:
                    _full_df = _full_df.drop(columns=[_artifact_col])
        elif _needs_merge and not _can_merge:
            print("  WARNING: Weather merge skipped — required keys missing in Excel or artifact.")

    # If there are still no exact weather columns but Excel has suffixed versions, normalize again.
    for _col in list(_full_df.columns):
        if _col.startswith("weather_") and (_col.endswith("_x") or _col.endswith("_y")):
            _base = _col[:-2]
            if _base not in _full_df.columns:
                _full_df[_base] = _full_df[_col]
            else:
                _full_df[_base] = _full_df[_base].fillna(_full_df[_col])
            _full_df = _full_df.drop(columns=[_col])

    # Make sure dashboard does not crash if any optional weather feature is absent.
    for _wc in WEATHER_FEATURES:
        if _wc not in _full_df.columns:
            _full_df[_wc] = np.nan

    # Encode Pest for numeric correlation.
    # NOTE: previously this branched on `_pest_col.dtype == object`, which is
    # NOT a reliable signal across pandas versions/platforms — pandas can hand
    # back a "string" extension dtype, a "category" dtype, or a mixed
    # int/str object column depending on the pandas build (Linux Docker
    # images often resolve a newer/older pandas than a Windows dev machine
    # because requirements.txt had no pinned version). Any of those cases
    # would silently skip the string->code mapping and leave literal
    # "Low"/"Medium"/"High" strings in the column, which later crashed
    # int(x) calls in /stats. Use the value-level normalizer instead, which
    # works regardless of dtype.
    if "Pest_Disease_Incidence" in _full_df.columns:
        _full_df["Pest_Disease_Incidence"] = normalize_pest_series(
            _full_df["Pest_Disease_Incidence"], context="startup full_df load"
        )
    print(f"  Full dataset loaded: {len(_full_df)} rows, cols: {list(_full_df.columns)}\n")
else:
    print(f"  WARNING: {WEATHER_FILE} not found — stats endpoints will use df_history only\n")

# ── BUILD SUITABILITY PROFILES ────────────────────────────────────────────────
# For each crop: historical mean/std of numeric features + categorical
# distributions for Season, Soil_Type, Irrigation_Type, Pest_Disease_Incidence,
# and District_Name. These profiles are used by compute_suitability(), so every
# UI input now has a direct effect on recommendation ranking.

print("Building crop suitability profiles from historical data...")


def _clean_category_value(x):
    """Normalize category values for robust case/space-insensitive matching."""
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    return str(x).strip()


def _category_distribution(series):
    """Return normalized value counts after stripping blanks/nulls."""
    if series is None:
        return {}
    cleaned = series.map(_clean_category_value)
    cleaned = cleaned[cleaned != ""]
    if cleaned.empty:
        return {}
    return cleaned.value_counts(normalize=True).to_dict()


PROFILES = {}

if _full_df is None:
    print(f"  WARNING: {WEATHER_FILE} not found — profiles unavailable")
else:
    df_wx = _full_df.copy()
    df_wx.columns = [str(c).strip() for c in df_wx.columns]

    # Normalize pest labels/codes once before building profiles so frontend
    # values like "Low"/"Medium"/"High" match historical numeric codes 0/1/2.
    if "Pest_Disease_Incidence" in df_wx.columns:
        df_wx["Pest_Disease_Incidence"] = normalize_pest_series(
            df_wx["Pest_Disease_Incidence"], default=1, context="profile_build"
        )

    if "Crop" not in df_wx.columns:
        print("  WARNING: Crop column missing — no suitability profiles built")
    else:
        for crop, grp in df_wx.groupby("Crop"):
            p = {}

            # Numeric profiles for all user-controlled weighted inputs.
            for feat in SUITABILITY_WEIGHTS:
                if feat in grp.columns:
                    vals = pd.to_numeric(grp[feat], errors="coerce").dropna()
                    if len(vals) > 0:
                        std = float(vals.std())
                        if not np.isfinite(std) or std <= 0:
                            std = 1.0
                        p[feat] = {"mean": float(vals.mean()), "std": std + 1e-3}

            # Average historical yield for anomaly / fallback predictions.
            if YIELD_COL in grp.columns:
                y = pd.to_numeric(grp[YIELD_COL], errors="coerce").dropna()
                p["avg_yield"] = float(y.mean()) if len(y) else 0.0
            else:
                p["avg_yield"] = 0.0

            # Categorical profiles. District can be named District_Name in the
            # dataset/model, while the frontend sends key "district".
            for cat in ["Season", "Soil_Type", "Irrigation_Type", "Pest_Disease_Incidence", "District_Name", "District"]:
                if cat in grp.columns:
                    p[cat] = _category_distribution(grp[cat])

            # If only District exists, expose it under District_Name as well so
            # compute_suitability has one canonical lookup key.
            if "District_Name" not in p and "District" in p:
                p["District_Name"] = p["District"]

            PROFILES[crop] = p

print(f"  Profiles built for {len(PROFILES)} crops\n")

# ── HELPERS ───────────────────────────────────────────────────────────────────

def gaussian(val, mean, std):
    return float(np.exp(-0.5 * ((val - mean) / std) ** 2))


def predict_yield_for_crop(crop, district, user_inputs):
    """
    Run XGBoost prediction for a specific crop.
    Returns (predicted_yield, source) where source is 'model' or 'hist_avg'.

    Real feat_cols (from get_dummies on training data, drop_first=True):
      Categorical: Crop_*, District_Name_*, Irrigation_Type_Drip,
                   Irrigation_Type_Rainfed, Soil_Type_Red Laterite
      Numeric:     Area (Hectare), Fertilizer_kg_per_ha,
                   Pest_Disease_Incidence (0/1/2),
                   Yield_Lag1, Yield_Roll3, Yield_Trend,
                   weather_temp_mean, weather_rain_total, weather_rain_days,
                   weather_et0_total, weather_solarrad_total
      NOTE: Season is dropped before training — NOT a model feature.
            Soil baseline is Alluvial (drop_first), Red Laterite is encoded.
            Irrigation baseline is Canal (drop_first), Drip/Rainfed encoded.
    """
    if crop not in valid_crops:
        avg = PROFILES.get(crop, {}).get("avg_yield", 1.0)
        return avg, "hist_avg"

    mu  = crop_stats.loc[crop, "crop_mean"]
    std = crop_stats.loc[crop, "crop_std"]

    row = {
        "District_Name":          district,
        "Crop":                   crop,
        "Soil_Type":              user_inputs.get("Soil_Type", "Alluvial"),
        "Irrigation_Type":        user_inputs.get("Irrigation_Type", "Canal"),
        "Area (Hectare)":         user_inputs.get("Area (Hectare)", 500),
        "Fertilizer_kg_per_ha":   user_inputs.get("Fertilizer_kg_per_ha", 70),
        "Pest_Disease_Incidence": normalize_pest_value(
            user_inputs.get("Pest_Disease_Incidence", "Low"), default=0
        ),
        "Yield_Lag1":  0.0,
        "Yield_Roll3": 0.0,
        "Yield_Trend": 0.0,
        **{feat: user_inputs.get(feat, 0.0) for feat in WEATHER_FEATURES},
    }

    X = pd.get_dummies(pd.DataFrame([row]), drop_first=True)
    X = X.reindex(columns=feat_cols, fill_value=0)
    X_sc = scaler.transform(X)

    norm_pred  = model.predict(X_sc)[0]
    pred_yield = float(norm_pred * std + mu)
    return pred_yield, "model"


def _categorical_fit(profile, key, user_value, default=0.0):
    """
    Return the historical frequency of user_value for this crop/profile.
    Matching is exact first, then case-insensitive. Missing values return default.
    """
    if user_value is None:
        return default
    dist = profile.get(key, {}) or {}
    if not dist:
        return default

    uv = _clean_category_value(user_value)
    if uv == "":
        return default

    if uv in dist:
        return float(dist[uv])

    uv_lower = uv.lower()
    for k, v in dist.items():
        if _clean_category_value(k).lower() == uv_lower:
            return float(v)

    return default


def compute_suitability(crop, user_inputs):
    """
    Returns (score, season_fit), where score is 0-1.

    The ranking now uses every recommender UI input:
      - Weather + fertilizer numeric profile match
      - Season
      - Soil_Type
      - Irrigation_Type
      - Pest_Disease_Incidence
      - District / District_Name
    """
    p = PROFILES.get(crop, {})
    if not p:
        return 0.0, 0.0

    # Numeric match: Gaussian similarity to the crop's historical conditions.
    num, wsum = 0.0, 0.0
    for feat, wt in SUITABILITY_WEIGHTS.items():
        if feat not in p or feat not in user_inputs:
            continue
        try:
            val = float(user_inputs.get(feat))
        except (TypeError, ValueError):
            continue
        mean = p[feat].get("mean", 0.0)
        std = p[feat].get("std", 1.0) or 1.0
        num += wt * gaussian(val, mean, std)
        wsum += wt
    numeric_fit = num / wsum if wsum > 0 else 0.0

    # Categorical matches: historical frequency for the selected category.
    season_fit = _categorical_fit(p, "Season", user_inputs.get("Season", "Kharif"), default=0.0)
    soil_fit = _categorical_fit(p, "Soil_Type", user_inputs.get("Soil_Type"), default=0.0)
    irrigation_fit = _categorical_fit(p, "Irrigation_Type", user_inputs.get("Irrigation_Type"), default=0.0)

    pest_raw = user_inputs.get("Pest_Disease_Incidence")
    pest_code = normalize_pest_value(pest_raw, default=1)
    pest_fit = _categorical_fit(p, "Pest_Disease_Incidence", pest_code, default=0.0)

    # Frontend sends "district"; model/data commonly use "District_Name".
    district_value = user_inputs.get("district") or user_inputs.get("District_Name") or user_inputs.get("District")
    district_fit = _categorical_fit(p, "District_Name", district_value, default=0.0)

    # Final blend. Numeric factors remain the largest signal, but district,
    # soil, irrigation, pest, and season now materially change the ranking.
    combined = (
        0.40 * numeric_fit
        + 0.20 * season_fit
        + 0.15 * soil_fit
        + 0.10 * irrigation_fit
        + 0.10 * district_fit
        + 0.05 * pest_fit
    )

    return float(combined), float(season_fit)


def safe_int(x, default=1):
    """int(x) that never raises — used anywhere a groupby key or dict key
    might unexpectedly still be a string/NaN despite upstream normalization."""
    try:
        if x is None:
            return default
        if isinstance(x, float) and math.isnan(x):
            return default
        return int(x)
    except (TypeError, ValueError):
        logger.warning("safe_int: could not coerce value %r to int, using default=%s", x, default)
        return default


def require_columns(df, required, endpoint=""):
    """
    Defensive validation for dashboard/stats endpoints. Returns (ok, missing).
    Does not raise — callers should use this to short-circuit with a clean
    JSON error/partial-data response instead of crashing.
    """
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning("%s: missing expected column(s) %s", endpoint or "endpoint", missing)
    return (len(missing) == 0), missing


def clean_json(obj):
    """Convert NaN/Infinity from pandas/numpy into JSON-safe None values."""
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [clean_json(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if not np.isfinite(v) else v
    return obj

# ── FLASK ─────────────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    static_folder=str((BASE_DIR / "../frontend").resolve()),
    static_url_path="",
)
CORS(app)

# ── FRONTEND DIRS (Windows-safe, resolved relative to this file) ──────────────
FRONTEND_DIR = (BASE_DIR / "../frontend").resolve()
HTML_DIR = FRONTEND_DIR / "html"
CSS_DIR  = FRONTEND_DIR / "css"
JS_DIR   = FRONTEND_DIR / "js"


# ── FRONTEND PAGE ROUTES ───────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
@app.route("/dashboard", methods=["GET"])
def serve_dashboard():
    return send_from_directory(str(HTML_DIR), "crop_dashboard.html")


@app.route("/alerts", methods=["GET"])
def serve_alerts():
    return send_from_directory(str(HTML_DIR), "alert_dashboard.html")


@app.route("/predictions.json", methods=["GET"])
def serve_predictions():
    """
    Serves predictions.json from the currently active state's data folder
    (DATA_DIR), e.g. data_and_model_rajasthan/predictions.json.
    The alert dashboard fetches this relative to /alerts.
    """
    predictions_file = DATA_DIR / "predictions.json"
    if not predictions_file.exists():
        return jsonify({
            "error": f"predictions.json not found in {DATA_DIR}. "
                     "Run generate_alerts.py first."
        }), 404
    return send_from_directory(str(DATA_DIR), "predictions.json")


@app.route("/recommend-page", methods=["GET"])
def serve_recommend_page():
    return send_from_directory(str(HTML_DIR), "crop_recommender.html")


@app.route("/irrigation", methods=["GET"])
def serve_irrigation():
    return send_from_directory(str(HTML_DIR), "irrigation_advisory1.html")


# ── STATIC ASSET ROUTES (CSS / JS) ─────────────────────────────────────────────

@app.route("/css/<path:filename>", methods=["GET"])
def serve_css(filename):
    return send_from_directory(str(CSS_DIR), filename)


@app.route("/js/<path:filename>", methods=["GET"])
def serve_js(filename):
    return send_from_directory(str(JS_DIR), filename)


@app.route("/api/crop/health", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "crops": len(valid_crops), "features": len(feat_cols)})


@app.route("/crop_trends", methods=["GET"])
def crop_trends():
    """
    Returns real year-by-year median yield for each crop from df_history.
    Query params:
      crop (str): specific crop, or omit for all crops
    Response:
    {
      "years": [2004, 2005, ...],
      "crops": {
        "Rice": [1.82, 1.85, ...],
        "Jute": [8.1, 8.2, ...],
        ...
      },
      "overall": [2.58, 2.63, ...],
      "decade": {
        "Rice": {"early": 1.95, "recent": 2.24},
        ...
      }
    }
    """
    crop_filter = request.args.get("crop", None)

    # df_history Year is 0-based offset from min year (2004)
    # Reconstruct actual years
    min_year = 2004
    df = df_history.copy()
    df["actual_year"] = df["Year"] + min_year

    years_available = sorted(df["actual_year"].unique().tolist())

    result_crops = {}
    decade_result = {}

    crops_to_process = [crop_filter] if crop_filter else df["Crop"].unique().tolist()

    for crop in crops_to_process:
        crop_df = df[df["Crop"] == crop]
        if crop_df.empty:
            continue

        # Year-by-year median yield across all districts
        yearly = (
            crop_df.groupby("actual_year")[YIELD_COL]
            .median()
            .reindex(years_available)
        )
        # Forward-fill missing years with interpolation
        yearly = yearly.interpolate(method="linear").bfill().ffill()
        result_crops[crop] = [round(float(v), 3) for v in yearly.values]

        # Decade comparison
        early_mask  = (crop_df["actual_year"] >= 2004) & (crop_df["actual_year"] <= 2013)
        recent_mask = (crop_df["actual_year"] >= 2014) & (crop_df["actual_year"] <= 2023)
        early_avg  = float(crop_df.loc[early_mask,  YIELD_COL].median()) if early_mask.any()  else 0
        recent_avg = float(crop_df.loc[recent_mask, YIELD_COL].median()) if recent_mask.any() else 0
        decade_result[crop] = {
            "early":  round(early_avg,  3),
            "recent": round(recent_avg, 3),
            "change_pct": round((recent_avg - early_avg) / early_avg * 100, 1) if early_avg > 0 else 0,
        }

    # Overall median across all crops per year
    overall_yearly = (
        df.groupby("actual_year")[YIELD_COL]
        .median()
        .reindex(years_available)
        .interpolate(method="linear")
        .bfill()
    )

    return jsonify({
        "years":   years_available,
        "crops":   result_crops,
        "overall": [round(float(v), 3) for v in overall_yearly.values],
        "decade":  decade_result,
    })


@app.route("/api/crop/predict", methods=["POST"])
@app.route("/predict", methods=["POST"])
def predict():
    """Single crop prediction. Used by the what-if panel."""
    data     = request.get_json()
    crop     = data.get("crop", "")
    district = data.get("district", "Dhalai")

    pred, source = predict_yield_for_crop(crop, district, data)

    # Compute anomaly vs crop historical normal
    normal = crop_stats.loc[crop, "crop_mean"] if crop in valid_crops else pred
    anomaly = round((pred - normal) / normal * 100, 1) if normal > 0 else 0.0

    return jsonify({
        "yield":   round(pred, 3),
        "normal":  round(normal, 3),
        "anomaly": anomaly,
        "source":  source,
    })


@app.route("/api/crop/recommend", methods=["POST"])
@app.route("/recommend", methods=["POST"])
def recommend():
    """
    Full recommendation run across all crops.
    Ranks by suitability score, returns top N with predicted yield + anomaly.

    Expects JSON:
    {
      "district":                "Dhalai",
      "Season":                  "Kharif",
      "Soil_Type":               "Alluvial",
      "Irrigation_Type":         "Rainfed",
      "Fertilizer_kg_per_ha":    70,
      "Area (Hectare)":          500,
      "Pest_Disease_Incidence":  "Low",
      "weather_rain_days":       170,
      "weather_rain_total":      1800,
      "weather_temp_mean":       24.5,
      "weather_et0_total":       1240,
      "weather_wind_mean":       11.2,
      "weather_solarrad_total":  5800,
      "top_n":                   7
    }
    """
    data     = request.get_json()
    district = data.get("district", "Dhalai")
    top_n    = int(data.get("top_n", 7))

    # Score all crops
    results = []
    all_crops = list(PROFILES.keys())

    for crop in all_crops:
        suit, season_fit = compute_suitability(crop, data)
        pred, source     = predict_yield_for_crop(crop, district, data)

        # Normal yield for anomaly
        if crop in valid_crops:
            normal = float(crop_stats.loc[crop, "crop_mean"])
        else:
            normal = PROFILES.get(crop, {}).get("avg_yield", pred)

        anomaly = round((pred - normal) / normal * 100, 1) if normal > 0 else 0.0

        results.append({
            "crop":        crop,
            "suit_score":  round(suit, 4),
            "season_fit":  round(season_fit, 3),
            "predicted":   round(pred, 3),
            "normal":      round(normal, 3),
            "anomaly":     anomaly,
            "source":      source,
        })

    # Sort by suitability descending
    results.sort(key=lambda x: x["suit_score"], reverse=True)

    # Normalise suitability to percentage
    max_s = results[0]["suit_score"] if results else 1.0
    for r in results:
        r["suit_pct"] = round(r["suit_score"] / max_s * 100) if max_s > 0 else 0

    return jsonify({
        "district":    district,
        "season":      data.get("Season", ""),
        "results":     results[:top_n],
        "weights_used": SUITABILITY_WEIGHTS,
    })


@app.route("/api/crop/valid_crops", methods=["GET"])
@app.route("/valid_crops", methods=["GET"])
def get_valid_crops():
    return jsonify({"valid_crops": valid_crops})


@app.route("/model_info", methods=["GET"])
def model_info():
    """
    Returns feature importances from the XGBoost model and
    Pearson correlations of numeric features with yield in df_history.
    """
    # ── Feature importances ────────────────────────────────────────────────
    # Use sklearn's feature_importances_ (mean gain across ALL trees, covers
    # every feature including those with zero splits — no silent omissions
    # like get_score() which skips features never chosen for a split).
    raw_arr = model.feature_importances_          # shape: (n_features,)
    total   = raw_arr.sum() or 1.0

    # Map each column to its raw importance
    col_imp = {col: float(raw_arr[i] / total) for i, col in enumerate(feat_cols)}

    # ── Group dummy-encoded columns into logical feature buckets ──────────
    GROUP_PREFIXES = [
        ("Crop_",             "Crop"),
        ("District_Name_",    "District"),
        ("Soil_Type_",        "Soil_Type"),
        ("Irrigation_Type_",  "Irrigation_Type"),
    ]

    grouped: dict = {}
    for col, imp in col_imp.items():
        label = col  # default: keep as-is
        for prefix, group_name in GROUP_PREFIXES:
            if col.startswith(prefix):
                label = group_name
                break
        grouped[label] = grouped.get(label, 0.0) + imp

    # Re-normalise after grouping so values still sum to 1
    g_total = sum(grouped.values()) or 1.0
    feat_imps_sorted = dict(
        sorted({k: round(v / g_total, 6) for k, v in grouped.items()}.items(),
               key=lambda x: -x[1])
    )

    # Pearson correlations with yield — use full dataset so Season/Soil/Irrigation/Pest/Fert are available
    corr_df = _full_df.copy() if _full_df is not None else df_history.copy()
    if "Pest_Disease_Incidence" in corr_df.columns:
        # Defensive re-normalization: this endpoint may run against a
        # dataframe that was loaded/merged differently than /stats' df, so
        # never assume this column is already clean numeric.
        corr_df["Pest_Disease_Incidence"] = normalize_pest_series(
            corr_df["Pest_Disease_Incidence"], context="/model_info correlations"
        )
    numeric_cols = [
        "Fertilizer_kg_per_ha", "Area (Hectare)",
        "weather_rain_total", "weather_rain_days",
        "weather_temp_mean", "weather_et0_total",
        "weather_wind_mean", "weather_solarrad_total",
        "Pest_Disease_Incidence",
    ]
    corr_result = {}
    for col in numeric_cols:
        if col in corr_df.columns:
            sub = corr_df[[col, YIELD_COL]].dropna()
            if len(sub) > 10:
                r = sub.corr().iloc[0, 1]
                corr_result[col] = round(float(r), 4)

    # Categorical correlations — encode then correlate
    for cat, label in [("Irrigation_Type", "Irrigation_Type"), ("Soil_Type", "Soil_Type"), ("Season", "Season")]:
        if cat in corr_df.columns:
            encoded = corr_df[cat].astype("category").cat.codes
            sub = pd.concat([encoded, corr_df[YIELD_COL]], axis=1).dropna()
            if len(sub) > 10:
                r = sub.corr().iloc[0, 1]
                corr_result[label] = round(float(r), 4)

    return jsonify({
        "feat_importances": feat_imps_sorted,
        "correlations": corr_result,
        "n_features": len(feat_cols),
    })


@app.route("/stats", methods=["GET"])
def stats():
    """
    Thin route wrapper — final safety net. Requirement: /stats must never
    return HTTP 500 due to malformed data. Any exception that slips past the
    per-column defensive checks in _compute_stats() is caught here, logged
    with the offending state/columns for diagnosis, and turned into a clean
    4xx JSON response instead of an unhandled 500.
    """
    try:
        return _compute_stats()
    except Exception as e:
        logger.exception("/stats: unhandled error computing stats for state=%s", STATE)
        return jsonify({
            "error": "failed to compute stats",
            "detail": str(e),
            "state": STATE,
        }), 500


def _compute_stats():
    """
    Computes EDA statistics from df_history for the dashboard.
    All chart data that was previously hardcoded in the frontend.
    """
    df = _full_df.copy() if _full_df is not None else df_history.copy()

    # ── defensive validation ──────────────────────────────────────────────
    # /stats must never 500 due to malformed/missing data. Guard the two
    # columns every downstream block assumes exist ("Crop", YIELD_COL); for
    # everything else (Season, Soil_Type, Pest_Disease_Incidence, ...) each
    # block below already checks column presence individually.
    if df is None or len(df) == 0:
        logger.warning("/stats: dataframe is empty for state=%s", STATE)
        return jsonify(clean_json({
            "summary": {"n_records": 0, "n_crops": 0, "n_seasons": 0,
                        "n_districts": 0, "avg_yield": None,
                        "avg_rainfall": None, "avg_temp": None},
            "crop_freq": {}, "crop_yield_med": {}, "season_counts": {},
            "season_yields": {}, "soil_yield": {}, "irr_yield": {},
            "pest_yield": {}, "pest_crop_dist": {}, "fert_usage": {},
            "rainfall_bins": {"labels": [], "yields": []},
            "rain_scatter": [], "temp_scatter": [], "et0_scatter": [],
            "fert_scatter": [], "crop_season": {}, "soil_x_irr": {},
        }))

    ok, missing = require_columns(df, ["Crop", YIELD_COL], endpoint="/stats")
    if not ok:
        logger.error("/stats: cannot compute stats, missing required column(s) %s", missing)
        return jsonify({"error": "dataset is missing required column(s)", "missing": missing}), 422

    # ── crop frequency (record count per crop) ──
    crop_freq = df["Crop"].value_counts().to_dict()

    # ── crop median yield ──
    crop_yield_med = df.groupby("Crop")[YIELD_COL].median().to_dict()

    # ── season distribution (% of records) ──
    if "Season" in df.columns:
        season_counts = (df["Season"].value_counts(normalize=True) * 100).round(2).to_dict()
        season_yields = df.groupby("Season")[YIELD_COL].median().to_dict()
    else:
        logger.warning("/stats: 'Season' column missing, skipping season charts")
        season_counts = {}
        season_yields = {}

    # ── soil yield ──
    soil_yield = df.groupby("Soil_Type")[YIELD_COL].median().to_dict() if "Soil_Type" in df.columns else {}

    # ── irrigation yield ──
    irr_yield = df.groupby("Irrigation_Type")[YIELD_COL].median().to_dict() if "Irrigation_Type" in df.columns else {}

    # ── pest yield ──
    # Defensive: normalize whatever is in this column (numeric codes, string
    # labels, mixed types, NaN) to a clean 0/1/2 int column *before*
    # grouping, so the groupby index is guaranteed to be int and the
    # int(x) lookup below can never see a stray "High"/"Low"/"Medium"
    # string or crash the endpoint.
    if "Pest_Disease_Incidence" in df.columns:
        df["Pest_Disease_Incidence"] = normalize_pest_series(
            df["Pest_Disease_Incidence"], context="/stats pest_yield"
        )
        pest_yield = (
            df.groupby("Pest_Disease_Incidence")[YIELD_COL]
            .median()
            .rename(index=lambda x: PEST_MAP_INV.get(safe_int(x), str(x)))
            .to_dict()
        )
    else:
        pest_yield = {}

    # ── fertilizer usage per crop ──
    fert_usage = {}
    if "Fertilizer_kg_per_ha" in df.columns:
        fert_usage = df.groupby("Crop")["Fertilizer_kg_per_ha"].median().to_dict()

    # ── binned weather vs yield ──
    def bin_yield(col, bins, labels):
        if col not in df.columns:
            return {"labels": labels, "yields": []}
        sub = df[[col, YIELD_COL]].dropna().copy()
        sub["bin"] = pd.cut(sub[col], bins=bins, labels=labels, right=False)
        result = sub.groupby("bin")[YIELD_COL].median()
        return {
            "labels": labels,
            "yields": [0 if pd.isna(result.get(l, np.nan)) else round(float(result.get(l)), 3) for l in labels],
        }

    def scatter_col(x_col, max_pts=800):
        """Return raw {x, y} scatter points for a column vs yield, downsampled if large."""
        if x_col not in df.columns:
            return []
        pts = df[[x_col, YIELD_COL]].dropna()
        if len(pts) > max_pts:
            pts = pts.sample(max_pts, random_state=42)
        pts = pts.sort_values(x_col)
        return [{"x": round(float(r[x_col]), 3), "y": round(float(r[YIELD_COL]), 3)}
                for _, r in pts.iterrows()]

    rainfall_bins = bin_yield(
        "weather_rain_total",
        [0, 100, 150, 200, 250, 300, 400, 9999],
        ["0–100", "100–150", "150–200", "200–250", "250–300", "300–400", "400+"],
    )
    rain_scatter = scatter_col("weather_rain_total")
    temp_scatter = scatter_col("weather_temp_mean")
    et0_scatter  = scatter_col("weather_et0_total")
    fert_scatter = scatter_col("Fertilizer_kg_per_ha")

    # ── crop × season yield table ──
    crop_season = {}
    if "Season" in df.columns:
        for (crop, season), grp in df.groupby(["Crop", "Season"]):
            if crop not in crop_season:
                crop_season[crop] = {}
            crop_season[crop][season] = round(float(grp[YIELD_COL].median()), 3)

    # ── soil × irrigation matrix ──
    sxi = {}
    if "Soil_Type" in df.columns and "Irrigation_Type" in df.columns:
        for (s, ir), grp in df.groupby(["Soil_Type", "Irrigation_Type"]):
            if s not in sxi:
                sxi[s] = {}
            sxi[s][ir] = round(float(grp[YIELD_COL].median()), 3)

    # ── pest distribution per crop (%) — for stacked bar chart ──
    pest_crop_dist = {}
    if "Pest_Disease_Incidence" in df.columns and "Crop" in df.columns:
        # Column was already normalized to int 0/1/2 above; safe_int is a
        # last-resort guard in case this block ever runs on an un-normalized
        # copy of df in the future.
        for crop, grp in df.groupby("Crop"):
            counts = grp["Pest_Disease_Incidence"].value_counts(normalize=True) * 100
            pest_crop_dist[crop] = {
                PEST_MAP_INV.get(safe_int(k), str(k)): round(float(v), 1)
                for k, v in counts.items()
            }

    # ── summary stats for the overview strip ──
    n_records = int(len(df))
    n_crops = int(df["Crop"].nunique())
    n_seasons = int(df["Season"].nunique()) if "Season" in df.columns else 6
    n_districts = int(df["District_Name"].nunique()) if "District_Name" in df.columns else 8
    avg_yield = round(float(df[YIELD_COL].median()), 3)
    avg_rainfall = round(float(df["weather_rain_total"].median()), 1) if "weather_rain_total" in df.columns else None
    avg_temp = round(float(df["weather_temp_mean"].median()), 1) if "weather_temp_mean" in df.columns else None

    payload = {
        "summary": {
            "n_records": n_records,
            "n_crops": n_crops,
            "n_seasons": n_seasons,
            "n_districts": n_districts,
            "avg_yield": avg_yield,
            "avg_rainfall": avg_rainfall,
            "avg_temp": avg_temp,
        },
        "crop_freq": crop_freq,
        "crop_yield_med": {k: round(float(v), 3) for k, v in crop_yield_med.items()},
        "season_counts": {k: round(float(v), 2) for k, v in season_counts.items()},
        "season_yields": {k: round(float(v), 3) for k, v in season_yields.items()},
        "soil_yield": {k: round(float(v), 3) for k, v in soil_yield.items()},
        "irr_yield": {k: round(float(v), 3) for k, v in irr_yield.items()},
        "pest_yield": {k: round(float(v), 3) for k, v in pest_yield.items()},
        "pest_crop_dist": pest_crop_dist,
        "fert_usage": {k: round(float(v), 1) for k, v in fert_usage.items()},
        "rainfall_bins": rainfall_bins,
        "rain_scatter":  rain_scatter,
        "temp_scatter":  temp_scatter,
        "et0_scatter":   et0_scatter,
        "fert_scatter":  fert_scatter,
        "crop_season": crop_season,
        "soil_x_irr": sxi,
    }
    return jsonify(clean_json(payload))


@app.route("/stats/crop_scatter", methods=["GET"])
def crop_scatter():
    """
    Returns per-crop scatter points for rainfall vs yield and fertilizer vs yield.
    Used by the Conditional Yield Explorer to draw data-backed scatter + LOESS trendlines.
    Query param: crop (required)
    """
    crop = request.args.get("crop", "")
    if not crop:
        return jsonify({"error": "crop param required"}), 400

    src = _full_df if _full_df is not None else df_history
    sub = src[src["Crop"] == crop] if "Crop" in src.columns else src

    def scatter_pts(col):
        if col not in sub.columns:
            return []
        pts = sub[[col, YIELD_COL]].dropna()
        return [{"x": round(float(r[col]), 2), "y": round(float(r[YIELD_COL]), 3)}
                for _, r in pts.iterrows()]

    return jsonify({
        "crop": crop,
        "rain_scatter":  scatter_pts("weather_rain_total"),
        "fert_scatter":  scatter_pts("Fertilizer_kg_per_ha"),
        "temp_scatter":  scatter_pts("weather_temp_mean"),
        "et0_scatter":   scatter_pts("weather_et0_total"),
    })


@app.route("/api/crop/profiles", methods=["GET"])
@app.route("/profiles", methods=["GET"])
def get_profiles():
    """Expose profiles so the HTML can show crop-specific info."""
    safe = {}
    for crop, p in PROFILES.items():
        safe[crop] = {
            "avg_yield":      p.get("avg_yield", 0),
            "Season":         p.get("Season", {}),
            "Soil_Type":      p.get("Soil_Type", {}),
            "Irrigation_Type": p.get("Irrigation_Type", {}),
        }
    return jsonify(safe)


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CropAI Crop Backend")
    parser.add_argument("--port",  type=int, default=5000,
                        help="Port to run on (default 5000 for Tripura, 5002 for Meghalaya)")
    parser.add_argument("--state", type=str, default="tripura",
                        help="State to pre-load on startup (tripura / meghalaya / rajasthan)")
    args = parser.parse_args()

    # NOTE: previously this called `_load_state(args.state)`, a function that
    # was never defined anywhere in this module — every startup logged
    # "Could not pre-load '<state>': name '_load_state' is not defined".
    # This was harmless for functionality: the module-level load block above
    # (STATE/DATA_DIR/ARTEFACTS_FILE/model/_full_df) already runs at import
    # time, driven by the same `--state` CLI arg parsed at the top of this
    # file, so by the time argparse runs here the correct state's artefacts
    # and dataframe are already loaded. The removed call was dead code from
    # an earlier refactor (state used to be lazily loaded per-request).
    # Kept as a no-op confirmation so startup output still shows what state
    # is live, without a bogus warning every time.
    if args.state.lower().strip() != STATE:
        print(
            f"WARNING: --state={args.state!r} does not match the module-level "
            f"STATE={STATE!r} resolved from sys.argv at import time. This can "
            f"happen if backend_2.py is launched with an unexpected argv order. "
            f"The already-loaded artefacts are for state={STATE!r}."
        )
    else:
        print(f"State '{STATE}' artefacts already loaded at import time.")

    print("=" * 55)
    print(f"  CROP BACKEND — state={args.state}")
    print(f"  Running at http://localhost:{args.port}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=args.port, debug=False)