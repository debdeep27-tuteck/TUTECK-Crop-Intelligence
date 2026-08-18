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
import smtplib

try:
    from dotenv import load_dotenv
    # Loads variables from a .env file (in the current working directory,
    # or nearest parent) into os.environ, without overriding any that are
    # already set for real in the environment. Safe no-op if no .env exists.
    load_dotenv()
except ImportError:
    # python-dotenv not installed — fall back to real environment variables
    # only. Run `pip install python-dotenv` to enable .env file support.
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

# ── CONFIG ─────────────────────────────────────────────────────────────

DEFAULT_PORT = 5009
DB_PATH = Path(__file__).resolve().parent / "auction.db"

GATEWAY_INTERNAL_URL = os.environ.get("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8085")

# SMTP config for mandi auction-start notification emails. All optional —
# if SMTP_HOST isn't set, sending is skipped gracefully (logged, not fatal),
# the same "not configured, degrade quietly" pattern used for the Mappls
# tile key in the yield editor.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "no-reply@cropai.local")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

# Brevo API key (separate from the SMTP key above) — used only to register
# a mandi's email as a verified sender so auction emails can go out with
# that mandi's real address in "From". Get this under Settings > SMTP & API
# > API Keys in Brevo (NOT the SMTP tab, that key is for SMTP_PASS above).
# If unset, mandi emails always fall back to the Reply-To approach instead.
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDERS_URL = "https://api.brevo.com/v3/senders"

