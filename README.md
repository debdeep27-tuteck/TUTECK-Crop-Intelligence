# 🌾 TUTECK Crop Intelligence System

An enterprise-grade, multi-state AI precision agriculture & market intelligence platform — delivering **crop yield prediction**, **supply shortage alerts**, **smart crop recommendations**, **FAO-56 irrigation planning**, **multimodal AI disease diagnosis**, **geofenced parcel yield estimation**, **live farmer-mandi crop auctions**, **cold storage intelligence**, and **real-time APMC mandi commodity pricing**.

---

## 🌟 Supported States & Geographic Coverage

The platform operates on high-resolution localized agronomic, meteorological, and soil datasets:

| State | Districts | Key Crops Covered | Data & Model Directory |
|---|---|---|---|
| **Tripura** | 8 Districts (*Dhalai, Gomati, Khowai, North Tripura, Sepahijala, South Tripura, Unakoti, West Tripura*) | Rice, Wheat, Maize, Jute, Groundnut, Mustard, Sugarcane, Cotton, and more | `data_and_model/` |
| **Meghalaya** | 12 Districts (*East Garo Hills, East Jaintia Hills, East Khasi Hills, Ri Bhoi, West Khasi Hills, etc.*) | Paddy, Maize, Ginger, Turmeric, Potato, Black Pepper, Pulses, and more | `data_and_model_meghalaya/` |
| **Rajasthan** | 33 Districts (*Jaipur, Jodhpur, Bikaner, Kota, Udaipur, Barmer, Nagaur, Alwar, etc.*) | Mustard, Wheat, Bajra, Cotton, Guar Seed, Gram, Soyabean, and more | `data_and_model_rajasthan/` |

---

## 📐 System Architecture

The platform follows a high-performance **Microservices Architecture** orchestrated through a unified **API Gateway** with Role-Based Access Control (RBAC) and state-aware routing.

```
                              ┌─────────────────────────────────────────┐
                              │           Web Browser / Client          │
                              │   (Single-Page Navigation Hub & Portals) │
                              └────────────────────┬────────────────────┘
                                                   │
                                                   ▼
                              ┌─────────────────────────────────────────┐
                              │       API GATEWAY (Port: 8085)          │
                              │        backend/gateway.py               │
                              │  - Unified Static & HTML Asset Serving  │
                              │  - Auth & RBAC (Admin, Trader, Farmer)  │
                              │  - Multi-State Routing & Proxy Layer    │
                              └────────────────────┬────────────────────┘
                                                   │
       ┌──────────────┬──────────────┬─────────────┼─────────────┬──────────────┬──────────────┐
       ▼              ▼              ▼             ▼             ▼              ▼              ▼
┌──────────────┐┌──────────────┐┌──────────┐┌─────────────┐┌───────────┐┌──────────────┐┌──────────────┐
│ Tripura Crop ││Meghalaya Crop││Rajasthan ││ FAO-56 Smart││AI Disease ││Yield Detect  ││Crop Auction  │
│ Analytics    ││Analytics     ││Crop API  ││  Irrigation ││ Diagnosis ││& Geofencing  ││   Platform   │
│ (Port: 5000) ││(Port: 5002)  ││Port:5006 ││(Port: 5001) ││(Port:5004)││ (Port: 5008) ││ (Port: 5009) │
└──────┬───────┘└──────┬───────┘└────┬─────┘└──────┬──────┘└─────┬─────┘└──────┬───────┘└──────┬───────┘
       │               │             │             │             │             │               │
       │               │             │             │             │             ▼               ▼
       │               │             │             │             │       ┌───────────┐   ┌───────────┐
       │               │             │             │             │       │Yield Plat-│   │  Auction  │
       │               │             │             │             │       │form Core  │   │  Engine   │
       │               │             │             │             │       │(Port:6100)│   │(Port:6000)│
       │               │             │             │             │       └───────────┘   └───────────┘
       ▼               ▼             ▼             ▼             ▼
┌──────────────┐┌──────────────┐┌──────────┐┌─────────────┐┌───────────┐┌──────────────┐┌──────────────┐
│ Cold Storage ││ Mandi Market ││ Nearest  ││ Open-Meteo  ││   Groq    ││  SQLite DBs  ││ Scikit-Learn │
│ Intelligence ││    Prices    ││  Mandi   ││ Weather API ││  Vision   ││ users/auction││   & XGBoost  │
│ (Port: 5010) ││ (Port: 5011) ││Port:5012 ││  Historical ││Llama-3.2  ││ /cold_store  ││   Artifacts  │
│              ││ (data.gov.in)││          ││ & Forecast  ││  -90b-V   ││ /yield_lands ││ (.pkl files) │
└──────────────┘└──────────────┘└──────────┘└─────────────┘└───────────┘└──────────────┘└──────────────┘
```

