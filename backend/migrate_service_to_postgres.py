"""
migrate_sqlite_to_postgres.py
==============================
One-time data migration: copies rows from the old per-service sqlite files
into the shared Postgres database. Run this AFTER each service has been
started at least once against Postgres (so init_db() has created the
tables), and BEFORE you start relying on Postgres as the source of truth.

Safe to re-run: every insert uses ON CONFLICT DO NOTHING, so re-running
after a partial run (or after the app has since added new rows) won't
duplicate or clobber anything.

Usage:
    DATABASE_URL=postgresql://user:pass@host/cropai python migrate_sqlite_to_postgres.py \
        --chatbot-db chatbot.db \
        --credit-score-db credit_score.db \
        --auction-service-db auction_service.db \
        --auction-db auction.db \
        --yield-platform-db yield_platform_service.db \
        --cold-storage-db cold_storage.db

Deliberately NOT covered here: yield_lands.db. That file belongs to an
older, now-unused data source and is not part of the Postgres migration —
credit_score_backend.py's cross-read of it stays sqlite-only until/unless
that source is revived.

Any --*-db flag can be omitted to skip that migration.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Reuse the real shared.db module so we talk to Postgres exactly the way
# the services do (same connection/env handling). This script expects to
# be run with backend/ on PYTHONPATH, or placed inside backend/ itself.
try:
    from shared.db import connect as pg_connect
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from shared.db import connect as pg_connect


def sqlite_rows(db_path: str, table: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"SELECT * FROM {table}").fetchall()
    con.close()
    return [dict(r) for r in rows]


def migrate_table(pg_conn, sqlite_db: str, table: str, columns: list[str], conflict_col: str):
    """Generic copy: same column names/order in both sqlite and Postgres."""
    rows = sqlite_rows(sqlite_db, table)
    if not rows:
        print(f"  {table}: 0 rows in source, nothing to do")
        return
    col_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_col}) DO NOTHING"
    )
    inserted = 0
    for row in rows:
        values = [row.get(c) for c in columns]
        cur = pg_conn.execute(sql, values)
        if getattr(cur, "rowcount", 0) and cur.rowcount > 0:
            inserted += 1
    pg_conn.commit()
    print(f"  {table}: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")


def migrate_chatbot(pg_conn, db_path: str):
    print(f"\n== advisory (chatbot.db -> messages) ==")
    migrate_table(
        pg_conn, db_path, "messages",
        columns=["user_id", "content", "updated_at"],
        conflict_col="user_id",
    )


def migrate_credit_score(pg_conn, db_path: str):
    print(f"\n== credit-score (credit_score.db -> farmer_credit_records) ==")
    # id is SERIAL in Postgres — let it auto-assign rather than carrying over
    # the old sqlite integer id, since farmer_id is the real unique key.
    migrate_table(
        pg_conn, db_path, "farmer_credit_records",
        columns=[
            "farmer_id", "user_email", "farmer_name", "state", "district",
            "land_acres", "crop", "past_loan_amount", "past_yield_quintals",
            "repayment_status", "created_at",
        ],
        conflict_col="farmer_id",
    )


def migrate_auction_service(pg_conn, db_path: str):
    print(f"\n== auction-engine (auction_service.db -> auctions/bids/invitations) ==")
    # Parent table first so bids/invitations' foreign keys resolve.
    migrate_table(
        pg_conn, db_path, "auctions",
        columns=[
            "id", "owner_id", "item_label", "category", "metadata",
            "target_quantity", "remaining_quantity", "base_price",
            "owner_price", "counter_gap", "auction_type", "duration_minutes",
            "extension_minutes", "starts_at", "ends_at", "status", "created_at",
        ],
        conflict_col="id",
    )
    migrate_table(
        pg_conn, db_path, "bids",
        columns=[
            "id", "auction_id", "bidder_id", "price", "quantity",
            "accepted_quantity", "status", "created_at",
        ],
        conflict_col="id",
    )
    migrate_table(
        pg_conn, db_path, "invitations",
        columns=[
            "id", "auction_id", "invitee_id", "status", "created_at", "responded_at",
        ],
        conflict_col="id",
    )


def migrate_parcels(pg_conn, db_path: str):
    """parcels has no natural unique key once we drop the old sqlite integer
    id (id is SERIAL in Postgres now, same reasoning as farmer_credit_records
    — no other table has a foreign key into parcels.id here, so renumbering
    is safe). ON CONFLICT can't target anything without a unique constraint,
    so dedupe in Python instead: skip a row if one with the same owner_id +
    label + created_at already exists in Postgres (created_at is an ISO
    timestamp string set at insert time, effectively unique per row)."""
    rows = sqlite_rows(db_path, "parcels")
    if not rows:
        print(f"  parcels: 0 rows in source, nothing to do")
        return
    inserted = 0
    for row in rows:
        existing = pg_conn.execute(
            "SELECT 1 FROM parcels WHERE owner_id = ? AND label = ? AND created_at = ?",
            (row["owner_id"], row["label"], row["created_at"]),
        ).fetchone()
        if existing:
            continue
        pg_conn.execute(
            """
            INSERT INTO parcels (owner_id, label, latitude, longitude, area_hectare,
                                  bounds_json, metadata_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                row["owner_id"], row["label"], row["latitude"], row["longitude"],
                row["area_hectare"], row["bounds_json"], row["metadata_json"],
                row["created_at"], row["updated_at"],
            ),
        )
        inserted += 1
    pg_conn.commit()
    print(f"  parcels: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")


