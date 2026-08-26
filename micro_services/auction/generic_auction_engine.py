"""
generic_auction_engine.py
==========================
A standalone, domain-agnostic auction/bidding engine, generalized from a
crop-marketplace auction backend. Contains NO references to farmers, crops,
mandis, districts, states, or any other domain-specific concept — those all
become plain strings/metadata the caller supplies.

Supports two auction directions:
    "forward" — seller wants the HIGHEST price (classic English auction).
                 e.g. selling goods, freelance gig to the highest bidder.
    "reverse" — buyer wants the LOWEST price (procurement / reverse auction).
                 e.g. sourcing a supply contract at the lowest cost.

Design goals
------------
* No web framework, no auth system, no email provider baked in. This is a
  pure engine: give it a DB connection and item/bid data, get back auction
  state and pricing decisions. Wire it into Flask/FastAPI/Django, whatever
  auth you use, and whatever notification channel you want, from the
  outside.
* Single SQLite schema (swap out `sqlite3` calls for another driver if you
  need Postgres/MySQL — the SQL is intentionally simple/portable).
* Deterministic, side-effect-light functions: nothing here sends emails,
  calls a gateway, or assumes a particular user model.

Usage sketch
------------
    import sqlite3
    from generic_auction_engine import AuctionEngine

    conn = sqlite3.connect("auctions.db")
    conn.row_factory = sqlite3.Row
    engine = AuctionEngine(conn)
    engine.init_db()

    auction_id = engine.create_auction(
        seller_id="seller_123",          # whoever owns/protects this auction
        item_label="Widget batch #42",   # free-text description
        category="widgets",              # optional grouping/matching field
        target_quantity=100,
        base_price=50.0,
        auction_type="forward",
        duration_minutes=60,
        counter_gap=1.0,
    )

    engine.place_bid(auction_id, bidder_id="buyer_9", price=51.0, quantity=10)
    state = engine.get_auction(auction_id)
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Optional


# ── ID / TIME HELPERS ────────────────────────────────────────────────────

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ── ENGINE ────────────────────────────────────────────────────────────────

class AuctionEngine:
    """
    A reusable bidding engine over a SQLite connection.

    The connection's row_factory should be set to sqlite3.Row by the caller
    (the engine relies on name-based column access).
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── SCHEMA ──────────────────────────────────────────────────────────

    def init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS auctions (
                id                  TEXT PRIMARY KEY,
                owner_id            TEXT NOT NULL,   -- who this auction protects
                item_label          TEXT NOT NULL,   -- free-text item description
                category            TEXT,             -- optional matching/grouping key
                metadata            TEXT,             -- optional JSON blob for domain extras
                target_quantity     REAL NOT NULL,
                remaining_quantity  REAL NOT NULL,
                base_price          REAL NOT NULL,
                owner_price         REAL,             -- owner's latest counter-offer, if any
                counter_gap         REAL DEFAULT 0,   -- required margin per new bid/counter
                auction_type        TEXT NOT NULL CHECK (auction_type IN ('forward','reverse')),
                duration_minutes    INTEGER NOT NULL,
                extension_minutes   INTEGER DEFAULT 0,
                starts_at           INTEGER NOT NULL,
                ends_at             INTEGER NOT NULL,
                status              TEXT NOT NULL DEFAULT 'scheduled'
                                    CHECK (status IN ('scheduled','active','closed')),
                created_at          INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bids (
                id                  TEXT PRIMARY KEY,
                auction_id          TEXT NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
                bidder_id           TEXT NOT NULL,
                price               REAL NOT NULL,
                quantity            REAL NOT NULL,
                accepted_quantity   REAL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending','accepted','rejected','expired')),
                created_at          INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invitations (
                id                  TEXT PRIMARY KEY,
                auction_id          TEXT NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
                invitee_id          TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending','accepted','declined')),
                created_at          INTEGER NOT NULL,
                responded_at        INTEGER,
                UNIQUE(auction_id, invitee_id)
            );

            CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids(auction_id);
            CREATE INDEX IF NOT EXISTS idx_invitations_invitee ON invitations(invitee_id, status);
            """
        )
        self.conn.commit()

    # ── AUCTION LIFECYCLE ──────────────────────────────────────────────

    def create_auction(
        self,
        owner_id: str,
        item_label: str,
        target_quantity: float,
        base_price: float,
        auction_type: str,
        duration_minutes: int,
        category: Optional[str] = None,
        metadata: Optional[str] = None,
        counter_gap: float = 0,
        extension_minutes: int = 0,
        starts_at: Optional[int] = None,
    ) -> str:
        if auction_type not in ("forward", "reverse"):
            raise ValueError("auction_type must be 'forward' or 'reverse'")

        auction_id = new_id("auc")
        starts = starts_at if starts_at is not None else now_ms()
        ends = starts + duration_minutes * 60_000

        self.conn.execute(
            """
            INSERT INTO auctions (
                id, owner_id, item_label, category, metadata,
                target_quantity, remaining_quantity, base_price, owner_price,
                counter_gap, auction_type, duration_minutes, extension_minutes,
                starts_at, ends_at, status, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scheduled',?)
            """,
            (
                auction_id, owner_id, item_label, category, metadata,
                target_quantity, target_quantity, base_price, None,
                counter_gap, auction_type, duration_minutes, extension_minutes,
                starts, ends, now_ms(),
            ),
        )
        self.conn.commit()
        return auction_id

    def _sync_status(self, row: sqlite3.Row) -> sqlite3.Row:
        """
        Lazily advances an auction's status against the clock. Call this
        before reading or mutating an auction — there's no background
        scheduler, so every access path syncs status first.
        """
        if row["status"] == "scheduled" and now_ms() >= row["starts_at"]:
            self.conn.execute("UPDATE auctions SET status='active' WHERE id=?", (row["id"],))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM auctions WHERE id=?", (row["id"],)).fetchone()

        if row["status"] == "active" and now_ms() >= row["ends_at"]:
            self.conn.execute("UPDATE auctions SET status='closed' WHERE id=?", (row["id"],))
            self.conn.execute(
                "UPDATE bids SET status='expired' WHERE auction_id=? AND status='pending'",
                (row["id"],),
            )
            self.conn.execute(
                "UPDATE invitations SET status='declined', responded_at=? "
                "WHERE auction_id=? AND status='pending'",
                (now_ms(), row["id"]),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM auctions WHERE id=?", (row["id"],)).fetchone()
        return row

    def get_auction(self, auction_id: str, include_bids: bool = True) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
        if row is None:
            return None
        row = self._sync_status(row)
        return self._serialize_auction(row, include_bids=include_bids)

    # ── PRICING ─────────────────────────────────────────────────────────

    def _leading_price(self, row: sqlite3.Row) -> float:
        """
        The price the auction currently sits at: the owner's own counter-
        offer (or base price if none yet), compared against the best live
        bid. Forward auctions take the max (favor higher price); reverse
        auctions take the min (favor lower price).
        """
        baseline = row["owner_price"] if row["owner_price"] is not None else row["base_price"]
        order = "DESC" if row["auction_type"] == "forward" else "ASC"
        best = self.conn.execute(
            f"""
            SELECT price FROM bids
            WHERE auction_id=? AND status IN ('pending','accepted')
            ORDER BY price {order} LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        if best is None:
            return baseline
        if row["auction_type"] == "forward":
            return max(baseline, best["price"])
        return min(baseline, best["price"])

    def required_next_price(self, auction_id: str) -> Optional[float]:
        """The exact price a new bid or counter-offer must clear right now."""
        row = self.conn.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
        if row is None:
            return None
        row = self._sync_status(row)
        leading = self._leading_price(row)
        gap = row["counter_gap"] or 0
        return leading + gap if row["auction_type"] == "forward" else leading - gap

    def set_owner_price(self, auction_id: str, price: float) -> dict:
        """
        Sets the auction owner's counter-offer. The rule is generic:
        forward auctions move upward, reverse auctions move downward.
        The new price must improve on the current leading price by the
        configured counter gap.
        """
        row = self.conn.execute(
            "SELECT * FROM auctions WHERE id=?", (auction_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Auction not found.")

        row = self._sync_status(row)

        if row["status"] == "scheduled":
            raise ValueError("This auction hasn't started yet.")
        if row["status"] != "active":
            raise ValueError("This auction has closed.")

        price = float(price)
        if price <= 0:
            raise ValueError("price must be greater than 0.")

        leading = self._leading_price(row)
        gap = float(row["counter_gap"] or 0)

        if row["auction_type"] == "forward":
            required = leading + gap
            if gap > 0:
                if price < required:
                    raise ValueError(
                        f"Forward auction — counter-offer must be at least {required}."
                    )
            elif price <= leading:
                raise ValueError(
                    f"Forward auction — counter-offer must be higher than {leading}."
                )
        else:
            required = leading - gap
            if gap > 0:
                if price > required:
                    raise ValueError(
                        f"Reverse auction — counter-offer must be at most {required}."
                    )
            elif price >= leading:
                raise ValueError(
                    f"Reverse auction — counter-offer must be lower than {leading}."
                )

        self.conn.execute(
            "UPDATE auctions SET owner_price=? WHERE id=?",
            (price, auction_id),
        )
        self.conn.commit()
        return self.get_auction(auction_id)

    @staticmethod
    def _bid_sort_key(auction_type: str):
        """Best bid first."""
        if auction_type == "forward":
            return lambda b: (-b["price"], b["createdAt"])
        return lambda b: (b["price"], b["createdAt"])

    # ── BIDDING ─────────────────────────────────────────────────────────

    def place_bid(
        self, auction_id: str, bidder_id: str, price: float, quantity: float,
        enforce_pricing: bool = True,
    ) -> dict:
        """
        Places a bid. Raises ValueError if the auction isn't active, or if
        enforce_pricing is True and the bid doesn't clear the required
        next price.
        """
        row = self.conn.execute("SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
        if row is None:
            raise ValueError("auction not found")
        row = self._sync_status(row)
        if row["status"] != "active":
            raise ValueError(f"auction is not active (status={row['status']})")

        if enforce_pricing:
            required = self.required_next_price(auction_id)
            if row["auction_type"] == "forward" and price < required:
                raise ValueError(f"bid must be >= {required}")
            if row["auction_type"] == "reverse" and price > required:
                raise ValueError(f"bid must be <= {required}")

        bid_id = new_id("bid")
        self.conn.execute(
            "INSERT INTO bids (id, auction_id, bidder_id, price, quantity, status, created_at) "
            "VALUES (?,?,?,?,?,'pending',?)",
            (bid_id, auction_id, bidder_id, price, quantity, now_ms()),
        )
        self.conn.commit()
        return {"id": bid_id, "auctionId": auction_id, "bidderId": bidder_id,
                "price": price, "quantity": quantity, "status": "pending"}

    def accept_bid(self, bid_id: str, accepted_quantity: Optional[float] = None) -> dict:
        """
        Marks a bid accepted and reduces the auction's remaining_quantity.
        If accepted_quantity meets/exceeds remaining_quantity, the auction
        is closed early (target met).
        """
        bid = self.conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
        if bid is None:
            raise ValueError("bid not found")
        qty = accepted_quantity if accepted_quantity is not None else bid["quantity"]

        self.conn.execute(
            "UPDATE bids SET status='accepted', accepted_quantity=? WHERE id=?",
            (qty, bid_id),
        )
        auction = self.conn.execute(
            "SELECT * FROM auctions WHERE id=?", (bid["auction_id"],)
        ).fetchone()
        remaining = max(0, auction["remaining_quantity"] - qty)
        self.conn.execute(
            "UPDATE auctions SET remaining_quantity=? WHERE id=?",
            (remaining, auction["id"]),
        )
        if remaining <= 0:
            self.conn.execute(
                "UPDATE auctions SET status='closed' WHERE id=?", (auction["id"],)
            )
        self.conn.commit()
        return {"bidId": bid_id, "acceptedQuantity": qty, "remainingQuantity": remaining}

    def reject_bid(self, bid_id: str) -> None:
        self.conn.execute("UPDATE bids SET status='rejected' WHERE id=?", (bid_id,))
        self.conn.commit()

    # ── INVITATIONS ──────────────────────────────────────────────────────

    def invite(self, auction_id: str, invitee_id: str) -> str:
        invite_id = new_id("inv")
        self.conn.execute(
            "INSERT OR IGNORE INTO invitations (id, auction_id, invitee_id, status, created_at) "
            "VALUES (?,?,?,'pending',?)",
            (invite_id, auction_id, invitee_id, now_ms()),
        )
        self.conn.commit()
        return invite_id

    def respond_to_invitation(self, invitation_id: str, accept: bool) -> None:
        self.conn.execute(
            "UPDATE invitations SET status=?, responded_at=? WHERE id=?",
            ("accepted" if accept else "declined", now_ms(), invitation_id),
        )
        self.conn.commit()

    # ── MATCHING (generic replacement for crop/state/district lookup) ───

    def find_candidates_by_category(self, category: str, source_table: str,
                                     id_column: str, category_column: str) -> list[str]:
        """
        Generic stand-in for "who should be notified about a new auction
        in this category". Point it at whatever table in your own app
        holds prior participant activity (e.g. a `listings` or `orders`
        table) and it returns distinct participant ids matching the
        category. This keeps the engine ignorant of your actual domain
        schema while still supporting the same notification pattern the
        original crop-auction service used.
        """
        query = (
            f"SELECT DISTINCT {id_column} FROM {source_table} "
            f"WHERE {category_column} = ?"
        )
        rows = self.conn.execute(query, (category,)).fetchall()
        return [r[id_column] for r in rows if r[id_column]]

    # ── SERIALIZATION ─────────────────────────────────────────────────

    def _serialize_bid(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "auctionId": row["auction_id"],
            "bidderId": row["bidder_id"],
            "price": row["price"],
            "quantity": row["quantity"],
            "acceptedQuantity": row["accepted_quantity"],
            "status": row["status"],
            "createdAt": row["created_at"],
        }

    def _serialize_auction(self, row: sqlite3.Row, include_bids: bool = True) -> dict:
        out = {
            "id": row["id"],
            "ownerId": row["owner_id"],
            "itemLabel": row["item_label"],
            "category": row["category"],
            "metadata": row["metadata"],
            "targetQuantity": row["target_quantity"],
            "remainingQuantity": row["remaining_quantity"],
            "basePrice": row["base_price"],
            "ownerPrice": row["owner_price"],
            "counterGap": row["counter_gap"],
            "auctionType": row["auction_type"],
            "durationMinutes": row["duration_minutes"],
            "extensionMinutes": row["extension_minutes"],
            "startsAt": row["starts_at"],
            "endsAt": row["ends_at"],
            "status": row["status"],
            "createdAt": row["created_at"],
        }

        bid_rows = self.conn.execute(
            "SELECT * FROM bids WHERE auction_id=? ORDER BY created_at ASC", (row["id"],)
        ).fetchall()

        out["leadingPrice"] = self._leading_price(row) if row["status"] == "active" else None
        out["requiredBidPrice"] = self.required_next_price(row["id"]) if row["status"] == "active" else None

        if row["status"] == "closed":
            out["outcome"] = "sold_out" if any(b["status"] == "accepted" for b in bid_rows) else "not_sold_out"
        else:
            out["outcome"] = None

        if include_bids:
            bids = [self._serialize_bid(b) for b in bid_rows]
            bids.sort(key=self._bid_sort_key(row["auction_type"]))
            out["bids"] = bids
            out["bidCount"] = len(bids)

        return out


# ── SELF-TEST / DEMO ────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    engine = AuctionEngine(conn)
    engine.init_db()

    # Forward auction: seller wants the HIGHEST price.
    aid = engine.create_auction(
        owner_id="seller_1",
        item_label="Vintage guitar",
        target_quantity=1,
        base_price=100.0,
        auction_type="forward",
        duration_minutes=60,
        counter_gap=5.0,
        category="instruments",
    )
    print("required first bid:", engine.required_next_price(aid))  # 105.0

    engine.place_bid(aid, bidder_id="buyer_a", price=105.0, quantity=1)
    print("required next bid:", engine.required_next_price(aid))  # 110.0

    engine.place_bid(aid, bidder_id="buyer_b", price=120.0, quantity=1)
    state = engine.get_auction(aid)
    print("leading price:", state["leadingPrice"])
    print("bids:", state["bids"])

    # Accept the winning bid, closing out the target quantity.
    winning_bid_id = state["bids"][0]["id"]
    print(engine.accept_bid(winning_bid_id))
    print("final status:", engine.get_auction(aid)["status"])