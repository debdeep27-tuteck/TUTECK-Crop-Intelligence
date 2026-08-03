import json
import time
import warnings
import re
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xgboost as xgb
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")


BASE_DIR = Path(__file__).resolve().parent

# ── RAJASTHAN TRAINING CONFIG ────────────────────────────────────────────────
# This standalone script trains only the Rajasthan model.
STATE = "rajasthan"
DATA_DIR = (BASE_DIR / "../data_and_model_rajasthan").resolve()
DATA_PATH  = DATA_DIR / "merged_crop_enriched_features_del.xlsx"
CACHE_PATH = DATA_DIR / "weather_cache.json"
YIELD_COL  = "Yield (Tonne or Bales/Hectare)"

DISTRICT_COORDS = {
    "Ajmer": (26.45, 74.64),
    "Alwar": (27.55, 76.63),
    "Anupgarh": (29.19, 73.21),
    "Balotra": (25.83, 72.24),
    "Banswara": (23.55, 74.45),
    "Baran": (25.10, 76.51),
    "Barmer": (25.75, 71.39),
    "Beawar": (26.10, 74.32),
    "Bharatpur": (27.22, 77.49),
    "Bhilwara": (25.35, 74.63),
    "Bikaner": (28.02, 73.31),
    "Bundi": (25.44, 75.64),
    "Chittorgarh": (24.88, 74.63),
    "Churu": (28.30, 74.97),
    "Dausa": (26.89, 76.33),
    "Deeg": (27.47, 77.33),
    "Dholpur": (26.70, 77.89),
    "Didwana-Kuchaman": (27.40, 74.57),
    "Didwana Kuchaman": (27.40, 74.57),
    "Dudu": (26.67, 75.34),
    "Dungarpur": (23.84, 73.72),
    "Ganganagar": (29.91, 73.88),
    "Sri Ganganagar": (29.91, 73.88),
    "Sriganganagar": (29.91, 73.88),
    "Gangapur City": (26.47, 76.72),
    "Hanumangarh": (29.58, 74.32),
    "Jaipur": (26.91, 75.79),
    "Jaipur Gramin": (26.91, 75.79),
    "Jaisalmer": (26.91, 70.91),
    "Jalore": (25.35, 72.62),
    "Jhalawar": (24.60, 76.16),
    "Jhunjhunu": (28.13, 75.40),
    "Jodhpur": (26.24, 73.02),
    "Jodhpur Gramin": (26.24, 73.02),
    "Karauli": (26.50, 77.02),
    "Kekri": (25.97, 75.15),
    "Khairthal-Tijara": (27.80, 76.65),
    "Khairthal Tijara": (27.80, 76.65),
    "Kota": (25.21, 75.86),
    "Kotputli-Behror": (27.70, 76.20),
    "Kotputli Behror": (27.70, 76.20),
    "Nagaur": (27.20, 73.74),
    "Neem Ka Thana": (27.74, 75.79),
    "Pali": (25.77, 73.32),
    "Phalodi": (27.13, 72.36),
    "Pratapgarh": (24.03, 74.78),
    "Rajsamand": (25.07, 73.88),
    "Salumbar": (24.14, 74.04),
    "Sanchore": (24.75, 71.77),
    "Sawai Madhopur": (26.02, 76.34),
    "Sawai madhopur": (26.02, 76.34),
    "Shahpura": (25.62, 74.92),
    "Sikar": (27.61, 75.14),
    "Sirohi": (24.89, 72.86),
    "Tonk": (26.16, 75.79),
    "Udaipur": (24.59, 73.71),
}



# ── NASA POWER DAILY WEATHER HELPERS ─────────────────────────────────────────
# Uses one daily time-series request per district, then aggregates locally by
# crop year + season. This avoids thousands of Open-Meteo archive calls.
POWER_PARAMS = [
    "T2M",                  # mean temperature, °C
    "T2M_MAX",              # max temperature, °C
    "T2M_MIN",              # min temperature, °C
    "PRECTOTCORR",          # corrected precipitation, mm/day
    "WS10M",                # wind speed at 10m, m/s
    "ALLSKY_SFC_SW_DWN",    # solar radiation, kWh/m²/day
]


