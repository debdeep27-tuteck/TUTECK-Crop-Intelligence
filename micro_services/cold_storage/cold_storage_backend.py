"""
cold_storage_backend.py

Backend service for "Cold Storage Intelligence": helps a farmer answer
    "If I grow this crop in my district, will there likely be enough
     registered cold-storage capacity during my harvest period?"

V1 is deterministic and transparent — no ML shortage prediction. Every
number returned has a source, a timestamp, and a data_quality tag.

This mirrors yield_detect_backend.py's conventions on purpose (same repo,
same patterns): a single-file Flask service, its own SQLite DB, and no
auth of its own — it verifies bearer tokens by asking the gateway's
/api/auth/me, the same way yield_detect_backend.py does, since the actual
session store (auth_excel.py's in-memory SESSIONS dict) lives inside the
gateway process, not here.

Run standalone:
    pip install flask flask-cors requests
    python cold_storage_backend.py --port 5010

Wire into main.py / gateway.py:
    - main.py launches this on port 5010 alongside the other services.
    - gateway.py forwards /api/cold-storage/* -> http://127.0.0.1:5010/api/cold-storage/*
      (see main.py / gateway.py changes shipped alongside this file).

Owned tables (this service's own SQLite DB — cold_storage.db):
    cold_storages               — individual cold-storage facilities (NHB or seeded)
    cold_storage_crop_capacity  — per-facility, per-crop capacity (a facility
                                   doesn't necessarily support every crop)
    farmer_crop_storage_plans   — a farmer's planned production + storage need
    storage_snapshots           — timestamped occupancy reports per facility

This service does NOT own farmer/user/crop/district/state master data —
those already live in auth_excel.py (users) and the per-page hardcoded
district lists (crop_recommender.html, crop_dashboard.html, admin.html).
There is no /api/crops or /api/districts endpoint in crop-intelligence yet,
so this service accepts state/district/crop as plain strings, matching the
Title-Case convention already used everywhere else (e.g. "Dhalai",
"Potato") rather than inventing IDs for entities crop-intelligence doesn't
have tables for.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import logging
import math
import os
import random
import sys
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS

# This file lives in micro_services/cold_storage/, several directories away
# from backend/shared/ — resolve backend/ relative to this file's own
# location, same fix as every other converted micro-service.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from shared.db import connect as db_connect, PGConnection
from shared.cache import ttl_cache, invalidate_prefix

logger = logging.getLogger("cold_storage")
logging.basicConfig(level=logging.INFO)

# ── CONFIG ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent

# Same pattern as yield_detect_backend.py — verify tokens via the gateway.
GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

# yield_platform_service.py owns farmer parcel/land data (the "Yield Detect"
# database). It's a separate microservice gated by its own shared API key —
# see yield_platform_service.py's docstring for the auth model.
YIELD_PLATFORM_URL = os.environ.get("YIELD_PLATFORM_URL", "http://127.0.0.1:6100")
YIELD_PLATFORM_API_KEY = os.environ.get("YIELD_PLATFORM_SERVICE_API_KEY", "")
YIELD_INTERNAL_URL = os.environ.get("YIELD_INTERNAL_URL", "http://127.0.0.1:5008")

# ── CAPACITY STATUS THRESHOLDS (config, not hardcoded logic) ──────────────────
# Utilization % -> status label. Checked in ascending order; first match wins.
# Override individual cutoffs with env vars if you need to tune them without
# touching code, e.g. COLD_STORAGE_THRESHOLD_LIMITED=85
STATUS_THRESHOLDS = [
    (float(os.environ.get("COLD_STORAGE_THRESHOLD_GOOD", 60)), "GOOD"),
    (float(os.environ.get("COLD_STORAGE_THRESHOLD_AVAILABLE", 80)), "AVAILABLE"),
    (float(os.environ.get("COLD_STORAGE_THRESHOLD_LIMITED", 90)), "LIMITED"),
    (float(os.environ.get("COLD_STORAGE_THRESHOLD_VERY_LIMITED", 100)), "VERY_LIMITED"),
    (float("inf"), "SHORTAGE"),
]

VALID_STORAGE_TYPES = {"static", "cold_room", "controlled_atmosphere", "other"}
VALID_PLAN_STATUSES = {"draft", "registered", "cancelled"}

app = Flask(__name__)
CORS(app)


# ── STORAGE STATUS ENGINE (pure, deterministic, unit-testable) ───────────────
# This is the one calculation everything else in this file feeds into. Keep
# it a pure function — no DB access — so it's trivial to unit test in
# isolation (see tests/test_storage_status.py).

def classify_status(utilization_percent: float | None) -> str:
    """Utilization % -> status label using STATUS_THRESHOLDS."""
    if utilization_percent is None:
        return "UNKNOWN"
    for cutoff, label in STATUS_THRESHOLDS:
        if utilization_percent < cutoff:
            return label
    return STATUS_THRESHOLDS[-1][1]


def calculate_storage_status(
    total_capacity_mt: float,
    occupied_mt: float = 0.0,
    reserved_mt: float = 0.0,
    committed_mt: float = 0.0,
    expected_releases_mt: float = 0.0,
    registered_demand_mt: float = 0.0,
) -> dict:
    """
    The one formula everything in this service is built around:

        Storage Capacity
                -
        Current Occupancy
                -
        Existing Reservations
                -
        Committed Capacity
                +
        Expected Releases
                -
        Registered Farmer Demand
                =
        Projected Available Capacity

    All inputs/outputs are in metric tonnes (MT) except *_percent. Never
    clamps negative values — a negative projected_available_mt is exactly
    the signal a SHORTAGE status is built on, and hiding it would make the
    numbers lie.
    """
    total_capacity_mt = float(total_capacity_mt or 0)
    occupied_mt = float(occupied_mt or 0)
    reserved_mt = float(reserved_mt or 0)
    committed_mt = float(committed_mt or 0)
    expected_releases_mt = float(expected_releases_mt or 0)
    registered_demand_mt = float(registered_demand_mt or 0)

    projected_available_mt = (
        total_capacity_mt
        - occupied_mt
        - reserved_mt
        - committed_mt
        + expected_releases_mt
        - registered_demand_mt
    )

    committed_utilization_mt = occupied_mt + reserved_mt + committed_mt - expected_releases_mt + registered_demand_mt

    if total_capacity_mt > 0:
        projected_utilization_percent = round((committed_utilization_mt / total_capacity_mt) * 100, 1)
    else:
        projected_utilization_percent = None

    return {
        "total_capacity_mt": round(total_capacity_mt, 2),
        "occupied_mt": round(occupied_mt, 2),
        "reserved_mt": round(reserved_mt, 2),
        "committed_mt": round(committed_mt, 2),
        "expected_releases_mt": round(expected_releases_mt, 2),
        "registered_demand_mt": round(registered_demand_mt, 2),
        "projected_available_mt": round(projected_available_mt, 2),
        "projected_utilization_percent": projected_utilization_percent,
        "status": classify_status(projected_utilization_percent),
    }


# ── DB SCHEMA ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cold_storages (
    id                  SERIAL PRIMARY KEY,
    nhb_id              TEXT,                 -- NHB registry ID, once imported; NULL for seed/manual entries
    name                TEXT NOT NULL,
    state               TEXT NOT NULL,
    district            TEXT NOT NULL,
    block               TEXT,
    village             TEXT,
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    total_capacity_mt   DOUBLE PRECISION NOT NULL DEFAULT 0,
    storage_type        TEXT DEFAULT 'static',
    source              TEXT NOT NULL DEFAULT 'SEED',   -- 'NHB' | 'SEED' | 'MANUAL'
    source_year         INTEGER,
    data_quality        TEXT NOT NULL DEFAULT 'OFFICIAL_STATIC',
    last_updated        TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cold_storage_crop_capacity (
    id                  SERIAL PRIMARY KEY,
    cold_storage_id     INTEGER NOT NULL REFERENCES cold_storages(id) ON DELETE CASCADE,
    crop                TEXT NOT NULL,
    capacity_mt         DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(cold_storage_id, crop)
);

CREATE TABLE IF NOT EXISTS farmer_crop_storage_plans (
    id                      SERIAL PRIMARY KEY,
    user_email              TEXT NOT NULL,        -- owning farmer, from the auth token
    state                   TEXT NOT NULL,
    district                TEXT NOT NULL,
    crop                    TEXT NOT NULL,
    area                    DOUBLE PRECISION,
    area_unit               TEXT DEFAULT 'hectare',
    sowing_date             TEXT,
    expected_harvest_date   TEXT NOT NULL,
    expected_production_mt  DOUBLE PRECISION NOT NULL,
    storage_required_mt     DOUBLE PRECISION NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'registered',  -- draft | registered | cancelled
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_snapshots (
    id                  SERIAL PRIMARY KEY,
    cold_storage_id     INTEGER NOT NULL REFERENCES cold_storages(id) ON DELETE CASCADE,
    occupied_mt         DOUBLE PRECISION NOT NULL DEFAULT 0,
    reserved_mt         DOUBLE PRECISION NOT NULL DEFAULT 0,
    committed_mt        DOUBLE PRECISION NOT NULL DEFAULT 0,
    expected_release_mt DOUBLE PRECISION NOT NULL DEFAULT 0,
    data_quality        TEXT NOT NULL DEFAULT 'OPERATOR_REPORTED',  -- OPERATOR_REPORTED | API_REPORTED | IOT_REPORTED
    recorded_at         TEXT NOT NULL,
    recorded_by         TEXT
);

CREATE INDEX IF NOT EXISTS idx_cs_state_district ON cold_storages(state, district);
CREATE INDEX IF NOT EXISTS idx_cscc_crop ON cold_storage_crop_capacity(crop);
CREATE INDEX IF NOT EXISTS idx_plans_district_crop ON farmer_crop_storage_plans(state, district, crop);
CREATE INDEX IF NOT EXISTS idx_snapshots_cs ON storage_snapshots(cold_storage_id, recorded_at);

-- Owned by storage_provider_backend.py originally, but this file also
-- mounts the storage-provider routes itself (see the __main__ block below),
-- and main.py may only ever launch this file, never storage_provider_backend.py
-- as its own process. If that table's creation lives only in the other
-- file's init_db(), it never runs — so it's created here too. Both files'
-- init_db() use CREATE TABLE IF NOT EXISTS, so having it in both places is
-- harmless no matter which service(s) actually get started.
CREATE TABLE IF NOT EXISTS storage_provider_configs (
    id                  SERIAL PRIMARY KEY,
    facility_id         INTEGER NOT NULL REFERENCES cold_storages(id) ON DELETE CASCADE,
    provider_email      TEXT NOT NULL,
    free_capacity_mt    DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_active           INTEGER NOT NULL DEFAULT 1,
    notes               TEXT,
    updated_at          TEXT NOT NULL,
    UNIQUE(facility_id, provider_email)
);
CREATE INDEX IF NOT EXISTS idx_spc_provider ON storage_provider_configs(provider_email);
CREATE INDEX IF NOT EXISTS idx_spc_facility ON storage_provider_configs(facility_id);
"""


def init_db():
    conn = db_connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ── REAL NHB-registered Rajasthan facilities ──────────────────────────────────