def migrate_yield_platform(pg_conn, db_path: str):
    print(f"\n== yield-platform (yield_platform_service.db -> parcels/soilgrids_cache) ==")
    migrate_parcels(pg_conn, db_path)
    migrate_table(
        pg_conn, db_path, "soilgrids_cache",
        columns=["lat", "lon", "fetched_at", "probs_json"],
        conflict_col="lat, lon",
    )


def migrate_auction_backend(pg_conn, db_path: str):
    print(f"\n== auction_backend (auction.db -> crop-specific tables) ==")
    migrate_table(
        pg_conn, db_path, "unused_crops",
        columns=[
            "id", "farmer_email", "crop_type", "state", "district",
            "total_production", "sold_production", "created_at",
        ],
        conflict_col="id",
    )
    migrate_table(
        pg_conn, db_path, "crop_sales",
        columns=[
            "id", "crop_id", "bid_id", "buyer", "quantity",
            "price_per_tonne", "sold_at",
        ],
        conflict_col="id",
    )
    migrate_table(
        pg_conn, db_path, "active_bids",
        columns=[
            "id", "crop_id", "buyer", "price_per_tonne", "quantity",
            "status", "ends_at",
        ],
        conflict_col="id",
    )
    migrate_table(
        pg_conn, db_path, "auction_crop_index",
        columns=["auction_id", "crop_type", "state", "district", "mandi_email", "created_at"],
        conflict_col="auction_id",
    )
    migrate_table(
        pg_conn, db_path, "invitations_seen",
        columns=["auction_id", "invitee_id", "created_at"],
        conflict_col="auction_id, invitee_id",
    )
    migrate_table(
        pg_conn, db_path, "mandi_senders",
        columns=["mandi_email", "brevo_sender_id", "status", "requested_at", "verified_at"],
        conflict_col="mandi_email",
    )