def _clean_district_label(name):
    """Remove Excel serial prefixes like '1. Ajmer' and normalize spacing."""
    s = str(name).strip()
    s = re.sub(r"^\s*\d+\s*[.)-]\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _norm_district_name(name):
    return re.sub(r"[^a-z0-9]", "", _clean_district_label(name).lower())


_COORDS_BY_NORM = {_norm_district_name(k): v for k, v in DISTRICT_COORDS.items()}


def get_district_coords(district):
    return _COORDS_BY_NORM.get(_norm_district_name(district))


def _power_safe_float(value):
    try:
        value = float(value)
        if value <= -900 or not np.isfinite(value):
            return np.nan
        return value
    except Exception:
        return np.nan


def _estimate_et0_from_power(tmean, tmax, tmin, solar_kwh):
    """Consistent ET0-like proxy from NASA POWER daily data."""
    tmean = _power_safe_float(tmean)
    tmax = _power_safe_float(tmax)
    tmin = _power_safe_float(tmin)
    solar_kwh = _power_safe_float(solar_kwh)
    if np.isnan(tmean) or np.isnan(tmax) or np.isnan(tmin) or np.isnan(solar_kwh):
        return np.nan
    temp_range = max(tmax - tmin, 0.1)
    solar_mj = solar_kwh * 3.6
    return max(0.0, 0.0023 * (tmean + 17.8) * np.sqrt(temp_range) * solar_mj)


def fetch_power_daily(district, start_date, end_date, cache):
    district_clean = _clean_district_label(district)
    coords = get_district_coords(district_clean)
    if coords is None:
        return None

    lat, lon = coords
    start_key = start_date.replace('-', '')
    end_key = end_date.replace('-', '')
    cache_key = f"NASA_POWER|{district_clean}|{start_key}|{end_key}"

    if cache_key in cache:
        cached_df = pd.DataFrame(cache[cache_key])
        if "date" in cached_df.columns:
            cached_df["date"] = pd.to_datetime(cached_df["date"])
        return cached_df

    url = "https://power.larc.nasa.gov/api/temporal/daily/point"
    params = {
        "parameters": ",".join(POWER_PARAMS),
        "community": "AG",
        "longitude": lon,
        "latitude": lat,
        "start": start_key,
        "end": end_key,
        "format": "JSON",
        "time-standard": "LST",
    }

    for attempt in range(5):
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code == 429:
            wait = min(int(resp.headers.get("Retry-After", 20)) * (attempt + 1), 180)
            print(f"  429 from NASA POWER for {district_clean}; waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        raise RuntimeError(f"NASA POWER rate limit after retries for {district_clean}")

    param_data = resp.json().get("properties", {}).get("parameter", {})
    if not param_data:
        return None

    all_dates = sorted(set().union(*[set(v.keys()) for v in param_data.values() if isinstance(v, dict)]))
    rows = []
    for dstr in all_dates:
        row = {
            "date": pd.to_datetime(dstr, format="%Y%m%d"),
            "T2M": _power_safe_float(param_data.get("T2M", {}).get(dstr)),
            "T2M_MAX": _power_safe_float(param_data.get("T2M_MAX", {}).get(dstr)),
            "T2M_MIN": _power_safe_float(param_data.get("T2M_MIN", {}).get(dstr)),
            "PRECTOTCORR": _power_safe_float(param_data.get("PRECTOTCORR", {}).get(dstr)),
            "WS10M": _power_safe_float(param_data.get("WS10M", {}).get(dstr)),
            "ALLSKY_SFC_SW_DWN": _power_safe_float(param_data.get("ALLSKY_SFC_SW_DWN", {}).get(dstr)),
        }
        row["ET0_PROXY"] = _estimate_et0_from_power(row["T2M"], row["T2M_MAX"], row["T2M_MIN"], row["ALLSKY_SFC_SW_DWN"])
        rows.append(row)

    daily_df = pd.DataFrame(rows)
    cache[cache_key] = daily_df.assign(date=daily_df["date"].dt.strftime("%Y-%m-%d")).to_dict("records")
    return daily_df


def aggregate_power_season(daily_df, start_date, end_date):
    if daily_df is None or daily_df.empty:
        return {k: np.nan for k in WEATHER_FEATURES}

    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)
    sub = daily_df[(daily_df["date"] >= start_ts) & (daily_df["date"] <= end_ts)].copy()
    if sub.empty:
        return {k: np.nan for k in WEATHER_FEATURES}

    rain = sub["PRECTOTCORR"]
    return {
        "weather_temp_mean":      float(sub["T2M"].mean()),
        "weather_rain_total":     float(rain.sum()),
        "weather_rain_days":      int((rain > 1.0).sum()),
        "weather_et0_total":      float(sub["ET0_PROXY"].sum()),
        "weather_wind_mean":      float(sub["WS10M"].mean()),
        "weather_solarrad_total": float(sub["ALLSKY_SFC_SW_DWN"].sum()),
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
    "weather_temp_mean",
    "weather_rain_total",
    "weather_rain_days",
    "weather_et0_total",
    "weather_wind_mean",
    "weather_solarrad_total",
]

DROP_COLS = [YIELD_COL, "Year", "Yield_raw", "Season"]


def season_date_range(crop_year_str, season):
    import calendar
    year_start = int(crop_year_str.split(" - ")[0])
    win = SEASON_WINDOWS[season]
    start_md, end_md, crosses_year = win
    start_date = f"{year_start}-{start_md}"
    end_year = year_start + 1 if crosses_year else year_start
    if end_md == "02-28":
        last_day = calendar.monthrange(end_year, 2)[1]
        end_md = f"02-{last_day}"
    return start_date, f"{end_year}-{end_md}"




def fetch_seasonal_weather(df, cache_path=CACHE_PATH):
    cache_file = Path(cache_path)
    try:
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    except Exception:
        cache = {}

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df["District_Name"] = df["District_Name"].apply(_clean_district_label)
    df["Season"] = df["Season"].astype(str).str.strip()

    keys = df[["District_Name", "Crop_Year", "Season"]].drop_duplicates()

    unknown_districts = sorted({
        str(d) for d in keys["District_Name"].dropna().unique()
        if get_district_coords(d) is None
    })
    if unknown_districts:
        print("WARNING: No coordinates found for these districts; weather will be NaN for them:")
        print("  " + ", ".join(unknown_districts[:80]))
        if len(unknown_districts) > 80:
            print(f"  ... and {len(unknown_districts) - 80} more")

    ranges = []
    for _, row in keys.iterrows():
        try:
            s, e = season_date_range(str(row["Crop_Year"]), str(row["Season"]).strip())
            ranges.append((s, e))
        except Exception:
            continue
    if not ranges:
        return pd.DataFrame(columns=["District_Name", "Crop_Year", "Season"] + WEATHER_FEATURES)

    global_start = min(s for s, _ in ranges)
    global_end = max(e for _, e in ranges)

    print(f"Fetching NASA POWER weather for {keys['District_Name'].nunique()} districts from {global_start} to {global_end}...")

    district_daily = {}
    for district in sorted(keys["District_Name"].dropna().unique()):
        if get_district_coords(district) is None:
            district_daily[district] = None
            continue
        try:
            daily = fetch_power_daily(district, global_start, global_end, cache)
            district_daily[district] = daily
            print(f" {district}")
            time.sleep(0.2)
        except Exception as e:
            print(f" {district} — {e}")
            district_daily[district] = None
        cache_file.write_text(json.dumps(cache, indent=2))

    records = []
    for _, row in keys.iterrows():
        district = _clean_district_label(row["District_Name"])
        crop_year = row["Crop_Year"]
        season = str(row["Season"]).strip()
        try:
            start, end = season_date_range(str(crop_year), season)
            weather = aggregate_power_season(district_daily.get(district), start, end)
        except Exception:
            weather = {k: np.nan for k in WEATHER_FEATURES}
        records.append({"District_Name": district, "Crop_Year": crop_year, "Season": season, **weather})

    cache_file.write_text(json.dumps(cache, indent=2))
    print(f"Cache saved to {cache_path}\n")
    return pd.DataFrame(records)


def load_and_join(data_path=DATA_PATH):
    df = pd.read_excel(data_path)
    df.columns = [str(c).strip() for c in df.columns]

    if "District_Name" in df.columns:
        df["District_Name"] = df["District_Name"].apply(_clean_district_label)
    if "Season" in df.columns:
        df["Season"] = df["Season"].astype(str).str.strip()

    df = df.drop(columns=["Production (Tonnes/Bales)"], errors="ignore")

    weather_df = fetch_seasonal_weather(df)

    # Guarantee weather_df has expected merge keys + weather columns even if weather fetch/cache is incomplete.
    expected_cols = ["District_Name", "Crop_Year", "Season"] + WEATHER_FEATURES
    if weather_df is None or weather_df.empty:
        weather_df = df[["District_Name", "Crop_Year", "Season"]].drop_duplicates().copy()
    for col in expected_cols:
        if col not in weather_df.columns:
            weather_df[col] = np.nan
    weather_df = weather_df[expected_cols].drop_duplicates(subset=["District_Name", "Crop_Year", "Season"])

    # If input Excel already has weather columns, avoid _x/_y suffix confusion by replacing from weather_df.
    df = df.drop(columns=WEATHER_FEATURES, errors="ignore")
    df = df.merge(weather_df, on=["District_Name", "Crop_Year", "Season"], how="left")

    # Final safety: ensure weather columns exist after merge.
    for col in WEATHER_FEATURES:
        if col not in df.columns:
            df[col] = np.nan

    joined = df["weather_temp_mean"].notna().sum()
    print(f"Weather joined: {joined}/{len(df)} rows have weather data")
    return df


def engineer_features(df):
    pest_map = {"Low": 0, "Medium": 1, "High": 2}
    if "District_Name" in df.columns:
        df["District_Name"] = df["District_Name"].apply(_clean_district_label)

    # Rajasthan file may not contain Pest_Disease_Incidence.
    # Use a neutral/default level so training can continue.
    if "Pest_Disease_Incidence" not in df.columns:
        print("WARNING: Pest_Disease_Incidence column missing; defaulting all rows to 'Medium'.")
        df["Pest_Disease_Incidence"] = "Medium"
    else:
        df["Pest_Disease_Incidence"] = df["Pest_Disease_Incidence"].fillna("Medium").astype(str).str.strip().str.title()

    df["Pest_Disease_Incidence"] = df["Pest_Disease_Incidence"].map(pest_map).fillna(1).astype(int)

    # Rajasthan file may not contain Fertilizer_kg_per_ha either.
    # Use a neutral/default value so training can continue instead of crashing.
    if "Fertilizer_kg_per_ha" not in df.columns:
        print("WARNING: Fertilizer_kg_per_ha column missing; defaulting all rows to 100.0.")
        df["Fertilizer_kg_per_ha"] = 100.0
    else:
        df["Fertilizer_kg_per_ha"] = pd.to_numeric(
            df["Fertilizer_kg_per_ha"], errors="coerce").fillna(100.0)
    # IMPORTANT: backend_2.py reconstructs Crop_Year using Year + 2004.
    # So Year must be an offset from 2004, not from the dataset minimum year.
    # Examples: 1997-1998 -> -7, 2004-2005 -> 0, 2022-2023 -> 18.
    df["Year"] = df["Crop_Year"].astype(str).str.extract(r"(\d{4})")[0].astype(int) - 2004
    df = df.drop(columns=["Crop_Year"])

    df = df.sort_values(["District_Name", "Crop", "Season", "Year"]).reset_index(drop=True)
    group_keys = ["District_Name", "Crop", "Season"]

    df["Yield_Lag1"]  = df.groupby(group_keys)[YIELD_COL].shift(1)
    df["Yield_Roll3"] = df.groupby(group_keys)[YIELD_COL].transform(
        lambda x: x.shift(1).rolling(3, min_periods=3).mean())
    df["Yield_Trend"] = df.groupby(group_keys)[YIELD_COL].transform(
        lambda x: x.shift(1).rolling(3, min_periods=3).apply(
            lambda w: np.polyfit(range(len(w)), w, 1)[0]))

    # NOTE: Season used to be dropped here ("only needed for season-aware lag
    # grouping above"). It is now KEPT so it can be saved into df_history for
    # generate_alerts.py's fallback lookup. It is excluded from the model's
    # training features via DROP_COLS below, so this does NOT change feat_cols
    # or require retraining behavior to differ.

    original_len = len(df)
    df = df.dropna(subset=["Yield_Lag1", "Yield_Roll3", "Yield_Trend"])
    print(f"Rows dropped (no lag history): {original_len - len(df)}")
    print(f"Remaining rows: {len(df)}")

    # Fill any weather NaNs with district-crop mean
    # Fill weather NaNs with district-crop mean, then overall feature median.
    for col in WEATHER_FEATURES:
        if col not in df.columns:
            df[col] = np.nan
    for col in WEATHER_FEATURES:
        if col not in df.columns:
            df[col] = np.nan
    df[WEATHER_FEATURES] = df.groupby(["District_Name", "Crop"])[WEATHER_FEATURES].transform(
        lambda x: x.fillna(x.mean()))
    for col in WEATHER_FEATURES:
        med = df[col].median()
        if pd.isna(med):
            defaults = {
                "weather_temp_mean": 27.0,
                "weather_rain_total": 350.0,
                "weather_rain_days": 20.0,
                "weather_et0_total": 650.0,
                "weather_wind_mean": 12.0,
                "weather_solarrad_total": 2200.0,
            }
            med = defaults.get(col, 0.0)
        df[col] = df[col].fillna(med)
    for col in WEATHER_FEATURES:
        med = df[col].median()
        if pd.isna(med):
            # Conservative fallback only if absolutely no weather joined.
            defaults = {
                "weather_temp_mean": 27.0,
                "weather_rain_total": 350.0,
                "weather_rain_days": 20.0,
                "weather_et0_total": 650.0,
                "weather_wind_mean": 12.0,
                "weather_solarrad_total": 2200.0,
            }
            med = defaults.get(col, 0.0)
        df[col] = df[col].fillna(med)
    print("Feature engineering complete\n")
    return df


def compute_crop_stats(train_df):
    stats = train_df.groupby("Crop")[YIELD_COL].agg(["mean", "std"]).rename(
        columns={"mean": "crop_mean", "std": "crop_std"})
    stats["crop_std"] = stats["crop_std"].replace(0, 1.0)
    return stats


def normalise(df_split, crop_stats):
    df_out = df_split.copy()
    for crop, idx in df_out.groupby("Crop").groups.items():
        if crop not in crop_stats.index:
            continue
        mu, std = crop_stats.loc[crop, "crop_mean"], crop_stats.loc[crop, "crop_std"]
        df_out.loc[idx, YIELD_COL]     = (df_out.loc[idx, YIELD_COL]     - mu) / std
        df_out.loc[idx, "Yield_Lag1"]  = (df_out.loc[idx, "Yield_Lag1"]  - mu) / std
        df_out.loc[idx, "Yield_Roll3"] = (df_out.loc[idx, "Yield_Roll3"] - mu) / std
        df_out.loc[idx, "Yield_Trend"] =  df_out.loc[idx, "Yield_Trend"]       / std
    return df_out


def denormalise(norm_pred, crop_series, crop_stats):
    result = np.empty(len(norm_pred))
    for i, (pred, crop) in enumerate(zip(norm_pred, crop_series)):
        if crop not in crop_stats.index:
            result[i] = pred
        else:
            result[i] = pred * crop_stats.loc[crop, "crop_std"] + crop_stats.loc[crop, "crop_mean"]
    return result


def prepare_matrices(train, test):
    X_tr = pd.get_dummies(train.drop(columns=DROP_COLS, errors="ignore"), drop_first=True)
    X_te = pd.get_dummies(test.drop(columns=DROP_COLS,  errors="ignore"), drop_first=True)
    X_te = X_te.reindex(columns=X_tr.columns, fill_value=0)
    sc = StandardScaler()
    return sc.fit_transform(X_tr), sc.transform(X_te), X_tr.columns.tolist(), sc


def rmse(a, p): return np.sqrt(mean_squared_error(a, p))
def mape(a, p): return np.mean(np.abs((a - p) / a)) * 100


def print_metrics(df_eval, label):
    a, p = df_eval["Yield_raw"].values, df_eval["pred"].values
    print(f"  {label:50s}  RMSE:{rmse(a,p):.4f}  MAPE:{mape(a,p):.2f}%  R²:{r2_score(a,p):.4f}")


def ts_cv_mape(estimator, X_tr, y_norm, year_all, train_df, crop_stats):
    """Time-series walk-forward CV MAPE (identical folds for all models)."""
    fold_mapes = []
    for val_year in range(9, 15):
        tr_mask = year_all < val_year
        va_mask = year_all == val_year
        if tr_mask.sum() == 0 or va_mask.sum() == 0:
            continue
        estimator.fit(X_tr[tr_mask], y_norm[tr_mask])
        val_rows    = train_df.iloc[np.where(va_mask)[0]]
        pred_real   = denormalise(estimator.predict(X_tr[va_mask]), val_rows["Crop"], crop_stats)
        actual_real = val_rows["Yield_raw"].values
        mask_core   = val_rows["Crop"].values != "Sugarcane"
        if mask_core.sum() > 0:
            fold_mapes.append(mape(actual_real[mask_core], pred_real[mask_core]))
    return np.mean(fold_mapes) if fold_mapes else np.inf


def tune_and_train(train_df, test_df, crop_stats):
    """
    Fast training mode: train ONLY XGBoost with fixed parameters.
    This skips hyperparameter tuning and skips RF/GB/Ridge/SVR.
    """
    y_norm = train_df[YIELD_COL].values
    X_tr, X_te, feat_cols, sc = prepare_matrices(train_df, test_df)

    print("FAST MODE: Training only XGBoost — no CV tuning, no RF/GB/Ridge/SVR.")

    xgb_model = xgb.XGBRegressor(
        n_estimators=250,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="reg:squarederror",
        random_state=42,
        n_jobs=-1,
        tree_method="hist",
    )
    xgb_model.fit(X_tr, y_norm)

    models = {"XGBoost": xgb_model}
    return models, feat_cols, sc


def fetch_forecast_weather(district, season, forecast_year):
    import datetime
    coords = get_district_coords(district)
    start_str, end_str = season_date_range(f"{forecast_year} - {forecast_year+1}", season)
    days_ahead = (datetime.date.fromisoformat(start_str) - datetime.date.today()).days

    if days_ahead > 16:
        print(f"  Season starts in {days_ahead} days — using 5-year climatology")
        records = []
        for y in range(forecast_year - 5, forecast_year):
            try:
                s, e = season_date_range(f"{y} - {y+1}", season)
                records.append(fetch_one(coords[0], coords[1], s, e))
                time.sleep(0.3)
            except Exception:
                pass
        if not records:
            raise RuntimeError("Could not fetch climatology")
        return {k: float(np.mean([r[k] for r in records if not np.isnan(r[k])])) for k in records[0]}
    else:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": coords[0], "longitude": coords[1],
            "start_date": start_str, "end_date": end_str,
            "daily": "temperature_2m_mean,precipitation_sum,"
                     "et0_fao_evapotranspiration,windspeed_10m_max,shortwave_radiation_sum",
            "timezone": "Asia/Kolkata",
        }
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        daily = resp.json()["daily"]
        rain  = daily.get("precipitation_sum", [])
        return {
            "weather_temp_mean":      np.nanmean([v for v in daily.get("temperature_2m_mean", []) if v]),
            "weather_rain_total":     np.nansum([v for v in rain if v]),
            "weather_rain_days":      sum(1 for r in rain if r and r > 1),
            "weather_et0_total":      np.nansum([v for v in daily.get("et0_fao_evapotranspiration", []) if v]),
            "weather_wind_mean":      np.nanmean([v for v in daily.get("windspeed_10m_max", []) if v]),
            "weather_solarrad_total": np.nansum([v for v in daily.get("shortwave_radiation_sum", []) if v]),
        }