---

## 🗂 Directory Structure

```
TUTECK-Crop-Intelligence/
│
├── backend/                                # Core Gateway & Central Services
│   ├── gateway.py                          # Unified API Gateway & static file server (Port 8085)
│   ├── main.py                             # One-command microservice launcher & process manager
│   ├── backend_2.py                        # Multi-state Crop Yield, Recommendation & Anomaly API
│   ├── yield_detect_backend.py             # Geofencing & Land Yield estimation service
│   ├── auction_backend.py                  # Farmer-mandi auction API
│   ├── auth/
│   │   ├── auth_excel.py                   # Role-Based Access Control, session management & users CRUD
│   │   └── users.db                        # SQLite user database
│   ├── auction.db                          # Auction listings, bids & transaction history
│   └── yield_lands.db                      # Registered geofenced parcel boundaries
│
├── micro_services/                         # Isolated Microservices
│   ├── auction/
│   │   ├── auction_engine_service.py       # Standalone HTTP Auction Engine (Port 6000)
│   │   └── generic_auction_engine.py       # Domain-agnostic high-concurrency bidding logic
│   ├── cold_storage/
│   │   ├── cold_storage_backend.py         # Cold storage intelligence API & directory (Port 5010)
│   │   └── cold_storage.db                 # Geocoded storage facilities & capacity database
│   ├── disease_detection/
│   │   └── disease_backend.py              # Multimodal AI crop disease diagnosis API (Port 5004)
│   ├── irrigation/
│   │   └── irrigation_backend2.py          # FAO-56 7-Day Precision Irrigation Advisor (Port 5001)
│   ├── mandi_prices/
│   │   └── mandi_prices_backend.py         # Daily APMC mandi commodity prices API (Port 5011)
│   ├── nearest_mandi/
│   │   └── nearest_mandi_backend.py        # Nearest APMC mandi locator & distance calculator (Port 5012)
│   └── yield-detect/
│       ├── yield_platform_service.py       # Spatial parcel storage & yield engine (Port 6100)
│       └── yield_platform_service.db       # Spatial polygon records
│
├── frontend/                               # Web UI Dashboards (HTML5 / Vanilla CSS / Modern JS)
│   ├── html/
│   │   ├── index.html                      # Unified portal shell & navigation container
│   │   ├── crop_dashboard.html             # Yield analytics & historical correlation dashboard
│   │   ├── crop_recommender.html           # AI crop suitability & recommendation UI
│   │   ├── alert_dashboard.html            # Real-time district supply shortage map & alert feed
│   │   ├── irrigation_advisory1.html       # 7-day soil moisture & irrigation planner
│   │   ├── disease_detection.html          # Leaf upload & visual AI disease diagnostic tool
│   │   ├── yield_detect.html               # Farm parcel list & yield summary overview
│   │   ├── yield_detect_editor.html        # Interactive Google Maps polygon boundary editor
│   │   ├── auction_farmer.html             # Farmer crop listing & bid monitoring interface
│   │   ├── auction_mandi.html              # Mandi trader marketplace & live bidding terminal
│   │   ├── cold_storage.html               # Cold storage directory & shelf-life advisor
│   │   ├── mandi_prices.html               # Daily mandi APMC price search & trend tracker
│   │   ├── nearest_mandi.html              # Nearest market yard locator
│   │   ├── login.html                      # Authentication & sign-in page
│   │   └── admin.html                      # User & district role administration panel
│   ├── css/                                # Modern dark/light styling & responsive tokens
│   ├── js/                                 # Client-side routing, API adapters & chart renderers
│   └── static/                             # Images, icons, and illustrations
│
├── data_and_model/                         # Tripura dataset, trained XGBoost model & weather cache
├── data_and_model_meghalaya/               # Meghalaya dataset, trained XGBoost model & weather cache
├── data_and_model_rajasthan/               # Rajasthan dataset, trained XGBoost model & weather cache
│
├── scripts/                                # Model Training & Pipeline Scripts
│   ├── crop_yield_with_weather_train_tripura.py
│   ├── crop_yield_with_weather_train_meghalaya.py
│   ├── crop_yield_with_weather_train_rajasthan.py
│   └── generate_alerts.py                  # Batch prediction & shortage anomaly generator
│
├── Dockerfile                              # Production containerization configuration
├── requirements.txt                        # Project Python dependencies
└── README.md
```

---

## ⚡ Key Modules & Features

