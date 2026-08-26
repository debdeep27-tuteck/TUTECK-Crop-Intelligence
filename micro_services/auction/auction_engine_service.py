"""
auction_engine_service.py
==========================
A fully standalone, domain-agnostic AUCTION MICROSERVICE.

Unlike auction_backend_v2.py (which imports AuctionEngine directly into a
crop-specific Flask process), this file IS the service: its own process,
its own port, its own database, its own HTTP API. Any other app — crop
platform, real estate app, freelance marketplace, procurement tool,
whatever — talks to it purely over HTTP. Nothing about "crops" exists
anywhere in this file.

Run it:
    python auction_engine_service.py --port 6000

Callers integrate by making plain HTTP requests — see the bottom of this
file for example client calls, and AUCTION_SERVICE.md (companion doc) for
the full API reference.

Auth model
----------
This service does NOT know about your users, sessions, or login system —
that's the whole point of keeping it generic. It only asks: does this
request carry a valid API key? Beyond that, `owner_id` / `bidder_id` are
just opaque strings the caller passes in — resolving *who* that string
maps to (a farmer's email, a freelancer's UUID, a company's account id)
is entirely the calling app's job, same as it always was for the
in-process version.

    Authorization: Bearer <AUCTION_SERVICE_API_KEY>

Set AUCTION_SERVICE_API_KEY in the environment. If it's unset, auth is
skipped (fine for local dev — never do this in production).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS

from generic_auction_engine import AuctionEngine

# ── CONFIG ────────────────────────────────────────────────────────────────

DEFAULT_PORT = 6000
DB_PATH = Path(__file__).resolve().parent / "auction_service.db"
API_KEY = os.environ.get("AUCTION_SERVICE_API_KEY", "")

app = Flask(__name__)
CORS(app)


# ── DB / ENGINE (one shared connection is fine for SQLite + Flask dev
#    server; swap for a pooled connection or Postgres in production) ──────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn = None
_engine = None


def get_engine() -> AuctionEngine:
    global _conn, _engine
    if _engine is None:
        _conn = get_conn()
        _engine = AuctionEngine(_conn)
        _engine.init_db()
    return _engine


# ── AUTH ──────────────────────────────────────────────────────────────────

def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return fn(*args, **kwargs)  # auth disabled — local dev only
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {API_KEY}":
            return jsonify({"error": "invalid or missing API key"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ── ROUTES ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "auction_engine_service"})


@app.route("/auctions", methods=["POST"])
@require_api_key
def create_auction():
    body = request.get_json(force=True) or {}
    required = ["ownerId", "itemLabel", "targetQuantity", "basePrice",
                "auctionType", "durationMinutes"]
    missing = [f for f in required if body.get(f) in (None, "")]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    try:
        auction_id = get_engine().create_auction(
            owner_id=body["ownerId"],
            item_label=body["itemLabel"],
            category=body.get("category"),
            metadata=body.get("metadata"),  # caller passes a JSON string, opaque to us
            target_quantity=float(body["targetQuantity"]),
            base_price=float(body["basePrice"]),
            auction_type=body["auctionType"],
            duration_minutes=int(body["durationMinutes"]),
            counter_gap=float(body.get("counterGap", 0)),
            extension_minutes=int(body.get("extensionMinutes", 0)),
            starts_at=int(body["startsAt"]) if body.get("startsAt") is not None else None,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(get_engine().get_auction(auction_id)), 201


@app.route("/auctions/<auction_id>", methods=["GET"])
@require_api_key
def get_auction(auction_id):
    auction = get_engine().get_auction(auction_id)
    if auction is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(auction)


@app.route("/auctions", methods=["GET"])
@require_api_key
def list_auctions():
    """Optional filters: ownerId, category, status."""
    conn = get_engine().conn
    filters, params = [], []
    if request.args.get("ownerId"):
        filters.append("owner_id = ?")
        params.append(request.args["ownerId"])
    if request.args.get("category"):
        filters.append("category = ?")
        params.append(request.args["category"])
    if request.args.get("status"):
        filters.append("status = ?")
        params.append(request.args["status"])
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = conn.execute(
        f"SELECT id FROM auctions {where} ORDER BY created_at DESC", params
    ).fetchall()
    auctions = [get_engine().get_auction(r["id"], include_bids=True) for r in rows]
    return jsonify({"auctions": auctions})


@app.route("/auctions/<auction_id>/price", methods=["PATCH"])
@require_api_key
def set_auction_price(auction_id):
    body = request.get_json(force=True) or {}
    price = body.get("price")

    if price is None:
        return jsonify({"error": "price is required"}), 400

    try:
        auction = get_engine().set_owner_price(auction_id, float(price))
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(auction)


@app.route("/auctions/<auction_id>/bids", methods=["POST"])
@require_api_key
def place_bid(auction_id):
    body = request.get_json(force=True) or {}
    bidder_id = body.get("bidderId")
    price = body.get("price")
    quantity = body.get("quantity")
    if not bidder_id or price is None or quantity is None:
        return jsonify({"error": "bidderId, price and quantity are required"}), 400

    try:
        bid = get_engine().place_bid(auction_id, bidder_id, float(price), float(quantity))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(bid), 201


@app.route("/auctions/<auction_id>/required-price", methods=["GET"])
@require_api_key
def required_price(auction_id):
    price = get_engine().required_next_price(auction_id)
    if price is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"auctionId": auction_id, "requiredPrice": price})


@app.route("/bids/<bid_id>", methods=["PATCH"])
@require_api_key
def resolve_bid(bid_id):
    body = request.get_json(force=True) or {}
    action = body.get("action")  # "accept" | "reject"
    if action == "accept":
        try:
            result = get_engine().accept_bid(bid_id, accepted_quantity=body.get("acceptedQuantity"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(result)
    elif action == "reject":
        get_engine().reject_bid(bid_id)
        return jsonify({"bidId": bid_id, "status": "rejected"})
    return jsonify({"error": "action must be 'accept' or 'reject'"}), 400


@app.route("/auctions/<auction_id>/invitations", methods=["POST"])
@require_api_key
def invite(auction_id):
    body = request.get_json(force=True) or {}
    invitee_id = body.get("inviteeId")
    if not invitee_id:
        return jsonify({"error": "inviteeId required"}), 400
    invitation_id = get_engine().invite(auction_id, invitee_id)
    return jsonify({"id": invitation_id, "auctionId": auction_id, "inviteeId": invitee_id}), 201


@app.route("/invitations/<invitation_id>", methods=["PATCH"])
@require_api_key
def respond_invitation(invitation_id):
    body = request.get_json(force=True) or {}
    accept = bool(body.get("accept"))
    get_engine().respond_to_invitation(invitation_id, accept)
    return jsonify({"id": invitation_id, "status": "accepted" if accept else "declined"})


@app.route("/invitations", methods=["GET"])
@require_api_key
def list_invitations():
    invitee_id = request.args.get("inviteeId")
    if not invitee_id:
        return jsonify({"error": "inviteeId query param required"}), 400
    conn = get_engine().conn
    status = request.args.get("status")
    if status and status != "all":
        rows = conn.execute(
            "SELECT * FROM invitations WHERE invitee_id = ? AND status = ? ORDER BY created_at DESC",
            (invitee_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM invitations WHERE invitee_id = ? ORDER BY created_at DESC",
            (invitee_id,),
        ).fetchall()
    return jsonify({"invitations": [
        {"id": r["id"], "auctionId": r["auction_id"], "inviteeId": r["invitee_id"],
         "status": r["status"], "createdAt": r["created_at"], "respondedAt": r["responded_at"]}
        for r in rows
    ]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    get_engine()  # ensures DB/tables exist before serving
    app.run(host="0.0.0.0", port=args.port, debug=False)