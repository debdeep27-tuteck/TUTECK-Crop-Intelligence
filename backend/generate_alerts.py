"""
generate_alerts.py
==================
Fetches real seasonal weather from Open-Meteo, loads model_artefacts.pkl,
runs XGBoost predictions for all 176 district-crop-season combinations,
computes yield anomalies, and writes predictions.json for the dashboard.

Usage:
    python generate_alerts.py

Output:
    predictions.json  (same folder — the dashboard reads this)

Run this once per season or whenever you want fresh predictions.
Requires: model_artefacts.pkl and weather_cache.json in the same folder.
"""

import json
import re
import time
import pickle
import datetime
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────


BASE_DIR = Path(__file__).resolve().parent

ARTEFACTS_PATH = BASE_DIR / "../data_and_model_rajasthan/model_artefacts.pkl"
CACHE_PATH     = BASE_DIR / "../data_and_model_rajasthan/weather_cache.json"
OUTPUT_PATH    = BASE_DIR / "../data_and_model_rajasthan/predictions.json"
DATA_PATH      = BASE_DIR / "../data_and_model_rajasthan/merged_crop_enriched_features_del.xlsx"


YIELD_COL = "Yield (Tonne or Bales/Hectare)"

# Logical column names the raw data file MUST have, and the aliases we'll
# accept for each (matched case/space/punctuation-insensitively).
REQUIRED_COLUMNS = {
    "Crop_Year":               ["Crop_Year", "CropYear", "Crop Year", "Year Range", "Crop_Year_Range"],
    "District_Name":           ["District_Name", "District", "DistrictName", "District Name"],
    "Crop":                    ["Crop", "Crop_Name", "CropName"],
    "Season":                  ["Season"],
    YIELD_COL:                 [YIELD_COL, "Yield", "Yield(Tonne or Bales/Hectare)",
                                 "Yield (Tonne/Bales per Hectare)", "Yield_Tonne_or_Bales_per_Hectare"],
    "Area (Hectare)":          ["Area (Hectare)", "Area(Hectare)", "Area_Hectare", "Area (ha)", "Area"],
}

# Columns needed by the model but NOT present in the raw
# merged_crop_enriched_features_del.xlsx file (confirmed by the actual run —
# that file only has State_Name/District_Name/Crop_Year/Season/Crop/
# Area (Hectare)/Production (Tonnes/Bales)/Yield (...)). These were engineered
# at training time, so we recover them from model_artefacts["df_history"]
# instead, falling back to a documented default if even that lookup misses.
OPTIONAL_COLUMNS = {
    "Fertilizer_kg_per_ha":    ["Fertilizer_kg_per_ha", "Fertilizer_Kg_Per_Ha", "Fertilizer (kg/ha)",
                                 "Fertilizer_kg_per_hectare", "Fertilizer Kg Per Ha", "Fertilizer_Use_kg_per_ha",
                                 "Fertilizer"],
    "Pest_Disease_Incidence":  ["Pest_Disease_Incidence", "Pest_Disease", "PestDiseaseIncidence",
                                 "Pest/Disease Incidence", "Pest_Disease_Incidence_Level"],
}

# Used only if a district/crop/season combo can't be found in df_history either.
DEFAULT_FERTILIZER_KG_PER_HA = 100.0
DEFAULT_PEST_CODE            = 1   # 1 == "Medium" in PEST_MAP

ALERT_THRESHOLD   = -20.0   # % — triggers alert
CRITICAL_THRESHOLD = -30.0  # % — critical alert

VALID_CROPS = [
    "Bajra", "Barley", "Cotton(lint)", "Gram", "Groundnut", "Jowar",
    "Linseed", "Maize", "Onion", "Rapeseed &Mustard", "Rice", "Sesamum",
    "Sugarcane", "Wheat", "Arhar/Tur", "Castor seed", "Coriander",
    "Dry chillies", "Garlic", "Masoor", "Moong(Green Gram)", "Moth",
    "Other Rabi pulses", "Other Kharif pulses", "Peas & beans (Pulses)",
    "Potato", "Sannhamp", "Soyabean", "Sweet potato", "Urad", "Guar seed",
    "Tapioca", "Small millets", "Sunflower", "Citrus Fruit", "Mango",
    "Other Fresh Fruits", "Other Vegetables", "Pome Fruit", "other oilseeds",
    "Ginger", "Tobacco", "Banana", "Papaya", "Water Melon",
    "Oilseeds total", "Turmeric", "Other Cereals", "Grapes", "Mesta", "Orange",
]