### 1. 📈 Yield Analytics Dashboard (`/dashboard`)
- Historical crop yield analysis across districts, seasons, and soil types.
- Weather correlation matrices (Rainfall, Min/Max Temperature, Evapotranspiration $ET_0$).
- Pest incidence impact and fertilizer response distributions.

### 2. 🌱 AI Crop Recommender (`/recommender`)
- Inputs: State, District, Season, Soil Type, Irrigation Method, Fertilizer Plan, and Pest Risk.
- Ranks candidate crops with predicted yield (kg/ha) and comprehensive suitability scores using state-specific XGBoost regressors.

### 3. 🚨 Supply Shortage & Anomaly Alerts (`/alerts`)
- Evaluates district × crop × season combinations with live seasonal weather fetched from Open-Meteo.
- Automatic anomaly classification:
  - **CRITICAL ALERT**: Yield shortfall $\le -30\%$ relative to historical baseline.
  - **WATCH ALERT**: Yield shortfall between $-20\%$ and $-30\%$.

### 4. 💧 FAO-56 Smart Irrigation Advisory (`/irrigation`)
- Implements FAO-56 Penman-Monteith crop evapotranspiration ($ET_c = K_c \times ET_0$) calculations across 10+ major crop types and growth stages.
- Connects to Open-Meteo for 7-day daily forecast updates (precipitation, temperature, humidity, solar radiation).
- Generates dynamic watering schedules and soil moisture deficit advisories.

### 5. 🔬 AI Crop Disease Diagnosis (`/disease`)
- Instant photo upload or camera capture of diseased crop leaves.
- Multimodal computer vision analysis powered by Groq (Llama-3.2-90b-vision).
- Identifies disease name, pathogen category, confidence rating, visual symptoms, and step-by-step chemical and organic remedies.

### 6. 🗺️ Geofenced Land Yield Detection (`/yield-detect`, `/yield-detect-editor`)
- Interactive Google Maps geofencing tool to draw, edit, and save farm parcel polygon boundaries.
- Automatic parcel area calculation, historical land registry, and localized yield estimations based on regional soil and weather features.

### 7. 🏷️ Agricultural Marketplace & Crop Auction (`/auction`, `/auction-mandi`)
- **Farmer Portal**: List harvested crop batches with quantity, reserve price, harvest date, and quality parameters.
- **Trader Portal**: Live bidding terminal for registered APMC mandi merchants to browse active lots and place competitive bids with real-time countdowns.

### 8. ❄️ Cold Storage Intelligence (`/cold-storage`)
- Comprehensive cold storage directory mapped by state and district.
- Temperature, humidity, and atmospheric control guidelines per commodity.
- Shelf-life extension estimations and real-time facility vacancy tracking.

### 9. 📊 Mandi Prices & Market Intelligence (`/mandi-prices`, `/nearest-mandi`)
- Live & historical commodity price tracking across APMC mandis via `data.gov.in` (Agmarknet).
- Minimum, maximum, and modal price trends with interactive filters.
- Geolocation-based nearest APMC mandi locator with driving distance calculation.

### 10. 🔐 Role-Based Access Control & User Admin (`/login`, `/admin`)
- Role-specific access levels: `Farmer`, `Trader / Merchant`, `District Admin`, `State Admin`, and `Super Admin`.
- District-scoped data visibility: District admins are securely restricted to their designated jurisdiction.

---

## 🌐 Public Portals & Routing Matrix

When running the system, all pages and services are consolidated behind the Gateway on port **`8085`**:

| Route | Feature Description | Underlying Service Port |
|---|---|---|
| `/` or `/dashboard` | Main Navigation Hub & Yield Analytics | Gateway (`8085`) $\rightarrow$ Backend 2 (`5000`/`5002`/`5006`) |
| `/recommender` | AI Crop Recommendation Tool | Gateway (`8085`) $\rightarrow$ Backend 2 (`5000`/`5002`/`5006`) |
| `/alerts` | Shortage & Anomaly Alert Monitor | Gateway (`8085`) $\rightarrow$ `predictions.json` |
| `/irrigation` | FAO-56 7-Day Irrigation Planner | Gateway (`8085`) $\rightarrow$ Irrigation (`5001`) |
| `/disease` | AI Leaf Disease Diagnostic Tool | Gateway (`8085`) $\rightarrow$ Disease Backend (`5004`) |
| `/yield-detect` | Farm Land Yield Overview | Gateway (`8085`) $\rightarrow$ Yield Backend (`5008` / `6100`) |
| `/yield-detect-editor`| Full-Screen Geofencing Editor | Gateway (`8085`) $\rightarrow$ Standalone Editor UI |
| `/auction` | Farmer Crop Listing Portal | Gateway (`8085`) $\rightarrow$ Auction Backend (`5009` / `6000`) |
| `/auction-mandi` | Mandi Merchant Live Bidding Portal | Gateway (`8085`) $\rightarrow$ Auction Backend (`5009` / `6000`) |
| `/cold-storage` | Cold Storage Facility Advisor | Gateway (`8085`) $\rightarrow$ Cold Storage (`5010`) |
| `/mandi-prices` | Daily APMC Mandi Market Prices | Gateway (`8085`) $\rightarrow$ Mandi Prices (`5011`) |
| `/nearest-mandi` | Nearest Mandi Geolocation Finder | Gateway (`8085`) $\rightarrow$ Nearest Mandi (`5012`) |
| `/login` | User Authentication | Gateway (`8085`) $\rightarrow$ Auth (`auth_excel.py`) |
| `/admin` | Administration & User Role Manager | Gateway (`8085`) $\rightarrow$ Auth (`auth_excel.py`) |

