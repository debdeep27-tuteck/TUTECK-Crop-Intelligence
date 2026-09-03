import os
import json
import sqlite3
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "credit_score.db"

# The Yield Detect page (port 6008, yield_platform_service.py) is the live
# service the frontend actually talks to — it lives in a sibling folder
# under micro_services/, not in the old top-level backend/ directory.
# (backend/yield_lands.db turned out to be a stale, unused earlier version
# of this feature — see chat history for how that was diagnosed.)
YIELD_LANDS_DB = (BASE_DIR / "../yield-detect/yield_platform_service.db").resolve()


# ── DATABASE SETUP ─────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create farmer_credit_records table and seed from yield_lands.db."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS farmer_credit_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                farmer_id TEXT UNIQUE NOT NULL,
                user_email TEXT,
                farmer_name TEXT NOT NULL,
                state TEXT DEFAULT 'tripura',
                district TEXT DEFAULT '',
                land_acres REAL NOT NULL DEFAULT 1.0,
                crop TEXT DEFAULT 'Paddy',
                past_loan_amount REAL NOT NULL DEFAULT 0.0,
                past_yield_quintals REAL NOT NULL DEFAULT 0.0,
                repayment_status TEXT DEFAULT 'Repaid on time',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pick up any farmers registered in yield_lands.db that aren't in our table yet
        sync_new_farmers_from_yield_lands(conn)


