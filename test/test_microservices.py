#!/usr/bin/env python3
"""
test_services_accessibility.py
================================
Standalone driver script to verify that your microservices are reachable
and respond correctly to plain HTTP requests from OUTSIDE the app — i.e.
the way any external piece of software (another service, a mobile app,
curl, Postman, etc.) would hit them: nothing but HTTP.

It does NOT import any of your Flask app code. It only makes real HTTP
requests against whatever host/ports you point it at, which is exactly
what proves "accessible from outside."

USAGE
-----
    pip install requests

    # Test everything running on localhost, default ports:
    python test_services_accessibility.py

    # Test services running on a different host (e.g. a server's public
    # IP or domain — this is the real "external access" test):
    python test_services_accessibility.py --host 203.0.113.10

    # Test only specific services:
    python test_services_accessibility.py --only mandi_prices,auction_engine

    # Also probe one representative data endpoint per service (not just
    # /health), and check CORS headers are present for browser clients:
    python test_services_accessibility.py --full

    # Custom port overrides (name=port,name=port):
    python test_services_accessibility.py --ports mandi_prices=5011,disease=5004

Exit code is 0 if every tested service passed, 1 otherwise — so this can
be dropped straight into CI or a pre-deploy smoke test.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

# ── SERVICE REGISTRY ───────────────────────────────────────────────────
# One entry per microservice: default port, health path, and an optional
# extra "smoke" request (method, path, kwargs) that exercises a real
# endpoint beyond just /health. Adjust ports here if your deployment
# doesn't use the defaults baked into each backend's own argparse/env.

@dataclass
class Service:
    name: str
    default_port: int
    health_path: str
    smoke: Optional[Callable[[str], "requests.Response"]] = None
    notes: str = ""


def _smoke_mandi(base: str) -> requests.Response:
    return requests.get(f"{base}/api/mandi-prices/states", timeout=15)


def _smoke_nearest_mandi(base: str) -> requests.Response:
    # New Delhi coords, small radius, just to confirm the route responds
    return requests.get(
        f"{base}/api/nearest-mandi/locate",
        params={"lat": 28.6139, "lon": 77.2090, "radius": 3000},
        timeout=20,
    )


def _smoke_auction(base: str) -> requests.Response:
    return requests.get(f"{base}/auctions", timeout=15)


def _smoke_cold_storage(base: str) -> requests.Response:
    return requests.get(f"{base}/api/cold-storage/facilities", timeout=15)


def _smoke_crop_recommender(base: str) -> requests.Response:
    return requests.get(f"{base}/api/crop/valid_crops", timeout=15)


def _smoke_disease(base: str) -> requests.Response:
    return requests.get(f"{base}/supported_crops", timeout=15)


def _smoke_irrigation(base: str) -> requests.Response:
    return requests.get(f"{base}/api/v1/districts", timeout=15)


def _smoke_yield_platform(base: str) -> requests.Response:
    return requests.get(f"{base}/valid_crops", timeout=15)


def _smoke_backend2(base: str) -> requests.Response:
    return requests.get(f"{base}/crop_trends", timeout=15)


# Real per-state port map, taken directly from main.py (the orchestrator) —
# this is the source of truth, not each backend's own --port default.
CROP_BACKENDS = {"tripura": 5000, "meghalaya": 5002, "rajasthan": 5006}
CROP_RECOMMENDER_BACKENDS = {"tripura": 5003, "meghalaya": 5005, "rajasthan": 5007}

SERVICES: list[Service] = [
    Service("cold_storage", 5010, "/api/cold-storage/health", _smoke_cold_storage),
    # backend_2.py — the dashboard/stats service. One instance per state.
    Service("backend2_tripura", CROP_BACKENDS["tripura"], "/api/crop/health", _smoke_backend2),
    Service("backend2_meghalaya", CROP_BACKENDS["meghalaya"], "/api/crop/health", _smoke_backend2),
    Service("backend2_rajasthan", CROP_BACKENDS["rajasthan"], "/api/crop/health", _smoke_backend2),
    # crop_recommender.py — predict/recommend/valid_crops. One instance per state.
    Service("crop_recommender_tripura", CROP_RECOMMENDER_BACKENDS["tripura"],
            "/api/crop/health", _smoke_crop_recommender),
    Service("crop_recommender_meghalaya", CROP_RECOMMENDER_BACKENDS["meghalaya"],
            "/api/crop/health", _smoke_crop_recommender),
    Service("crop_recommender_rajasthan", CROP_RECOMMENDER_BACKENDS["rajasthan"],
            "/api/crop/health", _smoke_crop_recommender),
    Service("disease", 5004, "/health", _smoke_disease),
    Service("irrigation", 5001, "/api/v1/health", _smoke_irrigation,
            notes="Single shared irrigation backend, not per-state (per main.py: IRRIGATION_PORT=5001)."),
    Service("yield_platform", 6100, "/health", _smoke_yield_platform),
    Service("auction_engine", 6000, "/health", _smoke_auction),
    Service("mandi_prices", 5011, "/health", _smoke_mandi),
    Service("nearest_mandi", 5012, "/health", _smoke_nearest_mandi),
]


# ── TEST RUNNER ─────────────────────────────────────────────────────────

def check_service(svc: Service, host: str, scheme: str, port: int, full: bool) -> bool:
    base = f"{scheme}://{host}:{port}"
    ok = True

    # 1. Basic reachability + health check
    try:
        t0 = time.time()
        resp = requests.get(f"{base}{svc.health_path}", timeout=10)
        elapsed = (time.time() - t0) * 1000
        if resp.ok:
            print(f"  [PASS] health   {base}{svc.health_path}  "
                  f"({resp.status_code}, {elapsed:.0f} ms)")
        else:
            print(f"  [FAIL] health   {base}{svc.health_path}  "
                  f"-> HTTP {resp.status_code}")
            ok = False
    except requests.exceptions.ConnectionError:
        print(f"  [FAIL] health   {base}{svc.health_path}  "
              f"-> connection refused / host unreachable")
        return False
    except requests.exceptions.Timeout:
        print(f"  [FAIL] health   {base}{svc.health_path}  -> timed out")
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [FAIL] health   {base}{svc.health_path}  -> {e}")
        return False

    # 2. CORS header presence — required for browser-based external clients
    if full:
        cors = resp.headers.get("Access-Control-Allow-Origin")
        if cors:
            print(f"  [PASS] cors     Access-Control-Allow-Origin: {cors}")
        else:
            print(f"  [WARN] cors     no Access-Control-Allow-Origin header "
                  f"(fine for server-to-server, blocks browser JS clients)")

    # 3. Representative data endpoint
    if full and svc.smoke is not None:
        try:
            t0 = time.time()
            resp2 = svc.smoke(base)
            elapsed = (time.time() - t0) * 1000
            if resp2.status_code < 500:
                print(f"  [PASS] smoke    {resp2.request.method} "
                      f"{resp2.url.split('?')[0]}  "
                      f"({resp2.status_code}, {elapsed:.0f} ms)")
            else:
                print(f"  [FAIL] smoke    {resp2.url.split('?')[0]}  "
                      f"-> HTTP {resp2.status_code}")
                ok = False
        except requests.exceptions.RequestException as e:
            print(f"  [FAIL] smoke    -> {e}")
            ok = False

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                         help="Host/IP/domain to test against (default: 127.0.0.1). "
                              "Point this at your server's public IP or domain to "
                              "actually prove external reachability.")
    parser.add_argument("--scheme", default="http", choices=["http", "https"])
    parser.add_argument("--only", default=None,
                         help="Comma-separated list of service names to test "
                              "(default: all). See names in SERVICES list.")
    parser.add_argument("--ports", default=None,
                         help="Override ports, e.g. mandi_prices=5011,disease=5004")
    parser.add_argument("--full", action="store_true",
                         help="Also hit one real data endpoint per service and "
                              "check CORS headers, not just /health.")
    args = parser.parse_args()

    port_overrides = {}
    if args.ports:
        for pair in args.ports.split(","):
            k, v = pair.split("=")
            port_overrides[k.strip()] = int(v.strip())

    only = {s.strip() for s in args.only.split(",")} if args.only else None

    services = [s for s in SERVICES if only is None or s.name in only]
    if not services:
        print("No matching services in --only filter. Available names:")
        print(", ".join(s.name for s in SERVICES))
        return 1

    print(f"Testing {len(services)} service(s) against "
          f"{args.scheme}://{args.host} ...\n")

    results = {}
    for svc in services:
        port = port_overrides.get(svc.name, svc.default_port)
        print(f"-- {svc.name}  (port {port})" + (f"  [{svc.notes}]" if svc.notes else ""))
        results[svc.name] = check_service(svc, args.host, args.scheme, port, args.full)
        print()

    print("=" * 55)
    print("SUMMARY")
    print("=" * 55)
    all_ok = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {status:5} {name}")
        all_ok = all_ok and passed

    print()
    if all_ok:
        print("All services reachable. ✅")
    else:
        print("One or more services unreachable/failing. ❌")
        print("Common causes if this worked on localhost but fails externally:")
        print("  - Service bound to 127.0.0.1 instead of 0.0.0.0")
        print("  - Firewall/security-group not allowing inbound on the port")
        print("  - Reverse proxy / gateway not forwarding the route")
        print("  - Wrong --host or --ports passed to this script")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())