DISTRICT_COORDS = {
    "Ajmer":           (26.45, 74.64),
    "Alwar":           (27.57, 76.61),
    "Banswara":        (23.55, 74.35),
    "Baran":           (25.10, 76.51),
    "Barmer":          (25.75, 71.38),
    "Bharatpur":       (27.22, 77.49),
    "Bhilwara":        (25.35, 74.63),
    "Bikaner":         (28.02, 73.31),
    "Bundi":           (25.44, 75.64),
    "Chittorgarh":     (24.88, 74.63),
    "Churu":           (28.30, 74.97),
    "Dausa":           (26.89, 76.33),
    "Dholpur":         (26.70, 77.89),
    "Dungarpur":       (23.84, 73.71),
    "Ganganagar":      (29.92, 73.88),
    "Hanumangarh":     (29.58, 74.32),
    "Jaipur":          (26.91, 75.79),
    "Jaisalmer":       (26.91, 70.91),
    "Jalore":          (25.35, 72.62),
    "Jhalawar":        (24.60, 76.16),
    "Jhunjhunu":       (28.13, 75.40),
    "Jodhpur":         (26.24, 73.02),
    "Karauli":         (26.50, 77.02),
    "Kota":            (25.21, 75.86),
    "Nagaur":          (27.20, 73.74),
    "Pali":            (25.77, 73.32),
    "Pratapgarh":      (24.03, 74.78),
    "Rajsamand":       (25.07, 73.88),
    "Sawai madhopur":  (26.02, 76.35),
    "Sikar":           (27.61, 75.14),
    "Sirohi":          (24.89, 72.86),
    "Tonk":            (26.16, 75.79),
    "Udaipur":         (24.58, 73.68),
}

SEASON_WINDOWS = {
    "Kharif":     ("06-01", "09-30", False),
    "Rabi":       ("10-15", "02-28", True),
    "Autumn":     ("08-01", "11-30", False),
    "Summer":     ("03-01", "06-30", False),
    "Winter":     ("11-01", "02-28", True),
    "Whole Year": ("01-01", "12-31", False),
}

WEATHER_FEATURES = [
    "weather_temp_mean", "weather_rain_total", "weather_rain_days",
    "weather_et0_total", "weather_wind_mean", "weather_solarrad_total",
]

PEST_MAP = {"Low": 0, "Medium": 1, "High": 2}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase and strip everything except letters/digits for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _match_columns(df: pd.DataFrame, aliases_by_canonical: dict):
    """
    Returns (rename_map, missing_list) for the given canonical->aliases dict,
    without mutating df or raising.
    """
    actual_cols = list(df.columns)
    normalized_actual = {_normalize(c): c for c in actual_cols}

    rename_map = {}
    missing = []

    for canonical, aliases in aliases_by_canonical.items():
        if canonical in actual_cols:
            continue
        found = None
        for alias in aliases:
            norm_alias = _normalize(alias)
            if norm_alias in normalized_actual:
                found = normalized_actual[norm_alias]
                break
        if found is not None:
            rename_map[found] = canonical
        else:
            missing.append(canonical)

    return rename_map, missing


def resolve_columns(df: pd.DataFrame, required: dict, source_name: str = None) -> pd.DataFrame:
    """
    Renames df's columns (returns a copy) so that every logical name in
    `required` exists exactly as spelled, no matter what casing/spacing/
    punctuation the source file used.

    Raises a clear error listing the actual columns if a required field
    can't be matched to anything in the file.
    """
    rename_map, missing = _match_columns(df, required)

    if missing:
        raise KeyError(
            "Could not find a match for the following expected column(s): "
            f"{missing}\n\n"
            f"Actual columns in '{source_name or 'the file'}':\n  "
            + "\n  ".join(df.columns)
            + "\n\nAdd the real column name to REQUIRED_COLUMNS in "
              "generate_alerts.py (near the top of the file) and re-run."
        )

    if rename_map:
        print("  Column name auto-mapping applied:")
        for src, dst in rename_map.items():
            print(f"    '{src}'  →  '{dst}'")
        df = df.rename(columns=rename_map)

    return df


