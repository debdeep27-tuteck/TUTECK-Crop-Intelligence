"""
Crop Recommender — Standalone Microservice
============================================
Owns all ML inference: single-crop yield prediction, suitability-ranked
crop recommendations, model introspection, and the crop/district lookup
lists the recommender UI needs.

This is a SEPARATE PROCESS from backend_2.py (the dashboard/stats
service). It does not import anything from backend_2.py and vice versa —
each service loads its own copy of the model artefacts and weather
dataset independently. That duplication is intentional: it's what makes
this a real microservice (independently deployable/restartable/scalable)
rather than just a second module of the same app.

Run:
    pip install flask flask-cors pandas xgboost scikit-learn openpyxl
    python crop_recommender_service.py --state tripura --port 5001

Port convention (backend port + 1, per state):
    Tripura   -> python backend_2.py --state tripura   --port 5000
                 python crop_recommender_service.py --state tripura   --port 5001
    Meghalaya -> python backend_2.py --state meghalaya --port 5002
                 python crop_recommender_service.py --state meghalaya --port 5003
    Rajasthan -> python backend_2.py --state rajasthan --port 5004
                 python crop_recommender_service.py --state rajasthan --port 5005

IMPORTANT — infra change required:
    crop_recommender.html currently calls /predict, /recommend,
    /valid_crops, /valid_districts, /model_info, /profiles as relative
    paths against backend_2.py's port. Since those routes now live on a
    different port, main.py/gateway.py (or the frontend fetch calls)
    must be updated to point those specific endpoints at this service's
    port instead. Every other route stays on backend_2.py unchanged.
"""

import math
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent

# ── STATE-AWARE DATA PATHS ───────────────────────────────────────────────────
import sys
STATE = "tripura"
if "--state" in sys.argv:
    _idx = sys.argv.index("--state")
    if _idx + 1 < len(sys.argv):
        STATE = sys.argv[_idx + 1].lower().strip()