def sync_new_farmers_from_yield_lands(conn):
    """
    Mirror farmer_credit_records to whatever is currently in
    yield_platform_service.db (the real, live Yield Detect database — see
    parcels table: owner_id=email, label=plot name, area_hectare, and
    metadata_json holding {state, district, crop, predicted_yield, ...}):
      - add a credit record (with dummy loan/yield data) for any farmer who
        has registered land but has no credit record yet
      - remove credit records for farmers who no longer have any land
        registered (stale/test rows, deleted plots, etc.)
    Called on every request that lists/reads farmers (cheap for this table size),
    so the credit score page always reflects what's actually registered —
    no restart needed, no leftover "ghost" farmers.

    Parcels with no owner_id attached are skipped entirely: we have no
    reliable way to turn an anonymous plot into a farmer identity, so we
    don't invent one. There's no separate farmer-name field in this table,
    so the display name is derived from the email (e.g. "farmer3@gmail.com"
    -> "Farmer3").
    """
    if not YIELD_LANDS_DB.exists():
        return

    try:
        with sqlite3.connect(str(YIELD_LANDS_DB)) as y_conn:
            y_conn.row_factory = sqlite3.Row
            parcels = y_conn.execute("SELECT * FROM parcels").fetchall()

            # Group parcels by owner_id (a farmer can register more than one plot).
            # Skip any parcel with no owner_id — we can't attach it to a real farmer.
            farmer_lands = {}
            for p in parcels:
                email = p["owner_id"]
                if not email:
                    continue  # anonymous/test plot with no owner — don't fabricate a farmer

                try:
                    meta = json.loads(p["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    meta = {}

                if email not in farmer_lands:
                    owner_name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
                    farmer_lands[email] = {
                        "email": email,
                        "name": owner_name,
                        "state": meta.get("state") or "rajasthan",
                        "district": meta.get("district") or "Ajmer",
                        "crop": meta.get("crop") or "Arhar/Tur",
                        "acres": 0.0,
                        "yield_quintals": 0.0
                    }

                # Convert hectares to acres (1 ha = 2.471 acres)
                ha = float(p["area_hectare"] or 1.0)
                farmer_lands[email]["acres"] += round(ha * 2.471, 2)

                # predicted_yield in metadata_json is a RATE in tonnes/hectare
                # (e.g. 0.505), not a total — multiply by this plot's area and
                # by 10 (1 tonne = 10 quintals) to get this plot's total yield.
                yield_rate_t_per_ha = float(meta.get("predicted_yield") or 0.0)
                farmer_lands[email]["yield_quintals"] += round(yield_rate_t_per_ha * ha * 10, 1)

            current_emails = {e.lower() for e in farmer_lands.keys()}

            # ── Remove stale records: farmers with no parcel registered anymore ──
            existing_rows = conn.execute("SELECT farmer_id, user_email FROM farmer_credit_records").fetchall()
            for r in existing_rows:
                email_lower = (r["user_email"] or "").lower()
                if not email_lower or email_lower not in current_emails:
                    conn.execute("DELETE FROM farmer_credit_records WHERE farmer_id = ?", (r["farmer_id"],))

            # Which emails do we already have a credit record for (after pruning above)?
            existing_emails = {
                (r["user_email"] or "").lower()
                for r in conn.execute("SELECT user_email FROM farmer_credit_records").fetchall()
            }

            # Next free farmer_id (F001, F002, ...)
            existing_ids = [
                r["farmer_id"] for r in conn.execute("SELECT farmer_id FROM farmer_credit_records").fetchall()
            ]
            max_num = 0
            for fid in existing_ids:
                if fid.startswith("F") and fid[1:].isdigit():
                    max_num = max(max_num, int(fid[1:]))
            next_idx = max_num + 1

            loan_samples = [50000, 30000, 70000, 20000, 100000]
            repayment_samples = ["Repaid on time", "Repaid on time", "Repaid on time", "1 late payment"]

            for email, info in farmer_lands.items():
                if email.lower() in existing_emails:
                    continue  # already tracked, don't touch their real record

                fid = f"F{next_idx:03d}"
                acres = max(1.0, round(info["acres"], 2))

                # Dummy past loan & yield data for a newly-registered farmer with no history yet
                loan_amt = loan_samples[(next_idx - 1) % len(loan_samples)]
                # Base the dummy yield loosely on their predicted yield so it isn't wildly off,
                # falling back to a reasonable default if predicted_yield is missing/zero.
                yield_qtl = max(5.0, round(info["yield_quintals"], 1)) if info["yield_quintals"] > 0 else 15.0
                repayment = repayment_samples[(next_idx - 1) % len(repayment_samples)]

                conn.execute("""
                    INSERT INTO farmer_credit_records
                    (farmer_id, user_email, farmer_name, state, district, land_acres, crop, past_loan_amount, past_yield_quintals, repayment_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    fid, email, info["name"], info["state"].title(), info["district"].title(),
                    acres, info["crop"], loan_amt, yield_qtl, repayment
                ))

                existing_emails.add(email.lower())
                next_idx += 1

            conn.commit()
    except Exception as e:
        print("Notice: Error syncing yield_platform_service.db:", e)


# ── SIMPLE CREDIT SCORE CALCULATION ────────────────────────────────────────────

def calculate_credit_score(land_acres, past_loan_amount, past_yield_quintals):
    """
    Calculate credit score (0-100) based on 3 simple factors:
      1. Land (max 30 pts): Higher land holding -> more collateral capacity
      2. Loan (max 20 pts): Having taken and repaid past loan -> good credit history
      3. Yield (max 50 pts): Higher annual crop yield -> strong agricultural repayment capacity
    """
    land_acres = max(0.1, float(land_acres or 1.0))
    past_loan = max(0.0, float(past_loan_amount or 0.0))
    past_yield = max(0.0, float(past_yield_quintals or 0.0))

    # Land: up to 10 acres -> max 30 pts
    land_score = min(land_acres / 10.0, 1.0) * 30.0

    # Past loan: 20 pts if repaid on time, 10 if high debt, 0 if no prior loan
    if past_loan > 0:
        if past_loan > (land_acres * 50000):
            loan_score = 10.0
        else:
            loan_score = 20.0
    else:
        loan_score = 0.0

    # Past yield: up to 50 quintals -> max 50 pts
    yield_score = min(past_yield / 50.0, 1.0) * 50.0

    total_score = round(max(0.0, min(100.0, land_score + loan_score + yield_score)), 1)

    # Risk Grade & Loan Eligibility
    if total_score >= 80.0:
        grade = "AAA"
        risk = "Very Low Risk"
        multiplier = 1.5
        kcc = True
        rate = "4.0% (KCC Subsidized)"
    elif total_score >= 65.0:
        grade = "AA"
        risk = "Low Risk"
        multiplier = 1.2
        kcc = True
        rate = "4.0% (KCC Subsidized)"
    elif total_score >= 50.0:
        grade = "A"
        risk = "Moderate Risk"
        multiplier = 1.0
        kcc = True
        rate = "5.5% (Agri Priority)"
    elif total_score >= 35.0:
        grade = "BBB"
        risk = "Elevated Risk"
        multiplier = 0.7
        kcc = False
        rate = "7.5% (Commercial Agri)"
    else:
        grade = "C"
        risk = "High Risk"
        multiplier = 0.4
        kcc = False
        rate = "9.0% (Risk Adjusted)"

    max_loan = round(((land_acres * 50000 + past_yield * 5000) * multiplier) / 5000) * 5000
    max_loan = max(25000, min(2000000, max_loan))

    return {
        "credit_score": total_score,
        "rating_grade": grade,
        "risk_level": risk,
        "kcc_eligible": kcc,
        "interest_rate": rate,
        "max_loan_limit": max_loan,
        "default_probability": round((100.0 - total_score) * 0.25, 1),
        "land_asset_value": round(land_acres * 350000),
        "breakdown": {
            "land_score": round(land_score, 1),
            "land_max": 30,
            "loan_score": round(loan_score, 1),
            "loan_max": 20,
            "yield_score": round(yield_score, 1),
            "yield_max": 50
        }
    }


# ── ROLE-BASED SCOPING HELPER ──────────────────────────────────────────────────

def _scope_filter():
    """
    Optional ?state=&district= query params, set by gateway.py (never by the
    browser directly) when the caller is a state_admin/district_admin, so the
    farmer list/stats returned here are already narrowed to that admin's
    territory. Empty when unscoped (admin/analyst/logged-out).
    """
    state = (request.args.get("state") or "").strip().lower()
    district = (request.args.get("district") or "").strip().lower()
    return state, district


def _in_scope(rec, scope_state, scope_district):
    if scope_state and (rec.get("state") or "").strip().lower() != scope_state:
        return False
    if scope_district and (rec.get("district") or "").strip().lower() != scope_district:
        return False
    return True


# ── API ROUTES ─────────────────────────────────────────────────────────────────

@app.route('/credit_score/<query>')
def get_credit_score(query):
    """
    Get credit score for a farmer by farmer_id (e.g. F001) or user_email (e.g. farmer1@gmail.com).
    """
    q = str(query).strip()
    with get_db() as conn:
        sync_new_farmers_from_yield_lands(conn)

        # 1. Exact match on farmer_id or user_email
        row = conn.execute("""
            SELECT * FROM farmer_credit_records
            WHERE LOWER(farmer_id) = LOWER(?) OR LOWER(user_email) = LOWER(?)
        """, (q, q)).fetchone()

        # 2. Partial match if not found
        if not row:
            row = conn.execute("""
                SELECT * FROM farmer_credit_records
                WHERE LOWER(farmer_name) LIKE LOWER(?) OR LOWER(user_email) LIKE LOWER(?)
            """, (f"%{q}%", f"%{q}%")).fetchone()

        if not row:
            return jsonify({
                "error": "Farmer not found in credit database",
                "searched": query
            }), 404

        rec = dict(row)
        assessment = calculate_credit_score(
            rec["land_acres"],
            rec["past_loan_amount"],
            rec["past_yield_quintals"]
        )

        return jsonify({
            # Original backward-compatible keys
            "farmer_id": rec["farmer_id"],
            "land_acres": rec["land_acres"],
            "past_loan_amount": rec["past_loan_amount"],
            "past_yield_quintals": rec["past_yield_quintals"],
            "credit_score": assessment["credit_score"],
            # Profile keys
            "farmer_name": rec["farmer_name"],
            "email": rec["user_email"],
            "state": rec["state"],
            "district": rec["district"],
            "primary_crop": rec["crop"],
            "repayment_status": rec["repayment_status"],
            # Assessment details
            "rating_grade": assessment["rating_grade"],
            "risk_level": assessment["risk_level"],
            "kcc_eligible": assessment["kcc_eligible"],
            "interest_rate": assessment["interest_rate"],
            "max_loan_limit": assessment["max_loan_limit"],
            "default_probability": assessment["default_probability"],
            "land_asset_value": assessment["land_asset_value"],
            "breakdown": assessment["breakdown"]
        })


@app.route('/farmers')
def list_farmers():
    """Returns list of all registered farmers from credit_score.db with their scores."""
    with get_db() as conn:
        sync_new_farmers_from_yield_lands(conn)
        scope_state, scope_district = _scope_filter()

        rows = conn.execute("SELECT * FROM farmer_credit_records ORDER BY id ASC").fetchall()
        result = []
        for r in rows:
            rec = dict(r)
            if not _in_scope(rec, scope_state, scope_district):
                continue
            assessment = calculate_credit_score(
                rec["land_acres"],
                rec["past_loan_amount"],
                rec["past_yield_quintals"]
            )
            result.append({
                "farmer_id": rec["farmer_id"],
                "name": rec["farmer_name"],
                "email": rec["user_email"],
                "state": rec["state"],
                "district": rec["district"],
                "land_acres": rec["land_acres"],
                "primary_crop": rec["crop"],
                "past_loan_amount": rec["past_loan_amount"],
                "past_yield_quintals": rec["past_yield_quintals"],
                "credit_score": assessment["credit_score"],
                "rating_grade": assessment["rating_grade"],
                "risk_level": assessment["risk_level"],
                "max_loan_limit": assessment["max_loan_limit"]
            })
        return jsonify({"farmers": result, "count": len(result)})


@app.route('/calculate_score', methods=['POST'])
def calculate_custom_score():
    """Dynamic calculation for what-if scenarios."""
    data = request.get_json(silent=True) or {}
    land = float(data.get("land_acres", 5.0))
    loan = float(data.get("past_loan_amount", 50000))
    yld = float(data.get("past_yield_quintals", 20.0))

    assessment = calculate_credit_score(land, loan, yld)
    return jsonify(assessment)


@app.route('/stats')
def get_stats():
    """Summary statistics for the evaluated portfolio."""
    with get_db() as conn:
        sync_new_farmers_from_yield_lands(conn)
        scope_state, scope_district = _scope_filter()

        rows = [r for r in conn.execute("SELECT * FROM farmer_credit_records").fetchall()
                if _in_scope(dict(r), scope_state, scope_district)]
        scores = []
        total_loan_pool = 0
        kcc_count = 0

        for r in rows:
            rec = dict(r)
            assessment = calculate_credit_score(
                rec["land_acres"],
                rec["past_loan_amount"],
                rec["past_yield_quintals"]
            )
            scores.append(assessment["credit_score"])
            total_loan_pool += assessment["max_loan_limit"]
            if assessment["kcc_eligible"]:
                kcc_count += 1

        avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
        kcc_ratio = round((kcc_count / max(1, len(rows))) * 100, 1)

        return jsonify({
            "total_farmers": len(rows),
            "average_credit_score": avg_score,
            "total_approved_credit_pool": total_loan_pool,
            "kcc_eligible_ratio": kcc_ratio
        })


@app.route('/health')
def health():
    return jsonify({"status": "ok", "service": "credit-score", "port": 6014, "database": "credit_score.db"})


# ── INITIALIZE ON MODULE LOAD ──────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6014, debug=True)