def try_resolve_columns(df: pd.DataFrame, optional: dict, source_name: str = None):
    """
    Best-effort version of resolve_columns: renames whatever it can match
    and returns (df, found_set, missing_set) instead of raising. Used for
    columns we know might legitimately be absent from a given source.
    """
    rename_map, missing = _match_columns(df, optional)
    if rename_map:
        print(f"  Column name auto-mapping applied ({source_name or 'source'}):")
        for src, dst in rename_map.items():
            print(f"    '{src}'  →  '{dst}'")
        df = df.rename(columns=rename_map)
    found = set(optional.keys()) - set(missing)
    return df, found, set(missing)


def build_fallback_lookup(df_history: pd.DataFrame):
    """
    Builds {(district, crop, season): {"Fertilizer_kg_per_ha": x, "Pest_Disease_Incidence": y}}
    from the model's training data (df_history), for combos whose fertilizer/
    pest values aren't present in the raw prediction-input Excel file.

    Returns (lookup_dict, available_fields_set). available_fields_set tells
    the caller which of the two optional columns were actually found in
    df_history at all, so it knows whether to expect per-combo hits or fall
    straight through to the hardcoded default.
    """
    if not isinstance(df_history, pd.DataFrame):
        print("  ⚠ df_history in model_artefacts.pkl is not a DataFrame — "
              "cannot use it to recover Fertilizer/Pest columns.")
        return {}, set()

    dfh, found, missing = try_resolve_columns(
        df_history.copy(),
        {
            "District_Name": REQUIRED_COLUMNS["District_Name"],
            "Crop":          REQUIRED_COLUMNS["Crop"],
            "Season":        REQUIRED_COLUMNS["Season"],
            "Crop_Year":     REQUIRED_COLUMNS["Crop_Year"],
            **OPTIONAL_COLUMNS,
        },
        source_name="model_artefacts['df_history']",
    )

    key_cols = {"District_Name", "Crop", "Season"}
    if not key_cols.issubset(dfh.columns):
        print(" df_history is missing District_Name/Crop/Season — "
              "cannot build fertilizer/pest lookup from it.")
        return {}, set()

    available_fields = set(OPTIONAL_COLUMNS.keys()) & set(dfh.columns)
    if not available_fields:
        print("  ⚠ df_history does not contain Fertilizer_kg_per_ha or "
              "Pest_Disease_Incidence either — will use hardcoded defaults.")
        return {}, set()

    # Sort so the *last* row per combo (most recent year, if we can tell) wins.
    if "Crop_Year" in dfh.columns:
        try:
            dfh["_YearSort"] = dfh["Crop_Year"].astype(str).str.split(" - ").str[0].astype(int)
            dfh = dfh.sort_values("_YearSort")
        except Exception:
            pass

    lookup = {}
    for (dist, crop, season), grp in dfh.groupby(["District_Name", "Crop", "Season"]):
        last_row = grp.iloc[-1]
        lookup[(dist, crop, season)] = {
            field: last_row[field] for field in available_fields
        }

    print(f"  Built fertilizer/pest fallback lookup from df_history: "
          f"{len(lookup)} combos, fields={sorted(available_fields)}")
    return lookup, available_fields


def resolve_pest_code(raw_value) -> int:
    """Accepts a Low/Medium/High label OR an already-numeric code and
    returns the integer code the model expects."""
    if raw_value is None:
        return DEFAULT_PEST_CODE
    # Already numeric (int/float, or a numeric string like "1" / "1.0")
    try:
        return int(round(float(raw_value)))
    except (TypeError, ValueError):
        pass
    return PEST_MAP.get(str(raw_value).strip(), DEFAULT_PEST_CODE)