DATA_DIRS = {
    "tripura":   (BASE_DIR / "../../data_and_model").resolve(),
    "meghalaya": (BASE_DIR / "../../data_and_model_meghalaya").resolve(),
    "rajasthan": (BASE_DIR / "../../data_and_model_rajasthan").resolve(),
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

logger = logging.getLogger("crop_recommender_service")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

# ── DEFENSIVE PEST_DISEASE_INCIDENCE HELPERS ─────────────────────────────────
# Duplicated from backend_2.py verbatim. This service and backend_2.py each
# need this independently — it's not shared via import because they're
# separate processes/deployables. If you change this logic, change it in
# BOTH files.

_PEST_LABELS_CI = {"low": 0, "medium": 1, "med": 1, "high": 2}


def normalize_pest_value(x, default=1):
    """Convert a single Pest_Disease_Incidence cell to an int code (0/1/2). Never raises."""
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

    if isinstance(x, (int, np.integer)):
        return int(x) if int(x) in (0, 1, 2) else default
    if isinstance(x, (float, np.floating)):
        xi = int(round(x))
        return xi if xi in (0, 1, 2) else default

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
    """Vectorized, crash-proof normalization of a Pest_Disease_Incidence column."""
    if series is None:
        return series

    raw_values = series.tolist() if hasattr(series, "tolist") else list(series)
    bad_values = []
    out = []
    for v in raw_values:
        code = normalize_pest_value(v, default=default)
        out.append(code)
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
SUITABILITY_WEIGHTS = {
    "weather_rain_days":    0.189,
    "Fertilizer_kg_per_ha": 0.140,
    "weather_et0_total":    0.119,
    "weather_temp_mean":    0.098,
    "weather_rain_total":   0.082,
    "weather_solarrad_total": 0.071,
    "weather_wind_mean":    0.054,
}
_wsum = sum(SUITABILITY_WEIGHTS.values())
SUITABILITY_WEIGHTS = {k: v / _wsum for k, v in SUITABILITY_WEIGHTS.items()}

# ── LOAD MODEL ARTEFACTS ───────────────────────────────────────────────────────

print(f"\n[recommender] Loading model artefacts for state={STATE} from {ARTEFACTS_FILE}...")
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

# ── LOAD FULL DATASET (needed for climatology + suitability profiles) ────────

print(f"[recommender] Loading full dataset from {WEATHER_FILE}...")
_full_df = None
if Path(WEATHER_FILE).exists():
    _full_df = pd.read_excel(WEATHER_FILE)
    _full_df.columns = [str(c).strip() for c in _full_df.columns]

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

    _dh = df_history.copy()
    _dh.columns = [str(c).strip() for c in _dh.columns]

    if "District_Name" in _dh.columns:
        _dh["District_Name"] = _dh["District_Name"].apply(_strip_serial_prefix)
    if "Crop" in _dh.columns:
        _dh["Crop"] = _dh["Crop"].astype(str).str.strip()

    if "Crop_Year" not in _dh.columns:
        if "Year" in _dh.columns:
            _dh["Crop_Year"] = _dh["Year"].apply(lambda y: f"{int(y)+2004} - {int(y)+2005}")
        else:
            print("  WARNING: df_history has neither Year nor Crop_Year; weather merge may be skipped.")

    _weather_cols = [c for c in _dh.columns if c.startswith("weather_")]
    for _extra_col in ["Pest_Disease_Incidence"]:
        if _extra_col in _dh.columns and _extra_col not in _weather_cols:
            _weather_cols.append(_extra_col)

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

    for _col in list(_full_df.columns):
        if _col.startswith("weather_") and (_col.endswith("_x") or _col.endswith("_y")):
            _base = _col[:-2]
            if _base not in _full_df.columns:
                _full_df[_base] = _full_df[_col]
            else:
                _full_df[_base] = _full_df[_base].fillna(_full_df[_col])
            _full_df = _full_df.drop(columns=[_col])

    for _wc in WEATHER_FEATURES:
        if _wc not in _full_df.columns:
            _full_df[_wc] = np.nan

    if "Pest_Disease_Incidence" in _full_df.columns:
        _full_df["Pest_Disease_Incidence"] = normalize_pest_series(
            _full_df["Pest_Disease_Incidence"], context="startup full_df load"
        )
    print(f"  Full dataset loaded: {len(_full_df)} rows, cols: {list(_full_df.columns)}\n")
else:
    print(f"  WARNING: {WEATHER_FILE} not found — climatology/profiles will be limited\n")

# ── BUILD SUITABILITY PROFILES ────────────────────────────────────────────────

print("[recommender] Building crop suitability profiles from historical data...")


def _clean_category_value(x):
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    return str(x).strip()


def _category_distribution(series):
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

    if "Pest_Disease_Incidence" in df_wx.columns:
        df_wx["Pest_Disease_Incidence"] = normalize_pest_series(
            df_wx["Pest_Disease_Incidence"], default=1, context="profile_build"
        )

    if "Crop" not in df_wx.columns:
        print("  WARNING: Crop column missing — no suitability profiles built")
    else:
        for crop, grp in df_wx.groupby("Crop"):
            p = {}

            for feat in SUITABILITY_WEIGHTS:
                if feat in grp.columns:
                    vals = pd.to_numeric(grp[feat], errors="coerce").dropna()
                    if len(vals) > 0:
                        std = float(vals.std())
                        if not np.isfinite(std) or std <= 0:
                            std = 1.0
                        p[feat] = {"mean": float(vals.mean()), "std": std + 1e-3}

            if YIELD_COL in grp.columns:
                y = pd.to_numeric(grp[YIELD_COL], errors="coerce").dropna()
                p["avg_yield"] = float(y.mean()) if len(y) else 0.0
            else:
                p["avg_yield"] = 0.0

            for cat in ["Season", "Soil_Type", "Irrigation_Type", "Pest_Disease_Incidence", "District_Name", "District"]:
                if cat in grp.columns:
                    p[cat] = _category_distribution(grp[cat])

            if "District_Name" not in p and "District" in p:
                p["District_Name"] = p["District"]

            PROFILES[crop] = p

print(f"  Profiles built for {len(PROFILES)} crops\n")

# ── VALID DISTRICTS ────────────────────────────────────────────────────────────
if "District_Name" in df_history.columns:
    valid_districts = sorted(df_history["District_Name"].dropna().astype(str).str.strip().unique().tolist())
elif _full_df is not None and "District_Name" in _full_df.columns:
    valid_districts = sorted(_full_df["District_Name"].dropna().astype(str).str.strip().unique().tolist())
else:
    valid_districts = []

print(f"  Valid districts: {valid_districts}\n")

# ── HELPERS ───────────────────────────────────────────────────────────────────

def gaussian(val, mean, std):
    return float(np.exp(-0.5 * ((val - mean) / std) ** 2))


def get_lag_features(crop, district, mu, std):
    """Mirrors training's predict_with_live_weather() lag/roll/trend computation."""
    if "District_Name" not in df_history.columns or "Crop" not in df_history.columns:
        return 0.0, 0.0, 0.0
    hist = df_history[
        (df_history["District_Name"].astype(str).str.strip().str.lower() == str(district).strip().lower())
        & (df_history["Crop"] == crop)
    ]
    if "Year" in hist.columns:
        hist = hist.sort_values("Year")
    if len(hist) < 3 or YIELD_COL not in hist.columns:
        return 0.0, 0.0, 0.0

    last3 = hist[YIELD_COL].values[-3:]
    yield_lag1  = float(last3[-1])
    yield_roll3 = float(np.mean(last3))
    try:
        yield_trend = float(np.polyfit(range(3), last3, 1)[0])
    except (TypeError, ValueError):
        yield_trend = 0.0

    if not std:
        return 0.0, 0.0, 0.0
    return (yield_lag1 - mu) / std, (yield_roll3 - mu) / std, yield_trend / std


def get_climatology_weather(district, season):
    """Best-effort climatology weather fill for requests missing live weather."""
    if _full_df is None:
        return {}
    df = _full_df
    out = {}

    scoped = df
    if "District_Name" in df.columns and district:
        scoped = scoped[scoped["District_Name"].astype(str).str.strip().str.lower() == str(district).strip().lower()]
    if "Season" in df.columns and season:
        season_scoped = scoped[scoped["Season"].astype(str).str.strip().str.lower() == str(season).strip().lower()]
        if len(season_scoped) > 0:
            scoped = season_scoped

    for feat in WEATHER_FEATURES:
        if feat not in df.columns:
            continue
        vals = pd.to_numeric(scoped.get(feat), errors="coerce").dropna() if feat in scoped.columns else pd.Series(dtype=float)
        if len(vals) == 0:
            vals = pd.to_numeric(df[feat], errors="coerce").dropna()
        if len(vals) > 0:
            out[feat] = float(vals.mean())
    return out


def predict_yield_for_crop(crop, district, user_inputs, season=None):
    """Run XGBoost prediction for a specific crop. Returns (predicted_yield, source)."""
    if crop not in valid_crops:
        avg = PROFILES.get(crop, {}).get("avg_yield", 1.0)
        return avg, "hist_avg"

    mu  = crop_stats.loc[crop, "crop_mean"]
    std = crop_stats.loc[crop, "crop_std"]

    lag1, roll3, trend = get_lag_features(crop, district, mu, std)

    climatology = get_climatology_weather(district, season)
    weather_row = {}
    for feat in WEATHER_FEATURES:
        supplied = user_inputs.get(feat)
        if supplied is not None:
            try:
                weather_row[feat] = float(supplied)
                continue
            except (TypeError, ValueError):
                pass
        weather_row[feat] = climatology.get(feat, 0.0)

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
        "Yield_Lag1":  lag1,
        "Yield_Roll3": roll3,
        "Yield_Trend": trend,
        **weather_row,
    }

    X = pd.get_dummies(pd.DataFrame([row]), drop_first=True)
    X = X.reindex(columns=feat_cols, fill_value=0)
    X_sc = scaler.transform(X)

    norm_pred  = model.predict(X_sc)[0]
    pred_yield = float(norm_pred * std + mu)
    return pred_yield, "model"


