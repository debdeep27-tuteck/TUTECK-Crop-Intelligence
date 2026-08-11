"""
auction_backend.py

Farmer auction/marketplace backend for the Crop Analytics suite.

Stores crop listings in a SQLite table called `unused_crops`, plus
`crop_sales` (sale history) and `active_bids` (open auction bids).

Runs standalone on its own port and is reached through gateway.py's
/api/auction/<path> proxy, the same way disease_backend.py and
yield_detect_backend.py are — see forward_request() in gateway.py.

Auth: like yield_detect_backend.py, this service has no session store of
its own. It verifies the caller's bearer token by calling the gateway's
/api/auth/me endpoint, so it needs to know where the gateway ended up
(passed in via GATEWAY_INTERNAL_URL, same convention main.py already uses
for yield_detect_backend.py).

Run directly for local testing:
    python auction_backend.py --port 5009

Usually started by main.py as part of the full suite.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ── CONFIG ─────────────────────────────────────────────────────────────

DEFAULT_PORT = 5009
DB_PATH = Path(__file__).resolve().parent / "auction.db"

GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

app = Flask(__name__)
CORS(app)


# ── DB SETUP ───────────────────────────────────────────────────────────

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

        CREATE INDEX IF NOT EXISTS idx_crops_farmer ON unused_crops(farmer_email);
        CREATE INDEX IF NOT EXISTS idx_sales_crop ON crop_sales(crop_id);
        CREATE INDEX IF NOT EXISTS idx_bids_crop ON active_bids(crop_id);
        """
    )
    conn.commit()
    conn.close()


# ── AUTH HELPER ────────────────────────────────────────────────────────

def current_farmer_email() -> Optional[str]:
    """
    Resolves the logged-in farmer's email from the caller's bearer token
    by asking the gateway (which owns the real session store in
    auth_excel.py). Returns None if there's no/invalid token — routes
    decide for themselves whether that's acceptable.
    """
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
            return (data.get("email") or data.get("user", {}).get("email"))
    except requests.exceptions.RequestException:
        pass

    return None


# ── SERIALIZATION ──────────────────────────────────────────────────────

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


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ── CROP LISTING ROUTES ────────────────────────────────────────────────

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
    return jsonify(serialize_crop(db, row)), 201


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


# ── BID ROUTES ──────────────────────────────────────────────────────────

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


# ── HEALTH ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "auction-backend", "db": str(DB_PATH)})


# ── MAIN ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CropAI auction backend")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    init_db()

    print("=" * 55)
    print(f"  AUCTION BACKEND — Running on http://127.0.0.1:{args.port}")
    print("=" * 55)
    app.run(host="127.0.0.1", port=args.port, debug=False)
else:
    # Ensure schema exists even if imported rather than run directly.
    init_db()