# App-facing URL to link back to the farmer auction floor from the email.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8085")

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

        -- ── Mandi-run auctions (buyer posts a requirement, farmers bid) ──

        CREATE TABLE IF NOT EXISTS mandi_auctions (
            id                  TEXT PRIMARY KEY,
            mandi_email         TEXT NOT NULL,
            auction_name        TEXT NOT NULL,
            crop_type           TEXT NOT NULL,
            state               TEXT NOT NULL,
            district            TEXT NOT NULL,
            target_quantity     REAL NOT NULL CHECK (target_quantity > 0),
            remaining_quantity  REAL NOT NULL,
            base_price          REAL NOT NULL,
            auction_type        TEXT NOT NULL CHECK (auction_type IN ('forward', 'reverse')),
            duration_minutes    INTEGER NOT NULL,
            extension_minutes   INTEGER NOT NULL DEFAULT 5,
            starts_at           INTEGER NOT NULL,
            ends_at             INTEGER NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active',  -- active | closed
            notified_count      INTEGER NOT NULL DEFAULT 0,
            created_at          INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mandi_bids (
            id                  TEXT PRIMARY KEY,
            auction_id          TEXT NOT NULL REFERENCES mandi_auctions(id) ON DELETE CASCADE,
            farmer_email        TEXT NOT NULL,
            price_per_tonne     REAL NOT NULL,
            quantity            REAL NOT NULL CHECK (quantity > 0),
            accepted_quantity   REAL,
            status              TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected | expired
            created_at          INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mandi_auctions_mandi ON mandi_auctions(mandi_email);
        CREATE INDEX IF NOT EXISTS idx_mandi_auctions_match ON mandi_auctions(state, district, crop_type, status);
        CREATE INDEX IF NOT EXISTS idx_mandi_bids_auction ON mandi_bids(auction_id);
        CREATE INDEX IF NOT EXISTS idx_mandi_bids_farmer ON mandi_bids(farmer_email);

        -- ── Brevo sender verification, per mandi. A mandi's email can only
        -- be used as the "From" address on outgoing mail once Brevo has
        -- confirmed they own it (they click a link Brevo emails them).
        -- Until then, emails fall back to SMTP_FROM with Reply-To set to
        -- the mandi's address (see send_auction_started_email).

        CREATE TABLE IF NOT EXISTS mandi_senders (
            mandi_email    TEXT PRIMARY KEY,
            brevo_sender_id TEXT,
            status         TEXT NOT NULL DEFAULT 'pending',  -- pending | verified | failed
            requested_at   INTEGER NOT NULL,
            verified_at    INTEGER
        );

        -- ── Invitations: a matching farmer must accept before an auction
        -- shows up on their Live Auctions tab / before they can bid ──

        CREATE TABLE IF NOT EXISTS auction_invitations (
            id            TEXT PRIMARY KEY,
            auction_id    TEXT NOT NULL REFERENCES mandi_auctions(id) ON DELETE CASCADE,
            farmer_email  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | declined
            created_at    INTEGER NOT NULL,
            responded_at  INTEGER,
            UNIQUE(auction_id, farmer_email)
        );

        CREATE INDEX IF NOT EXISTS idx_invitations_auction ON auction_invitations(auction_id);
        CREATE INDEX IF NOT EXISTS idx_invitations_farmer ON auction_invitations(farmer_email, status);
        """
    )

    # Migration: mandi_bids.crop_id — a bid now always ties back to the
    # exact unused_crops listing it was placed with, since quantity is
    # derived from that listing's remaining production rather than typed
    # in by the farmer.
    try:
        conn.execute("ALTER TABLE mandi_bids ADD COLUMN crop_id TEXT REFERENCES unused_crops(id)")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: mandi_auctions.mandi_price — the mandi's own counter-offer,
    # set after the initial base price. When present it replaces base_price
    # as the reference farmers' next bids are measured against.
    try:
        conn.execute("ALTER TABLE mandi_auctions ADD COLUMN mandi_price REAL")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: mandi_auctions.counter_gap — a fixed ₹/tonne step the mandi
    # sets once, at auction creation. Farmers must bid at or beyond
    # (baseline - counter_gap) in a reverse auction, or (baseline +
    # counter_gap) in a forward auction, where baseline is the current
    # mandi_price (or base_price before any counter-offer has been made).
    # NULL/0 means no extra gap is enforced beyond the existing baseline
    # rule (equal-to-baseline still allowed once a mandi_price exists).
    try:
        conn.execute("ALTER TABLE mandi_auctions ADD COLUMN counter_gap REAL")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.commit()
    conn.close()


# ── AUTH HELPER ────────────────────────────────────────────────────────

def current_user() -> Optional[dict]:
    """
    Resolves the logged-in user's session info (email, role, state,
    district, ...) from the caller's bearer token by asking the gateway
    (which owns the real session store in auth_excel.py). Returns None if
    there's no/invalid token — routes decide for themselves whether that's
    acceptable.
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
            user = data.get("user") if isinstance(data.get("user"), dict) else data
            return user
    except requests.exceptions.RequestException:
        pass

    return None


def current_farmer_email() -> Optional[str]:
    user = current_user()
    if not user:
        return None
    return user.get("email")


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


# ── MANDI AUCTION SERIALIZATION ─────────────────────────────────────────

def _bid_sort_key(auction_type: str):
    """Best bid first: forward auctions favor the highest price (the farmer
    is being paid more), reverse auctions favor the lowest price (the mandi
    is paying less). Operates on already-serialized (camelCase) bid dicts."""
    if auction_type == "forward":
        return lambda b: (-b["pricePerTonne"], b["createdAt"])
    return lambda b: (b["pricePerTonne"], b["createdAt"])


def _leading_price(db: sqlite3.Connection, row: sqlite3.Row) -> float:
    """
    The price level the auction currently sits at — whichever is more
    favorable to the side the auction protects: the mandi's own counter-
    offer (or the original base price if it hasn't countered yet), versus
    the best live bid a farmer has already placed. Forward auctions favor
    higher prices so this is a max; reverse auctions favor lower prices so
    it's a min. Both the next farmer bid and the mandi's next counter-offer
    are measured against this same number.
    """
    baseline = row["mandi_price"] if row["mandi_price"] is not None else row["base_price"]
    best_row = db.execute(
        """
        SELECT price_per_tonne FROM mandi_bids
        WHERE auction_id = ? AND status IN ('pending', 'accepted')
        ORDER BY price_per_tonne {} LIMIT 1
        """.format("DESC" if row["auction_type"] == "forward" else "ASC"),
        (row["id"],),
    ).fetchone()
    if best_row is None:
        return baseline
    if row["auction_type"] == "forward":
        return max(baseline, best_row["price_per_tonne"])
    return min(baseline, best_row["price_per_tonne"])


def _pricing_requirement(db: sqlite3.Connection, row: sqlite3.Row) -> tuple[float, float]:
    """Returns (leading_price, counter_gap) — the reference point that both
    the next farmer bid and the mandi's next counter-offer must clear, and
    the fixed per-tonne margin (set once at auction creation) required on
    top of it. The gap always compounds on the auction's current leading
    price, not the original base price, so each new bid or counter-offer
    ratchets the price a further ₹counter_gap beyond whatever came before."""
    leading = _leading_price(db, row)
    gap = row["counter_gap"] if "counter_gap" in row.keys() and row["counter_gap"] else 0
    return leading, gap


def _required_next_price(db: sqlite3.Connection, row: sqlite3.Row) -> float:
    """The exact price a new farmer bid or mandi counter-offer must clear
    (>= for forward, <= for reverse) to be accepted right now."""
    leading, gap = _pricing_requirement(db, row)
    return leading + gap if row["auction_type"] == "forward" else leading - gap


def serialize_mandi_bid(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "auctionId": row["auction_id"],
        "farmerEmail": row["farmer_email"],
        "cropId": row["crop_id"] if "crop_id" in row.keys() else None,
        "pricePerTonne": row["price_per_tonne"],
        "quantity": row["quantity"],
        "acceptedQuantity": row["accepted_quantity"],
        "status": row["status"],
        "createdAt": row["created_at"],
    }


def serialize_invitation(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "auctionId": row["auction_id"],
        "farmerEmail": row["farmer_email"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "respondedAt": row["responded_at"],
    }


def serialize_auction(db: sqlite3.Connection, row: sqlite3.Row, include_bids: bool = True) -> dict:
    out = {
        "id": row["id"],
        "mandiEmail": row["mandi_email"],
        "auctionName": row["auction_name"],
        "cropType": row["crop_type"],
        "state": row["state"],
        "district": row["district"],
        "targetQuantity": row["target_quantity"],
        "remainingQuantity": row["remaining_quantity"],
        "basePrice": row["base_price"],
        "mandiPrice": row["mandi_price"] if "mandi_price" in row.keys() else None,
        "counterGap": row["counter_gap"] if "counter_gap" in row.keys() else None,
        "auctionType": row["auction_type"],
        "durationMinutes": row["duration_minutes"],
        "extensionMinutes": row["extension_minutes"],
        "startsAt": row["starts_at"],
        "endsAt": row["ends_at"],
        "status": row["status"],
        "notifiedCount": row["notified_count"],
        "createdAt": row["created_at"],
        "leadingPrice": _leading_price(db, row) if row["status"] == "active" else None,
        "requiredBidPrice": _required_next_price(db, row) if row["status"] == "active" else None,
    }

    bid_rows = db.execute(
        "SELECT * FROM mandi_bids WHERE auction_id = ? ORDER BY created_at ASC",
        (row["id"],),
    ).fetchall()

    # Outcome only means anything once the auction has actually closed —
    # while active there's nothing to report yet.
    if row["status"] == "closed":
        out["outcome"] = "sold_out" if any(b["status"] == "accepted" for b in bid_rows) else "not_sold_out"
    else:
        out["outcome"] = None

    if include_bids:
        bids = [serialize_mandi_bid(b) for b in bid_rows]
        bids.sort(key=_bid_sort_key(row["auction_type"]))
        out["bids"] = bids
        out["bidCount"] = len(bids)

    return out


def _auto_decline_pending_invitations(db: sqlite3.Connection, auction_id: str) -> None:
    """
    Once an auction closes — whether from the clock running out or the
    target being met early — any invitation still sitting unanswered is
    resolved to 'declined'. The farmer never acted on it, and there's no
    auction left for them to accept into.
    """
    db.execute(
        "UPDATE auction_invitations SET status = 'declined', responded_at = ? "
        "WHERE auction_id = ? AND status = 'pending'",
        (now_ms(), auction_id),
    )


def _maybe_close_auction(db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """
    Lazily syncs an auction's status against the clock (there's no
    background scheduler in this service, so every read/write path that
    touches an auction runs it through here first):

    - 'scheduled' -> 'active' once its start time actually arrives — a
      mandi picking a future start time must NOT make the auction live
      immediately, only once now reaches starts_at.
    - 'active' -> 'closed' once its end time has passed. Any bid still
      'pending' when the clock runs out is marked 'expired' — the
      auctioneer simply never acted on it, so nothing was sold for it.
    """
    if row["status"] == "scheduled" and now_ms() >= row["starts_at"]:
        db.execute("UPDATE mandi_auctions SET status = 'active' WHERE id = ?", (row["id"],))
        db.commit()
        row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (row["id"],)).fetchone()

    if row["status"] == "active" and now_ms() >= row["ends_at"]:
        db.execute("UPDATE mandi_auctions SET status = 'closed' WHERE id = ?", (row["id"],))
        db.execute(
            "UPDATE mandi_bids SET status = 'expired' WHERE auction_id = ? AND status = 'pending'",
            (row["id"],),
        )
        _auto_decline_pending_invitations(db, row["id"])
        db.commit()
        row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (row["id"],)).fetchone()
    return row


# ── EMAIL NOTIFICATION ───────────────────────────────────────────────────

def _matching_farmer_emails(db: sqlite3.Connection, state: str, district: str, crop_type: str) -> list[str]:
    """
    Farmers to notify when a new auction opens: anyone who has listed this
    crop in this state/district on the auction floor before (unused_crops
    is the closest thing this service has to a farmer directory).
    """
    rows = db.execute(
        """
        SELECT DISTINCT farmer_email FROM unused_crops
        WHERE state = ? AND district = ? AND crop_type = ?
        """,
        (state, district, crop_type),
    ).fetchall()
    return [r["farmer_email"] for r in rows if r["farmer_email"]]


def _brevo_headers() -> dict:
    return {"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"}


def ensure_mandi_sender_registered(db: sqlite3.Connection, mandi_email: str) -> None:
    """
    Registers mandi_email as a sender with Brevo if we haven't already
    requested it. Brevo sends that address a verification email on its own
    — we're just kicking that off, not completing it. Safe to call
    repeatedly; no-ops once a row already exists for this mandi. Never
    raises: if Brevo is unreachable or misconfigured, the mandi just stays
    "pending" and emails keep using the Reply-To fallback.
    """
    if not BREVO_API_KEY or not mandi_email:
        return

    existing = db.execute(
        "SELECT status FROM mandi_senders WHERE mandi_email = ?", (mandi_email,)
    ).fetchone()
    if existing is not None:
        return  # already requested (or verified/failed) — don't re-request

    try:
        resp = requests.post(
            BREVO_SENDERS_URL,
            headers=_brevo_headers(),
            json={"name": mandi_email.split("@")[0], "email": mandi_email},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            sender_id = str(resp.json().get("id", ""))
            db.execute(
                "INSERT INTO mandi_senders (mandi_email, brevo_sender_id, status, requested_at) "
                "VALUES (?, ?, 'pending', ?)",
                (mandi_email, sender_id, now_ms()),
            )
        else:
            print(f"[auction_backend] Brevo sender registration failed for {mandi_email}: "
                  f"{resp.status_code} {resp.text}")
            db.execute(
                "INSERT INTO mandi_senders (mandi_email, status, requested_at) VALUES (?, 'failed', ?)",
                (mandi_email, now_ms()),
            )
        db.commit()
    except requests.exceptions.RequestException as e:
        print(f"[auction_backend] Brevo sender registration errored for {mandi_email}: {e}")


def refresh_mandi_sender_status(db: sqlite3.Connection, mandi_email: str) -> str:
    """
    Polls Brevo for whether this mandi's sender has been verified yet
    (they've clicked the confirmation link Brevo emailed them) and updates
    our local row to match. Returns the current status: 'pending',
    'verified', or 'failed' (or 'unregistered' if we've never asked Brevo
    about this mandi at all). Safe to call often — e.g. right before
    sending an auction email — since it's just a status check.
    """
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
    """
    Sends the auction-open notification to each matching farmer. Returns
    how many sends were attempted. If SMTP isn't configured, this logs and
    returns 0 without raising — auction creation should never fail just
    because the mail server isn't set up in this environment.
    """
    if not farmer_emails:
        return 0

    if not SMTP_HOST:
        print(f"[auction_backend] SMTP not configured — skipping email to "
              f"{len(farmer_emails)} farmer(s) for auction '{auction['auctionName']}'.")
        return 0

    starts = time.strftime("%d %b %Y, %I:%M %p", time.localtime(auction["startsAt"] / 1000))
    ends = time.strftime("%d %b %Y, %I:%M %p", time.localtime(auction["endsAt"] / 1000))
    auction_type_label = "Forward auction (highest price wins)" if auction["auctionType"] == "forward" \
        else "Reverse auction (lowest price wins)"
    link = f"{APP_BASE_URL}/auction-farmer"

    # If Brevo has confirmed the mandi owns this address, send with their
    # real email as From. Otherwise fall back to the platform's verified
    # sender with Reply-To pointed at the mandi — Brevo will reject/rewrite
    # an unverified From, so this fallback keeps sends working regardless.
    mandi_email = auction.get("mandiEmail") or SMTP_FROM
    sender_status = refresh_mandi_sender_status(get_db(), mandi_email) if mandi_email != SMTP_FROM else "verified"
    use_mandi_as_from = sender_status == "verified"
    from_addr = mandi_email if use_mandi_as_from else SMTP_FROM

    subject = f"New auction open — {auction['cropType']} — {auction['auctionName']}"
    html = f"""
    <div style="font-family:Georgia,'Times New Roman',serif;max-width:560px;margin:0 auto;
                border:1px solid #e0ddd5;border-radius:10px;overflow:hidden;">
      <div style="background:#16281c;color:#ecf3ec;padding:22px 26px;">
        <div style="font-size:20px;letter-spacing:.02em;">CropAI Auction Floor</div>
        <div style="font-family:monospace;font-size:11px;text-transform:uppercase;
                    letter-spacing:.12em;color:#9db89f;margin-top:4px;">New Auction Notification</div>
      </div>
      <div style="padding:24px 26px;color:#1a1a18;font-family:Arial,Helvetica,sans-serif;">
        <p style="font-size:15px;margin:0 0 14px;">
          A mandi buyer has opened a new auction for <b>{auction['cropType']}</b> in your area and
          you're invited to take part. Details are below — accept the invitation on the Auction
          Floor's <b>Invitations</b> tab to have it added to your Live Auctions, where you can then
          place a bid before it closes.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin:18px 0;">
          <tr><td style="padding:8px 0;color:#6b6b62;width:42%;">Auction name</td>
              <td style="padding:8px 0;font-weight:600;">{auction['auctionName']}</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Crop</td>
              <td style="padding:8px 0;font-weight:600;">{auction['cropType']}</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Location</td>
              <td style="padding:8px 0;">{auction['district']}, {auction['state'].title()}</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Target quantity</td>
              <td style="padding:8px 0;font-weight:600;">{auction['targetQuantity']} tonnes</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Base price</td>
              <td style="padding:8px 0;font-weight:600;">₹{auction['basePrice']} / tonne</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Auction type</td>
              <td style="padding:8px 0;">{auction_type_label}</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Opens</td>
              <td style="padding:8px 0;">{starts}</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Closes</td>
              <td style="padding:8px 0;">{ends}</td></tr>
          <tr style="border-top:1px solid #e0ddd5;"><td style="padding:8px 0;color:#6b6b62;">Extension window</td>
              <td style="padding:8px 0;">+{auction['extensionMinutes']} min on a late bid</td></tr>
        </table>
        <div style="text-align:center;margin:26px 0 10px;">
          <a href="{link}" style="background:#378c50;color:#fff;text-decoration:none;
             padding:12px 26px;border-radius:7px;font-size:14px;display:inline-block;">
             Review invitation &amp; respond
          </a>
        </div>
        <p style="font-size:12px;color:#8a8678;margin-top:22px;">
          You're receiving this because you've listed {auction['cropType']} in {auction['district']},
          {auction['state'].title()} on the CropAI Auction Floor. Accepting is required before you
          can bid — declining or ignoring it keeps this auction off your Live Auctions tab.
        </p>
        <p style="font-size:12px;color:#8a8678;margin-top:8px;">
          {"This email was sent by the mandi directly." if use_mandi_as_from else f"Replying to this email will reach the mandi ({mandi_email}) directly."}
        </p>
      </div>
    </div>
    """

    sent = 0
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)

        for email in farmer_emails:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_addr
            if not use_mandi_as_from:
                msg["Reply-To"] = mandi_email
            msg["To"] = email
            msg.attach(MIMEText(html, "html"))
            try:
                server.sendmail(SMTP_FROM, [email], msg.as_string())
                sent += 1
            except smtplib.SMTPException as e:
                print(f"[auction_backend] Failed to email {email}: {e}")

        server.quit()
    except (smtplib.SMTPException, OSError) as e:
        print(f"[auction_backend] SMTP connection failed, notifications skipped: {e}")
        return sent

    return sent


@app.route("/api/mandi/auctions/test-email", methods=["POST"])
def send_test_email():
    """
    Sends a single real test email through the exact SMTP config the app
    uses for auction notifications — lets a mandi confirm delivery (Gmail
    App Password, etc.) actually works before relying on it for a real
    auction. Unlike send_auction_started_email, this does NOT swallow
    errors — it reports the real SMTP failure back so misconfiguration is
    obvious instead of silently vanishing into a server log.
    """
    body = request.get_json(silent=True) or {}
    to_email = body.get("email")
    if not to_email:
        return jsonify({"error": "email is required."}), 400

    if not SMTP_HOST:
        return jsonify({
            "error": "SMTP isn't configured on the server yet — set SMTP_HOST, SMTP_USER, "
                     "SMTP_PASS (and SMTP_FROM) as environment variables and restart the backend."
        }), 400

    subject = "CropAI Auction Floor — test email"
    html = """
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:0 auto;
                border:1px solid #e0ddd5;border-radius:10px;padding:24px 26px;">
      <p style="font-size:15px;margin:0 0 10px;">This is a test email from the CropAI Auction Floor.</p>
      <p style="font-size:13px;color:#6b6b62;margin:0;">
        If you're reading this, your SMTP setup is working — auction-start
        notifications will be delivered the same way.
      </p>
    </div>
    """

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg.attach(MIMEText(html, "html"))
        server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        server.quit()
    except (smtplib.SMTPException, OSError) as e:
        return jsonify({"error": f"SMTP send failed: {e}"}), 502

    return jsonify({"sent": True, "to": to_email, "from": SMTP_FROM})


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
    serialized = serialize_crop(db, row)

    # A crop listing filed after a matching auction is already running used
    # to get no invitation at all — the match check only ran once, at the
    # moment the auction was created. Catch that here: any currently-active
    # auction for this exact crop/state/district gets this farmer a pending
    # invitation (and the same notification email) right now, so they can
    # still join it while it's running.
    active_auctions = db.execute(
        "SELECT * FROM mandi_auctions WHERE state = ? AND district = ? AND crop_type = ? AND status = 'active'",
        (state, district, crop_type),
    ).fetchall()
    newly_invited = []
    for auction_row in active_auctions:
        auction_row = _maybe_close_auction(db, auction_row)
        if auction_row["status"] != "active":
            continue
        existing = db.execute(
            "SELECT id FROM auction_invitations WHERE auction_id = ? AND farmer_email = ?",
            (auction_row["id"], farmer_email),
        ).fetchone()
        if existing:
            continue  # already invited (or already responded) — don't touch it
        db.execute(
            "INSERT INTO auction_invitations (id, auction_id, farmer_email, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (new_id("invite"), auction_row["id"], farmer_email, now_ms()),
        )
        newly_invited.append(serialize_auction(db, auction_row, include_bids=False))
    db.commit()

    for auction in newly_invited:
        send_auction_started_email(auction, [farmer_email])

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


# ── MANDI AUCTION ROUTES ─────────────────────────────────────────────────

@app.route("/api/mandi/auctions", methods=["POST"])
def create_auction():
    """
    Mandi starts a new auction. Notifies (by email) every farmer who has
    previously listed this crop in this state/district.
    """
    db = get_db()
    body = request.get_json(silent=True) or {}

    user = current_user()
    mandi_email = body.get("mandiEmail") or (user.get("email") if user else None)
    state = body.get("state") or (user.get("state") if user else None)
    district = body.get("district") or (user.get("district") if user else None)

    auction_name = (body.get("auctionName") or "").strip()
    crop_type = body.get("cropType")
    target_quantity = body.get("targetQuantity")
    base_price = body.get("basePrice")
    auction_type = body.get("auctionType")
    duration_minutes = body.get("durationMinutes")
    extension_minutes = body.get("extensionMinutes", 5)
    starts_at = body.get("startsAt")  # ms epoch; defaults to now
    counter_gap = body.get("counterGap")  # optional ₹/tonne step, set once at creation

    missing = [k for k, v in {
        "mandiEmail": mandi_email, "state": state, "district": district,
        "auctionName": auction_name, "cropType": crop_type,
        "targetQuantity": target_quantity, "basePrice": base_price,
        "auctionType": auction_type, "durationMinutes": duration_minutes,
    }.items() if not v]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}."}), 400

    if auction_type not in ("forward", "reverse"):
        return jsonify({"error": "auctionType must be 'forward' or 'reverse'."}), 400

    try:
        target_quantity = float(target_quantity)
        base_price = float(base_price)
        duration_minutes = int(duration_minutes)
        extension_minutes = int(extension_minutes) if extension_minutes not in (None, "") else 5
        counter_gap = float(counter_gap) if counter_gap not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "targetQuantity, basePrice, durationMinutes, extensionMinutes and counterGap must be numbers."}), 400

    if target_quantity <= 0 or base_price <= 0 or duration_minutes <= 0:
        return jsonify({"error": "targetQuantity, basePrice and durationMinutes must be greater than 0."}), 400
    if extension_minutes < 0:
        extension_minutes = 0
    if counter_gap is not None and counter_gap < 0:
        return jsonify({"error": "counterGap can't be negative."}), 400

    created_at = now_ms()
    starts_at = int(starts_at) if starts_at else created_at
    ends_at = starts_at + duration_minutes * 60_000
    # A future start time means the auction isn't live yet — it sits as
    # 'scheduled' and _maybe_close_auction flips it to 'active' itself once
    # the clock actually reaches starts_at (same lazy-sync pattern already
    # used for closing). Bidding, "Live Auctions" visibility etc. all key
    # off status='active', so this alone is what stops an auction from
    # accepting bids before its scheduled start.
    initial_status = "scheduled" if starts_at > created_at else "active"

    auction_id = new_id("auct")
    db.execute(
        """
        INSERT INTO mandi_auctions
            (id, mandi_email, auction_name, crop_type, state, district,
             target_quantity, remaining_quantity, base_price, auction_type,
             duration_minutes, extension_minutes, starts_at, ends_at, status,
             notified_count, created_at, counter_gap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (auction_id, mandi_email, auction_name, crop_type, state, district,
         target_quantity, target_quantity, base_price, auction_type,
         duration_minutes, extension_minutes, starts_at, ends_at, initial_status, created_at,
         counter_gap),
    )
    db.commit()

    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    serialized = serialize_auction(db, row)

    farmer_emails = _matching_farmer_emails(db, state, district, crop_type)

    # Every matching farmer gets a pending invitation — this is what gates
    # the auction from their Live Auctions tab until they explicitly accept.
    for email in farmer_emails:
        db.execute(
            """
            INSERT INTO auction_invitations (id, auction_id, farmer_email, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(auction_id, farmer_email) DO NOTHING
            """,
            (new_id("invite"), auction_id, email, created_at),
        )
    db.commit()

    # Fire-and-forget: ask Brevo to start verifying this mandi's email as a
    # sender. No-ops if already requested, or if BREVO_API_KEY isn't set.
    # Until verified, emails keep using the Reply-To fallback — this never
    # blocks or delays auction creation.
    ensure_mandi_sender_registered(db, mandi_email)

    notified = send_auction_started_email(serialized, farmer_emails)
    if notified:
        db.execute("UPDATE mandi_auctions SET notified_count = ? WHERE id = ?", (notified, auction_id))
        db.commit()
        row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
        serialized = serialize_auction(db, row)

    serialized["matchedFarmerCount"] = len(farmer_emails)
    return jsonify(serialized), 201


@app.route("/api/mandi/auctions/invitations", methods=["GET"])
def list_invitations():
    """
    A farmer's invitations — defaults to their still-pending ones (what the
    Invitations tab shows), or pass status=accepted/declined/all for the
    others. farmerEmail is required; falls back to the session user.
    """
    db = get_db()
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()
    status = request.args.get("status", "pending")

    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400

    if status == "all":
        rows = db.execute(
            "SELECT * FROM auction_invitations WHERE farmer_email = ? ORDER BY created_at DESC",
            (farmer_email,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM auction_invitations WHERE farmer_email = ? AND status = ? ORDER BY created_at DESC",
            (farmer_email, status),
        ).fetchall()

    out = []
    for r in rows:
        auction_row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (r["auction_id"],)).fetchone()
        if not auction_row:
            continue
        auction_row = _maybe_close_auction(db, auction_row)
        item = serialize_invitation(r)
        item["auction"] = serialize_auction(db, auction_row, include_bids=True)
        out.append(item)

    return jsonify(out)


@app.route("/api/mandi/auctions/participated", methods=["GET"])
def list_participated_auctions():
    """
    Every auction a farmer actually joined (their invitation was accepted),
    active or closed — this is the farmer's full auction history, unlike
    /active which only returns ones still running and matching their
    current crop/state/district. Powers the Participated tab: for each
    auction the UI can show its live status while running, or once closed,
    whose bid (if anyone's) was accepted.
    """
    db = get_db()
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()
    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400

    rows = db.execute(
        """
        SELECT a.* FROM mandi_auctions a
        JOIN auction_invitations i ON i.auction_id = a.id
        WHERE i.farmer_email = ? AND i.status = 'accepted'
        ORDER BY a.created_at DESC
        """,
        (farmer_email,),
    ).fetchall()
    rows = [_maybe_close_auction(db, r) for r in rows]

    return jsonify([serialize_auction(db, r) for r in rows])


@app.route("/api/mandi/auctions/<auction_id>/invitations/respond", methods=["PATCH"])
def respond_to_invitation(auction_id):
    """
    Farmer accepts or declines their invitation to a given auction.
    Accepting is what makes the auction appear on their Live Auctions tab
    and is required before place_bid() will let them bid on it.
    """
    db = get_db()
    body = request.get_json(silent=True) or {}

    farmer_email = body.get("farmerEmail") or current_farmer_email()
    action = body.get("action")

    if not farmer_email:
        return jsonify({"error": "farmerEmail is required."}), 400
    if action not in ("accept", "decline"):
        return jsonify({"error": "action must be 'accept' or 'decline'."}), 400

    invite = db.execute(
        "SELECT * FROM auction_invitations WHERE auction_id = ? AND farmer_email = ?",
        (auction_id, farmer_email),
    ).fetchone()
    if not invite:
        return jsonify({"error": "No invitation found for this farmer on this auction."}), 404

    new_status = "accepted" if action == "accept" else "declined"
    db.execute(
        "UPDATE auction_invitations SET status = ?, responded_at = ? WHERE id = ?",
        (new_status, now_ms(), invite["id"]),
    )
    db.commit()

    row = db.execute("SELECT * FROM auction_invitations WHERE id = ?", (invite["id"],)).fetchone()
    return jsonify(serialize_invitation(row))


@app.route("/api/mandi/auctions", methods=["GET"])
def list_mandi_auctions():
    """Auctions started by a given mandi (their own sidebar/right-panel list)."""
    db = get_db()
    mandi_email = request.args.get("mandiEmail")
    user = current_user()
    if not mandi_email and user:
        mandi_email = user.get("email")

    if mandi_email:
        rows = db.execute(
            "SELECT * FROM mandi_auctions WHERE mandi_email = ? ORDER BY created_at DESC",
            (mandi_email,),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM mandi_auctions ORDER BY created_at DESC").fetchall()

    rows = [_maybe_close_auction(db, r) for r in rows]
    return jsonify([serialize_auction(db, r) for r in rows])


@app.route("/api/mandi/auctions/active", methods=["GET"])
def list_active_auctions_for_farmer():
    """
    Auctions a farmer should see on their Live Auctions tab: active,
    matching state + district + cropType, AND already accepted by that
    farmer's invitation. A pending or declined invitation keeps the
    auction off this list — it only shows up under /invitations until
    then. farmerEmail is required for that reason.
    """
    db = get_db()
    state = request.args.get("state")
    district = request.args.get("district")
    crop_type = request.args.get("cropType")
    farmer_email = request.args.get("farmerEmail") or current_farmer_email()

    if not all([state, district, crop_type, farmer_email]):
        return jsonify({"error": "state, district, cropType and farmerEmail are required."}), 400

    rows = db.execute(
        """
        SELECT a.* FROM mandi_auctions a
        JOIN auction_invitations i ON i.auction_id = a.id
        WHERE a.state = ? AND a.district = ? AND a.crop_type = ?
          AND i.farmer_email = ? AND i.status = 'accepted'
        ORDER BY a.created_at DESC
        """,
        (state, district, crop_type, farmer_email),
    ).fetchall()
    rows = [_maybe_close_auction(db, r) for r in rows]
    rows = [r for r in rows if r["status"] == "active"]
    return jsonify([serialize_auction(db, r) for r in rows])


@app.route("/api/mandi/auctions/<auction_id>", methods=["GET"])
def get_auction(auction_id):
    db = get_db()
    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    if not row:
        return jsonify({"error": "Auction not found."}), 404
    row = _maybe_close_auction(db, row)
    return jsonify(serialize_auction(db, row))


@app.route("/api/mandi/auctions/<auction_id>/bids", methods=["POST"])
def place_bid(auction_id):
    """
    A farmer places (or re-places — a farmer may hold several bids on the
    same auction at once, e.g. across price rounds) a bid on an active
    auction. Quantity is never typed in: it's derived from the farmer's own
    crop listing (cropId), specifically whatever production is still left
    unsold on that listing. Price must actually improve on the current
    best bid so the auction always moves in the direction that favors
    whichever side the auction type protects.
    """
    db = get_db()
    body = request.get_json(silent=True) or {}

    user = current_user()
    farmer_email = body.get("farmerEmail") or (user.get("email") if user else None)
    price_per_tonne = body.get("pricePerTonne")
    crop_id = body.get("cropId")

    if not all([farmer_email, price_per_tonne, crop_id]):
        return jsonify({"error": "farmerEmail, pricePerTonne and cropId are required."}), 400

    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    if not row:
        return jsonify({"error": "Auction not found."}), 404
    row = _maybe_close_auction(db, row)
    if row["status"] == "scheduled":
        return jsonify({"error": "This auction hasn't started yet."}), 400
    if row["status"] != "active":
        return jsonify({"error": "This auction has closed."}), 400

    invite = db.execute(
        "SELECT status FROM auction_invitations WHERE auction_id = ? AND farmer_email = ?",
        (auction_id, farmer_email),
    ).fetchone()
    if not invite or invite["status"] != "accepted":
        return jsonify({"error": "Accept this auction's invitation before placing a bid."}), 403

    crop = db.execute(
        "SELECT * FROM unused_crops WHERE id = ? AND farmer_email = ?", (crop_id, farmer_email)
    ).fetchone()
    if not crop:
        return jsonify({"error": "Crop listing not found."}), 404
    if crop["crop_type"] != row["crop_type"] or crop["state"] != row["state"] or crop["district"] != row["district"]:
        return jsonify({"error": "That crop listing doesn't match this auction's crop/state/district."}), 400

    quantity = round(crop["total_production"] - crop["sold_production"], 4)
    if quantity <= 0:
        return jsonify({"error": "This crop listing has no production left — add a new listing to bid again."}), 400

    try:
        price_per_tonne = float(price_per_tonne)
    except (TypeError, ValueError):
        return jsonify({"error": "pricePerTonne must be a number."}), 400

    if price_per_tonne <= 0:
        return jsonify({"error": "pricePerTonne must be greater than 0."}), 400
    if quantity > row["target_quantity"]:
        quantity = row["target_quantity"]

    # Price must move the auction in the right direction, and clear the
    # mandi's fixed counter_gap (set once at creation, ₹/tonne) on top of
    # wherever the auction currently stands — the best live bid a farmer
    # has already placed, or the mandi's last counter-offer, whichever is
    # more favorable to the mandi's side. This is what makes the gap
    # ratchet: base price ₹2000 with a ₹5 gap needs a first forward bid of
    # ≥ ₹2005; if a farmer then bids ₹2006, the next bid or counter-offer
    # needs ≥ ₹2011; if the mandi counters at ₹2012, the next bid needs
    # ≥ ₹2017 — each new price becomes the new floor the next one must
    # clear by the gap. Without a counter_gap, a new bid just has to
    # strictly beat the current leading price (no ties between farmers).
    leading_price, counter_gap = _pricing_requirement(db, row)

    if row["auction_type"] == "forward":
        if counter_gap > 0:
            required = leading_price + counter_gap
            if price_per_tonne < required:
                return jsonify({"error": f"Forward auction — your bid must be at least ₹{required}/tonne."}), 400
        elif price_per_tonne <= leading_price:
            return jsonify({"error": f"Forward auction — your bid must beat ₹{leading_price}/tonne."}), 400
    else:
        if counter_gap > 0:
            required = leading_price - counter_gap
            if price_per_tonne > required:
                return jsonify({"error": f"Reverse auction — your bid must be at most ₹{required}/tonne."}), 400
        elif price_per_tonne >= leading_price:
            return jsonify({"error": f"Reverse auction — your bid must beat ₹{leading_price}/tonne."}), 400

    # Anti-snipe: a bid arriving inside the extension window pushes the
    # close time back by extension_minutes.
    extension_ms = row["extension_minutes"] * 60_000
    new_ends_at = row["ends_at"]
    if extension_ms > 0 and (row["ends_at"] - now_ms()) <= extension_ms:
        new_ends_at = now_ms() + extension_ms
        db.execute("UPDATE mandi_auctions SET ends_at = ? WHERE id = ?", (new_ends_at, auction_id))

    bid_id = new_id("mbid")
    db.execute(
        """
        INSERT INTO mandi_bids (id, auction_id, farmer_email, crop_id, price_per_tonne, quantity, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (bid_id, auction_id, farmer_email, crop_id, price_per_tonne, quantity, now_ms()),
    )
    db.commit()

    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    return jsonify(serialize_auction(db, row)), 201


@app.route("/api/mandi/auctions/<auction_id>/price", methods=["PATCH"])
def set_mandi_price(auction_id):
    """
    The mandi places its own counter-offer on an already-running auction —
    e.g. a reverse auction opened at a base price of ₹2000/t, and the mandi
    now wants to push that down to ₹1800/t. From this point on, farmers'
    bids are measured against this new price instead of the original base
    price (equal to it is allowed, since it's the price the mandi just
    invited). The move has to tighten the auction in the same direction
    bids already move in — a forward auction's counter must go up, a
    reverse auction's must go down — and, like a farmer's bid, it has to
    clear the auction's current leading price (a live farmer bid is more
    binding than a stale base price) by at least the fixed counter_gap, so
    the ratchet keeps moving in one direction no matter which side raises
    the price last.
    """
    db = get_db()
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

    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    if not row:
        return jsonify({"error": "Auction not found."}), 404
    row = _maybe_close_auction(db, row)
    if row["status"] == "scheduled":
        return jsonify({"error": "This auction hasn't started yet."}), 400
    if row["status"] != "active":
        return jsonify({"error": "This auction has closed."}), 400

    leading_price, counter_gap = _pricing_requirement(db, row)
    if row["auction_type"] == "forward":
        if counter_gap > 0:
            required = leading_price + counter_gap
            if new_price < required:
                return jsonify({"error": f"Forward auction — your counter-offer must be at least ₹{required}/tonne."}), 400
        elif new_price <= leading_price:
            return jsonify({"error": f"Forward auction — your counter-offer must be higher than ₹{leading_price}/tonne."}), 400
    else:
        if counter_gap > 0:
            required = leading_price - counter_gap
            if new_price > required:
                return jsonify({"error": f"Reverse auction — your counter-offer must be at most ₹{required}/tonne."}), 400
        elif new_price >= leading_price:
            return jsonify({"error": f"Reverse auction — your counter-offer must be lower than ₹{leading_price}/tonne."}), 400

    db.execute("UPDATE mandi_auctions SET mandi_price = ? WHERE id = ?", (new_price, auction_id))
    db.commit()

    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    return jsonify(serialize_auction(db, row))


@app.route("/api/mandi/auctions/<auction_id>/bids/<bid_id>", methods=["PATCH"])
def decide_bid(auction_id, bid_id):
    """
    Mandi accepts or rejects a bid.

    Accepting only ever fills up to whatever quantity is still left on the
    target — if the auctioneer accepts a bid for more than remains, it's
    filled partially and the leftover (unfilled) amount is reported back so
    the UI can tell that farmer they still have that much free to bid
    elsewhere. Once remaining hits 0 the auction closes and every other
    still-pending bid is auto-rejected, since there's nothing left to buy.
    Rejecting just frees the farmer to submit a new bid while time remains.
    """
    db = get_db()
    body = request.get_json(silent=True) or {}
    action = body.get("action")

    if action not in ("accept", "reject"):
        return jsonify({"error": "action must be 'accept' or 'reject'."}), 400

    auction = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    if not auction:
        return jsonify({"error": "Auction not found."}), 404
    auction = _maybe_close_auction(db, auction)

    bid = db.execute("SELECT * FROM mandi_bids WHERE id = ? AND auction_id = ?", (bid_id, auction_id)).fetchone()
    if not bid:
        return jsonify({"error": "Bid not found."}), 404
    if bid["status"] != "pending":
        return jsonify({"error": f"This bid is already {bid['status']}."}), 400

    if action == "reject":
        db.execute("UPDATE mandi_bids SET status = 'rejected' WHERE id = ?", (bid_id,))
        db.commit()
        row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
        return jsonify(serialize_auction(db, row))

    # action == "accept"
    if auction["status"] != "active":
        return jsonify({"error": "This auction has already closed."}), 400

    remaining = auction["remaining_quantity"]
    if remaining <= 0:
        return jsonify({"error": "No quantity remaining on this auction's target."}), 400

    accepted_qty = min(bid["quantity"], remaining)
    leftover = round(bid["quantity"] - accepted_qty, 4)
    new_remaining = round(remaining - accepted_qty, 4)

    db.execute(
        "UPDATE mandi_bids SET status = 'accepted', accepted_quantity = ? WHERE id = ?",
        (accepted_qty, bid_id),
    )
    db.execute(
        "UPDATE mandi_auctions SET remaining_quantity = ? WHERE id = ?",
        (max(0.0, new_remaining), auction_id),
    )
    # The accepted quantity comes off the farmer's crop listing, same as any
    # other sale — leaves them whatever production is still unsold so they
    # can bid that leftover into another auction (or nothing, if it hits 0).
    if bid["crop_id"]:
        db.execute(
            "UPDATE unused_crops SET sold_production = sold_production + ? WHERE id = ?",
            (accepted_qty, bid["crop_id"]),
        )
        db.execute(
            """
            INSERT INTO crop_sales (id, crop_id, bid_id, buyer, quantity, price_per_tonne, sold_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("sale"), bid["crop_id"], bid_id, auction["mandi_email"], accepted_qty, bid["price_per_tonne"], now_ms()),
        )

    auction_closed = new_remaining <= 0
    if auction_closed:
        db.execute("UPDATE mandi_auctions SET status = 'closed' WHERE id = ?", (auction_id,))
        db.execute(
            "UPDATE mandi_bids SET status = 'auto_rejected' WHERE auction_id = ? AND status = 'pending'",
            (auction_id,),
        )
        _auto_decline_pending_invitations(db, auction_id)

    db.commit()

    row = db.execute("SELECT * FROM mandi_auctions WHERE id = ?", (auction_id,)).fetchone()
    result = serialize_auction(db, row)
    crop_remaining = None
    if bid["crop_id"]:
        crop_row = db.execute("SELECT * FROM unused_crops WHERE id = ?", (bid["crop_id"],)).fetchone()
        if crop_row:
            crop_remaining = round(crop_row["total_production"] - crop_row["sold_production"], 4)

    result["lastAccept"] = {
        "bidId": bid_id,
        "farmerEmail": bid["farmer_email"],
        "cropId": bid["crop_id"],
        "requestedQuantity": bid["quantity"],
        "acceptedQuantity": accepted_qty,
        "leftoverQuantity": leftover,
        "cropRemaining": crop_remaining,
        "auctionClosed": auction_closed,
    }
    return jsonify(result)


# ── HEALTH ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "auction-backend", "db": str(DB_PATH)})


@app.route("/api/server-time")
def server_time():
    """
    Just the server's current clock, in epoch ms. Countdown timers on
    auctions are computed against absolute timestamps (starts_at/ends_at)
    that were stamped using *this* server's clock — if that clock drifts
    from whatever machine is viewing the page, every countdown looks off
    by the same amount. The frontend hits this once at load (and
    periodically after) to work out that offset and correct for it,
    instead of trusting its own clock blindly.
    """
    return jsonify({"serverTime": now_ms()})


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