def _categorical_fit(profile, key, user_value, default=0.0):
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
    """Returns (score, season_fit), where score is 0-1."""
    p = PROFILES.get(crop, {})
    if not p:
        return 0.0, 0.0

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

    season_fit = _categorical_fit(p, "Season", user_inputs.get("Season", "Kharif"), default=0.0)
    soil_fit = _categorical_fit(p, "Soil_Type", user_inputs.get("Soil_Type"), default=0.0)
    irrigation_fit = _categorical_fit(p, "Irrigation_Type", user_inputs.get("Irrigation_Type"), default=0.0)

    pest_raw = user_inputs.get("Pest_Disease_Incidence")
    pest_code = normalize_pest_value(pest_raw, default=1)
    pest_fit = _categorical_fit(p, "Pest_Disease_Incidence", pest_code, default=0.0)

    district_value = user_inputs.get("district") or user_inputs.get("District_Name") or user_inputs.get("District")
    district_fit = _categorical_fit(p, "District_Name", district_value, default=0.0)

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
    try:
        if x is None:
            return default
        if isinstance(x, float) and math.isnan(x):
            return default
        return int(x)
    except (TypeError, ValueError):
        logger.warning("safe_int: could not coerce value %r to int, using default=%s", x, default)
        return default


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

app = Flask(__name__)
CORS(app)