def predict_with_live_weather(model, feat_cols, sc, crop_stats, df_history,
                               district, crop, season, area_ha, fertilizer,
                               pest_level, forecast_year, model_name="XGBoost"):
    pest_map = {"Low": 0, "Medium": 1, "High": 2}
    hist = df_history[(df_history["District_Name"] == district) &
                      (df_history["Crop"] == crop)].sort_values("Year")
    if len(hist) < 3:
        raise ValueError(f"Need ≥3 historical rows. Got {len(hist)}.")

    last3 = hist[YIELD_COL].values[-3:]
    yield_lag1  = float(last3[-1])
    yield_roll3 = float(np.mean(last3))
    yield_trend = float(np.polyfit(range(3), last3, 1)[0])
    normal_yield = float(hist[YIELD_COL].values[-5:].mean())

    wx = fetch_forecast_weather(district, season, forecast_year)
    print(f"  Rain: {wx['weather_rain_total']:.0f} mm | Temp: {wx['weather_temp_mean']:.1f}°C")

    mu, std = crop_stats.loc[crop, "crop_mean"], crop_stats.loc[crop, "crop_std"]

    row = {
        "District_Name": district, "Crop": crop,
        "Area (Hectare)": area_ha, "Fertilizer_kg_per_ha": fertilizer,
        "Pest_Disease_Incidence": pest_map.get(pest_level, 1),
        "Yield_Lag1":   (yield_lag1  - mu) / std,
        "Yield_Roll3":  (yield_roll3 - mu) / std,
        "Yield_Trend":   yield_trend       / std,
        **{k: wx[k] for k in WEATHER_FEATURES},
    }

    row_df = pd.get_dummies(pd.DataFrame([row]), drop_first=True)
    row_sc = sc.transform(row_df.reindex(columns=feat_cols, fill_value=0))
    pred_yield  = model.predict(row_sc)[0] * std + mu
    anomaly_pct = (pred_yield - normal_yield) / normal_yield * 100

    result = {
        "district": district, "crop": crop, "season": season,
        "forecast_year": forecast_year,
        "predicted_yield": round(pred_yield, 3),
        "normal_yield":    round(normal_yield, 3),
        "anomaly_pct":     round(anomaly_pct, 1),
        "alert":           anomaly_pct < -20,
        "weather_used":    {k: round(wx[k], 2) for k in WEATHER_FEATURES},
    }

    print(f"\n{'='*55}")
    print(f"  District : {district}  |  Crop: {crop}  |  Season: {season}")
    print(f"  Predicted: {pred_yield:.3f} t/ha  |  Normal: {normal_yield:.3f} t/ha")
    print(f"  Anomaly  : {anomaly_pct:+.1f}%")
    print(f"  {'⚠️  SHORTAGE ALERT' if result['alert'] else '✅ Within normal range'}")
    print(f"{'='*55}")
    return result