def season_date_range(year_start: int, season: str):
    import calendar
    win = SEASON_WINDOWS[season]
    start_md, end_md, crosses = win
    start = f"{year_start}-{start_md}"
    end_year = year_start + 1 if crosses else year_start
    if end_md == "02-28":
        last = calendar.monthrange(end_year, 2)[1]
        end_md = f"02-{last}"
    return start, f"{end_year}-{end_md}"


def fetch_weather(lat: float, lon: float, start: str, end: str,
                  cache: dict, cache_key: str) -> dict:
    """Fetch from cache or Open-Meteo API."""
    if cache_key in cache:
        return cache[cache_key]

    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": ",".join([
            "temperature_2m_mean", "precipitation_sum",
            "et0_fao_evapotranspiration", "windspeed_10m_max",
            "shortwave_radiation_sum",
        ]),
        "timezone": "Asia/Kolkata",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    daily = resp.json()["daily"]

    def smean(lst): v = [x for x in lst if x]; return float(np.mean(v)) if v else np.nan
    def ssum(lst):  v = [x for x in lst if x]; return float(np.sum(v)) if v else np.nan

    rain = daily.get("precipitation_sum", [])
    result = {
        "weather_temp_mean":      smean(daily.get("temperature_2m_mean", [])),
        "weather_rain_total":     ssum(rain),
        "weather_rain_days":      sum(1 for r in rain if r and r > 1.0),
        "weather_et0_total":      ssum(daily.get("et0_fao_evapotranspiration", [])),
        "weather_wind_mean":      smean(daily.get("windspeed_10m_max", [])),
        "weather_solarrad_total": ssum(daily.get("shortwave_radiation_sum", [])),
    }
    cache[cache_key] = result
    time.sleep(0.35)   # polite rate limiting
    return result


def get_season_weather(district: str, season: str, cache: dict) -> dict:
    """
    Get weather for the most recently completed instance of this season.
    For a season currently in progress, uses the most recent full year available.
    Falls back to 5-year climatology if the current year isn't available yet.
    """
    coords = DISTRICT_COORDS[district]
    today  = datetime.date.today()

    # Try last 3 years in reverse, return first successful fetch
    for years_back in range(0, 4):
        candidate_year = today.year - years_back
        start, end = season_date_range(candidate_year, season)
        season_end_date = datetime.date.fromisoformat(end)

        # Only use this year if the season has ended
        if season_end_date >= today and years_back == 0:
            continue   # Season not complete yet — try previous year

        cache_key = f"{district}|{candidate_year} - {candidate_year+1}|{season}"
        # Also check crop-year style key used by training cache
        alt_key = f"{district}|{candidate_year} - {candidate_year+1}|{season}"

        try:
            wx = fetch_weather(coords[0], coords[1], start, end, cache, alt_key)
            if not any(np.isnan(v) for v in wx.values()):
                return wx, candidate_year
        except Exception:
            continue

    # Fallback: 5-year climatology
    print(f"    Using climatology for {district} | {season}")
    records = []
    for y in range(today.year - 6, today.year - 1):
        try:
            s, e = season_date_range(y, season)
            key = f"{district}|{y} - {y+1}|{season}"
            wx = fetch_weather(coords[0], coords[1], s, e, cache, key)
            records.append(wx)
        except Exception:
            pass
    if records:
        avg = {k: float(np.nanmean([r[k] for r in records])) for k in records[0]}
        return avg, today.year - 1
    raise RuntimeError(f"Cannot fetch weather for {district} {season}")



def clean_json(obj):
    """Convert numpy values and non-finite floats into strict JSON-safe values."""
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

# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("RAJASTHAN CROP SHORTAGE ALERT GENERATOR")
    print(f"Run date: {datetime.date.today()}")
    print("=" * 60 + "\n")

    # 1. Load model artefacts
    print("Loading model artefacts...")
    with open(ARTEFACTS_PATH, "rb") as f:
        art = pickle.load(f)
    model      = art["model"]
    feat_cols  = art["feat_cols"]
    scaler     = art["scaler"]
    crop_stats = art["crop_stats"]
    df_history = art["df_history"]
    print(f"  Model loaded. Feature count: {len(feat_cols)}\n")

    # 2. Load weather cache
    cache_file = Path(CACHE_PATH)
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    print(f"Weather cache: {len(cache)} entries loaded\n")

    # 3. Build prediction combos from historical data
    print("Building prediction combos from historical data...")
    df = pd.read_excel(DATA_PATH)
    df = resolve_columns(df, REQUIRED_COLUMNS, source_name=DATA_PATH.name)

    # Fertilizer/Pest aren't in the raw Excel — try to match them anyway
    # (in case a differently-named version does exist), then fall back to
    # df_history, then to hardcoded defaults.
    df, found_optional, _ = try_resolve_columns(df, OPTIONAL_COLUMNS, source_name=DATA_PATH.name)
    fallback_lookup, fallback_fields = build_fallback_lookup(df_history)

    df["Year"] = df["Crop_Year"].str.split(" - ").str[0].astype(int)

    missing_fert_count = 0
    missing_pest_count = 0

    combos = []
    for (dist, crop, season), grp in df.groupby(["District_Name", "Crop", "Season"]):
        if crop not in VALID_CROPS:
            continue
        if crop not in crop_stats.index:
            continue
        grp = grp.sort_values("Year")
        if len(grp) < 3:
            continue
        last3  = grp[YIELD_COL].values[-3:]
        last_r = grp.iloc[-1]

        combo_key = (dist, crop, season)
        fallback  = fallback_lookup.get(combo_key, {})

        # Fertilizer: raw file → df_history lookup → hardcoded default
        if "Fertilizer_kg_per_ha" in found_optional:
            fertilizer = float(last_r["Fertilizer_kg_per_ha"])
        elif "Fertilizer_kg_per_ha" in fallback:
            fertilizer = float(fallback["Fertilizer_kg_per_ha"])
        else:
            fertilizer = DEFAULT_FERTILIZER_KG_PER_HA
            missing_fert_count += 1

        # Pest/Disease: raw file → df_history lookup → hardcoded default
        if "Pest_Disease_Incidence" in found_optional:
            pest_code = resolve_pest_code(last_r["Pest_Disease_Incidence"])
        elif "Pest_Disease_Incidence" in fallback:
            pest_code = resolve_pest_code(fallback["Pest_Disease_Incidence"])
        else:
            pest_code = DEFAULT_PEST_CODE
            missing_pest_count += 1

        combos.append({
            "district":     dist,
            "crop":         crop,
            "season":       season,
            "last3_yields": [float(y) for y in last3],
            "area_ha":      float(last_r["Area (Hectare)"]),
            "fertilizer":   fertilizer,
            "pest_code":    pest_code,
            "normal_yield": float(grp[YIELD_COL].values[-5:].mean()),
        })

    print(f"  {len(combos)} valid combos to predict")
    if missing_fert_count:
        print(f" {missing_fert_count} combo(s) used the hardcoded fertilizer default "
              f"({DEFAULT_FERTILIZER_KG_PER_HA} kg/ha) — not found in Excel or df_history")
    if missing_pest_count:
        print(f" {missing_pest_count} combo(s) used the hardcoded pest default "
              f"(code {DEFAULT_PEST_CODE} = Medium) — not found in Excel or df_history")
    print()

    # 4. Predict for each combo
    print("Running predictions...\n")
    results = []
    n = len(combos)

    for i, combo in enumerate(combos):
        dist   = combo["district"]
        crop   = combo["crop"]
        season = combo["season"]

        print(f"  [{i+1:3d}/{n}] {dist:15s} | {crop:25s} | {season}", end="  ")

        # Get weather
        try:
            wx, wx_year = get_season_weather(dist, season, cache)
        except Exception as e:
            print(f"SKIP (weather error: {e})")
            continue

        # Yield lags
        last3       = combo["last3_yields"]
        yield_lag1  = last3[-1]
        yield_roll3 = float(np.mean(last3))
        yield_trend = float(np.polyfit(range(3), last3, 1)[0])
        normal      = combo["normal_yield"]

        # Normalise
        mu  = crop_stats.loc[crop, "crop_mean"]
        std = crop_stats.loc[crop, "crop_std"]

        row = {
            "District_Name":          dist,
            "Crop":                   crop,
            "Area (Hectare)":         combo["area_ha"],
            "Fertilizer_kg_per_ha":   combo["fertilizer"],
            "Pest_Disease_Incidence": combo["pest_code"],
            "Yield_Lag1":             (yield_lag1  - mu) / std,
            "Yield_Roll3":            (yield_roll3 - mu) / std,
            "Yield_Trend":             yield_trend       / std,
            **{k: wx[k] for k in WEATHER_FEATURES},
        }

        row_df = pd.get_dummies(pd.DataFrame([row]), drop_first=True)
        row_df = row_df.reindex(columns=feat_cols, fill_value=0)
        row_sc = scaler.transform(row_df)

        norm_pred   = model.predict(row_sc)[0]
        pred_yield  = norm_pred * std + mu

        # Avoid invalid JSON values like Infinity when the historical normal is 0.
        # If normal <= 0, anomaly percentage is not mathematically meaningful.
        if normal is None or not np.isfinite(normal) or normal <= 0:
            anomaly_pct = None
            status = "normal"
        else:
            anomaly_pct = (pred_yield - normal) / normal * 100
            status = ("critical" if anomaly_pct <= CRITICAL_THRESHOLD
                      else "watch"    if anomaly_pct <= ALERT_THRESHOLD
                      else "normal")

        anom_text = "N/A" if anomaly_pct is None else f"{anomaly_pct:+.1f}%"
        print(f" {pred_yield:.2f} t/ha  anomaly: {anom_text}  [{status.upper()}]")

        results.append({
            "district":      dist,
            "crop":          crop,
            "season":        season,
            "predicted":     round(float(pred_yield), 3),
            "normal":        round(float(normal), 3),
            "anomaly":       None if anomaly_pct is None else round(float(anomaly_pct), 1),
            "status":        status,
            "weather_year":  wx_year,
            "weather": {
                "rain_total":    round(wx["weather_rain_total"], 1),
                "rain_days":     int(wx["weather_rain_days"]),
                "temp_mean":     round(wx["weather_temp_mean"], 1),
                "et0_total":     round(wx["weather_et0_total"], 1),
                "wind_mean":     round(wx["weather_wind_mean"], 1),
                "solarrad_total": round(wx["weather_solarrad_total"], 1),
            },
        })

    # 5. Save cache (new entries added)
    cache_file.write_text(json.dumps(cache, indent=2))
    print(f"\nCache updated: {len(cache)} entries")

    # 6. Summary
    critical = [r for r in results if r["status"] == "critical"]
    watch    = [r for r in results if r["status"] == "watch"]
    normal   = [r for r in results if r["status"] == "normal"]
    flagged_districts = len(set(r["district"] for r in results if r["status"] != "normal"))

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Total predictions : {len(results)}")
    print(f"  Critical alerts   : {len(critical)}  (anomaly <= {CRITICAL_THRESHOLD}%)")
    print(f"  Watch alerts      : {len(watch)}   (anomaly > {ALERT_THRESHOLD}% and <= {CRITICAL_THRESHOLD}%)")
    print(f"  Normal            : {len(normal)}")
    print(f"  Districts flagged : {flagged_districts} of {len(DISTRICT_COORDS)}")

    if critical:
        print(f"\n  TOP CRITICAL ALERTS:")
        for r in sorted(critical, key=lambda x: x["anomaly"])[:5]:
            print(f"    {r['district']:15s} | {r['crop']:20s} | {r['season']:10s} -> {r['anomaly']:+.1f}%")

    # 7. Write output JSON
    output = {
        "generated_at":   datetime.datetime.now().isoformat(),
        "run_date":       str(datetime.date.today()),
        "model_version":  "XGBoost · seasonal weather features · 33 features",
        "alert_threshold": ALERT_THRESHOLD,
        "critical_threshold": CRITICAL_THRESHOLD,
        "summary": {
            "total":            len(results),
            "critical":         len(critical),
            "watch":            len(watch),
            "normal":           len(normal),
            "districts_flagged": flagged_districts,
        },
        "predictions": results,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n predictions.json written ({len(results)} rows)")
    print(f"   Open alert_dashboard.html in your browser to view results.")


if __name__ == "__main__":
    main()