@app.route("/api/crop/health", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "crop_recommender", "crops": len(valid_crops), "features": len(feat_cols)})


@app.route("/api/crop/predict", methods=["POST"])
@app.route("/predict", methods=["POST"])
def predict():
    """Single crop prediction. Used by the what-if panel."""
    data     = request.get_json()
    crop     = data.get("crop", "")
    district = data.get("district", "Dhalai")
    season   = data.get("Season") or data.get("season")

    pred, source = predict_yield_for_crop(crop, district, data, season=season)

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
    """
    data     = request.get_json()
    district = data.get("district", "Dhalai")
    top_n    = int(data.get("top_n", 7))
    season   = data.get("Season") or data.get("season")

    results = []
    all_crops = list(PROFILES.keys())

    for crop in all_crops:
        suit, season_fit = compute_suitability(crop, data)
        pred, source     = predict_yield_for_crop(crop, district, data, season=season)

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

    results.sort(key=lambda x: x["suit_score"], reverse=True)

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


@app.route("/api/crop/valid_districts", methods=["GET"])
@app.route("/valid_districts", methods=["GET"])
def get_valid_districts():
    return jsonify({"valid_districts": valid_districts})


@app.route("/model_info", methods=["GET"])
def model_info():
    """
    Returns feature importances from the XGBoost model and
    Pearson correlations of numeric features with yield in df_history.
    """
    raw_arr = model.feature_importances_
    total   = raw_arr.sum() or 1.0

    col_imp = {col: float(raw_arr[i] / total) for i, col in enumerate(feat_cols)}

    GROUP_PREFIXES = [
        ("Crop_",             "Crop"),
        ("District_Name_",    "District"),
        ("Soil_Type_",        "Soil_Type"),
        ("Irrigation_Type_",  "Irrigation_Type"),
    ]

    grouped: dict = {}
    for col, imp in col_imp.items():
        label = col
        for prefix, group_name in GROUP_PREFIXES:
            if col.startswith(prefix):
                label = group_name
                break
        grouped[label] = grouped.get(label, 0.0) + imp

    g_total = sum(grouped.values()) or 1.0
    feat_imps_sorted = dict(
        sorted({k: round(v / g_total, 6) for k, v in grouped.items()}.items(),
               key=lambda x: -x[1])
    )

    corr_df = _full_df.copy() if _full_df is not None else df_history.copy()
    if "Pest_Disease_Incidence" in corr_df.columns:
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
    parser = argparse.ArgumentParser(description="CropAI Recommender Microservice")
    parser.add_argument("--port",  type=int, default=5001,
                        help="Port to run on (default 5001 for Tripura, 5003 for Meghalaya, 5005 for Rajasthan)")
    parser.add_argument("--state", type=str, default="tripura",
                        help="State to pre-load on startup (tripura / meghalaya / rajasthan)")
    args = parser.parse_args()

    if args.state.lower().strip() != STATE:
        print(
            f"WARNING: --state={args.state!r} does not match the module-level "
            f"STATE={STATE!r} resolved from sys.argv at import time. The "
            f"already-loaded artefacts are for state={STATE!r}."
        )
    else:
        print(f"State '{STATE}' artefacts already loaded at import time.")

    print("=" * 55)
    print(f"  CROP RECOMMENDER SERVICE — state={args.state}")
    print(f"  Running at http://localhost:{args.port}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=args.port, debug=False)