---

## 🚀 Quick Start & Installation

### Prerequisites
- **Python 3.10+** (Python 3.11 recommended)
- Optional: **Groq API Key** (for multimodal AI disease detection) — configure in `backend/.env`

### 1. Clone & Setup Environment

```bash
git clone https://github.com/debdeep27-tuteck/TUTECK-Crop-Intelligence.git
cd TUTECK-Crop-Intelligence

# Create and activate virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux / macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables (Optional)

Create a `backend/.env` file:

```env
GROQ_API_KEY=your_groq_api_key_here
```

### 3. Launch All Services with One Command

Run the unified orchestrator from the project root:

```bash
python backend/main.py
```

This automatically starts all backend microservices, the API gateway, and opens your browser to `http://localhost:8085`.

---

## ⚙️ CLI Launcher Options

`backend/main.py` provides versatile CLI flags for targeted development and lightweight execution:

```bash
# Launch only Tripura backend + all shared microservices
python backend/main.py --state tripura

# Launch only Meghalaya backend + all shared microservices
python backend/main.py --state meghalaya

# Launch only Rajasthan backend + all shared microservices
python backend/main.py --state rajasthan

# Launch specific subset of states
python backend/main.py --states tripura,rajasthan

# Run headless (ideal for servers / Docker / CI)
python backend/main.py --no-browser

# Disable specific microservices if not required
python backend/main.py --no-disease --no-auction --no-cold-storage
```

---

## 🐳 Docker Deployment

The application is fully containerized for cloud or on-premise deployments:

```bash
# Build the Docker image
docker build -t tuteck-crop-intelligence:latest .

# Run the container
docker run -d -p 8085:8085 --name crop-intelligence tuteck-crop-intelligence:latest
```

Access the platform at `http://localhost:8085`.

---

## 🤖 Model Performance & Machine Learning Pipeline

Models are trained on multi-decade agricultural and meteorology records with cross-validation:

| Model Architecture | Test $R^2$ | Test MAPE | Future Set $R^2$ | Future Set MAPE | Status |
|---|---|---|---|---|---|
| **XGBoost Regressor** | **0.9981** | **7.48%** | **0.9965** | **10.16%** | **Selected Production Model** ✅ |
| GradientBoostingRegressor | 0.9981 | 6.99% | 0.9979 | 8.66% | Benchmark |
| Ridge Regression | 0.9983 | 9.12% | 0.9986 | 11.15% | Benchmark |
| Random Forest Regressor | 0.9966 | 7.90% | 0.9957 | 10.11% | Benchmark |
| Support Vector Regressor (SVR) | 0.9969 | 8.89% | 0.9947 | 11.56% | Benchmark |

### Retraining State Models
To retrain or update the models with new datasets:

```bash
# Train Tripura Model
python scripts/crop_yield_with_weather_train_tripura.py

# Train Meghalaya Model
python scripts/crop_yield_with_weather_train_meghalaya.py

# Train Rajasthan Model
python scripts/crop_yield_with_weather_train_rajasthan.py

# Regenerate shortage alert predictions
python scripts/generate_alerts.py
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgements & Integrations

- **Weather & Forecasts**: [Open-Meteo Weather API](https://open-meteo.com/) (Historical archives & 7-day numerical weather predictions)
- **Evapotranspiration Guidelines**: [FAO Irrigation and Drainage Paper 56](https://www.fao.org/3/x0490e/x0490e00.htm)
- **Market Data**: [Open Government Data (OGD) Platform India](https://data.gov.in/) & Agmarknet
- **Vision AI**: [Groq Cloud](https://groq.com/) (Llama-3.2 Vision Model Inference)