# Facility NAMES sourced directly from NHB's own live facility database
# (https://www.nhb.gov.in/Handlers/GeoIndiaHandlerGis.ashx), filtered to
# State = Rajasthan — these are REAL facility names, verbatim from NHB's
# registry, not invented.
#
# NHB's public feed does NOT expose per-facility capacity, storage type, or
# crop mix. Where possible, capacity was separately cross-matched by facility
# NAME against napanta.com's public cold-storage directory (a private
# farmer-facing agri platform, not a government source) — see caveat above
# NHB_FACILITIES_RAJASTHAN. That cross-match is currently done for Jaipur,
# Ajmer, Alwar, and Bikaner only; every other district's facilities still
# have total_capacity_mt=0 / data_quality='CAPACITY_TBD' because no capacity
# source was matched for them yet. No crop-capacity rows exist for ANY of
# these facilities (crop mix isn't known for any of them) — so none of them
# populate the district crop-capacity cards; they only show via
# /api/cold-storage/facilities.
#
# Tripura and Meghalaya are deliberately absent from this list: NHB's
# registry and per-state facility lists turned up zero NHB-registered
# private cold storage facilities for either state — consistent with their
# minimal cold-chain infrastructure, not a gap in this data.
# Each row is (district, name, capacity_mt_or_None). Where capacity_mt is not
# None, it was cross-matched by facility name against napanta.com's public
# cold-storage directory (a farmer-facing agri platform, NOT a government
# registry). Where None, no capacity figure was found anywhere and it stays
# CAPACITY_TBD.
#
# CAVEAT: napanta capacities are self-reported / directory data, not verified
# against an authoritative source, and matching was done by facility-name
# similarity (not a shared ID) — treat these as indicative, not audited.
# data_quality is set to 'UNVERIFIED_CAPACITY' (not 'OFFICIAL_STATIC') so
# that distinction stays visible everywhere this is used.
NHB_FACILITIES_RAJASTHAN = [
    ("Jaipur", "M D COLD STORAGE", 3000), ("Jaipur", "ADINATH INDUSTRIES AND COLD STORAGE", 3000),
    ("Jaipur", "JAIPUR COLD STORAGE PVT LTD", 220), ("Jaipur", "I.G.INTERNATIONAL PVT. LTD.", 3000),
    ("Jaipur", "SATNAM SAKSHI COLD STORAGE PRIVATE LIMITED", 3000), ("Jaipur", "ARAVALI TRADE VISION PRIVATE LIMITED", 3000),
    ("Jaipur", "NAKODA MAHAVIR AGRO INFRA PVT. LTD", 3000), ("Jaipur", "SRI BALAJI FRESH PRODUCT", 3000),
    ("Jaipur", "SARAH DAIRY", 3000), ("Jaipur", "PINK CITY COLD STORAGE", 4000),
    ("Jaipur", "SAROJ COLD STORAGE", 2260), ("Jaipur", "BABA GANESH COLD STORAGE AND ICE FACTORY", 4000),
    ("Jaipur", "BHAGWATI UDYOG", 7995), ("Jaipur", "V.K. COLD STORAGE", 2500),
    ("Jaipur", "HIRA COLD STORAGE", 5894), ("Jaipur", "ANNAPURNA COLD STORAGE", 25000),
    ("Jaipur", "RACHIT COLD STORAGE", 3000), ("Jaipur", "MOUNT COLD STORAGE", 10000),
    ("Jaipur", "RANCHOD NATH COLD STORAGE", 360), ("Jaipur", "VISHANDAS COLD STORAGE PVT LTD", 3380),
    ("Jaipur", "GAURI ENTERPRISES COLD STORAGE AND ICE FACTORY", 12000),
    ("Alwar", "RSAMB COLD STORAGE", 4000), ("Alwar", "JAYANTI COLD STORAGE", 200),
    ("Ajmer", "SUBHLAXMI COLD STORAGE & ICE FACTORY PVT. LTD.", 2000), ("Ajmer", "SHREE JI COLD STORAGE", 1000),
    ("Ajmer", "K.M. COLD STORAGE AND ICE FACTORY PVT LTD", 2800),
    ("Bikaner", "SHRI POONRASAR ENTERPRISES PRIVATE LIMITED", 900), ("Bikaner", "NOKHA WARE HOUSE", 3000),
    ("Bikaner", "JAIN COLD STORE", 3000), ("Bikaner", "SUSHMA DEVI PROP SHREE RAM COLD STORAGE", 3000),
    ("Bikaner", "BIKANER COLD STORAG", 1600), ("Bikaner", "PARIK COLD STORAGE", 4000),
    ("Bikaner", "CHTTRA FACTORY / COLD STORAGE", 1858), ("Bikaner", "NAGARJUN COLD STORAGE PVT", 1200),
    ("Bikaner", "APNA COLD STORE", 3000), ("Bikaner", "NOKHA COLD STORAGE", 2000),
    ("Bharatpur", "SHREE GANESH PVT LTD", 3000), ("Bharatpur", "AKASH COLD STORAGE PVT LTD", 3000),
    ("Bharatpur", "KAMLESH COLD STORAGE", 3000), ("Bharatpur", "GOLDEN COLD STORAGE", 3000),
    ("Bharatpur", "KARAN SHANTI COLD STORAGE PVT LTD", 3000), ("Bharatpur", "BHARATPUR COLD STORAGE PVT LTD", 3000),
    ("Bharatpur", "NADBAI COLD STORAGE PVT LTD", 3000), ("Bharatpur", "VAIBHAV COLD STORAGE", 3000),
    ("Kota", "MAHALAXMI ASSOCIATION COLD STORAGE & ICE FACTORY", 3000), ("Kota", "MAYUR COLD STORAGE & ICE FACTORY", 3000),
    ("Udaipur", "UDAIPUR COLD STORAGE", 3000), ("Udaipur", "PATAAL GANGA COLD STORAGE", 3000),
    ("Udaipur", "MAHARAJA COLD STORAGE", 3000), ("Udaipur", "SHIDH VINAYAK KARPORESAN", 3000),
    ("Jodhpur", "ANNAPURNA COLD STORAGE AND WARE HOUSE", 3000), ("Jodhpur", "VINAYAK COLD STORAGE & WAREHOUSE", 3000),
    ("Jodhpur", "TEJPARAS COLD STORAGE", 3000), ("Jodhpur", "RATHI COLD STORAGE", 3000),
    ("Sikar", "HARI AGRO INDUSTRIES", 3000), ("Sikar", "MOUNT MALTBRU LIMITED", 3000),
    ("Nagaur", "SHRI MAHESH COLD STORAGE", 3000), ("Nagaur", "LAXMI COLD STORAGE", 3000),
    ("Bhilwara", "STERLITE AGRO LIMITED", 3000),
    ("Dholpur", "SRI GAJANAND SHEETGRAH PRIVATE LIMITED", 3000), ("Dholpur", "PRATAP COLD STORAGE PVT. LTD.", 3000),
    ("Dholpur", "MAA KAILA DEVI COLD STORAGE", 3000), ("Dholpur", "SAIPAU COLD STORAGE", 3000),
    ("Dholpur", "MAA REHNAWALI SHEETGRAH LLP", 3000), ("Dholpur", "SHIVA COLD STORAGE", 3000),
    ("Dholpur", "AGRASEN COLD STORAGE", 3000), ("Dholpur", "TYAGI COLD STORAGE PVT", 3000),
    ("Dholpur", "KAYLA DEVI COLD STORAGE PVT", 3000), ("Dholpur", "MITTA COLD STORAGE DHOLPUR", 3000),
    ("Dholpur", "MAHARAJA SHIT GREHAYA", 3000), ("Dholpur", "MATRELAL SHANKARLAL COLD STORAGE PVT", 3000),
]

# Additional real facilities found in napanta's directory that are NOT in
# NHB's registry feed at all (napanta-only — both existence and capacity are
# from napanta, no NHB cross-match). Filtered from raw napanta district pages
# by dropping personal/farmer-name rows: some napanta district pages (e.g.
# Bikaner, 245 raw rows) mix genuine commercial cold storages with individual
# farmer warehouse-receipt registrants under the same template — those
# personal-name rows were excluded, not just ones lacking a capacity figure.
NAPANTA_ONLY_FACILITIES_RAJASTHAN = [
    ("Jaipur", "Annapurna CS", 8120), ("Jaipur", "Jain Arihant CS & IF", 3596),
    ("Jaipur", "Jain Arihant Cold Storage", 3598), ("Jaipur", "Jain Saraogi Cold Storage", 3666),
    ("Jaipur", "Jain Saraogi Cold Storage P. Ltd", 3665), ("Jaipur", "Jhuramal Cold Storaga And Ice Factory", 3200),
    ("Jaipur", "KOTPUTLI", 5000), ("Jaipur", "Keshav CS", 5584), ("Jaipur", "Keshav Cold Storage", 5584),
    ("Jaipur", "M/S Anukul Agro Tech, Bassi", 760), ("Jaipur", "M/S Bhagwati Udyog (Expansion)", 3200),
    ("Jaipur", "M/S Narayan Lal Sharma Agro Facilities, Bhankrota, Basri", 4775),
    ("Jaipur", "M/S Sardarmal Cold Storage", 3400), ("Jaipur", "Ms Sumit Ice Manufacture", 3200),
    ("Jaipur", "Nagpal CS", 8191), ("Jaipur", "Pansari CS & IF", 4702),
    ("Jaipur", "Rachit Cold Storage (Sitapura)", 4438),
    ("Jaipur", "Rajasthan State Warehousing Corp, Biharipura, Chomu", 5850),
    ("Jaipur", "SITAPURA-I", 14870), ("Jaipur", "SITAPURA-II", 11729),
    ("Jaipur", "Sardarmal Cold Storage And Ice Factory", 7200),
    ("Jaipur", "Sarvodaya CS", 7187), ("Jaipur", "Shobhraj Cold Storage", 2500),
    ("Jaipur", "Shri Narain Jat, Amer", 5860), ("Jaipur", "Shubhlaxmi CS", 4739),
    ("Jaipur", "Shyam Kripa Agry Cold Ltd.", 7500), ("Jaipur", "Sitapura CS", 6917),
    ("Jaipur", "Smt Raj Kumari Agarwal, Morija, Chomu", 10000),
    ("Jaipur", "Smt Gayatri Devi Agarwal, Chomu", 9762),
    ("Jaipur", "V K Cold Storage", 3390),
    ("Ajmer", "RSAMB Cold Storage, New Mandi Yard", 4000),
    ("Ajmer", "Rajasthan State Warehousing Corp, Beawar Road", 3600),
    ("Ajmer", "Balu Dhakar, Sarwar Tehsil", 837), ("Ajmer", "Laxmi Warehouse, Tabiji", 650),
    ("Ajmer", "Mohini Warehouse, Naseerabad", 1853), ("Ajmer", "Salasar Balaji Gramin Bhandar, Sarwar", 5000),
    ("Alwar", "ALWAR (RSWC)", 3574), ("Alwar", "Alwer Zila Dugdh Utpadak Sahkari Sangh Ltd.", 150),
    ("Alwar", "BHIWADI (RSWC)", 4356), ("Alwar", "Central Warehouse, Alwar", 14849),
    ("Alwar", "Certral Warehouse, Alwar", 8133),
    ("Alwar", "M/S Raghu Warehouse Samola, Alwar", 4085), ("Alwar", "M/S Renu Agarwal, Alwar", 2000),
    ("Alwar", "M/S Shree Shubham Logistics, Alwar", 19945),
    ("Alwar", "Satyavrat Bansal, Kathumar", 4029), ("Alwar", "Shree Balaji Trading, Alwar", 2836),
    ("Alwar", "Shri Rohit Goyal, Alwar", 3000),
    ("Bikaner", "BIKANER (RSWC)", 25400), ("Bikaner", "BIKANER-II (RSWC)", 5000),
    ("Bikaner", "Atal Warehouse", 1389), ("Bikaner", "Bisme Rabbik Sortex", 2500),
    ("Bikaner", "Jambheshwar Warehousing", 1848), ("Bikaner", "LTC Commercial Pvt. Ltd., Husangar", 13365),
    ("Bikaner", "M/S Bikaner Agro Services", 8563), ("Bikaner", "M/S Bisme Rabbick Cold Storage", 3500),
    ("Bikaner", "M/S Kansal Warehouse", 2854), ("Bikaner", "M/S Kishan Agro Services", 8243),
    ("Bikaner", "M/S Lalita Udyog, Bikasar", 4638), ("Bikaner", "M/S Navkar Agro Services", 8069),
    ("Bikaner", "M/S Nokha Agro Services", 8693), ("Bikaner", "M/S Raghav Agro Services", 8279),
    ("Bikaner", "M/S Rathi Cold Storage Pvt. Ltd.", 5700), ("Bikaner", "M/S Shree Balaji Agro Service", 11786),
    ("Bikaner", "M/S Shree Shubham Logistics, Bikaner, Bassi", 19500),
    ("Bikaner", "M/S Swadeshi Industries", 984), ("Bikaner", "Nokha CS (Expansion)", 2340),
    ("Bikaner", "Nokha Cold Storage (P) Ltd.", 2000), ("Bikaner", "Nokha Cold Store Pvt Ltd", 3240),
    ("Bikaner", "Pareek CS", 3184), ("Bikaner", "RajRatan CS", 2293),
    ("Bikaner", "Shree Bikaner C S", 2332), ("Bikaner", "Thar Warehouse Pvt. Ltd.", 13478),
    ("Bikaner", "Urmul Dairy", 5),
]
# STATUS: only Jaipur, Ajmer, Alwar, Bikaner districts have been pulled and
# filtered from napanta so far (of 33 Rajasthan districts). The remaining 29
# follow the same process: fetch napanta.com/cold-storage/rajasthan/<district>,
# drop personal-name rows, cross-match capacity into NHB_FACILITIES_RAJASTHAN
# by name where it matches, add napanta-only facilities to the list above
# otherwise. Not yet done for the other 29 districts.

