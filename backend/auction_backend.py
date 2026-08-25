"""
auction_backend.py — calls the standalone auction_engine_service over HTTP
=============================================================================
This is the crop-platform side of a true two-microservice setup:

    auction_backend.py (this file, port 5009)
        │  HTTP calls only — no shared import, no shared process
        ▼
    auction_engine_service.py (separate process, its own port e.g. 6000)
        │
        ▼
    generic_auction_engine.py (only ever imported by the service above)

This file keeps every crop-specific concern exactly as before — auth via
GATEWAY_INTERNAL_URL, Brevo/SMTP emails, unused_crops/crop_sales/active_bids
tables and routes — but the mandi-auction logic no longer touches
AuctionEngine directly. Instead it makes plain HTTP requests to
AUCTION_SERVICE_URL, the same way it already calls the gateway for auth.

Run both services:
    python auction_engine_service.py --port 6000
    AUCTION_SERVICE_URL=http://127.0.0.1:6000 python auction_backend.py --port 5009

If AUCTION_SERVICE_API_KEY is set on the service side, set the same value
here as AUCTION_SERVICE_API_KEY so this backend's requests are authorized.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import sqlite3
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ── CONFIG ───────────────────────────────────────────────────────────────

DEFAULT_PORT = 5009
DB_PATH = Path(__file__).resolve().parent / "auction.db"

GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

# The standalone generic auction microservice — a totally separate process
# reached only over HTTP, same pattern as GATEWAY_INTERNAL_URL above.
AUCTION_SERVICE_URL = os.environ.get("AUCTION_SERVICE_URL", "http://127.0.0.1:6000")
AUCTION_SERVICE_API_KEY = os.environ.get("AUCTION_SERVICE_API_KEY", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "no-reply@cropai.local")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDERS_URL = "https://api.brevo.com/v3/senders"

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8085")

app = Flask(__name__)
CORS(app)


# ── AUCTION SERVICE CLIENT ──────────────────────────────────────────────
#
# Thin HTTP wrapper around auction_engine_service.py's API. This is the
# *entire* coupling point between this app and the auction microservice —
# no imports, no shared DB, no shared process. If the service is down,
# every one of these raises AuctionServiceError, which routes turn into a
# 502 so callers get a clear signal rather than a stack trace.

class AuctionServiceError(Exception):
    pass


def _auction_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if AUCTION_SERVICE_API_KEY:
        headers["Authorization"] = f"Bearer {AUCTION_SERVICE_API_KEY}"
    return headers


def _auction_request(method: str, path: str, **kwargs) -> dict:
    try:
        resp = requests.request(
            method, f"{AUCTION_SERVICE_URL}{path}",
            headers=_auction_headers(), timeout=10, **kwargs,
        )
    except requests.exceptions.RequestException as e:
        raise AuctionServiceError(f"auction service unreachable: {e}") from e

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise AuctionServiceError(detail)

    return resp.json() if resp.content else {}


def auction_create(**payload) -> dict:
    return _auction_request("POST", "/auctions", json=payload)


def auction_get(auction_id: str) -> Optional[dict]:
    try:
        return _auction_request("GET", f"/auctions/{auction_id}")
    except AuctionServiceError:
        return None


def auction_list(**params) -> list[dict]:
    return _auction_request("GET", "/auctions", params=params).get("auctions", [])


def auction_place_bid(auction_id: str, bidder_id: str, price: float, quantity: float) -> dict:
    return _auction_request(
        "POST", f"/auctions/{auction_id}/bids",
        json={"bidderId": bidder_id, "price": price, "quantity": quantity},
    )


def auction_resolve_bid(bid_id: str, action: str, accepted_quantity: Optional[float] = None) -> dict:
    body = {"action": action}
    if accepted_quantity is not None:
        body["acceptedQuantity"] = accepted_quantity
    return _auction_request("PATCH", f"/bids/{bid_id}", json=body)


def auction_invite(auction_id: str, invitee_id: str) -> dict:
    return _auction_request("POST", f"/auctions/{auction_id}/invitations", json={"inviteeId": invitee_id})


def auction_respond_invitation(invitation_id: str, accept: bool) -> dict:
    return _auction_request("PATCH", f"/invitations/{invitation_id}", json={"accept": accept})


def auction_list_invitations(invitee_id: str, status: str = "all") -> list[dict]:
    params = {"inviteeId": invitee_id}
    if status and status != "all":
        params["status"] = status
    return _auction_request("GET", "/invitations", params=params).get("invitations", [])


def auction_set_price(auction_id: str, price: float) -> dict:
    return _auction_request(
        "PATCH",
        f"/auctions/{auction_id}/price",
        json={"price": price},
    )


@app.errorhandler(AuctionServiceError)
def handle_auction_service_error(err):
    return jsonify({"error": f"auction service error: {err}"}), 502


# ── DB SETUP (crop listings only — the auction service owns its own DB) ──

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Crop listings — this app's own data. The mandi_auctions/mandi_bids/
    # invitations tables that used to live here now live entirely inside
    # auction_engine_service.py's own database — this app never touches
    # that DB file directly, only through HTTP calls above.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS unused_crops (
            id                TEXT PRIMARY KEY,
            farmer_email      TEXT NOT NULL,
            crop_type         TEXT NOT NULL,
            state             TEXT NOT NULL,
            district          TEXT NOT NULL,
            total_production  REAL NOT NULL CHECK (total_production > 0),
            sold_production   REAL NOT NULL DEFAULT 0 CHECK (sold_production >= 0),
            created_at        INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS crop_sales (
            id                TEXT PRIMARY KEY,
            crop_id           TEXT NOT NULL REFERENCES unused_crops(id) ON DELETE CASCADE,
            bid_id            TEXT,
            buyer             TEXT NOT NULL,
            quantity          REAL NOT NULL CHECK (quantity > 0),
            price_per_tonne   REAL NOT NULL,
            sold_at           INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS active_bids (
            id                TEXT PRIMARY KEY,
            crop_id           TEXT NOT NULL REFERENCES unused_crops(id) ON DELETE CASCADE,
            buyer             TEXT NOT NULL,
            price_per_tonne   REAL NOT NULL,
            quantity          REAL NOT NULL CHECK (quantity > 0),
            status            TEXT NOT NULL DEFAULT 'leading',
            ends_at           INTEGER NOT NULL
        );

        -- Local cache of which auctions (by id, from the auction service)
        -- match which state/district/crop — needed so create_crop can
        -- find "any active auction I should now be invited to" without
        -- asking the auction service to understand crop matching, which
        -- it deliberately knows nothing about.
        CREATE TABLE IF NOT EXISTS auction_crop_index (
            auction_id  TEXT PRIMARY KEY,
            crop_type   TEXT NOT NULL,
            state       TEXT NOT NULL,
            district    TEXT NOT NULL,
            mandi_email TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_crops_farmer ON unused_crops(farmer_email);
        CREATE INDEX IF NOT EXISTS idx_sales_crop ON crop_sales(crop_id);
        CREATE INDEX IF NOT EXISTS idx_bids_crop ON active_bids(crop_id);
        CREATE INDEX IF NOT EXISTS idx_auction_index_match ON auction_crop_index(state, district, crop_type);

        -- Local record of which (auction, farmer) invitations this app has
        -- already sent, so create_crop's late-invite check doesn't spam
        -- the same farmer twice. The auction service is the source of
        -- truth for invitation *status* (pending/accepted/declined); this
        -- table only prevents duplicate invite calls.
        CREATE TABLE IF NOT EXISTS invitations_seen (
            auction_id  TEXT NOT NULL,
            invitee_id  TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            PRIMARY KEY (auction_id, invitee_id)
        );

        -- The generic auction service intentionally knows nothing about
        -- CropAI's unused_crops table. Keep this local mapping so that when
        -- a generic bid is accepted, CropAI can deduct the exact listing.
        CREATE TABLE IF NOT EXISTS auction_bid_crop_index (
            bid_id       TEXT PRIMARY KEY,
            auction_id   TEXT NOT NULL,
            crop_id      TEXT NOT NULL REFERENCES unused_crops(id) ON DELETE CASCADE,
            farmer_email TEXT NOT NULL,
            created_at   INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_auction_bid_crop_auction
            ON auction_bid_crop_index(auction_id);

        CREATE TABLE IF NOT EXISTS mandi_senders (
            mandi_email      TEXT PRIMARY KEY,
            brevo_sender_id  TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            requested_at     INTEGER NOT NULL,
            verified_at      INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ── AUTH HELPER (unchanged) ──────────────────────────────────────────────

def current_user() -> Optional[dict]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    try:
        resp = requests.get(
            f"{GATEWAY_INTERNAL_URL}/api/auth/me",
            headers={"Authorization": auth_header},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("user") if isinstance(data.get("user"), dict) else data
    except requests.exceptions.RequestException:
        pass
    return None


def current_farmer_email() -> Optional[str]:
    user = current_user()
    return user.get("email") if user else None


# ── CROP <-> ENGINE FIELD MAPPING ────────────────────────────────────────
#
# The engine only knows about owner_id / bidder_id / category / metadata.
# These two helpers are the entire "translation layer" between crop
# vocabulary and the generic auction schema — this is the piece you'd
# rewrite if you pointed this same engine at a different domain.

def _auction_metadata(state: str, district: str) -> str:
    return json.dumps({"state": state, "district": district})


def _parse_auction_metadata(meta_json: Optional[str]) -> dict:
    if not meta_json:
        return {"state": None, "district": None}
    try:
        return json.loads(meta_json)
    except (TypeError, ValueError):
        return {"state": None, "district": None}


def serialize_mandi_auction(auction: dict) -> dict:
    """Reshapes the engine's generic auction dict back into the
    crop-flavored JSON the existing frontend already expects."""
    meta = _parse_auction_metadata(auction["metadata"])
    out = {
        "id": auction["id"],
        "mandiEmail": auction["ownerId"],
        "auctionName": auction["itemLabel"],
        "cropType": auction["category"],
        "state": meta.get("state"),
        "district": meta.get("district"),
        "targetQuantity": auction["targetQuantity"],
        "remainingQuantity": auction["remainingQuantity"],
        "basePrice": auction["basePrice"],
        "mandiPrice": auction["ownerPrice"],
        "counterGap": auction["counterGap"],
        "auctionType": auction["auctionType"],
        "durationMinutes": auction["durationMinutes"],
        "extensionMinutes": auction["extensionMinutes"],
        "startsAt": auction.get("startsAt", auction.get("createdAt")),
        "endsAt": auction.get(
            "endsAt",
            (auction.get("startsAt", auction.get("createdAt", now_ms()))
             + int(auction.get("durationMinutes", 0) or 0) * 60_000)
        ),
        "status": auction["status"],
        "createdAt": auction["createdAt"],
        "leadingPrice": auction["leadingPrice"],
        "requiredBidPrice": auction["requiredBidPrice"],
        "outcome": auction["outcome"],
    }
    if "bids" in auction:
        out["bids"] = [
            {
                "id": b["id"],
                "auctionId": b["auctionId"],
                "farmerEmail": b["bidderId"],
                "pricePerTonne": b["price"],
                "quantity": b["quantity"],
                "acceptedQuantity": b["acceptedQuantity"],
                "status": b["status"],
                "createdAt": b["createdAt"],
            }
            for b in auction["bids"]
        ]
        out["bidCount"] = auction["bidCount"]
    return out


# ── SERIALIZATION FOR CROP LISTINGS / DIRECT BIDS (unchanged) ────────────

def serialize_crop(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    sales = db.execute(
        "SELECT id, buyer, quantity, price_per_tonne, sold_at FROM crop_sales "
        "WHERE crop_id = ? ORDER BY sold_at DESC",
        (row["id"],),
    ).fetchall()

    remaining = max(0.0, row["total_production"] - row["sold_production"])
    status = "sold" if remaining <= 0 else ("partial" if row["sold_production"] > 0 else "open")

    return {
        "id": row["id"],
        "farmerEmail": row["farmer_email"],
        "cropType": row["crop_type"],
        "state": row["state"],
        "district": row["district"],
        "totalProduction": row["total_production"],
        "soldProduction": row["sold_production"],
        "remainingProduction": remaining,
        "status": status,
        "createdAt": row["created_at"],
        "saleHistory": [
            {
                "id": s["id"],
                "buyer": s["buyer"],
                "quantity": s["quantity"],
                "pricePerTonne": s["price_per_tonne"],
                "soldAt": s["sold_at"],
            }
            for s in sales
        ],
    }


def serialize_bid(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "cropId": row["crop_id"],
        "buyer": row["buyer"],
        "pricePerTonne": row["price_per_tonne"],
        "quantity": row["quantity"],
        "status": row["status"],
        "endsAt": row["ends_at"],
    }


# ── unused_crops ROUTES (unchanged from original) ─────────────────────────

@app.route("/api/unused-crops", methods=["GET"])
def list_crops():
    db = get_db()
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()

    if farmer_email:
        rows = db.execute(
            "SELECT * FROM unused_crops WHERE farmer_email = ? ORDER BY created_at DESC",
            (farmer_email,),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM unused_crops ORDER BY created_at DESC").fetchall()

    return jsonify([serialize_crop(db, r) for r in rows])


@app.route("/api/unused-crops/<crop_id>", methods=["GET"])
def get_crop(crop_id):
    db = get_db()
    row = db.execute("SELECT * FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found."}), 404
    return jsonify(serialize_crop(db, row))


@app.route("/api/unused-crops", methods=["POST"])
def create_crop():
    db = get_db()
    body = request.get_json(silent=True) or {}

    farmer_email = body.get("farmerEmail") or current_farmer_email()
    crop_type = body.get("cropType")
    state = body.get("state")
    district = body.get("district")
    total_production = body.get("totalProduction")

    if not all([farmer_email, crop_type, state, district, total_production]):
        return jsonify({"error": "farmerEmail, cropType, state, district and totalProduction are required."}), 400

    try:
        total_production = float(total_production)
    except (TypeError, ValueError):
        return jsonify({"error": "totalProduction must be a number."}), 400

    if total_production <= 0:
        return jsonify({"error": "Total production must be greater than 0."}), 400

    crop_id = new_id("crop")
    created_at = now_ms()

    db.execute(
        """
        INSERT INTO unused_crops
            (id, farmer_email, crop_type, state, district, total_production, sold_production, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (crop_id, farmer_email, crop_type, state, district, total_production, created_at),
    )
    db.commit()

    row = db.execute("SELECT * FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    serialized = serialize_crop(db, row)

    # A crop listing filed after a matching auction is already running used
    # to get no invitation at all — the match check only ran once, at the
    # moment the auction was created. Catch that here: any currently-active
    # auction for this exact crop/state/district gets this farmer a pending
    # invitation (and the same notification email) right now, so they can
    # still join it while it's running.
    #
    # Since mandi_auctions now lives entirely in the auction service's own
    # DB, we can't just SELECT against it — we keep a small local index
    # (auction_crop_index) written at auction-creation time and check that
    # instead, then confirm each candidate is still active via one HTTP
    # call to the service.
    candidates = db.execute(
        "SELECT auction_id, mandi_email FROM auction_crop_index "
        "WHERE crop_type = ? AND state = ? AND district = ?",
        (crop_type, state, district),
    ).fetchall()
    newly_invited = []
    for candidate in candidates:
        auction = auction_get(candidate["auction_id"])
        if auction is None or auction["status"] != "active":
            continue
        existing = db.execute(
            "SELECT 1 FROM invitations_seen WHERE auction_id = ? AND invitee_id = ?",
            (auction["id"], farmer_email),
        ).fetchone()
        if existing:
            continue  # already invited (or already responded) — don't touch it
        try:
            auction_invite(auction["id"], farmer_email)
        except AuctionServiceError:
            continue
        db.execute(
            "INSERT OR IGNORE INTO invitations_seen (auction_id, invitee_id, created_at) VALUES (?, ?, ?)",
            (auction["id"], farmer_email, now_ms()),
        )
        newly_invited.append((auction, candidate["mandi_email"]))
    db.commit()

    for auction, _mandi_email in newly_invited:
        send_auction_started_email(serialize_mandi_auction(auction), [farmer_email])

    serialized["newInvitations"] = len(newly_invited)
    return jsonify(serialized), 201


@app.route("/api/unused-crops/<crop_id>", methods=["PATCH"])
def update_crop(crop_id):
    db = get_db()
    row = db.execute("SELECT * FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found."}), 404

    body = request.get_json(silent=True) or {}

    crop_type = body.get("cropType", row["crop_type"])
    state = body.get("state", row["state"])
    district = body.get("district", row["district"])
    total_production = body.get("totalProduction", row["total_production"])

    try:
        total_production = float(total_production)
    except (TypeError, ValueError):
        return jsonify({"error": "totalProduction must be a number."}), 400

    if total_production < row["sold_production"]:
        return jsonify({"error": "Total production can't be less than what's already sold."}), 400

    db.execute(
        "UPDATE unused_crops SET crop_type = ?, state = ?, district = ?, total_production = ? WHERE id = ?",
        (crop_type, state, district, total_production, crop_id),
    )
    db.commit()

    updated = db.execute("SELECT * FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    return jsonify(serialize_crop(db, updated))


@app.route("/api/unused-crops/<crop_id>", methods=["DELETE"])
def delete_crop(crop_id):
    db = get_db()
    cur = db.execute("DELETE FROM unused_crops WHERE id = ?", (crop_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Listing not found."}), 404
    return jsonify({"success": True})


@app.route("/api/unused-crops/<crop_id>/sell", methods=["POST"])
def sell_crop(crop_id):
    """
    Called the moment an auction closes and a bid wins. Deducts the sold
    quantity from the listing's remaining production and logs the sale —
    this is the auto-update: a 20t listing minus a 15t sale immediately
    reports 5t remaining, no manual edit required.
    """
    db = get_db()
    body = request.get_json(silent=True) or {}

    quantity = body.get("quantity")
    price_per_tonne = body.get("pricePerTonne")
    buyer = body.get("buyer")
    bid_id = body.get("bidId")

    if not all([quantity, price_per_tonne, buyer]):
        return jsonify({"error": "quantity, pricePerTonne and buyer are required."}), 400

    try:
        quantity = float(quantity)
        price_per_tonne = float(price_per_tonne)
    except (TypeError, ValueError):
        return jsonify({"error": "quantity and pricePerTonne must be numbers."}), 400

    row = db.execute("SELECT * FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found."}), 404

    remaining = row["total_production"] - row["sold_production"]
    if quantity > remaining:
        return jsonify({"error": f"Only {remaining} t remaining — can't sell {quantity} t."}), 400

    db.execute(
        "UPDATE unused_crops SET sold_production = sold_production + ? WHERE id = ?",
        (quantity, crop_id),
    )
    db.execute(
        """
        INSERT INTO crop_sales (id, crop_id, bid_id, buyer, quantity, price_per_tonne, sold_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("sale"), crop_id, bid_id, buyer, quantity, price_per_tonne, now_ms()),
    )
    if bid_id:
        db.execute("UPDATE active_bids SET status = 'won' WHERE id = ?", (bid_id,))

    db.commit()

    updated = db.execute("SELECT * FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    return jsonify(serialize_crop(db, updated))


# ── DIRECT BID ROUTES (unchanged — /api/bids, separate from mandi auctions) ─

@app.route("/api/bids", methods=["GET"])
def list_bids():
    db = get_db()
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()
    crop_id = request.args.get("cropId")

    if crop_id:
        rows = db.execute(
            "SELECT * FROM active_bids WHERE crop_id = ? ORDER BY ends_at ASC", (crop_id,)
        ).fetchall()
    elif farmer_email:
        rows = db.execute(
            """
            SELECT b.* FROM active_bids b
            JOIN unused_crops c ON c.id = b.crop_id
            WHERE c.farmer_email = ?
            ORDER BY b.ends_at ASC
            """,
            (farmer_email,),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM active_bids ORDER BY ends_at ASC").fetchall()

    return jsonify([serialize_bid(r) for r in rows])


@app.route("/api/bids", methods=["POST"])
def create_bid():
    db = get_db()
    body = request.get_json(silent=True) or {}

    crop_id = body.get("cropId")
    buyer = body.get("buyer")
    price_per_tonne = body.get("pricePerTonne")
    quantity = body.get("quantity")
    ends_at = body.get("endsAt")

    if not all([crop_id, buyer, price_per_tonne, quantity, ends_at]):
        return jsonify({"error": "cropId, buyer, pricePerTonne, quantity and endsAt are required."}), 400

    crop = db.execute("SELECT id FROM unused_crops WHERE id = ?", (crop_id,)).fetchone()
    if not crop:
        return jsonify({"error": "Listing not found."}), 404

    bid_id = new_id("bid")
    db.execute(
        """
        INSERT INTO active_bids (id, crop_id, buyer, price_per_tonne, quantity, status, ends_at)
        VALUES (?, ?, ?, ?, ?, 'leading', ?)
        """,
        (bid_id, crop_id, buyer, float(price_per_tonne), float(quantity), int(ends_at)),
    )
    db.commit()

    row = db.execute("SELECT * FROM active_bids WHERE id = ?", (bid_id,)).fetchone()
    return jsonify(serialize_bid(row)), 201


@app.route("/api/bids/<bid_id>/status", methods=["PATCH"])
def update_bid_status(bid_id):
    db = get_db()
    body = request.get_json(silent=True) or {}
    status = body.get("status")

    if status not in ("leading", "outbid", "won"):
        return jsonify({"error": "status must be leading, outbid, or won."}), 400

    cur = db.execute("UPDATE active_bids SET status = ? WHERE id = ?", (status, bid_id))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Bid not found."}), 404

    row = db.execute("SELECT * FROM active_bids WHERE id = ?", (bid_id,)).fetchone()
    return jsonify(serialize_bid(row))


# ── MANDI AUCTION ROUTES (calling auction_engine_service over HTTP) ──────

@app.route("/api/mandi/matching-farmers", methods=["GET"])
def matching_farmers():
    db = get_db()
    state = request.args.get("state")
    district = request.args.get("district")
    crop_type = request.args.get("cropType")
    if not all([state, district, crop_type]):
        return jsonify({"error": "state, district and cropType are required."}), 400

    rows = db.execute(
        """
        SELECT farmer_email, SUM(total_production - sold_production) AS available_production
        FROM unused_crops
        WHERE state=? AND district=? AND crop_type=?
          AND farmer_email IS NOT NULL AND farmer_email != ''
        GROUP BY farmer_email
        ORDER BY farmer_email
        """,
        (state, district, crop_type),
    ).fetchall()

    return jsonify([
        {"farmerEmail": r["farmer_email"],
         "availableProduction": r["available_production"] or 0}
        for r in rows
    ])


@app.route("/api/mandi/auctions", methods=["POST"])
def create_mandi_auction():
    user = current_user()
    if not user or not user.get("email"):
        return jsonify({"error": "authentication required"}), 401

    body = request.get_json(force=True) or {}
    required = ["auctionName", "cropType", "state", "district",
                "targetQuantity", "basePrice", "auctionType", "durationMinutes"]
    missing = [f for f in required if body.get(f) in (None, "")]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    try:
        auction = auction_create(
            ownerId=user["email"],
            itemLabel=body["auctionName"],
            category=body["cropType"],
            metadata=_auction_metadata(body["state"], body["district"]),
            targetQuantity=float(body["targetQuantity"]),
            basePrice=float(body["basePrice"]),
            auctionType=body["auctionType"],
            durationMinutes=int(body["durationMinutes"]),
            counterGap=float(body.get("counterGap", 0) or 0),
            extensionMinutes=int(body.get("extensionMinutes", 0) or 0),
            startsAt=int(body["startsAt"]) if body.get("startsAt") is not None else None,
        )
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 400

    db = get_db()
    db.execute(
        """INSERT OR REPLACE INTO auction_crop_index
           (auction_id, crop_type, state, district, mandi_email, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (auction["id"], body["cropType"], body["state"], body["district"],
         user["email"], now_ms()),
    )

    rows = db.execute(
        """SELECT DISTINCT farmer_email FROM unused_crops
           WHERE state=? AND district=? AND crop_type=?""",
        (body["state"], body["district"], body["cropType"]),
    ).fetchall()

    farmer_emails = [r["farmer_email"] for r in rows if r["farmer_email"]]

    selected = body.get("selectedFarmerEmails", body.get("farmerEmails"))
    if selected is not None:
        if not isinstance(selected, list):
            return jsonify({"error": "selectedFarmerEmails must be an array."}), 400
        selected_set = set(selected)
        farmer_emails = [e for e in farmer_emails if e in selected_set]

    for email in farmer_emails:
        try:
            auction_invite(auction["id"], email)
            db.execute(
                """INSERT OR IGNORE INTO invitations_seen
                   (auction_id, invitee_id, created_at) VALUES (?, ?, ?)""",
                (auction["id"], email, now_ms()),
            )
        except AuctionServiceError as e:
            print(f"[auction_backend] failed to invite {email}: {e}")

    db.commit()

    result = serialize_mandi_auction(auction)
    result["notifiedCount"] = send_auction_started_email(result, farmer_emails)
    result["matchedFarmerCount"] = len(farmer_emails)
    return jsonify(result), 201


@app.route("/api/mandi/auctions", methods=["GET"])
def list_mandi_auctions():
    params = {}
    mandi_email = request.args.get("mandiEmail")
    if not mandi_email:
        user = current_user()
        mandi_email = user.get("email") if user else None
    if mandi_email:
        params["ownerId"] = mandi_email
    if request.args.get("status"):
        params["status"] = request.args["status"]

    auctions = auction_list(**params)
    # Keep the original frontend contract: plain array.
    return jsonify([serialize_mandi_auction(a) for a in auctions])


@app.route("/api/mandi/auctions/<auction_id>", methods=["GET"])
def get_mandi_auction(auction_id):
    auction = auction_get(auction_id)
    if auction is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(serialize_mandi_auction(auction))


@app.route("/api/mandi/auctions/<auction_id>/price", methods=["PATCH"])
def set_mandi_price(auction_id):
    body = request.get_json(silent=True) or {}
    new_price = body.get("pricePerTonne")

    if new_price is None:
        return jsonify({"error": "pricePerTonne is required."}), 400

    try:
        new_price = float(new_price)
    except (TypeError, ValueError):
        return jsonify({"error": "pricePerTonne must be a number."}), 400

    if new_price <= 0:
        return jsonify({"error": "pricePerTonne must be greater than 0."}), 400

    try:
        updated = auction_set_price(auction_id, new_price)
        return jsonify(serialize_mandi_auction(updated))
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/mandi/auctions/active", methods=["GET"])
def list_active_auctions_for_farmer():
    state = request.args.get("state")
    district = request.args.get("district")
    crop_type = request.args.get("cropType")
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()

    if not all([state, district, crop_type, farmer_email]):
        return jsonify({
            "error": "state, district, cropType and farmerEmail are required."
        }), 400

    try:
        invitations = auction_list_invitations(farmer_email, status="all")
        accepted_ids = {
            i["auctionId"] for i in invitations
            if i.get("status") == "accepted"
        }
        if not accepted_ids:
            return jsonify([])

        result = []
        for auction in auction_list():
            if auction["id"] not in accepted_ids or auction.get("status") != "active":
                continue
            meta = _parse_auction_metadata(auction.get("metadata"))
            if (auction.get("category") == crop_type
                    and meta.get("state") == state
                    and meta.get("district") == district):
                result.append(serialize_mandi_auction(auction))
        return jsonify(result)
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/mandi/auctions/participated", methods=["GET"])
def list_participated_auctions():
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()
    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400

    try:
        invitations = auction_list_invitations(farmer_email, status="all")
        accepted_ids = {
            i["auctionId"] for i in invitations
            if i.get("status") == "accepted"
        }
        result = [
            serialize_mandi_auction(a)
            for a in auction_list()
            if a["id"] in accepted_ids
        ]
        result.sort(key=lambda a: a.get("createdAt", 0), reverse=True)
        return jsonify(result)
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/mandi/auctions/<auction_id>/bids", methods=["POST"])
def place_mandi_bid(auction_id):
    user = current_user()
    body = request.get_json(silent=True) or {}

    farmer_email = (
        body.get("farmerEmail")
        or (user.get("email") if user else None)
        or current_farmer_email()
    )
    price = body.get("pricePerTonne")
    crop_id = body.get("cropId")

    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400
    if price is None:
        return jsonify({"error": "pricePerTonne is required."}), 400

    try:
        invitations = auction_list_invitations(farmer_email, status="all")
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 502

    if not any(i.get("auctionId") == auction_id and i.get("status") == "accepted"
               for i in invitations):
        return jsonify({"error": "Accept this auction's invitation before placing a bid."}), 403

    auction = auction_get(auction_id)
    if auction is None:
        return jsonify({"error": "Auction not found."}), 404

    db = get_db()
    quantity = body.get("quantity")

    if crop_id:
        crop = db.execute(
            """SELECT * FROM unused_crops WHERE id=? AND farmer_email=?""",
            (crop_id, farmer_email),
        ).fetchone()
        if not crop:
            return jsonify({"error": "Crop listing not found."}), 404

        meta = _parse_auction_metadata(auction.get("metadata"))
        if (crop["crop_type"] != auction.get("category")
                or crop["state"] != meta.get("state")
                or crop["district"] != meta.get("district")):
            return jsonify({"error": "That crop listing doesn't match this auction."}), 400

        available = float(crop["total_production"]) - float(crop["sold_production"])
        if available <= 0:
            return jsonify({"error": "This crop listing has no production left."}), 400

        quantity = min(
            available,
            float(auction.get("remainingQuantity", available)),
        )
    elif quantity is None:
        return jsonify({"error": "cropId or quantity is required."}), 400

    try:
        bid = auction_place_bid(
            auction_id, farmer_email, float(price), float(quantity)
        )
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 400

    if crop_id:
        db.execute(
            """INSERT OR REPLACE INTO auction_bid_crop_index
               (bid_id, auction_id, crop_id, farmer_email, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (bid["id"], auction_id, crop_id, farmer_email, now_ms()),
        )
        db.commit()

    return jsonify({
        "id": bid["id"],
        "auctionId": bid["auctionId"],
        "farmerEmail": bid["bidderId"],
        "cropId": crop_id,
        "pricePerTonne": bid["price"],
        "quantity": bid["quantity"],
        "acceptedQuantity": bid.get("acceptedQuantity"),
        "status": bid["status"],
        "createdAt": bid.get("createdAt"),
    }), 201


@app.route("/api/mandi/auctions/<auction_id>/bids/<bid_id>", methods=["PATCH"])
def resolve_mandi_bid(auction_id, bid_id):
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("accept", "reject"):
        return jsonify({"error": "action must be 'accept' or 'reject'"}), 400

    db = get_db()
    mapping = db.execute(
        """SELECT crop_id, farmer_email
           FROM auction_bid_crop_index
           WHERE bid_id=? AND auction_id=?""",
        (bid_id, auction_id),
    ).fetchone()

    # Read the bid before resolving it so the local crop sale keeps the
    # exact auction price in its sale history.
    auction_before = auction_get(auction_id) or {}
    bid_before = next(
        (b for b in auction_before.get("bids", []) if b.get("id") == bid_id),
        None,
    )

    try:
        result = auction_resolve_bid(
            bid_id,
            action,
            accepted_quantity=body.get("acceptedQuantity") if action == "accept" else None,
        )
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 400

    last_accept = None

    if action == "accept":
        accepted_qty = float(result.get("acceptedQuantity") or 0)

        if mapping and accepted_qty > 0:
            crop = db.execute(
                """SELECT * FROM unused_crops
                   WHERE id=? AND farmer_email=?""",
                (mapping["crop_id"], mapping["farmer_email"]),
            ).fetchone()

            if crop:
                # Clamp against current remaining stock so a stale page can
                # never drive sold_production beyond total_production.
                available = max(
                    0.0,
                    float(crop["total_production"]) - float(crop["sold_production"]),
                )
                deducted = min(accepted_qty, available)

                if deducted > 0:
                    db.execute(
                        """UPDATE unused_crops
                           SET sold_production = MIN(total_production, sold_production + ?)
                           WHERE id=?""",
                        (deducted, mapping["crop_id"]),
                    )

                    auction = auction_get(auction_id) or {}
                    buyer = auction.get("ownerId", "Mandi")

                    # UNIQUE is not required here because the bid is already
                    # resolved by the auction service; INSERT OR IGNORE makes
                    # retries safe when the same accepted bid is returned.
                    existing = db.execute(
                        "SELECT id FROM crop_sales WHERE bid_id=?",
                        (bid_id,),
                    ).fetchone()
                    if not existing:
                        db.execute(
                            """INSERT INTO crop_sales
                               (id, crop_id, bid_id, buyer, quantity, price_per_tonne, sold_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                new_id("sale"),
                                mapping["crop_id"],
                                bid_id,
                                buyer,
                                deducted,
                                float((bid_before or {}).get("price", 0)),
                                now_ms(),
                            ),
                        )

                    last_accept = {
                        "farmerEmail": mapping["farmer_email"],
                        "acceptedQuantity": deducted,
                        "leftoverQuantity": max(0.0, accepted_qty - deducted),
                        "auctionClosed": float(result.get("remainingQuantity") or 0) <= 0,
                    }
                    db.commit()

    response = dict(result)
    if last_accept:
        response["lastAccept"] = last_accept
    return jsonify(response)


@app.route("/api/mandi/auctions/<auction_id>/invitations/respond", methods=["PATCH"])
def respond_to_invitation(auction_id):
    user = current_user()
    body = request.get_json(silent=True) or {}

    farmer_email = (
        body.get("farmerEmail")
        or (user.get("email") if user else None)
        or current_farmer_email()
    )

    action = body.get("action")
    invitation_id = body.get("invitationId")

    # Backward-compatible support for {accept: true/false}.
    if not action and "accept" in body:
        action = "accept" if bool(body["accept"]) else "decline"

    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400
    if action not in ("accept", "decline"):
        return jsonify({"error": "action must be 'accept' or 'decline'."}), 400

    try:
        if not invitation_id:
            invitations = auction_list_invitations(farmer_email, status="all")
            invitation = next(
                (i for i in invitations if i.get("auctionId") == auction_id),
                None,
            )
            if invitation is None:
                return jsonify({"error": "No invitation found for this farmer on this auction."}), 404
            invitation_id = invitation["id"]

        updated = auction_respond_invitation(
            invitation_id,
            action == "accept",
        )
        return jsonify({
            "id": updated.get("id", invitation_id),
            "invitationId": updated.get("id", invitation_id),
            "auctionId": updated.get("auctionId", auction_id),
            "farmerEmail": updated.get("inviteeId", farmer_email),
            "status": updated.get(
                "status",
                "accepted" if action == "accept" else "declined",
            ),
            "createdAt": updated.get("createdAt"),
            "respondedAt": updated.get("respondedAt"),
        })
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/mandi/auctions/invitations", methods=["GET"])
def list_invitations():
    user = current_user()
    farmer_email = (
        request.args.get("farmerEmail")
        or (user.get("email") if user else None)
        or current_farmer_email()
    )
    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400

    status_filter = request.args.get("status", "all")

    try:
        invitations = auction_list_invitations(farmer_email, status=status_filter)
        result = []

        for invitation in invitations:
            auction = auction_get(invitation["auctionId"])
            if auction is None:
                continue
            result.append({
                "id": invitation["id"],
                "auctionId": invitation["auctionId"],
                "farmerEmail": invitation.get("inviteeId", farmer_email),
                "status": invitation["status"],
                "createdAt": invitation.get("createdAt"),
                "respondedAt": invitation.get("respondedAt"),
                "auction": serialize_mandi_auction(auction),
            })

        # Keep the original frontend contract: plain array.
        return jsonify(result)
    except AuctionServiceError as e:
        return jsonify({"error": str(e)}), 502


# ── EMAIL NOTIFICATION (unchanged from original, minor param rename) ────

def _brevo_headers() -> dict:
    return {"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"}


def ensure_mandi_sender_registered(db: sqlite3.Connection, mandi_email: str) -> None:
    if not BREVO_API_KEY or not mandi_email:
        return
    existing = db.execute(
        "SELECT status FROM mandi_senders WHERE mandi_email = ?", (mandi_email,)
    ).fetchone()
    if existing is not None:
        return
    try:
        resp = requests.post(
            BREVO_SENDERS_URL, headers=_brevo_headers(),
            json={"name": mandi_email.split("@")[0], "email": mandi_email}, timeout=10,
        )
        if resp.status_code in (200, 201):
            sender_id = str(resp.json().get("id", ""))
            db.execute(
                "INSERT INTO mandi_senders (mandi_email, brevo_sender_id, status, requested_at) "
                "VALUES (?, ?, 'pending', ?)", (mandi_email, sender_id, now_ms()),
            )
        else:
            db.execute(
                "INSERT INTO mandi_senders (mandi_email, status, requested_at) VALUES (?, 'failed', ?)",
                (mandi_email, now_ms()),
            )
        db.commit()
    except requests.exceptions.RequestException as e:
        print(f"[auction_backend] Brevo sender registration errored for {mandi_email}: {e}")


def refresh_mandi_sender_status(db: sqlite3.Connection, mandi_email: str) -> str:
    row = db.execute(
        "SELECT brevo_sender_id, status FROM mandi_senders WHERE mandi_email = ?", (mandi_email,)
    ).fetchone()
    if row is None:
        return "unregistered"
    if row["status"] == "verified" or not BREVO_API_KEY or not row["brevo_sender_id"]:
        return row["status"]
    try:
        resp = requests.get(f"{BREVO_SENDERS_URL}/{row['brevo_sender_id']}",
                             headers=_brevo_headers(), timeout=10)
        if resp.status_code == 200 and resp.json().get("active"):
            db.execute(
                "UPDATE mandi_senders SET status = 'verified', verified_at = ? WHERE mandi_email = ?",
                (now_ms(), mandi_email),
            )
            db.commit()
            return "verified"
    except requests.exceptions.RequestException as e:
        print(f"[auction_backend] Brevo sender status check failed for {mandi_email}: {e}")
    return row["status"]


def send_auction_started_email(auction: dict, farmer_emails: list[str]) -> int:
    if not farmer_emails:
        return 0
    if not SMTP_HOST:
        print(f"[auction_backend] SMTP not configured — skipping email to "
              f"{len(farmer_emails)} farmer(s) for auction '{auction['auctionName']}'.")
        return 0

    db = get_db()
    link = f"{APP_BASE_URL}/auction-farmer"
    mandi_email = auction.get("mandiEmail") or SMTP_FROM
    sender_status = refresh_mandi_sender_status(db, mandi_email) if mandi_email != SMTP_FROM else "verified"
    use_mandi_as_from = sender_status == "verified"
    from_addr = mandi_email if use_mandi_as_from else SMTP_FROM

    subject = f"New auction open — {auction['cropType']} — {auction['auctionName']}"
    html = f"""
    <p>A mandi buyer has opened a new auction for <b>{auction['cropType']}</b> in your area and
    invited you to bid. <a href="{link}">Open the auction floor</a>.</p>
    <table>
      <tr><td style="padding:8px 0;font-weight:600;">{auction['cropType']}</td></tr>
    </table>
    """

    sent = 0
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USER and SMTP_PASS:
            server.login(SMTP_USER, SMTP_PASS)
        for email in farmer_emails:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = email
            if not use_mandi_as_from:
                msg["Reply-To"] = mandi_email
            msg.attach(MIMEText(html, "html"))
            server.sendmail(from_addr, [email], msg.as_string())
            sent += 1
        server.quit()
    except Exception as e:
        print(f"[auction_backend] Failed to send auction emails: {e}")

    if mandi_email and mandi_email != SMTP_FROM:
        ensure_mandi_sender_registered(db, mandi_email)

    return sent


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "auction_backend"})


@app.route("/api/server-time")
def server_time():
    return jsonify({"now": now_ms(), "serverTime": now_ms()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    init_db()
    app.run(host="0.0.0.0", port=args.port, debug=False)