def migrate_cold_storage(pg_conn, db_path: str):
    print(f"\n== cold-storage (cold_storage.db -> cold_storages + 4 dependent tables) ==")

    # 1. cold_storages — parent table, id is SERIAL now (was INTEGER
    # AUTOINCREMENT), so we drop the old sqlite id and let Postgres assign
    # fresh ones. Every child table below references cold_storage_id/
    # facility_id by that old id, so we build an old_id -> new_id map here
    # and use it to remap the children's foreign keys before inserting them
    # — otherwise the children would point at the wrong (or nonexistent)
    # facility once ids are renumbered.
    old_to_new_facility_id: dict[int, int] = {}
    rows = sqlite_rows(db_path, "cold_storages")
    inserted = 0
    for row in rows:
        # Same natural key sync_real_facility_data() already uses for its
        # own idempotency check (name + state + district) — use it here too
        # so re-running this migration after the app has synced its own
        # bundled facility list doesn't create duplicates.
        existing = pg_conn.execute(
            "SELECT id FROM cold_storages WHERE lower(name)=lower(?) AND lower(state)=lower(?) AND lower(district)=lower(?)",
            (row["name"], row["state"], row["district"]),
        ).fetchone()
        if existing:
            old_to_new_facility_id[row["id"]] = existing["id"]
            continue
        cur = pg_conn.execute(
            """
            INSERT INTO cold_storages (
                nhb_id, name, state, district, block, village, latitude, longitude,
                total_capacity_mt, storage_type, source, source_year, data_quality,
                last_updated, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            RETURNING id
            """,
            (
                row["nhb_id"], row["name"], row["state"], row["district"],
                row["block"], row["village"], row["latitude"], row["longitude"],
                row["total_capacity_mt"], row["storage_type"], row["source"],
                row["source_year"], row["data_quality"], row["last_updated"],
                row["created_at"],
            ),
        )
        new_id = cur.fetchone()["id"]
        pg_conn.commit()
        old_to_new_facility_id[row["id"]] = new_id
        inserted += 1
    print(f"  cold_storages: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")

    # 2. cold_storage_crop_capacity — child of cold_storages, has a real
    # UNIQUE(cold_storage_id, crop) constraint, so ON CONFLICT works once
    # cold_storage_id is remapped to the new facility id.
    rows = sqlite_rows(db_path, "cold_storage_crop_capacity")
    inserted = 0
    for row in rows:
        new_facility_id = old_to_new_facility_id.get(row["cold_storage_id"])
        if new_facility_id is None:
            print(f"  cold_storage_crop_capacity: skipping row {row['id']} — "
                  f"its cold_storage_id {row['cold_storage_id']} wasn't migrated above")
            continue
        cur = pg_conn.execute(
            "INSERT INTO cold_storage_crop_capacity (cold_storage_id, crop, capacity_mt) "
            "VALUES (?,?,?) ON CONFLICT (cold_storage_id, crop) DO NOTHING",
            (new_facility_id, row["crop"], row["capacity_mt"]),
        )
        if cur.rowcount > 0:
            inserted += 1
    pg_conn.commit()
    print(f"  cold_storage_crop_capacity: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")

    # 3. storage_snapshots — child of cold_storages, no natural unique key.
    # Dedupe in Python on (new cold_storage_id, recorded_at, recorded_by),
    # same approach as parcels in migrate_yield_platform above.
    rows = sqlite_rows(db_path, "storage_snapshots")
    inserted = 0
    for row in rows:
        new_facility_id = old_to_new_facility_id.get(row["cold_storage_id"])
        if new_facility_id is None:
            print(f"  storage_snapshots: skipping row {row['id']} — "
                  f"its cold_storage_id {row['cold_storage_id']} wasn't migrated above")
            continue
        existing = pg_conn.execute(
            "SELECT 1 FROM storage_snapshots WHERE cold_storage_id = ? AND recorded_at = ? AND recorded_by IS NOT DISTINCT FROM ?",
            (new_facility_id, row["recorded_at"], row["recorded_by"]),
        ).fetchone()
        if existing:
            continue
        pg_conn.execute(
            """
            INSERT INTO storage_snapshots (
                cold_storage_id, occupied_mt, reserved_mt, committed_mt,
                expected_release_mt, data_quality, recorded_at, recorded_by
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                new_facility_id, row["occupied_mt"], row["reserved_mt"],
                row["committed_mt"], row["expected_release_mt"],
                row["data_quality"], row["recorded_at"], row["recorded_by"],
            ),
        )
        inserted += 1
    pg_conn.commit()
    print(f"  storage_snapshots: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")

    # 4. storage_provider_configs — child of cold_storages, has a real
    # UNIQUE(facility_id, provider_email) constraint, so ON CONFLICT works
    # once facility_id is remapped.
    rows = sqlite_rows(db_path, "storage_provider_configs")
    inserted = 0
    for row in rows:
        new_facility_id = old_to_new_facility_id.get(row["facility_id"])
        if new_facility_id is None:
            print(f"  storage_provider_configs: skipping row {row['id']} — "
                  f"its facility_id {row['facility_id']} wasn't migrated above")
            continue
        cur = pg_conn.execute(
            """
            INSERT INTO storage_provider_configs
                (facility_id, provider_email, free_capacity_mt, is_active, notes, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT (facility_id, provider_email) DO NOTHING
            """,
            (
                new_facility_id, row["provider_email"], row["free_capacity_mt"],
                row["is_active"], row["notes"], row["updated_at"],
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    pg_conn.commit()
    print(f"  storage_provider_configs: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")

    # 5. farmer_crop_storage_plans — independent, no FK into cold_storages.
    # No natural unique key besides the old id (SERIAL now), so dedupe on
    # (user_email, crop, state, district, created_at) — created_at is set
    # at insert time and effectively unique per row, same reasoning as
    # parcels/soilgrids.
    rows = sqlite_rows(db_path, "farmer_crop_storage_plans")
    inserted = 0
    for row in rows:
        existing = pg_conn.execute(
            "SELECT 1 FROM farmer_crop_storage_plans WHERE user_email = ? AND crop = ? "
            "AND state = ? AND district = ? AND created_at = ?",
            (row["user_email"], row["crop"], row["state"], row["district"], row["created_at"]),
        ).fetchone()
        if existing:
            continue
        pg_conn.execute(
            """
            INSERT INTO farmer_crop_storage_plans (
                user_email, state, district, crop, area, area_unit, sowing_date,
                expected_harvest_date, expected_production_mt, storage_required_mt,
                status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["user_email"], row["state"], row["district"], row["crop"],
                row["area"], row["area_unit"], row["sowing_date"],
                row["expected_harvest_date"], row["expected_production_mt"],
                row["storage_required_mt"], row["status"], row["created_at"],
                row["updated_at"],
            ),
        )
        inserted += 1
    pg_conn.commit()
    print(f"  farmer_crop_storage_plans: {len(rows)} rows in source, {inserted} newly inserted "
          f"({len(rows) - inserted} already present / skipped)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chatbot-db")
    parser.add_argument("--credit-score-db")
    parser.add_argument("--auction-service-db")
    parser.add_argument("--auction-db")
    parser.add_argument("--yield-platform-db")
    parser.add_argument("--cold-storage-db")
    args = parser.parse_args()

    if not any([
        args.chatbot_db, args.credit_score_db, args.auction_service_db,
        args.auction_db, args.yield_platform_db, args.cold_storage_db,
    ]):
        parser.error("pass at least one --*-db flag")

    pg_conn = pg_connect()

    if args.chatbot_db:
        migrate_chatbot(pg_conn, args.chatbot_db)
    if args.credit_score_db:
        migrate_credit_score(pg_conn, args.credit_score_db)
    if args.auction_service_db:
        migrate_auction_service(pg_conn, args.auction_service_db)
    if args.auction_db:
        migrate_auction_backend(pg_conn, args.auction_db)
    if args.yield_platform_db:
        migrate_yield_platform(pg_conn, args.yield_platform_db)
    if args.cold_storage_db:
        migrate_cold_storage(pg_conn, args.cold_storage_db)

    print("\nDone.")


if __name__ == "__main__":
    main()