# ── Real Tripura & Meghalaya facilities ───────────────────────────────────────
# Unlike Rajasthan, NHB's own registry has ZERO entries for either state
# (confirmed by direct search — consistent with their minimal cold-chain
# infrastructure). These facilities instead come from:
#   - Tripura: napanta.com's cold-storage directory (a farmer-facing agri
#     platform), which lists facility name, address, manager, contact, and
#     capacity. Fetched directly per-district.
#   - Meghalaya: MEGAMB (Meghalaya State Agricultural Marketing Board)'s own
#     scheme page and a Meghalaya government CM press release (meghalaya.gov.in).
#
# (state, district, name, total_capacity_mt or None if unknown, data_quality)
TRIPURA_MEGHALAYA_FACILITIES = [
    # Tripura — West Tripura (napanta.com, contact: Rupak Saha, 0381 2370384)
    ("tripura", "West Tripura", "AGARTALA", 19583, "OFFICIAL_STATIC"),
    ("tripura", "West Tripura", "AGARTALA C.S.", 4750, "OFFICIAL_STATIC"),
    # Tripura — Khowai (napanta.com, Gamaibari; manager Swapan Devnath, 9436556295)
    ("tripura", "Khowai", "Teliamura 500 Mt Cap Cold Storage", 500, "OFFICIAL_STATIC"),
    # Meghalaya — East Khasi Hills: state govt scheme (MEGAMB), stores potato/
    # pineapple/ginger/citrus/flowers/vegetables; no public capacity figure found
    ("meghalaya", "East Khasi Hills", "MEG COLD STORAGE, Mawiong", None, "CAPACITY_TBD"),
    # Meghalaya — West Garo Hills: solar-powered, CM-inaugurated Feb 2022,
    # funded by MEGHA-LAMP & SELCO Foundation, ~Rs 15 lakh cost
    ("meghalaya", "West Garo Hills", "Rengsingpara IVCS Solar Cold Storage", 5, "OFFICIAL_STATIC"),
]


# ── POC-ONLY: additional real facility NAMES (napanta), DUMMY capacity ───────
# These districts had zero facilities in the lists above. Names are real
# (napanta.com, filtered to drop personal/farmer-name rows), but capacity
# figures below are PLACEHOLDER values for POC/demo purposes only — NOT
# researched, NOT real. Every row is tagged data_quality='POC_DUMMY_CAPACITY'
# so it can never be confused with NHB/napanta-sourced real capacity above.
#
# Coverage: Baran, Banswara, Barmer, Bundi, Chittorgarh, Dausa, Dungarpur,
# Ganganagar. Churu was checked and genuinely has zero commercial facilities
# in napanta's listing (all 34 raw rows were individual farmers) — not a
# fetch gap. Still NOT covered: Hanumangarh, Jaisalmer, Jalore, Jhalawar,
# Jhunjhunu, Pali, Rajsamand, Sawai Madhopur, Sirohi, Tonk (not yet fetched),
# plus Karauli and Pratapgarh (no napanta page exists for either).
POC_DUMMY_CAPACITY_FACILITIES_RAJASTHAN = [
    ("Baran", "Rajasthan State Warehousing Corp, Antah, Baran", 3000),
    ("Banswara", "Central Warehouse, Banswara", 3000), ("Banswara", "Jeevan Jyoti Industries", 3000),
    ("Barmer", "Mahaveer Cold Storage", 3000),
    ("Bundi", "Amit Udyog Limited", 3000), ("Bundi", "Jhanwar Industries", 3000),
    ("Bundi", "M/S Bahediya Rice & Dall Mill", 3000), ("Bundi", "M/S Hadoti Rice Mill", 3000),
    ("Bundi", "Shankar Gauri Agro Products Pvt Ltd", 3000), ("Bundi", "Shanker Shetalay", 3000),
    ("Chittorgarh", "M/S Laxmi Warehouse", 3000), ("Chittorgarh", "Riddhi Siddhi Warehouse", 3000),
    ("Chittorgarh", "Shiv Shakti Warehouse", 3000),
    ("Dausa", "Janki Warehousing Corp", 3000), ("Dausa", "Rajasthan State Warehousing Corp, Dausa", 3000),
    ("Dungarpur", "M/S KK Fruits", 3000),
    ("Ganganagar", "Anjana Warehouse", 3000), ("Ganganagar", "Farid Cold Storage", 3000),
    ("Ganganagar", "Godara Warehouse", 3000), ("Ganganagar", "Gurunanak Cold", 3000),
    ("Ganganagar", "Janta Cold Storage", 3000), ("Ganganagar", "KESARISINGHPUR (RSWC)", 3000),
    ("Ganganagar", "Khandelia Oil & General Mills Pvt. Ltd.", 3000),
    ("Ganganagar", "M/S Ganesh Enterprises", 3000), ("Ganganagar", "M/S Gurukripa Warehouse", 3000),
    ("Ganganagar", "M/S Madhav Traders", 3000), ("Ganganagar", "M/S S S Logistics", 3000),
    ("Ganganagar", "M/S Shree Shubham Logistics, Chotti", 3000),
    ("Ganganagar", "Ms Godra Cotton Ginning And Pressing Factory", 3000),
    ("Ganganagar", "Paras Warehousing", 3000), ("Ganganagar", "RSWC Padampura", 3000),
    ("Ganganagar", "SRIGANGANAGAR-I (RSWC)", 3000), ("Ganganagar", "SRIGANGANAGAR-II (RSWC)", 3000),
    ("Ganganagar", "Shree Jee Warehouse", 3000), ("Ganganagar", "Sri Cold Storage", 3000),
    ("Ganganagar", "Sri Ganganagar Cold Storage", 3000),
]


GOVT_COLD_STORAGE_URL = os.environ.get(
    "GOVT_COLD_STORAGE_URL",
    "https://www.nhb.gov.in/Handlers/GeoIndiaHandlerGis.ashx",
)
GOVT_ICAP_URL = "https://www.nhb.gov.in/IcapMap/ICAPMap_rpt.aspx"
DEFAULT_POC_CAPACITY_MT = float(os.environ.get("COLD_STORAGE_POC_CAPACITY_MT", "3000"))
POC_CAPACITY_MIN_MT = float(os.environ.get("COLD_STORAGE_POC_CAPACITY_MIN_MT", "3000"))
POC_CAPACITY_MAX_MT = float(os.environ.get("COLD_STORAGE_POC_CAPACITY_MAX_MT", "10000"))


