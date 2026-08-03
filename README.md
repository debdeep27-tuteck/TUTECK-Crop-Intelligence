# 🌾 Tripura Crop Intelligence System

An end-to-end AI-powered precision agriculture platform for the state of Tripura, India — covering **crop yield prediction**, **shortage alerts**, **smart crop recommendations**, and **AI irrigation advisory** across all 8 districts and 15 crop varieties.

---

## 🚀 Live Demo

> *Streamlit app coming soon — deploy instructions below.*

---

## 📐 System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA LAYER                               │
│  merged_crop_enriched_features_del.xlsx                     │
│  (historical yield · weather · soil · pest · fertilizer)   │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│                  MODEL TRAINING                             │
│  crop_yield_with_weather.py                                 │
│  XGBoost + RF + GBM + Ridge + SVR · 33 features            │
│  → model_artefacts.pkl  · model_comparison.json            │
└────────────────────┬────────────────────────────────────────┘
                     │
       ┌─────────────┼────────────────┐
       ▼             ▼                ▼
 ┌───────────┐ ┌───────────┐  ┌─────────────────┐
 │  Flask    │ │  Alert    │  │  Irrigation     │
 │ backend   │ │ Generator │  │  Backend        │
 │ (port 5000│ │generate_  │  │ irrigation_     │
 │ )         │ │alerts.py  │  │ backend2.py     │
 └─────┬─────┘ └─────┬─────┘  └────────┬────────┘
       │             │                  │
       ▼             ▼                  ▼
 crop_recommender  alert_dashboard  irrigation_advisory1
 .html             .html            .html
 crop_dashboard
 .html
```

---

## 🗂 Project Structure

```
tripura-crop-intelligence/
│
├── 📊 Data & Model
│   ├── merged_crop_enriched_features_del.xlsx   # Historical dataset
│   ├── model_artefacts.pkl                       # Trained XGBoost + scaler + stats
│   ├── model_comparison.json                     # Benchmark across 5 models
│   ├── weather_cache.json                        # Open-Meteo API cache
│   └── predictions.json                          # Latest alert predictions
│
├── 🐍 Python Scripts
│   ├── crop_yield_with_weather.py               # Model training pipeline
│   ├── generate_alerts.py                        # Batch prediction & alert engine
│   ├── backend.py                                # Flask API for crop recommender & dashboard
│   └── irrigation_backend2.py                    # Flask API for irrigation advisory
│
├── 🌐 Web Frontends (HTML/JS — no build step)
│   ├── crop_recommender.html                     # AI crop recommendation UI
│   ├── crop_dashboard.html                       # Yield analytics dashboard
│   ├── alert_dashboard.html                      # Real-time shortage alert map
│   └── irrigation_advisory1.html                 # 7-day irrigation planner
│
├── 🚀 Streamlit App
│   └── streamlit_app.py                          # Unified Streamlit interface
│
├── requirements.txt
├── .gitignore
└── README.md
```

---

## ✨ Features

### 🔴 Shortage Alert System
- Runs XGBoost predictions for all **176 district × crop × season** combinations
- Fetches **real seasonal weather** from Open-Meteo archive API
- Flags **CRITICAL** (anomaly ≤ −30%) and **WATCH** (anomaly ≤ −20%) alerts
- Interactive map dashboard with district-level drill-down

### 🌱 Crop Recommender
- Input: district, season, soil type, irrigation type, fertilizer, pest level
- Output: ranked list of best crops with predicted yield and suitability score
- Backed by Flask API serving the trained XGBoost model

### 📈 Yield Analytics Dashboard
- Historical yield trends by crop, season, district
- Weather correlation scatter plots (rainfall, temperature, ET₀)
- Pest incidence breakdown, fertilizer usage analysis

### 💧 Irrigation Advisory
- FAO-56 crop coefficient method for 10 crop varieties
- Fetches **7-day weather forecast** from Open-Meteo
- Recommends irrigation schedule by growth stage
- Soil moisture deficit calculation

---

## ⚙️ Setup & Usage

### Prerequisites
```bash
Python 3.10+
```

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

> ⚠️ The full `requirements.txt` includes the complete training environment. For running the app only, the minimal set is:
> ```bash
> pip install flask flask-cors pandas numpy xgboost scikit-learn openpyxl requests streamlit plotly
> ```

### 2. (Optional) Retrain the model
```bash
python crop_yield_with_weather.py
```
This fetches weather data from Open-Meteo and trains all 5 models. Takes ~15–30 min depending on API rate limits.

### 3. Generate fresh predictions
```bash
python generate_alerts.py
```
Outputs `predictions.json` — required by `alert_dashboard.html`.

### 4a. Run the HTML dashboards (Flask backend required)
```bash
# Terminal 1 — Crop recommender & dashboard backend
python backend.py         # http://localhost:5000

# Terminal 2 — Irrigation advisory backend
python irrigation_backend2.py  # http://localhost:5001
```
Then open any `.html` file in your browser.

### 4b. Run the Streamlit app
```bash
streamlit run streamlit_app.py
```
Opens at `http://localhost:8501`

---

## 🤖 Model Performance

| Model | Test R² | Test MAPE | Future R² | Future MAPE |
|---|---|---|---|---|
| **XGBoost** ✅ | 0.9981 | 7.48% | 0.9965 | 10.16% |
| GradientBoosting | 0.9981 | 6.99% | 0.9979 | 8.66% |
| Ridge | 0.9983 | 9.12% | 0.9986 | 11.15% |
| RandomForest | 0.9966 | 7.90% | 0.9957 | 10.11% |
| SVR | 0.9969 | 8.89% | 0.9947 | 11.56% |

XGBoost was selected for production due to its best balance of test and future-set performance.

---

## 🌍 Coverage

- **8 Districts**: Dhalai, Gomati, Khowai, North Tripura, Sepahijala, South Tripura, Unakoti, West Tripura
- **15 Crops**: Rice, Wheat, Maize, Jute, Groundnut, Mustard, Sugarcane, Cotton, and more
- **6 Seasons**: Kharif, Rabi, Autumn, Summer, Winter, Whole Year
- **Weather source**: [Open-Meteo](https://open-meteo.com/) (free, no API key required)

---

## 📦 Key Files — What to Commit vs Exclude

| File | Commit? | Reason |
|---|---|---|
| `*.py` | ✅ Yes | Core logic |
| `*.html` | ✅ Yes | Frontends |
| `*.json` | ✅ Yes | Config & cached results |
| `*.xlsx` | ✅ Yes | Dataset (~200 KB) |
| `model_artefacts.pkl` | ⚠️ Optional | 24 MB — use Git LFS or exclude |
| `requirements.txt` | ✅ Yes | |

> To use Git LFS for the pickle file:
> ```bash
> git lfs install
> git lfs track "*.pkl"
> git add .gitattributes
> ```

---

## 🚀 Deploy to Streamlit Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub repo
4. Set **Main file**: `streamlit_app.py`
5. Add any secrets if needed (none required for this app)
6. Click **Deploy**

> **Note**: `model_artefacts.pkl` (24 MB) must be in the repo or loaded from cloud storage. Streamlit Cloud supports files up to 100 MB.

---

## 📄 License

MIT License — see [LICENSE](LICENSE)

---

## 🙏 Acknowledgements

- Weather data: [Open-Meteo](https://open-meteo.com/)
- Crop coefficient method: [FAO Irrigation and Drainage Paper 56](https://www.fao.org/3/x0490e/x0490e00.htm)
- Dataset: Tripura agricultural statistics (district-level)