def run_pipeline():
    print("=" * 65)
    print(f"CROP YIELD MODEL — {STATE.upper()} — RETRAINED WITH SEASONAL WEATHER FEATURES")
    print("=" * 65 + "\n")

    df = load_and_join(DATA_PATH)
    df = engineer_features(df)

    test_years, future_year = [15, 16, 17], 18
    train_df  = df[~df["Year"].isin(test_years + [future_year])].copy()
    test_df   = df[df["Year"].isin(test_years)].copy()
    future_df = df[df["Year"] == future_year].copy()

    crop_counts = train_df["Crop"].value_counts()
    valid_crops = crop_counts[crop_counts >= 60].index.tolist()
    train_df    = train_df[train_df["Crop"].isin(valid_crops)].copy()
    test_df     = test_df[test_df["Crop"].isin(valid_crops)].copy()
    future_df   = future_df[future_df["Crop"].isin(valid_crops)].copy()
    print(f"Crops retained (>=60 training rows): {len(valid_crops)}\n")

    crop_stats = compute_crop_stats(train_df)
    for split in [train_df, test_df, future_df]:
        split["Yield_raw"] = split[YIELD_COL].copy()

    train_df  = normalise(train_df,  crop_stats)
    test_df   = normalise(test_df,   crop_stats)
    future_df = normalise(future_df, crop_stats)
    print(f"Train: {len(train_df)} | Test: {len(test_df)} | Future: {len(future_df)}\n")

    models, feat_cols, sc = tune_and_train(train_df, test_df, crop_stats)

    X_tr, X_te, _, _ = prepare_matrices(train_df, test_df)
    X_fut = pd.get_dummies(future_df.drop(columns=DROP_COLS, errors="ignore"), drop_first=True)
    X_fut = sc.transform(X_fut.reindex(columns=feat_cols, fill_value=0))

    # ── Evaluate all models ──────────────────────────────────────────────────
    all_metrics = {}
    print("\n" + "=" * 65)
    print("EVALUATION RESULTS — ALL MODELS")
    print("=" * 65)

    for name, model in models.items():
        test_pred_norm  = model.predict(X_te)
        fut_pred_norm   = model.predict(X_fut)

        all_test   = test_df[["Crop", "District_Name", "Yield_raw"]].copy().reset_index(drop=True)
        all_future = future_df[["Crop", "District_Name", "Yield_raw"]].copy().reset_index(drop=True)
        all_test["pred"]   = denormalise(test_pred_norm,  all_test["Crop"],   crop_stats)
        all_future["pred"] = denormalise(fut_pred_norm,   all_future["Crop"], crop_stats)

        core_test   = all_test[all_test["Crop"] != "Sugarcane"]
        core_future = all_future[all_future["Crop"] != "Sugarcane"]

        a_te, p_te   = all_test["Yield_raw"].values,   all_test["pred"].values
        a_fu, p_fu   = all_future["Yield_raw"].values, all_future["pred"].values
        ac_te, pc_te = core_test["Yield_raw"].values,  core_test["pred"].values
        ac_fu, pc_fu = core_future["Yield_raw"].values,core_future["pred"].values

        metrics = {
            "test_all":    {"rmse": float(rmse(a_te,p_te)),   "mape": float(mape(a_te,p_te)),   "r2": float(r2_score(a_te,p_te)),   "mae": float(np.mean(np.abs(a_te-p_te)))},
            "test_core":   {"rmse": float(rmse(ac_te,pc_te)), "mape": float(mape(ac_te,pc_te)), "r2": float(r2_score(ac_te,pc_te)), "mae": float(np.mean(np.abs(ac_te-pc_te)))},
            "future_all":  {"rmse": float(rmse(a_fu,p_fu)),   "mape": float(mape(a_fu,p_fu)),   "r2": float(r2_score(a_fu,p_fu)),   "mae": float(np.mean(np.abs(a_fu-p_fu)))},
            "future_core": {"rmse": float(rmse(ac_fu,pc_fu)), "mape": float(mape(ac_fu,pc_fu)), "r2": float(r2_score(ac_fu,pc_fu)), "mae": float(np.mean(np.abs(ac_fu-pc_fu)))},
        }
        all_metrics[name] = metrics

        print(f"\n  -- {name} --")
        print(f"  TEST  (all crops)  RMSE:{metrics['test_all']['rmse']:.4f}  MAPE:{metrics['test_all']['mape']:.2f}%  R²:{metrics['test_all']['r2']:.4f}  MAE:{metrics['test_all']['mae']:.4f}")
        print(f"  TEST  (core crops) RMSE:{metrics['test_core']['rmse']:.4f}  MAPE:{metrics['test_core']['mape']:.2f}%  R²:{metrics['test_core']['r2']:.4f}  MAE:{metrics['test_core']['mae']:.4f}")
        print(f"  FUTURE(all crops)  RMSE:{metrics['future_all']['rmse']:.4f}  MAPE:{metrics['future_all']['mape']:.2f}%  R²:{metrics['future_all']['r2']:.4f}  MAE:{metrics['future_all']['mae']:.4f}")
        print(f"  FUTURE(core crops) RMSE:{metrics['future_core']['rmse']:.4f}  MAPE:{metrics['future_core']['mape']:.2f}%  R²:{metrics['future_core']['r2']:.4f}  MAE:{metrics['future_core']['mae']:.4f}")

    print("=" * 65)

    # Save metrics JSON for dashboard
    import json
    with open(DATA_DIR / "model_comparison.json", "w") as f:
        json.dump(all_metrics, f, indent=2)
    print(f"\nModel comparison metrics saved to {DATA_DIR / 'model_comparison.json'}")

    # Feature importance (XGBoost only)
    xgb_model = models["XGBoost"]
    imp_df = pd.DataFrame({"feature": feat_cols,
                           "importance": xgb_model.feature_importances_}).sort_values(
                           "importance", ascending=False)
    imp_df["importance"] /= imp_df["importance"].sum()
    print("\nTop 20 Feature Importance (XGBoost):")
    print(imp_df.head(20).to_string(index=False))

    import pickle
    artefacts = {"model": xgb_model, "models": models, "feat_cols": feat_cols,
                 "scaler": sc, "crop_stats": crop_stats,
                 "model_metrics": all_metrics,
                 "df_history": df[["District_Name", "Crop", "Season", "Year", YIELD_COL,
                                    "Fertilizer_kg_per_ha", "Pest_Disease_Incidence"]
                                   + WEATHER_FEATURES]}
    with open(DATA_DIR / "model_artefacts.pkl", "wb") as f:
        pickle.dump(artefacts, f)
    print(f"\nModel artefacts saved to {DATA_DIR / 'model_artefacts.pkl'}")
    return artefacts


if __name__ == "__main__":
    artefacts = run_pipeline()

    # Uncomment to score a future district:
    # result = predict_with_live_weather(
    #     model=artefacts["model"], feat_cols=artefacts["feat_cols"],
    #     sc=artefacts["scaler"], crop_stats=artefacts["crop_stats"],
    #     df_history=artefacts["df_history"],
    #     district="Dhalai", crop="Rice", season="Kharif",
    #     area_ha=14000, fertilizer=62.0, pest_level="Low", forecast_year=2025,
    # )