def _first_value(record: dict, *keys):
    normalized = {
        str(key).lower().replace("_", "").replace(" ", ""): value
        for key, value in record.items()
    }
    for key in keys:
        value = normalized.get(key.lower().replace("_", "").replace(" ", ""))
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _walk_records(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("properties"), dict):
            yield payload["properties"]
        yield payload
        for value in payload.values():
            yield from _walk_records(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_records(value)


def fetch_govt_cold_storage_names(states=None, timeout=60):
    """Fetch NHB names on demand. This is never called during normal startup."""
    requested = {str(state).strip().lower() for state in (states or []) if str(state).strip()}
    aliases = {"rajsthan": "rajasthan", "rajasthan": "rajasthan"}
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    session = requests.Session()
    landing = session.get(GOVT_ICAP_URL, headers=browser_headers, timeout=timeout)
    landing.raise_for_status()
    response = session.get(
        GOVT_COLD_STORAGE_URL,
        headers={
            **browser_headers,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": GOVT_ICAP_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
    )
    response.raise_for_status()

    response_text = response.text.lstrip("\ufeff")
    # NHB's handler sends back a fixed-size buffer (observed: exactly
    # 512000 bytes / 500 KiB) rather than trimming its output to the real
    # CSV content length. When the real data is shorter than the buffer,
    # the remainder is leftover/uninitialized null bytes ('\x00'). Those
    # nulls contain no newlines, so csv.DictReader either silently treats
    # them as garbage trailing fields or blows past the field-size limit
    # entirely (`_csv.Error: field larger than field limit`). Cut the
    # response off at the first null byte — everything after it is buffer
    # padding, not data — before any CSV/JSON parsing happens.
    if "\x00" in response_text:
        response_text = response_text.split("\x00", 1)[0]
    response_text = response_text.strip()
    content_type = response.headers.get("Content-Type", "")
    if not response_text:
        raise RuntimeError("The NHB handler returned an empty response.")
    if response_text.lower().startswith(("<!doctype", "<html")):
        debug_path = BASE_DIR / "nhb_debug_response.html"
        debug_path.write_text(response.text, encoding="utf-8")
        raise RuntimeError(
            "NHB returned HTML instead of facility data. "
            f"The response was saved to: {debug_path}"
        )
    # NHB CSV may contain large embedded fields. Python defaults to 128 KiB.
    csv_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(csv_limit)
            break
        except OverflowError:
            csv_limit //= 10

    if "csv" in content_type.lower():
        sample = response_text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        records = list(csv.DictReader(io.StringIO(response_text), dialect=dialect))
        if not records:
            debug_path = BASE_DIR / "nhb_debug_response.txt"
            debug_path.write_text(response.text, encoding="utf-8")
            raise RuntimeError(
                "NHB returned CSV, but it contained no data rows. "
                f"The response was saved to: {debug_path}"
            )
    else:
        try:
            payload = response.json()
            records = list(_walk_records(payload))
        except requests.exceptions.JSONDecodeError as exc:
            try:
                dialect = csv.Sniffer().sniff(response_text[:4096], delimiters=",;\t|")
                records = list(csv.DictReader(io.StringIO(response_text), dialect=dialect))
            except (csv.Error, UnicodeError):
                records = []
            if not records:
                debug_path = BASE_DIR / "nhb_debug_response.txt"
                debug_path.write_text(response.text, encoding="utf-8")
                raise RuntimeError(
                    "NHB returned neither readable JSON nor CSV. "
                    f"HTTP status={response.status_code}, content_type={content_type!r}. "
                    f"The response was saved to: {debug_path}"
                ) from exc

    facilities = {}
    for record in records:
        # "NameofProject" is NHB's actual current CSV header (confirmed live,
        # 2026): NameofProject,latitude,longitude,StateName,DistrictName.
        # It was previously missing from this alias list entirely, so
        # _first_value never matched it, name was always None, and every
        # record got silently dropped by the `if not (name and state)`
        # check below — regardless of which state/district was requested.
        name = _first_value(
            record, "NameofProject", "coldStorageName", "projectName",
            "facilityName", "name", "csName",
        )
        state = _first_value(record, "stateName", "state")
        district = _first_value(record, "districtName", "district") or "Unknown"
        if not (name and state):
            continue
        state_key = aliases.get(state.lower(), state.lower())
        if requested and state_key not in requested:
            continue
        facilities[(state_key, district.casefold(), name.casefold())] = {
            "state": state_key, "district": district, "name": name
        }
    return list(facilities.values())


def _bundled_facilities_for_states(states=None):
    """Return bundled cold-store names when the live NHB endpoint has no rows."""
    requested = {str(x).strip().lower() for x in (states or []) if str(x).strip()}
    if not requested:
        requested = {"rajasthan", "tripura", "meghalaya"}
    facilities = {}
    if "rajasthan" in requested:
        rows = (list(NHB_FACILITIES_RAJASTHAN)
                + list(NAPANTA_ONLY_FACILITIES_RAJASTHAN)
                + list(POC_DUMMY_CAPACITY_FACILITIES_RAJASTHAN))
        for district, name, _capacity in rows:
            facilities[("rajasthan", district.casefold(), name.casefold())] = {
                "state": "rajasthan", "district": district, "name": name}
    for state, district, name, _capacity, _quality in TRIPURA_MEGHALAYA_FACILITIES:
        state_key = state.strip().lower()
        if state_key in requested:
            facilities[(state_key, district.casefold(), name.casefold())] = {
                "state": state_key, "district": district, "name": name}
    return list(facilities.values())


def import_govt_facilities(states=None, dummy_capacity_mt=None):
    """Import store names and assign a dummy capacity per facility.

    If dummy_capacity_mt is None (default), each facility gets its own
    random capacity in [POC_CAPACITY_MIN_MT, POC_CAPACITY_MAX_MT]. Pass an
    explicit number to fall back to the old fixed-value-for-all behavior.
    """
    try:
        facilities = fetch_govt_cold_storage_names(states=states)
    except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
        logger.warning("Live NHB import failed; using bundled data: %s", exc)
        facilities = []
    source = "NHB_GOVT_IMPORT"
    if not facilities:
        facilities = _bundled_facilities_for_states(states)
        source = "BUNDLED_REGISTRY_IMPORT"
        logger.warning("NHB returned no matching rows; using %d bundled records.", len(facilities))
    if not facilities:
        raise RuntimeError("No cold-storage names are available for the requested states.")

    init_db()
    conn = db_connect()
    now = now_iso()
    inserted = updated = 0
    facility_ids = []
    try:
        for item in facilities:
            facility_capacity_mt = (
                dummy_capacity_mt
                if dummy_capacity_mt is not None
                else round(random.uniform(POC_CAPACITY_MIN_MT, POC_CAPACITY_MAX_MT), 2)
            )
            row = conn.execute(
                """SELECT id FROM cold_storages
                   WHERE lower(state)=lower(?) AND lower(district)=lower(?)
                     AND lower(name)=lower(?)""",
                (item["state"], item["district"], item["name"]),
            ).fetchone()
            if row:
                facility_id = row["id"]
                conn.execute(
                    """UPDATE cold_storages
                       SET total_capacity_mt=?, source=?, storage_type='static',
                           data_quality='POC_DUMMY_CAPACITY', last_updated=?
                       WHERE id=?""",
                    (facility_capacity_mt, source, now, facility_id),
                )
                updated += 1
            else:
                cursor = conn.execute(
                    """INSERT INTO cold_storages (
                       nhb_id, name, state, district, total_capacity_mt, storage_type,
                       source, source_year, data_quality, last_updated, created_at
                       ) VALUES (NULL, ?, ?, ?, ?, 'static', ?, NULL,
                       'POC_DUMMY_CAPACITY', ?, ?)
                       RETURNING id""",
                    (item["name"], item["state"], item["district"],
                     facility_capacity_mt, source, now, now),
                )
                facility_id = cursor.fetchone()["id"]
                inserted += 1
            facility_ids.append(facility_id)

        # Remove the earlier incorrect per-crop duplication. Capacity belongs
        # to the facility once, not once for every crop.
        if facility_ids:
            placeholders = ",".join("?" for _ in facility_ids)
            conn.execute(
                f"DELETE FROM cold_storage_crop_capacity WHERE cold_storage_id IN ({placeholders})",
                facility_ids,
            )
        conn.commit()
    finally:
        conn.close()
    return {"fetched": len(facilities), "inserted": inserted, "updated": updated,
            "source": source}


def _normalize_district(text: str) -> str:
    return " ".join(str(text).strip().casefold().split())


def import_missing_district_facilities(state: str, districts: list[str]) -> dict:
    """
    Fill in facility NAMES for a specific list of districts that are
    currently missing from the DB, using the same live NHB feed as
    import_govt_facilities() — but scoped, so it never touches districts
    you didn't ask about and never falls back to invented data.

    Several Rajasthan districts were never actually researched (see the
    STATUS note above NAPANTA_ONLY_FACILITIES_RAJASTHAN): Hanumangarh,
    Jaisalmer, Jalore, Jhalawar, Jhunjhunu, Pali, Rajsamand, Sawai
    Madhopur, Sirohi, Tonk, plus Karauli and Pratapgarh (no napanta page
    exists for either). This is the entry point for filling those in from
    NHB once real internet access is available.

    Unlike import_govt_facilities(), this function:
      - Only inserts rows for the districts you pass in (no accidental
        overwrite of districts you already have data for).
      - Never falls back to bundled/guessed data on a failed fetch — a
        network error must never look like "NHB has zero facilities here".
      - Never invents capacity: NHB's feed doesn't carry it, so new rows
        get total_capacity_mt=0 / data_quality='CAPACITY_TBD', matching
        the convention sync_real_facility_data() already uses for
        NHB-name-only rows.

    Returns a dict with per-district counts plus inserted/skipped totals,
    so the caller (the --import-missing-districts CLI, or an admin route)
    can report exactly what happened.

    IMPORTANT: NHB's own district spellings don't always match ours —
    confirmed live: NHB has "Jhunjhunun" (not "Jhunjhunu") and "Dhaulpur"
    (not "Dholpur", which is what the bundled facility lists elsewhere in
    this file use). An exact-string match alone would silently call those
    "0 facilities / real absence" when NHB actually has data under a
    slightly different spelling. So any requested district with zero
    exact matches gets checked with difflib against every district name
    NHB actually returned for this state, and near-misses are surfaced as
    'possible_spelling_mismatches' instead of being folded into
    empty_districts — a district is only reported as a genuine absence
    if no close spelling match exists either.
    """
    wanted = {_normalize_district(d): d for d in districts if str(d).strip()}
    if not wanted:
        raise ValueError("districts must contain at least one district name.")

    all_facilities = fetch_govt_cold_storage_names(states=[state])  # raises on failure — no silent fallback
    all_nhb_districts = sorted({f["district"] for f in all_facilities})
    all_nhb_districts_normalized = {_normalize_district(d): d for d in all_nhb_districts}

    matched = [f for f in all_facilities if _normalize_district(f["district"]) in wanted]
    found_districts = {_normalize_district(f["district"]) for f in matched}

    empty_districts = []
    possible_spelling_mismatches = {}
    for key, raw in wanted.items():
        if key in found_districts:
            continue
        close = difflib.get_close_matches(key, all_nhb_districts_normalized.keys(), n=3, cutoff=0.8)
        if close:
            possible_spelling_mismatches[raw] = [all_nhb_districts_normalized[c] for c in close]
        else:
            empty_districts.append(raw)

    per_district = {
        raw: sum(1 for f in matched if _normalize_district(f["district"]) == key)
        for key, raw in wanted.items()
    }

    init_db()
    conn = db_connect()
    now = now_iso()
    inserted = skipped = 0
    try:
        for item in matched:
            exists = conn.execute(
                """SELECT id FROM cold_storages
                   WHERE lower(state)=lower(?) AND lower(district)=lower(?)
                     AND lower(name)=lower(?)""",
                (item["state"], item["district"], item["name"]),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO cold_storages (
                    nhb_id, name, state, district, total_capacity_mt, storage_type,
                    source, source_year, data_quality, last_updated, created_at
                ) VALUES (NULL, ?, ?, ?, 0, NULL, 'NHB_GOVT_IMPORT', NULL, 'CAPACITY_TBD', ?, ?)
                """,
                (item["name"], item["state"], item["district"], now, now),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "state": state,
        "per_district_found": per_district,
        "empty_districts": empty_districts,  # NHB genuinely has 0 registered facilities for these
        "possible_spelling_mismatches": possible_spelling_mismatches,  # requested -> [NHB's closest district spellings]
        "inserted": inserted,
        "skipped_existing": skipped,
    }


def sync_real_facility_data():
    """
    Called on every startup:
      1. Purges the old fake demo data (data_quality='SEED_EXAMPLE') —
         cascades to its crop-capacity rows and snapshots automatically.
      2. Inserts the real NHB-registered Rajasthan facilities, idempotently
         (skips any that already exist by name+district).
      3. Inserts the real Tripura/Meghalaya facilities the same way,
         including known capacities where a real source gave one.
    Farmer-submitted plans (farmer_crop_storage_plans) are untouched —
    those are real user data, not demo seed data, regardless of which
    email created them.
    """
    conn = db_connect()

    purged = conn.execute(
        "SELECT COUNT(*) AS cnt FROM cold_storages WHERE data_quality = 'SEED_EXAMPLE'"
    ).fetchone()["cnt"]
    conn.execute("DELETE FROM cold_storages WHERE data_quality = 'SEED_EXAMPLE'")

    now = now_iso()
    inserted = 0
    for district, name, capacity_mt in NHB_FACILITIES_RAJASTHAN:
        exists = conn.execute(
            "SELECT id FROM cold_storages WHERE name = ? AND state = 'rajasthan' AND district = ?",
            (name, district),
        ).fetchone()
        if exists:
            continue
        if capacity_mt is not None:
            source, data_quality = "NHB+napanta", "UNVERIFIED_CAPACITY"
        else:
            source, data_quality = "NHB", "CAPACITY_TBD"
        conn.execute(
            """
            INSERT INTO cold_storages (
                nhb_id, name, state, district, total_capacity_mt, storage_type,
                source, source_year, data_quality, last_updated, created_at
            ) VALUES (NULL, ?, 'rajasthan', ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (name, district, capacity_mt or 0, source, data_quality, now, now),
        )
        inserted += 1

    for district, name, capacity_mt in NAPANTA_ONLY_FACILITIES_RAJASTHAN:
        exists = conn.execute(
            "SELECT id FROM cold_storages WHERE name = ? AND state = 'rajasthan' AND district = ?",
            (name, district),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO cold_storages (
                nhb_id, name, state, district, total_capacity_mt, storage_type,
                source, source_year, data_quality, last_updated, created_at
            ) VALUES (NULL, ?, 'rajasthan', ?, ?, NULL, 'napanta', NULL, 'UNVERIFIED_CAPACITY', ?, ?)
            """,
            (name, district, capacity_mt or 0, now, now),
        )
        inserted += 1

    for district, name, capacity_mt in POC_DUMMY_CAPACITY_FACILITIES_RAJASTHAN:
        exists = conn.execute(
            "SELECT id FROM cold_storages WHERE name = ? AND state = 'rajasthan' AND district = ?",
            (name, district),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO cold_storages (
                nhb_id, name, state, district, total_capacity_mt, storage_type,
                source, source_year, data_quality, last_updated, created_at
            ) VALUES (NULL, ?, 'rajasthan', ?, ?, NULL, 'napanta', NULL, 'POC_DUMMY_CAPACITY', ?, ?)
            """,
            (name, district, capacity_mt, now, now),
        )
        inserted += 1

    for state, district, name, capacity_mt, data_quality in TRIPURA_MEGHALAYA_FACILITIES:
        exists = conn.execute(
            "SELECT id FROM cold_storages WHERE name = ? AND state = ? AND district = ?",
            (name, state, district),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO cold_storages (
                nhb_id, name, state, district, total_capacity_mt, storage_type,
                source, source_year, data_quality, last_updated, created_at
            ) VALUES (NULL, ?, ?, ?, ?, NULL, 'MANUAL', NULL, ?, ?, ?)
            """,
            (name, state, district, capacity_mt or 0, data_quality, now, now),
        )
        inserted += 1

    conn.commit()
    conn.close()
    if purged:
        logger.info("Purged %d SEED_EXAMPLE dummy facilities.", purged)
    if inserted:
        logger.info("Inserted %d real NHB-registered Rajasthan facilities (capacity TBD).", inserted)


def get_db() -> PGConnection:
    return db_connect()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row) -> dict:
    return dict(row)


# ── AUTH: verify session token against the gateway's /api/auth/me ────────────
# Identical pattern to yield_detect_backend.py's verify_token/require_auth.

@ttl_cache(seconds=20, key_prefix="cold_storage:verify_token")
def verify_token(token: str) -> dict | None:
    """Cached for 20s. Without this, every single request to this service
    pays a synchronous HTTP round-trip to the gateway's /api/auth/me
    before doing any actual work — if the auth service is ever slow or
    briefly unavailable, that round-trip is exactly what turns a slow
    request here into a 504 at the gateway. A short TTL keeps a
    restricted/revoked token from staying valid here for more than ~20s
    after it's killed on the auth side."""
    if not token:
        return None
    try:
        resp = requests.get(
            f"{GATEWAY_INTERNAL_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("email"):
            return None
        return {
            "uid": data.get("uid"),
            "email": data.get("email"),
            "role": data.get("role"),
            "state": data.get("state") or "",
            "district": data.get("district") or "",
        }
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not verify token against gateway (%s): %s", GATEWAY_INTERNAL_URL, exc)
        return None


def require_auth(roles: list[str] | None = None):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
            user = verify_token(token)
            if not user:
                return jsonify({"error": "Unauthorized — missing or invalid session token"}), 401
            if roles and (user.get("role") or "").lower() not in [r.lower() for r in roles]:
                return jsonify({"error": "Forbidden — this action requires role: " + ", ".join(roles)}), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def scope_clause_for_role(user: dict) -> tuple[list[str], list]:
    """
    Returns (sql_clauses, params) that scope a query by the caller's role,
    same convention as yield_detect_backend.py: farmer -> own records only,
    district_admin -> their district, state_admin -> their state, admin /
    analyst -> unrestricted.
    """
    role = (user.get("role") or "").lower()
    clauses, params = [], []
    if role == "district_admin":
        if user.get("district"):
            clauses.append("district = ?")
            params.append(user["district"])
        if user.get("state"):
            clauses.append("state = ?")
            params.append(user["state"])
    elif role == "state_admin":
        if user.get("state"):
            clauses.append("state = ?")
            params.append(user["state"])
    return clauses, params


# ── AGGREGATION HELPERS ────────────────────────────────────────────────────────

def harvest_period(date_str: str | None) -> str | None:
    """'2027-02-15' -> '2027-02'. Storage demand is bucketed by the month of
    expected harvest (see class docstring / README on the harvest-period
    limitation)."""
    if not date_str:
        return None
    return date_str[:7]


def crop_capacity_for_district(db: PGConnection, state: str, district: str, crop: str) -> dict:
    """
    Returns {"capacity_mt": float, "basis": "PER_CROP_CONFIGURED" | "FACILITY_TOTAL_FALLBACK" | "NONE"}.

    FIX: previously this only summed cold_storage_crop_capacity, which
    nothing in this file ever inserts into — so it always returned 0, and
    every advisory computed against nonzero demand came back SHORTAGE no
    matter how much real capacity existed. Now it falls back to the sum of
    total_capacity_mt for facilities in the district when no facility there
    has this crop specifically configured yet. That fallback is coarser
    (it assumes the whole facility could serve this crop, not just a
    crop-specific share of it) so callers get "basis" to know which one
    they're looking at.
    """
    row = db.execute(
        """
        SELECT COALESCE(SUM(ccc.capacity_mt), 0) AS total
        FROM cold_storage_crop_capacity ccc
        JOIN cold_storages cs ON cs.id = ccc.cold_storage_id
        WHERE cs.state = ? AND cs.district = ? AND ccc.crop = ?
        """,
        (state, district, crop),
    ).fetchone()
    configured_total = float(row["total"] or 0)
    if configured_total > 0:
        return {"capacity_mt": configured_total, "basis": "PER_CROP_CONFIGURED"}

    fallback_row = db.execute(
        """
        SELECT COALESCE(SUM(total_capacity_mt), 0) AS total, COUNT(*) AS n
        FROM cold_storages
        WHERE state = ? AND district = ?
        """,
        (state, district),
    ).fetchone()
    if fallback_row["n"]:
        return {"capacity_mt": float(fallback_row["total"] or 0), "basis": "FACILITY_TOTAL_FALLBACK"}
    return {"capacity_mt": 0.0, "basis": "NONE"}


def latest_snapshot_totals(db: PGConnection, state: str, district: str, crop: str,
                            capacity_basis: str = "PER_CROP_CONFIGURED") -> dict:
    """
    Sums each cold storage's MOST RECENT snapshot (not just the newest
    snapshot row overall) across every facility in the district relevant to
    this crop. Facilities with no snapshot yet contribute 0 and are flagged
    via has_unreported_facilities.

    storage_snapshots records occupancy for the FACILITY AS A WHOLE (a
    facility reports one occupied_mt for all the crops it stores, it
    doesn't break occupancy down per-crop). So when computing this for a
    single crop with per-crop capacity CONFIGURED, we prorate each
    facility's occupied/reserved/committed/expected_release by this crop's
    share of that facility's total_capacity_mt (crop_capacity /
    facility_capacity) — an approximation, but a much closer one than
    applying the whole facility figure to every crop unscaled.

    FIX: when capacity_basis is FACILITY_TOTAL_FALLBACK (crop_capacity_for_district
    found no crop-specific rows and fell back to facility totals — see that
    function's docstring), there is no crop share to prorate by, since the
    facility isn't broken down per crop at all yet. In that case we take
    every facility in the district (not just ones with a crop-capacity row,
    since there are none) and count occupancy at full share (1.0), matching
    the FACILITY_TOTAL_FALLBACK capacity figure it's being compared against.
    """
    if capacity_basis == "FACILITY_TOTAL_FALLBACK":
        facilities = db.execute(
            "SELECT id, total_capacity_mt, total_capacity_mt AS crop_capacity_mt "
            "FROM cold_storages WHERE state = ? AND district = ?",
            (state, district),
        ).fetchall()
    else:
        facilities = db.execute(
            """
            SELECT cs.id, cs.total_capacity_mt, ccc.capacity_mt AS crop_capacity_mt
            FROM cold_storages cs
            JOIN cold_storage_crop_capacity ccc ON ccc.cold_storage_id = cs.id
            WHERE cs.state = ? AND cs.district = ? AND ccc.crop = ?
            """,
            (state, district, crop),
        ).fetchall()

    occupied = reserved = committed = expected_release = 0.0
    latest_recorded_at = None
    has_unreported = False

    for fac in facilities:
        snap = db.execute(
            """
            SELECT occupied_mt, reserved_mt, committed_mt, expected_release_mt, recorded_at
            FROM storage_snapshots
            WHERE cold_storage_id = ?
            ORDER BY recorded_at DESC LIMIT 1
            """,
            (fac["id"],),
        ).fetchone()
        if snap:
            facility_capacity = fac["total_capacity_mt"] or 0
            crop_capacity = fac["crop_capacity_mt"] or 0
            share = (crop_capacity / facility_capacity) if facility_capacity > 0 else 0.0
            occupied += (snap["occupied_mt"] or 0) * share
            reserved += (snap["reserved_mt"] or 0) * share
            committed += (snap["committed_mt"] or 0) * share
            expected_release += (snap["expected_release_mt"] or 0) * share
            if latest_recorded_at is None or snap["recorded_at"] > latest_recorded_at:
                latest_recorded_at = snap["recorded_at"]
        else:
            has_unreported = True

    return {
        "occupied_mt": occupied,
        "reserved_mt": reserved,
        "committed_mt": committed,
        "expected_release_mt": expected_release,
        "last_reported_at": latest_recorded_at,
        "has_unreported_facilities": has_unreported,
    }


def registered_demand_for_period(
    db: PGConnection, state: str, district: str, crop: str, period: str | None, exclude_plan_id: int | None = None
) -> float:
    """Sums storage_required_mt for all active (non-cancelled) plans in the
    same district+crop+harvest-month bucket."""
    clauses = ["state = ?", "district = ?", "crop = ?", "status != 'cancelled'"]
    params: list = [state, district, crop]
    if period:
        clauses.append("substr(expected_harvest_date, 1, 7) = ?")
        params.append(period)
    if exclude_plan_id is not None:
        clauses.append("id != ?")
        params.append(exclude_plan_id)

    row = db.execute(
        f"SELECT COALESCE(SUM(storage_required_mt), 0) AS total FROM farmer_crop_storage_plans WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return float(row["total"] or 0)


def fetch_land_yield_production(state: str, district: str) -> dict | None:
    """
    Pulls this district's expected crop production from the Yield Detect
    service's /api/yield/internal/production (geofenced lands a farmer has
    drawn and analyzed — see yield_detect_backend.py).

    Returns the full response dict:
      {"production_mt": {crop_name: total_mt}, "by_farmer": [{crop, user_email, production_mt}, ...]}
    or None if the yield-detect backend couldn't be reached (caller should
    treat that as "unknown", not "zero").
    """
    try:
        resp = requests.get(
            f"{YIELD_INTERNAL_URL}/api/yield/internal/production",
            params={"state": state, "district": district},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("Yield Detect backend returned %s for /api/yield/internal/production", resp.status_code)
            return None
        data = resp.json()
        return {
            "production_mt": data.get("production_mt", {}),
            "by_farmer": data.get("by_farmer", []),
        }
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not reach Yield Detect backend (%s): %s", YIELD_INTERNAL_URL, exc)
        return None


def district_crop_status(db: PGConnection, state: str, district: str, crop: str, period: str | None,
                          yield_totals: dict | None = None, farmers: list | None = None) -> dict:
    """The full, transparent breakdown for one state+district+crop
    (+ optional harvest-month period), with source/timestamp attached."""
    capacity_info = crop_capacity_for_district(db, state, district, crop)
    total_capacity = capacity_info["capacity_mt"]
    snap = latest_snapshot_totals(db, state, district, crop, capacity_basis=capacity_info["basis"])
    demand = registered_demand_for_period(db, state, district, crop, period)

    result = calculate_storage_status(
        total_capacity_mt=total_capacity,
        occupied_mt=snap["occupied_mt"],
        reserved_mt=snap["reserved_mt"],
        committed_mt=snap["committed_mt"],
        expected_releases_mt=snap["expected_release_mt"],
        registered_demand_mt=demand,
    )
    # Informational only — expected production for this crop in this
    # district, derived from farmers' geofenced Yield Detect lands
    # (predicted_yield * area_hectare). NOT folded into
    # registered_demand_mt/projected_available_mt above, since analyzing a
    # plot of land doesn't necessarily commit its crop to cold storage
    # (that's what farmer_crop_storage_plans is for) — this is a
    # separate, softer signal surfaced for context.
    land_production_mt = None
    if yield_totals is not None:
        land_production_mt = yield_totals.get(crop, 0.0)

    result.update({
        "state": state,
        "district": district,
        "crop": crop,
        "period": period,
        "last_reported_occupancy_at": snap["last_reported_at"],
        "has_unreported_facilities": snap["has_unreported_facilities"],
        # FIX: capacity_basis tells the caller whether total_capacity_mt came
        # from crop-specific configured rows or the coarser facility-total
        # fallback (see crop_capacity_for_district) — surfaced so the UI/API
        # consumer can show that distinction instead of presenting a
        # fallback estimate as if it were precisely-configured capacity.
        "capacity_basis": capacity_info["basis"],
        "data_quality": "MIXED" if snap["last_reported_at"] else "OFFICIAL_STATIC",
        "registered_land_production_mt": land_production_mt,
        # Per-farmer breakdown of registered_land_production_mt for this
        # crop, sourced from Yield Detect's /internal/production
        # "by_farmer" list. user_email is the only farmer-identifying
        # field available anywhere in this stack (lands table + auth
        # gateway both store email only, no separate display name) — so
        # that's what's surfaced here rather than a fabricated name.
        "farmers": farmers or [],
    })
    return result


# ── ROUTES: health ────────────────────────────────────────────────────────────

@app.route("/api/cold-storage/health")
def health():
    return jsonify({"status": "ok", "service": "cold-storage-intelligence", "db": "postgres"})


# ── ROUTES: facilities (read-only in this pass; NHB import populates these) ──

@ttl_cache(seconds=45, key_prefix="cold_storage:facilities_raw")
def _fetch_facilities_raw(state: str | None, district: str | None, crop: str | None) -> list[dict]:
    """The actual DB work behind list_facilities, cached for 45s and
    deliberately NOT keyed by the caller's role/jurisdiction — only by
    the state/district/crop filters, which is the same result for every
    caller. Role-based scoping is applied afterward in Python on the
    (cached) result, so two different admins hitting this with the same
    filters share one cache entry instead of each paying their own query.

    Also fixes the previous N+1: crop capacities for every matched
    facility are fetched in a single query instead of one query per row.
    """
    db = get_db()
    try:
        sql = "SELECT * FROM cold_storages"
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if district:
            clauses.append("district = ?")
            params.append(district)
        if crop:
            clauses.append("id IN (SELECT cold_storage_id FROM cold_storage_crop_capacity WHERE crop = ?)")
            params.append(crop)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = db.execute(sql, params).fetchall()

        facilities = [row_to_dict(r) for r in rows]
        ids = [f["id"] for f in facilities]
        crop_caps_by_id: dict[int, dict] = {fid: {} for fid in ids}
        if ids:
            placeholders = ",".join(["?"] * len(ids))
            caps = db.execute(
                f"SELECT cold_storage_id, crop, capacity_mt FROM cold_storage_crop_capacity "
                f"WHERE cold_storage_id IN ({placeholders})",
                ids,
            ).fetchall()
            for c in caps:
                crop_caps_by_id[c["cold_storage_id"]][c["crop"]] = c["capacity_mt"]

        for f in facilities:
            f["crop_capacity"] = crop_caps_by_id.get(f["id"], {})
        return facilities
    finally:
        db.close()


@app.route("/api/cold-storage/facilities", methods=["GET"])
@require_auth()
def list_facilities():
    state = request.args.get("state")
    district = request.args.get("district")
    crop = request.args.get("crop")

    facilities = _fetch_facilities_raw(state, district, crop)

    # Role-based scoping is applied here, in Python, on the cached
    # result — district_admin/state_admin still only see their own
    # jurisdiction, it's just filtered after the cache lookup instead of
    # baked into the SQL (which would fragment the cache per-user).
    role = (g.user.get("role") or "").lower()
    if role == "district_admin":
        if g.user.get("district"):
            facilities = [f for f in facilities if f.get("district") == g.user["district"]]
        if g.user.get("state"):
            facilities = [f for f in facilities if f.get("state") == g.user["state"]]
    elif role == "state_admin":
        if g.user.get("state"):
            facilities = [f for f in facilities if f.get("state") == g.user["state"]]

    return jsonify(facilities)


@app.route("/api/cold-storage/facilities/<int:facility_id>", methods=["GET"])
@require_auth()
def get_facility(facility_id):
    db = get_db()
    row = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "facility not found"}), 404

    role_clauses, role_params = scope_clause_for_role(g.user)
    if role_clauses:
        ok = db.execute(f"SELECT 1 FROM cold_storages WHERE id = ? AND {' AND '.join(role_clauses)}", [facility_id] + role_params).fetchone()
        if not ok:
            db.close()
            return jsonify({"error": "Forbidden — outside your assigned jurisdiction"}), 403

    d = row_to_dict(row)
    crop_caps = db.execute(
        "SELECT crop, capacity_mt FROM cold_storage_crop_capacity WHERE cold_storage_id = ?", (facility_id,)
    ).fetchall()
    d["crop_capacity"] = {c["crop"]: c["capacity_mt"] for c in crop_caps}

    snapshots = db.execute(
        "SELECT * FROM storage_snapshots WHERE cold_storage_id = ? ORDER BY recorded_at DESC LIMIT 10", (facility_id,)
    ).fetchall()
    d["recent_snapshots"] = [row_to_dict(s) for s in snapshots]

    db.close()
    return jsonify(d)


# ── ROUTES: district summary / crops (the core "will there be enough        ──
# ── capacity" answer the whole feature exists to give)                     ──

@app.route("/api/cold-storage/districts/<district>/crops", methods=["GET"])
@require_auth()
def district_crops(district):
    state = request.args.get("state", "").strip()
    if not state:
        return jsonify({"error": "state is required"}), 400
    db = get_db()
    rows = db.execute(
        """
        SELECT DISTINCT ccc.crop
        FROM cold_storage_crop_capacity ccc
        JOIN cold_storages cs ON cs.id = ccc.cold_storage_id
        WHERE cs.state = ? AND cs.district = ?
        ORDER BY ccc.crop
        """,
        (state, district),
    ).fetchall()
    db.close()
    return jsonify({"state": state, "district": district, "crops": [r["crop"] for r in rows]})


@app.route("/api/cold-storage/districts/<district>/summary", methods=["GET"])
@require_auth()
def district_summary(district):
    state = request.args.get("state", "").strip()
    period = request.args.get("period")
    if not state:
        return jsonify({"error": "state is required"}), 400

    role = (g.user.get("role") or "").lower()
    if role == "district_admin" and g.user.get("district") and g.user["district"] != district:
        return jsonify({"error": "Forbidden — outside your assigned district"}), 403
    if role in ("district_admin", "state_admin") and g.user.get("state") and g.user["state"] != state:
        return jsonify({"error": "Forbidden — outside your assigned state"}), 403

    db = get_db()
    rows = db.execute(
        """SELECT id, name, state, district, block, village,
                  total_capacity_mt, storage_type, source, data_quality, last_updated
           FROM cold_storages
           WHERE lower(state)=lower(?) AND lower(district)=lower(?)
           ORDER BY LOWER(name)""",
        (state, district),
    ).fetchall()
    facilities = [row_to_dict(row) for row in rows]
    total_capacity = sum(float(row["total_capacity_mt"] or 0) for row in rows)

    # FIX: this used to hardcode "crops": [] unconditionally, so the
    # frontend's edit-plan flow (which re-fetches this endpoint after a PUT
    # looking for advData.crops.find(c => c.crop === crop)) could never find
    # a match, and silently showed no advisory after editing a plan.
    #
    # Candidate crop list = union of crops with per-crop capacity configured
    # in this district, and crops any farmer has actually registered a plan
    # for here — covers both "operator has set up crop capacity" and
    # "someone just registered a plan for a crop that isn't configured yet"
    # (the latter still gets an answer via the FACILITY_TOTAL_FALLBACK path
    # in crop_capacity_for_district).
    configured_crop_rows = db.execute(
        """
        SELECT DISTINCT ccc.crop
        FROM cold_storage_crop_capacity ccc
        JOIN cold_storages cs ON cs.id = ccc.cold_storage_id
        WHERE lower(cs.state)=lower(?) AND lower(cs.district)=lower(?)
        """,
        (state, district),
    ).fetchall()
    planned_crop_rows = db.execute(
        """
        SELECT DISTINCT crop FROM farmer_crop_storage_plans
        WHERE lower(state)=lower(?) AND lower(district)=lower(?) AND status != 'cancelled'
        """,
        (state, district),
    ).fetchall()
    yield_data = fetch_land_yield_production(state, district)
    yield_totals = yield_data["production_mt"] if yield_data is not None else None

    # Group the per-land farmer breakdown by crop so district_crop_status
    # can attach "farmers": [{user_email, production_mt}, ...] to each
    # crop's advisory below.
    farmers_by_crop: dict[str, list] = {}
    for land in (yield_data or {}).get("by_farmer", []):
        farmers_by_crop.setdefault(land["crop"], []).append({
            "user_email": land.get("user_email"),
            "production_mt": land.get("production_mt"),
        })

    # FIX: candidate_crops previously only included crops with configured
    # cold-storage capacity or a booked farmer plan — a crop that only
    # exists as a Yield Detect land prediction (predicted_yield * area on
    # a geofenced plot, no capacity configured and no plan booked yet)
    # never made it into this list, so it silently never appeared as an
    # advisory even though yield_totals had real production for it.
    candidate_crops = sorted(
        {r["crop"] for r in configured_crop_rows}
        | {r["crop"] for r in planned_crop_rows}
        | set((yield_totals or {}).keys())
    )

    crops = [
        district_crop_status(
            db, state, district, crop, period,
            yield_totals=yield_totals, farmers=farmers_by_crop.get(crop, []),
        )
        for crop in candidate_crops
    ]
    db.close()

    # ── District capacity meter ──────────────────────────────────────
    # capacity meter = total cold-storage capacity in this district minus
    # expected production from farmers' geofenced Yield Detect lands
    # (predicted_yield * area_hectare, in yield_detect_backend.py). Uses
    # the SAME yield_totals fetched above for the per-crop advisories,
    # just summed across every crop instead of one — e.g. a 10-hectare
    # rice plot predicted at 4 t/ha in Ajmer shows up here as
    # registered_production_mt=40, reducing storage_left_mt by 40.
    #
    # yield_totals is None when the Yield Detect backend couldn't be
    # reached at all (vs. an empty dict, which means it responded but this
    # district has no analyzed lands yet) — production_data_available
    # tells the frontend which case it's looking at so it doesn't show
    # "0 t used" when the real answer is "unknown".
    production_data_available = yield_totals is not None
    registered_production = sum((yield_totals or {}).values())
    storage_left = total_capacity - registered_production
    if total_capacity > 0 and production_data_available:
        production_utilization_percent = round((registered_production / total_capacity) * 100, 1)
    else:
        production_utilization_percent = None
    production_status = classify_status(production_utilization_percent)

    return jsonify({
        "state": state,
        "district": district,
        "period": period,
        "facility_count": len(facilities),
        "total_capacity_mt": total_capacity,
        "capacity_basis": "DUMMY_PER_FACILITY",
        "facilities": facilities,
        "crops": crops,
        "registered_production_mt": round(registered_production, 2),
        "storage_left_mt": round(storage_left, 2),
        "production_utilization_percent": production_utilization_percent,
        "production_status": production_status,
        "production_data_available": production_data_available,
    })


@app.route("/api/cold-storage/dashboard/district/<district>", methods=["GET"])
@require_auth()
def dashboard_district(district):
    # Same payload as district_summary for now — kept as a distinct route
    # per the API list in the spec, since the dashboard may want a
    # different shape (e.g. pre-aggregated totals) once the UI pass lands.
    return district_summary(district)


# ── ROUTES: farmer crop storage plans ─────────────────────────────────────────

@app.route("/api/cold-storage/farmer-plans", methods=["POST"])
@require_auth(roles=["farmer"])
def create_farmer_plan():
    body = request.get_json(force=True) or {}

    required = ["state", "district", "crop", "expected_harvest_date", "expected_production_mt"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    storage_required_mt = body.get("storage_required_mt")
    if storage_required_mt in (None, ""):
        # Sensible default per the spec's own example: storage requirement
        # defaults to the full expected production if the farmer doesn't
        # give a narrower figure (e.g. selling part of it fresh).
        storage_required_mt = body["expected_production_mt"]

    status = body.get("status", "registered")
    if status not in VALID_PLAN_STATUSES:
        return jsonify({"error": f"status must be one of {sorted(VALID_PLAN_STATUSES)}"}), 400

    now = now_iso()
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO farmer_crop_storage_plans (
            user_email, state, district, crop, area, area_unit, sowing_date,
            expected_harvest_date, expected_production_mt, storage_required_mt,
            status, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        RETURNING id
        """,
        (
            g.user["email"],
            body["state"], body["district"], body["crop"],
            body.get("area"), body.get("area_unit", "hectare"), body.get("sowing_date"),
            body["expected_harvest_date"], float(body["expected_production_mt"]), float(storage_required_mt),
            status, now, now,
        ),
    )
    plan_id = cur.fetchone()["id"]
    db.commit()
    row = db.execute("SELECT * FROM farmer_crop_storage_plans WHERE id = ?", (plan_id,)).fetchone()

    # Transparent, deterministic answer computed immediately, same request —
    # this is the "will there be enough capacity" moment from the spec.
    period = harvest_period(body["expected_harvest_date"])
    advisory = district_crop_status(db, body["state"], body["district"], body["crop"], period)
    db.close()

    return jsonify({"plan": row_to_dict(row), "advisory": advisory}), 201


@app.route("/api/cold-storage/farmer-plans", methods=["GET"])
@require_auth()
def list_all_farmer_plans():
    """
    Browse plans across farmers, scoped to the caller's jurisdiction
    (district_admin -> their district, state_admin -> their state,
    admin/analyst -> everything). Farmers should use /farmer-plans/me
    instead — this route 403s for them since "all plans" isn't a farmer
    concept.
    """
    role = (g.user.get("role") or "").lower()
    if role == "farmer":
        return jsonify({"error": "Use /api/cold-storage/farmer-plans/me instead"}), 403

    db = get_db()
    clauses, params = [], []
    for field in ("state", "district", "crop", "status"):
        val = request.args.get(field)
        if val:
            clauses.append(f"{field} = ?")
            params.append(val)

    role_clauses, role_params = scope_clause_for_role(g.user)
    clauses += role_clauses
    params += role_params

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"SELECT * FROM farmer_crop_storage_plans {where} ORDER BY created_at DESC", params
    ).fetchall()
    db.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/cold-storage/farmer-plans/<scope>", methods=["GET"])
@require_auth()
def list_farmer_plans(scope):
    """
    scope='me'   -> the calling farmer's own plans.
    scope=<email> -> that farmer's plans (admin/analyst/state_admin/
                     district_admin only, and still scoped to jurisdiction
                     for the admin roles).
    """
    role = (g.user.get("role") or "").lower()
    db = get_db()

    if scope == "me":
        if role != "farmer":
            db.close()
            return jsonify({"error": "'me' is only meaningful for farmer accounts"}), 400
        rows = db.execute(
            "SELECT * FROM farmer_crop_storage_plans WHERE user_email = ? ORDER BY created_at DESC",
            (g.user["email"],),
        ).fetchall()
        db.close()
        return jsonify([row_to_dict(r) for r in rows])

    # Looking up another farmer's plans by email.
    if role == "farmer":
        db.close()
        return jsonify({"error": "Forbidden"}), 403

    clauses, params = ["user_email = ?"], [scope]
    role_clauses, role_params = scope_clause_for_role(g.user)
    clauses += role_clauses
    params += role_params

    rows = db.execute(
        f"SELECT * FROM farmer_crop_storage_plans WHERE {' AND '.join(clauses)} ORDER BY created_at DESC", params
    ).fetchall()
    db.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/cold-storage/farmer-plans/<int:plan_id>", methods=["PUT"])
@require_auth(roles=["farmer"])
def update_farmer_plan(plan_id):
    db = get_db()
    existing = db.execute("SELECT * FROM farmer_crop_storage_plans WHERE id = ?", (plan_id,)).fetchone()
    if not existing:
        db.close()
        return jsonify({"error": "plan not found"}), 404
    if existing["user_email"] != g.user["email"]:
        db.close()
        return jsonify({"error": "Forbidden — not your plan"}), 403

    body = request.get_json(force=True) or {}
    if "status" in body and body["status"] not in VALID_PLAN_STATUSES:
        db.close()
        return jsonify({"error": f"status must be one of {sorted(VALID_PLAN_STATUSES)}"}), 400

    fields = [
        "state", "district", "crop", "area", "area_unit", "sowing_date",
        "expected_harvest_date", "expected_production_mt", "storage_required_mt", "status",
    ]
    updates = {f: body[f] for f in fields if f in body}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE farmer_crop_storage_plans SET {set_clause}, updated_at = ? WHERE id = ?",
            [*updates.values(), now_iso(), plan_id],
        )
        db.commit()

    row = db.execute("SELECT * FROM farmer_crop_storage_plans WHERE id = ?", (plan_id,)).fetchone()
    db.close()
    return jsonify(row_to_dict(row))


@app.route("/api/cold-storage/farmer-plans/<int:plan_id>", methods=["DELETE"])
@require_auth(roles=["farmer"])
def delete_farmer_plan(plan_id):
    db = get_db()
    existing = db.execute("SELECT id, user_email FROM farmer_crop_storage_plans WHERE id = ?", (plan_id,)).fetchone()
    if not existing:
        db.close()
        return jsonify({"error": "plan not found"}), 404
    if existing["user_email"] != g.user["email"]:
        db.close()
        return jsonify({"error": "Forbidden — not your plan"}), 403
    db.execute("DELETE FROM farmer_crop_storage_plans WHERE id = ?", (plan_id,))
    db.commit()
    db.close()
    return jsonify({"deleted": True})


# ── MAIN ────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Cold Storage Intelligence backend")
    p.add_argument("--port", type=int, default=6010)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--import-govt", action="store_true",
                   help="Fetch NHB names once, save them in SQLite, and exit.")
    p.add_argument("--states", default="rajasthan,tripura,meghalaya",
                   help="Comma-separated states used with --import-govt.")
    p.add_argument("--dummy-capacity", type=float, default=None,
                   help="Fixed POC capacity in MT to assign to every imported "
                        "facility. If omitted, each facility gets a random "
                        f"capacity between {POC_CAPACITY_MIN_MT:g} and "
                        f"{POC_CAPACITY_MAX_MT:g} MT.")
    p.add_argument("--import-missing-districts", action="store_true",
                   help="Fetch NHB names for specific districts only (see "
                        "--district-state / --districts), insert new facilities, "
                        "and exit. Use this to fill districts that were never "
                        "researched, without touching districts you already have.")
    p.add_argument("--district-state", default="rajasthan",
                   help="Single state to fetch, used with --import-missing-districts.")
    p.add_argument("--districts", default=None,
                   help="Comma-separated district names, used with "
                        "--import-missing-districts, e.g. "
                        '"Hanumangarh,Jaisalmer,Jalore,Tonk,Karauli,Pratapgarh"')
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    init_db()
    if args.import_govt:
        selected_states = [x.strip().lower() for x in args.states.split(",") if x.strip()]
        result = import_govt_facilities(selected_states, args.dummy_capacity)
        print(
            "Government import complete: "
            f"fetched={result['fetched']}, inserted={result['inserted']}, "
            f"updated={result['updated']}, "
            f"capacity_mt={args.dummy_capacity if args.dummy_capacity is not None else f'random[{POC_CAPACITY_MIN_MT:g}-{POC_CAPACITY_MAX_MT:g}]'}"
        )
        raise SystemExit(0)

    if args.import_missing_districts:
        if not args.districts:
            raise SystemExit("--import-missing-districts requires --districts \"District1,District2,...\"")
        district_list = [d.strip() for d in args.districts.split(",") if d.strip()]
        try:
            result = import_missing_district_facilities(args.district_state.strip().lower(), district_list)
        except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
            raise SystemExit(
                f"Live NHB fetch failed for state={args.district_state!r}: {exc}\n"
                "Not falling back to bundled/guessed data — a failed fetch should "
                "not look like 'NHB has zero facilities here'. Fix connectivity and retry."
            )
        print("Missing-district import complete:")
        for district, count in result["per_district_found"].items():
            if district in result["empty_districts"]:
                note = "  <- NHB has NO registered facilities here (real absence, not an error)"
            elif district in result["possible_spelling_mismatches"]:
                candidates = ", ".join(repr(c) for c in result["possible_spelling_mismatches"][district])
                note = f"  <- 0 exact matches, but NHB has similarly-spelled district(s): {candidates} — re-run with that spelling"
            else:
                note = ""
            print(f"  {district:20s} {count} facilities found{note}")
        print(f"  inserted={result['inserted']}, skipped_existing={result['skipped_existing']}")
        raise SystemExit(0)

    # Normal startup reads/writes persisted SQLite rows only; it makes no
    # live NHB network request (that only happens with --import-govt).
    # FIX: sync_real_facility_data() previously existed but was never
    # called anywhere, so the bundled real NHB/napanta facility lists never
    # made it into the DB unless someone separately ran --import-govt. It's
    # idempotent (skips rows that already exist by name+district), so it's
    # safe to run on every startup.
    sync_real_facility_data()

    # ── STORAGE PROVIDER CONFIG ROUTES ────────────────────────────────────────
    # Mounted here so they share the already-running cold-storage service
    # instead of requiring a separate process on port 5020.

    @app.route("/api/storage-provider/facilities", methods=["GET"])
    @require_auth()
    def sp_list_facilities():
        return jsonify(_sp_fetch_facilities())

    @ttl_cache(seconds=45, key_prefix="storage_provider:facilities_list")
    def _sp_fetch_facilities() -> list[dict]:
        db = get_db()
        rows = db.execute(
            "SELECT id, name, state, district, block, village, latitude, longitude, total_capacity_mt, storage_type FROM cold_storages ORDER BY LOWER(name)"
        ).fetchall()
        db.close()
        return [row_to_dict(r) for r in rows]

    @app.route("/api/storage-provider/facilities/<int:facility_id>", methods=["GET"])
    @require_auth()
    def sp_get_facility(facility_id):
        db = get_db()
        row = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        db.close()
        if not row:
            return jsonify({"error": "facility not found"}), 404
        return jsonify(row_to_dict(row))

    @app.route("/api/storage-provider/facilities/<int:facility_id>/geofence", methods=["POST"])
    @require_auth(roles=["storage_provider", "admin"])
    def sp_update_facility_geofence(facility_id):
        body = request.get_json(silent=True) or {}
        latitude = body.get("latitude")
        longitude = body.get("longitude")

        if latitude is None or longitude is None:
            return jsonify({"error": "latitude and longitude are required"}), 400

        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return jsonify({"error": "latitude and longitude must be valid numbers"}), 400

        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return jsonify({"error": "latitude must be between -90 and 90, longitude between -180 and 180"}), 400

        db = get_db()
        row = db.execute("SELECT id FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        if not row:
            db.close()
            return jsonify({"error": "facility not found"}), 404

        db.execute(
            "UPDATE cold_storages SET latitude = ?, longitude = ? WHERE id = ?",
            (latitude, longitude, facility_id),
        )
        db.commit()
        updated = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        db.close()
        invalidate_prefix("cold_storage:facilities_raw")
        invalidate_prefix("storage_provider:facilities_list")
        return jsonify(row_to_dict(updated))

    @app.route("/api/storage-provider/facilities/<int:facility_id>/geocode-address", methods=["POST"])
    @require_auth(roles=["storage_provider", "admin"])
    def sp_geocode_facility_address(facility_id):
        """Turn a free-text address into lat/lng and save it onto this
        facility. Delegates the actual geocoding to yield_platform_service's
        /geocode/search, which tries Mappls (handles Indian village/tehsil
        style addresses far better than raw Nominatim) before falling back
        to Nominatim."""
        body = request.get_json(silent=True) or {}
        address = (body.get("address") or "").strip()
        if not address:
            return jsonify({"error": "address is required"}), 400

        db = get_db()
        row = db.execute("SELECT id FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        if not row:
            db.close()
            return jsonify({"error": "facility not found"}), 404

        try:
            headers = {}
            if YIELD_PLATFORM_API_KEY:
                headers["Authorization"] = f"Bearer {YIELD_PLATFORM_API_KEY}"
            geo_resp = requests.get(
                f"{YIELD_PLATFORM_URL}/geocode/search",
                params={"q": address},
                headers=headers,
                timeout=10,
            )
            geo_resp.raise_for_status()
            geo_results = (geo_resp.json() or {}).get("results") or []
        except requests.exceptions.RequestException as exc:
            db.close()
            return jsonify({"error": f"Could not reach the geocoding service: {exc}"}), 502

        if not geo_results:
            db.close()
            return jsonify({"error": "Could not find that address on the map. Try a more specific address, or set coordinates manually."}), 404

        best = geo_results[0]
        try:
            latitude = float(best["lat"])
            longitude = float(best["lon"])
        except (KeyError, TypeError, ValueError):
            db.close()
            return jsonify({"error": "Geocoding service returned an invalid result."}), 502

        db.execute(
            "UPDATE cold_storages SET latitude = ?, longitude = ? WHERE id = ?",
            (latitude, longitude, facility_id),
        )
        db.commit()
        updated = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        db.close()

        result = row_to_dict(updated)
        result["matched_address"] = best.get("display_name")
        result["geocode_source"] = best.get("source")
        invalidate_prefix("cold_storage:facilities_raw")
        invalidate_prefix("storage_provider:facilities_list")
        return jsonify(result)


    def _sp_scope_clause(user: dict) -> tuple[str, list]:
        role = (user.get("role") or "").lower()
        clauses = []
        params = []
        if role == "storage_provider":
            provider_email = (user.get("email") or "").strip().lower()
            clauses.append("spc.provider_email = ?")
            params.append(provider_email)
        elif role == "district_admin":
            user_state = (user.get("state") or "").strip()
            user_district = (user.get("district") or "").strip()
            if user_district:
                clauses.append("cs.district = ?")
                params.append(user_district)
            if user_state:
                clauses.append("cs.state = ?")
                params.append(user_state)
        elif role == "state_admin":
            user_state = (user.get("state") or "").strip()
            if user_state:
                clauses.append("cs.state = ?")
                params.append(user_state)
        return " AND ".join(clauses) if clauses else "", params


    @app.route("/api/storage-provider/config", methods=["GET"])
    @require_auth(roles=["storage_provider", "admin", "state_admin", "district_admin"])
    def sp_list_configs():
        role = (g.user.get("role") or "").lower()
        return jsonify(_sp_fetch_configs(
            role,
            (g.user.get("email") or "").strip().lower(),
            (g.user.get("state") or "").strip(),
            (g.user.get("district") or "").strip(),
        ))

    # Cached per (role, email, state, district) — NOT a plain route cache,
    # because this response is scoped by who's asking (a storage_provider
    # only sees their own configs; a district_admin only sees their
    # district's) and that scope comes from the verified token, not from
    # anything in the URL. Caching by path alone (cached_route) would risk
    # handing one provider's/admin's configs to another. Keying ttl_cache
    # on the actual scope values keeps each cache entry correctly isolated
    # while still skipping the Neon round-trip on repeat requests from the
    # same user within the TTL window.
    @ttl_cache(seconds=30, key_prefix="storage_provider:configs_list")
    def _sp_fetch_configs(role: str, provider_email: str, state: str, district: str) -> list[dict]:
        user = {"role": role, "email": provider_email, "state": state, "district": district}
        db = get_db()
        where_clause, params = _sp_scope_clause(user)
        query = """
            SELECT spc.*, cs.name as facility_name, cs.state, cs.district, cs.block, cs.village
            FROM storage_provider_configs spc
            JOIN cold_storages cs ON cs.id = spc.facility_id
        """
        if where_clause:
            query += " WHERE " + where_clause
        query += " ORDER BY spc.updated_at DESC"
        rows = db.execute(query, params).fetchall()
        db.close()
        return [row_to_dict(r) for r in rows]

    @app.route("/api/storage-provider/config", methods=["POST"])
    @require_auth(roles=["storage_provider", "admin"])
    def sp_create_or_update_config():
        provider_email = (g.user.get("email") or "").strip().lower()
        body = request.get_json(silent=True) or {}
        facility_id = body.get("facility_id")
        free_capacity_mt = body.get("free_capacity_mt")
        is_active = body.get("is_active")
        notes = body.get("notes")

        if not facility_id:
            return jsonify({"error": "facility_id is required"}), 400

        try:
            facility_id = int(facility_id)
        except (TypeError, ValueError):
            return jsonify({"error": "facility_id must be an integer"}), 400

        db = get_db()
        facility = db.execute("SELECT id FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        if not facility:
            db.close()
            return jsonify({"error": "Facility not found"}), 404

        now = datetime.now(timezone.utc).isoformat()
        free_capacity_mt = float(free_capacity_mt) if free_capacity_mt is not None else 0.0
        is_active = 1 if is_active else 0
        notes = notes if notes is not None else ""

        existing = db.execute(
            "SELECT id FROM storage_provider_configs WHERE facility_id = ? AND provider_email = ?",
            (facility_id, provider_email),
        ).fetchone()

        if existing:
            db.execute("""
                UPDATE storage_provider_configs
                SET free_capacity_mt = ?, is_active = ?, notes = ?, updated_at = ?
                WHERE id = ?
            """, (free_capacity_mt, is_active, notes, now, existing["id"]))
            config_id = existing["id"]
        else:
            cursor = db.execute("""
                INSERT INTO storage_provider_configs (facility_id, provider_email, free_capacity_mt, is_active, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                RETURNING id
            """, (facility_id, provider_email, free_capacity_mt, is_active, notes, now))
            config_id = cursor.fetchone()["id"]

        db.commit()

        db.execute(
            "UPDATE cold_storages SET total_capacity_mt = ?, last_updated = ? WHERE id = ?",
            (free_capacity_mt, now, facility_id),
        )
        db.commit()

        row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
        db.close()
        invalidate_prefix("storage_provider:configs_list")
        invalidate_prefix("storage_provider:facilities_list")
        return jsonify(row_to_dict(row)), 200

    @app.route("/api/storage-provider/config/<int:config_id>", methods=["PATCH"])
    @require_auth(roles=["storage_provider", "admin"])
    def sp_update_config(config_id):
        provider_email = (g.user.get("email") or "").strip().lower()
        body = request.get_json(silent=True) or {}
        db = get_db()
        row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            db.close()
            return jsonify({"error": "Config not found"}), 404

        if (g.user.get("role") or "").lower() != "admin" and row["provider_email"] != provider_email:
            db.close()
            return jsonify({"error": "Forbidden — not your config"}), 403

        updates = {}
        if "free_capacity_mt" in body:
            updates["free_capacity_mt"] = float(body["free_capacity_mt"])
        if "is_active" in body:
            updates["is_active"] = 1 if body["is_active"] else 0
        if "notes" in body:
            updates["notes"] = body["notes"]
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [config_id]
        db.execute(f"UPDATE storage_provider_configs SET {set_clause} WHERE id = ?", values)
        db.commit()

        if "free_capacity_mt" in updates:
            db.execute(
                "UPDATE cold_storages SET total_capacity_mt = ?, last_updated = ? WHERE id = ?",
                (updates["free_capacity_mt"], datetime.now(timezone.utc).isoformat(), row["facility_id"]),
            )
            db.commit()

        updated = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
        db.close()
        invalidate_prefix("storage_provider:configs_list")
        invalidate_prefix("storage_provider:facilities_list")
        return jsonify(row_to_dict(updated))

    @app.route("/api/storage-provider/config/<int:config_id>", methods=["DELETE"])
    @require_auth(roles=["storage_provider", "admin", "state_admin", "district_admin"])
    def sp_delete_config(config_id):
        provider_email = (g.user.get("email") or "").strip().lower()
        db = get_db()
        row = db.execute("SELECT * FROM storage_provider_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            db.close()
            return jsonify({"error": "Config not found"}), 404

        role = (g.user.get("role") or "").lower()
        if role not in ("admin", "storage_provider"):
            facility = db.execute("SELECT state, district FROM cold_storages WHERE id = ?", (row["facility_id"],)).fetchone()
            db.close()
            if not facility:
                return jsonify({"error": "Config not found"}), 404
            user_state = (g.user.get("state") or "").strip().lower()
            user_district = (g.user.get("district") or "").strip().lower()
            fac_state = (facility["state"] or "").strip().lower()
            fac_district = (facility["district"] or "").strip().lower()
            if role == "district_admin":
                if user_state and fac_state != user_state:
                    return jsonify({"error": "Forbidden — facility outside your state scope"}), 403
                if user_district and fac_district != user_district:
                    return jsonify({"error": "Forbidden — facility outside your district scope"}), 403
            elif role == "state_admin":
                if user_state and fac_state != user_state:
                    return jsonify({"error": "Forbidden — facility outside your state scope"}), 403
        elif role == "storage_provider" and row["provider_email"] != provider_email:
            db.close()
            return jsonify({"error": "Forbidden — not your config"}), 403

        db.execute("DELETE FROM storage_provider_configs WHERE id = ?", (config_id,))
        db.commit()
        db.close()
        invalidate_prefix("storage_provider:configs_list")
        return jsonify({"status": "deleted", "id": config_id})

    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance between two lat/lon points, in kilometers."""
        r = 6371.0088  # mean Earth radius, km
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lon2 - lon1)
        a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        return 2 * r * math.asin(min(1.0, math.sqrt(a)))

    @app.route("/api/storage-provider/facilities/<int:facility_id>/nearby-farmers", methods=["GET"])
    @require_auth(roles=["storage_provider", "admin"])
    def sp_nearby_farmers(facility_id):
        """Find farmer land parcels (from yield_platform_service's parcels
        table) within a given radius of this facility's location."""
        radius_km = request.args.get("radius_km", type=float)
        if radius_km is None or radius_km <= 0:
            return jsonify({"error": "radius_km must be a positive number"}), 400

        db = get_db()
        facility = db.execute("SELECT * FROM cold_storages WHERE id = ?", (facility_id,)).fetchone()
        db.close()
        if not facility:
            return jsonify({"error": "facility not found"}), 404

        facility = row_to_dict(facility)
        f_lat, f_lng = facility.get("latitude"), facility.get("longitude")
        if f_lat is None or f_lng is None:
            return jsonify({"error": "This facility does not have a location set yet."}), 400

        try:
            headers = {}
            if YIELD_PLATFORM_API_KEY:
                headers["Authorization"] = f"Bearer {YIELD_PLATFORM_API_KEY}"
            resp = requests.get(f"{YIELD_PLATFORM_URL}/parcels", headers=headers, timeout=10)
            resp.raise_for_status()
            parcels = resp.json()
        except requests.exceptions.RequestException as exc:
            return jsonify({"error": f"Could not reach the Yield Detect service: {exc}"}), 502

        results = []
        for p in parcels:
            p_lat, p_lng = p.get("latitude"), p.get("longitude")
            if p_lat is None or p_lng is None:
                continue
            try:
                distance_km = haversine_km(float(f_lat), float(f_lng), float(p_lat), float(p_lng))
            except (TypeError, ValueError):
                continue
            if distance_km <= radius_km:
                results.append({
                    "parcel_id": p.get("id"),
                    "owner_id": p.get("ownerId"),
                    "label": p.get("label"),
                    "latitude": p_lat,
                    "longitude": p_lng,
                    "area_hectare": p.get("areaHectare"),
                    "distance_km": round(distance_km, 2),
                })

        results.sort(key=lambda r: r["distance_km"])
        return jsonify({
            "facility_id": facility_id,
            "facility_name": facility.get("name"),
            "center": {"latitude": f_lat, "longitude": f_lng},
            "radius_km": radius_km,
            "count": len(results),
            "farmers": results,
        })

    print("=" * 60)
    print(f"  COLD STORAGE INTELLIGENCE — http://{args.host}:{args.port}")
    print(f"  DB: postgres")
    print(f"  Verifying tokens against gateway: {GATEWAY_INTERNAL_URL}")
    print("=" * 60)

    # Warm the facilities cache once at launch instead of making the
    # first real request pay for it. This fires the same query
    # list_facilities() would run with no filters, so it's already hot
    # by the time the site's first user loads the page.
    try:
        warmed = _fetch_facilities_raw(None, None, None)
        print(f"  Warmed facilities cache: {len(warmed)} facilities")
    except Exception as exc:
        print(f"  Cache warm-up failed (non-fatal, will fetch on first request): {exc}")

    app.run(host=args.host, port=args.port, debug=False)