"""
Crop Dashboard Backend — Stats & Page-Serving Service
=======================================================
Uses model_artefacts.pkl from crop_yield_with_weather.py for df_history
(trend charts), and the weather Excel for the fuller EDA dataset. Serves
the dashboard/stats endpoints and the static frontend pages.

Crop yield PREDICTION and RECOMMENDATION ranking have been split out into
a separate microservice: crop_recommender_service.py. This file no longer
loads the XGBoost model or scaler and no longer serves /predict,
/recommend, /valid_crops, /valid_districts, /model_info, or /profiles —
those live on the recommender service now (default port = this port + 1).
See crop_recommender_service.py's module docstring for the port
convention and the required gateway/frontend routing change.

Run:
    pip install flask flask-cors pandas openpyxl
    python backend_2.py --state tripura --port 5000

Then open crop_recommender.html in your browser (its /predict, /recommend,
etc. calls must be pointed at crop_recommender_service.py's port).
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
# Duplicated from crop_recommender_service.py verbatim. This file and the
# recommender service each need this independently — it's not shared via
# import because they're separate processes/deployables. If you change
# this logic, change it in BOTH files.
#
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

# ── LOAD ──────────────────────────────────────────────────────────────────────
# NOTE: this file only needs df_history from the pickle (used by
# /crop_trends). model/scaler/feat_cols/crop_stats are loaded and used by
# crop_recommender_service.py instead — they are intentionally NOT
# unpacked here even though they're present in the same pickle.

print(f"\nLoading model artefacts for state={STATE} from {ARTEFACTS_FILE}...")
if not Path(ARTEFACTS_FILE).exists():
    print(f"\nERROR: {ARTEFACTS_FILE} not found.")
    print("Run crop_yield_with_weather.py first to generate it.")
    raise SystemExit(1)

with open(ARTEFACTS_FILE, "rb") as f:
    art = pickle.load(f)

df_history = art["df_history"]

print(f"  df_history loaded — {len(df_history)} rows\n")

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

# ── HELPERS ───────────────────────────────────────────────────────────────────

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
                     "Run scripts/generate_alerts.py first."
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
    return jsonify({"status": "ok", "service": "crop_dashboard", "history_rows": len(df_history)})


@app.route("/crop_trends", methods=["GET"])
@app.route("/stats/trends", methods=["GET"])
@app.route("/api/crop/crop_trends", methods=["GET"])
@app.route("/api/crop/stats/trends", methods=["GET"])
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

    Query param: district (optional). When present and not "all", every
    chart below is computed on the subset of rows for that district only,
    so a district admin (e.g. Ajmer) sees analysis scoped to their district
    while a state admin (no district param, or district=all) keeps seeing
    the full state-wide breakdown exactly as before.
    """
    df = _full_df.copy() if _full_df is not None else df_history.copy()

    district_param = (request.args.get("district") or "").strip()
    if district_param and district_param.lower() != "all" and "District_Name" in df.columns:
        _mask = df["District_Name"].astype(str).str.strip().str.lower() == district_param.lower()
        if _mask.any():
            df = df[_mask]
        else:
            logger.warning(
                "/stats: district=%r not found in District_Name values for state=%s; "
                "falling back to full state dataset", district_param, STATE,
            )

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

    # Same district scoping as /stats — district admins get scatter data
    # confined to their own district, state admins get the full picture.
    district_param = (request.args.get("district") or "").strip()
    if district_param and district_param.lower() != "all" and "District_Name" in sub.columns:
        _mask = sub["District_Name"].astype(str).str.strip().str.lower() == district_param.lower()
        if _mask.any():
            sub = sub[_mask]

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


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CropAI Dashboard Backend")
    parser.add_argument("--port",  type=int, default=5000,
                        help="Port to run on (default 5000 for Tripura, 5002 for Meghalaya)")
    parser.add_argument("--state", type=str, default="tripura",
                        help="State to pre-load on startup (tripura / meghalaya / rajasthan)")
    args = parser.parse_args()

    # NOTE: previously this called `_load_state(args.state)`, a function that
    # was never defined anywhere in this module — every startup logged
    # "Could not pre-load '<state>': name '_load_state' is not defined".
    # This was harmless for functionality: the module-level load block above
    # (STATE/DATA_DIR/ARTEFACTS_FILE/df_history) already runs at import
    # time, driven by the same `--state` CLI arg parsed at the top of this
    # file, so by the time argparse runs here the correct state's data is
    # already loaded. The removed call was dead code from an earlier
    # refactor (state used to be lazily loaded per-request).
    # Kept as a no-op confirmation so startup output still shows what state
    # is live, without a bogus warning every time.
    if args.state.lower().strip() != STATE:
        print(
            f"WARNING: --state={args.state!r} does not match the module-level "
            f"STATE={STATE!r} resolved from sys.argv at import time. This can "
            f"happen if backend_2.py is launched with an unexpected argv order. "
            f"The already-loaded data is for state={STATE!r}."
        )
    else:
        print(f"State '{STATE}' data already loaded at import time.")

    print("=" * 55)
    print(f"  CROP DASHBOARD BACKEND — state={args.state}")
    print(f"  Running at http://localhost:{args.port}")
    print(f"  Recommender microservice expected on port {args.port + 1}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=args.port, debug=False)