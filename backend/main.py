"""
main.py

One-command launcher for the Crop Analytics suite.

Starts:
  • gateway.py                  -> http://0.0.0.0:8085  (single public entry point)
  • backend_2.py Tripura         -> http://127.0.0.1:6000 (crop dashboard/stats API)
  • backend_2.py Meghalaya       -> http://127.0.0.1:6002 (crop dashboard/stats API)
  • backend_2.py Rajasthan       -> http://127.0.0.1:6006 (crop dashboard/stats API)
  • crop_recommender_service.py Tripura   -> http://127.0.0.1:6003 (predict/recommend API)
  • crop_recommender_service.py Meghalaya -> http://127.0.0.1:6005 (predict/recommend API)
  • crop_recommender_service.py Rajasthan -> http://127.0.0.1:6007 (predict/recommend API)
  • irrigation_backend2.py       -> http://127.0.0.1:6001 (irrigation advisory API, if present)
  • disease_backend.py           -> http://127.0.0.1:6004 (crop disease detection API)
  • yield_detect_backend.py      -> http://127.0.0.1:6008 (yield detect & geofencing API)
  • yield_platform_service.py    -> http://127.0.0.1:6100 (land parcel & yield platform microservice)
  • cold_storage_backend.py      -> http://127.0.0.1:6010 (cold storage intelligence API)
  • mandi_prices_backend.py      -> http://127.0.0.1:6011 (daily mandi price API, data.gov.in)
  • advisory_backend.py          -> http://127.0.0.1:6013 (Farmer AI advisory chatbot, Groq LLM)

Public URLs:
  http://localhost:8085/dashboard
  http://localhost:8085/irrigation
  http://localhost:8085/recommender
  http://localhost:8085/alerts
  http://localhost:8085/disease
  http://localhost:8085/auction
  http://localhost:8085/auction-mandi
  http://localhost:8085/cold-storage
  http://localhost:8085/mandi-prices
  http://localhost:8085/advisory

Usage:
  python main.py                         # launch all services
  python main.py --state tripura          # launch only Tripura crop backend + shared services
  python main.py --state meghalaya        # launch only Meghalaya crop backend + shared services
python main.py --state rajasthan        # launch only Rajasthan crop backend + shared services
  python main.py --states tripura,meghalaya,rajasthan
  python main.py --no-browser
  python main.py --no-disease             # skip disease backend
  python main.py --no-advisory            # skip advisory chatbot backend

Stop:
  Ctrl+C  (shuts down all servers cleanly)
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

# Load .env from the backend/ directory so child processes (e.g.
# disease_backend.py now living in disease_detection/) inherit all
# secrets like GROQ_API_KEY via os.environ.copy() in start_process().
try:
    from dotenv import load_dotenv as _load_dotenv
    _backend_env = Path(__file__).resolve().parent / ".env"
    if _backend_env.exists():
        _load_dotenv(dotenv_path=str(_backend_env), override=False)
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment

# ── DEFAULT CONFIG ─────────────────────────────────────────────────────────────

DEFAULT_GATEWAY_PORT = 8085
READY_TIMEOUT = 30

# Must match gateway.py state-aware targets.
CROP_BACKENDS = {
    "tripura": 6000,
    "meghalaya": 6002,
    "rajasthan": 6006,
}

# Crop Recommender microservices (predict/recommend/valid_crops/
# valid_districts/model_info/profiles) — split out of backend_2.py.
# One per state, alongside that state's dashboard/stats backend above.
# Must match gateway.py's RECOMMENDER_APIS targets.
CROP_RECOMMENDER_BACKENDS = {
    "tripura": 6003,
    "meghalaya": 6005,
    "rajasthan": 6007,
}

# Existing single irrigation backend; gateway.py uses 6001.
IRRIGATION_PORT = 6001

# Disease backend; gateway.py forwards /api/disease/* to this port.
DISEASE_PORT = 6004

# Yield Detect backend (geofenced land yield predictions); gateway.py
# forwards /api/yield/*, /content/yield-detect, /content/yield-detect-editor,
# /yield-detect and /yield-detect-editor to this port.
YIELD_DETECT_PORT = 6008

# Generic yield platform microservice (land parcel storage + yield prediction);
# yield_detect_backend.py talks to this over HTTP via YIELD_PLATFORM_SERVICE_URL.
YIELD_PLATFORM_SERVICE_PORT = 6100

# Auction backend (farmer crop listings + bidding, unused_crops table);
# gateway.py forwards /api/unused-crops/*, /api/bids/*, /content/auction
# and /auction to this port.
AUCTION_PORT = 6009

# Generic auction engine microservice (domain-agnostic, standalone);
# auction_backend.py talks to this over HTTP via AUCTION_SERVICE_URL.
AUCTION_ENGINE_PORT = 6200

# Cold Storage Intelligence backend (deterministic storage-capacity
# advisory); gateway.py forwards /api/cold-storage/* to this port.
COLD_STORAGE_PORT = 6010

# Mandi Prices backend (daily commodity market prices from data.gov.in /
# Agmarknet); gateway.py forwards /api/mandi-prices/* to this port.
MANDI_PRICES_PORT = 6011

# Nearest Mandi backend
NEAREST_MANDI_PORT = 6012

# Advisory Chatbot backend (Groq LLM orchestration layer over the other
# services above); gateway.py forwards /api/advisory/* to this port.
ADVISORY_PORT = 6013

# ── COLOUR HELPERS ─────────────────────────────────────────────────────────────

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def log(colour: str, tag: str, msg: str) -> None:
    print(f"{colour}[{tag}]{RESET} {msg}", flush=True)


# ── PORT CHECK ─────────────────────────────────────────────────────────────────


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_for_port(port: int, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_free(port):
            return True
        time.sleep(0.3)
    return False


# ── PROCESS REGISTRY ───────────────────────────────────────────────────────────

_procs: list[subprocess.Popen] = []


def start_process(label: str, cmd: list[str], cwd: Path, env_extra: Optional[dict[str, str]] = None) -> Optional[subprocess.Popen]:
    """Start a child process and register it for clean shutdown."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    if env_extra:
        env.update(env_extra)

    log(CYAN, label, "Starting: " + " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=None,
            stderr=None,
            stdin=None,
        )
        _procs.append(proc)
        return proc
    except FileNotFoundError as exc:
        log(RED, label, f"Could not start process: {exc}")
        return None
    except Exception as exc:
        log(RED, label, f"Failed to start: {exc}")
        return None


def shutdown(*_: object) -> None:
    if _procs:
        log(YELLOW, "main", "Shutting down all servers…")

    for proc in reversed(_procs):
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # Give processes a chance to exit gracefully.
    deadline = time.time() + 5
    for proc in reversed(_procs):
        remaining = max(0, deadline - time.time())
        if proc.poll() is None:
            try:
                proc.wait(timeout=remaining)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# ── HELPERS ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified launcher for Crop Analytics services")

    parser.add_argument(
        "--state",
        choices=["tripura", "meghalaya", "rajasthan", "all"],
        default="all",
        help="Crop backend state to launch. Default: all",
    )

    parser.add_argument(
        "--states",
        default=None,
        help="Comma-separated states to launch, e.g. tripura,meghalaya,rajasthan. Overrides --state.",
    )

    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT)
    parser.add_argument("--ready-timeout", type=int, default=READY_TIMEOUT)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--no-irrigation", action="store_true", help="Skip irrigation backend")
    parser.add_argument("--no-crop-recommender", action="store_true", help="Skip crop recommender microservice(s)")
    parser.add_argument("--no-disease", action="store_true", help="Skip disease detection backend")
    parser.add_argument("--no-yield-platform", action="store_true", help="Skip generic yield platform microservice (port 6100)")
    parser.add_argument("--no-yield-detect", action="store_true", help="Skip yield detect (geofencing) backend")
    parser.add_argument("--no-cold-storage", action="store_true", help="Skip cold storage intelligence backend")
    parser.add_argument("--no-auction-engine", action="store_true", help="Skip generic auction engine microservice (port 6200)")
    parser.add_argument("--no-auction", action="store_true", help="Skip auction backend")
    parser.add_argument("--no-mandi-prices", action="store_true", help="Skip mandi prices backend")
    parser.add_argument("--no-nearest-mandi", action="store_true", help="Skip nearest mandi backend")
    parser.add_argument("--no-advisory", action="store_true", help="Skip advisory chatbot backend")

    return parser.parse_args()


def selected_states(args: argparse.Namespace) -> list[str]:
    if args.states:
        states = [s.strip().lower() for s in args.states.split(",") if s.strip()]
    elif args.state == "all":
        states = list(CROP_BACKENDS.keys())
    else:
        states = [args.state]

    invalid = [s for s in states if s not in CROP_BACKENDS]
    if invalid:
        raise SystemExit(f"Invalid state(s): {', '.join(invalid)}. Valid: {', '.join(CROP_BACKENDS)}")

    # Preserve order and remove duplicates.
    seen = set()
    unique_states = []
    for state in states:
        if state not in seen:
            unique_states.append(state)
            seen.add(state)

    return unique_states


def find_script(start_dir: Path, filename: str) -> Optional[Path]:
    """Find script whether main.py is run from backend/ or project root.

    Checks backend/, root, and dedicated sub-folders under micro_services/
    (or project root) so that services can be moved or organized without
    breaking the launcher.
    """
    project_root = start_dir.parent if (start_dir / "backend").exists() or start_dir.name == "backend" else start_dir

    candidates = [
        start_dir / filename,
        start_dir / "backend" / filename,
        start_dir.parent / "backend" / filename,
        start_dir.parent / filename,
        project_root / filename,
        project_root / "backend" / filename,
    ]

    # ── dedicated service folders under micro_services/ ──
    micro_services_dir = project_root / "micro_services"
    if micro_services_dir.exists() and micro_services_dir.is_dir():
        candidates.append(micro_services_dir / filename)
        try:
            for sub in micro_services_dir.iterdir():
                if sub.is_dir():
                    candidates.append(sub / filename)
        except Exception:
            pass

    # ── direct dedicated service sub-folders at project root level ──
    subfolders = [
        "disease_detection",
        "irrigation",
        "yield-detect",
        "yield_detect",
        "auction",
        "mandi_prices",
        "nearest_mandi",
        "cold_storage",
        "chatbot",
        "advisory",
    ]
    for folder in subfolders:
        candidates.append(project_root / folder / filename)
        candidates.append(start_dir / folder / filename)
        candidates.append(start_dir.parent / folder / filename)
        candidates.append(project_root / "micro_services" / folder / filename)
        candidates.append(start_dir / "micro_services" / folder / filename)
        candidates.append(start_dir.parent / "micro_services" / folder / filename)

    for path in candidates:
        if path.exists():
            return path.resolve()

    return None


def start_if_needed(label: str, script: Path, cmd_args: list[str], port: int, timeout: int, env_extra: Optional[dict[str, str]] = None) -> None:
    """Start a Flask service unless its target port is already in use."""
    if not port_free(port):
        log(YELLOW, label, f"Port {port} already in use; assuming service is already running.")
        return

    cmd = [sys.executable, str(script), *cmd_args]
    start_process(label, cmd, cwd=script.parent, env_extra=env_extra)

    if wait_for_port(port, timeout):
        log(GREEN, label, f"Ready on http://127.0.0.1:{port}")
    else:
        log(RED, label, f"Did not become ready on port {port} within {timeout}s. Check the terminal error above.")


# ── MAIN ───────────────────────────────────────────────────────────────────────


def main() -> None:
    args = parse_args()
    states = selected_states(args)
    base_dir = Path(__file__).resolve().parent

    log(BOLD, "main", "Launching Crop Analytics suite")
    log(CYAN, "main", f"Selected crop states: {', '.join(states)}")

    backend_script = find_script(base_dir, "backend_2.py")
    crop_recommender_script = find_script(base_dir, "crop_recommender.py")
    irrigation_script = find_script(base_dir, "irrigation_backend2.py")
    disease_script = find_script(base_dir, "disease_backend.py")
    yield_platform_script = find_script(base_dir, "yield_platform_service.py")
    yield_detect_script = find_script(base_dir, "yield_detect_backend.py")
    auction_engine_script = find_script(base_dir, "auction_engine_service.py")
    auction_script = find_script(base_dir, "auction_backend.py")
    cold_storage_script = find_script(base_dir, "cold_storage_backend.py")
    mandi_prices_script = find_script(base_dir, "mandi_prices_backend.py")
    nearest_mandi_script = find_script(base_dir, "nearest_mandi_backend.py")
    advisory_script = find_script(base_dir, "advisory_backend.py")
    gateway_script = find_script(base_dir, "gateway.py")

    if not backend_script:
        raise SystemExit("backend_2.py not found. Place main.py in the project root or backend folder.")

    if not gateway_script:
        raise SystemExit("gateway.py not found. Place main.py in the project root or backend folder.")

    # 1) Start selected crop backends.
    for state in states:
        port = CROP_BACKENDS[state]
        start_if_needed(
            label=f"crop:{state}",
            script=backend_script,
            cmd_args=["--state", state, "--port", str(port)],
            port=port,
            timeout=args.ready_timeout,
        )

    # 1b) Start selected crop recommender microservices (predict/recommend/
    # valid_crops/valid_districts/model_info/profiles) — one per state,
    # split out of backend_2.py. gateway.py routes these paths here instead
    # of to the dashboard backend above.
    if args.no_crop_recommender:
        log(YELLOW, "crop-recommender", "Skipped by --no-crop-recommender")
    elif crop_recommender_script:
        for state in states:
            port = CROP_RECOMMENDER_BACKENDS[state]
            start_if_needed(
                label=f"crop-recommender:{state}",
                script=crop_recommender_script,
                cmd_args=["--state", state, "--port", str(port)],
                port=port,
                timeout=args.ready_timeout,
            )
    else:
        log(RED, "crop-recommender", "crop_recommender_service.py not found; /predict, /recommend and the recommender page will show BACKEND OFFLINE.")

    # 2) Start irrigation backend if available.
    if args.no_irrigation:
        log(YELLOW, "irrigation", "Skipped by --no-irrigation")
    elif irrigation_script:
        start_if_needed(
            label="irrigation",
            script=irrigation_script,
            cmd_args=[],
            port=IRRIGATION_PORT,
            timeout=args.ready_timeout,
        )
    else:
        log(YELLOW, "irrigation", "irrigation_backend2.py not found; skipping irrigation backend.")

    # 3) Start disease backend if available.
    # This is the important part for /api/disease/health through gateway.py.
    if args.no_disease:
        log(YELLOW, "disease", "Skipped by --no-disease")
    elif disease_script:
        start_if_needed(
            label="disease",
            script=disease_script,
            cmd_args=[],
            port=DISEASE_PORT,
            timeout=args.ready_timeout,
            # Explicitly pass GROQ_API_KEY because disease_backend.py moved to
            # disease_detection/ and its own load_dotenv() no longer finds
            # backend/.env. The value is already in os.environ (loaded above)
            # so this is just a safety-net explicit injection.
            env_extra={
                "DISEASE_PORT": str(DISEASE_PORT),
                "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),
            },
        )
    else:
        log(RED, "disease", "disease_backend.py not found; disease detection will show BACKEND OFFLINE.")

    # 3b-1) Start generic yield-platform microservice (land parcel storage + yield prediction).
    # The generic yield platform service (port 6100) must come up first so
    # yield_detect_backend.py can reach it via YIELD_PLATFORM_SERVICE_URL.
    if args.no_yield_platform:
        log(YELLOW, "yield-platform", "Skipped by --no-yield-platform")
        yield_platform_service_url = f"http://127.0.0.1:{YIELD_PLATFORM_SERVICE_PORT}"
    elif yield_platform_script:
        start_if_needed(
            label="yield-platform",
            script=yield_platform_script,
            cmd_args=["--port", str(YIELD_PLATFORM_SERVICE_PORT)],
            port=YIELD_PLATFORM_SERVICE_PORT,
            timeout=args.ready_timeout,
            env_extra={
                "YIELD_PLATFORM_SERVICE_API_KEY": os.environ.get("YIELD_PLATFORM_SERVICE_API_KEY", ""),
                "MAPPLS_CLIENT_ID": os.environ.get("MAPPLS_CLIENT_ID", ""),
                "MAPPLS_CLIENT_SECRET": os.environ.get("MAPPLS_CLIENT_SECRET", ""),
                "MAPPLS_MAP_KEY": os.environ.get("MAPPLS_MAP_KEY", ""),
            },
        )
        yield_platform_service_url = f"http://127.0.0.1:{YIELD_PLATFORM_SERVICE_PORT}"
    else:
        log(YELLOW, "yield-platform", "yield_platform_service.py not found in yield-detect/ or backend/; yield platform engine skipped.")
        yield_platform_service_url = f"http://127.0.0.1:{YIELD_PLATFORM_SERVICE_PORT}"

    # 3b-2) Start yield-detect backend (geofenced land yield predictions).
    if args.no_yield_detect:
        log(YELLOW, "yield-detect", "Skipped by --no-yield-detect")
    elif yield_detect_script:
        start_if_needed(
            label="yield-detect",
            script=yield_detect_script,
            cmd_args=[],
            port=YIELD_DETECT_PORT,
            timeout=args.ready_timeout,
            # yield_detect_backend.py has no session store of its own — it
            # verifies bearer tokens by calling the gateway's /api/auth/me,
            # and reaches yield_platform_service via YIELD_PLATFORM_SERVICE_URL.
            env_extra={
                "GATEWAY_INTERNAL_URL": f"http://127.0.0.1:{args.gateway_port}",
                "YIELD_PLATFORM_SERVICE_URL": yield_platform_service_url,
                "YIELD_PLATFORM_SERVICE_API_KEY": os.environ.get("YIELD_PLATFORM_SERVICE_API_KEY", ""),
            },
        )
    else:
        log(RED, "yield-detect", "yield_detect_backend.py not found; Yield Detect tab will show BACKEND OFFLINE.")

     # 3c) Start auction backend (farmer crop listings + bidding).
    # The generic auction engine (port 6000) must come up first so
    # auction_backend.py can reach it via AUCTION_SERVICE_URL.
    if args.no_auction_engine:
        log(YELLOW, "auction-engine", "Skipped by --no-auction-engine")
        auction_engine_url = f"http://127.0.0.1:{AUCTION_ENGINE_PORT}"
    elif auction_engine_script:
        start_if_needed(
            label="auction-engine",
            script=auction_engine_script,
            cmd_args=["--port", str(AUCTION_ENGINE_PORT)],
            port=AUCTION_ENGINE_PORT,
            timeout=args.ready_timeout,
        )
        auction_engine_url = f"http://127.0.0.1:{AUCTION_ENGINE_PORT}"
    else:
        log(YELLOW, "auction-engine", "auction_engine_service.py not found in auction/ or backend/; auction engine skipped.")
        auction_engine_url = f"http://127.0.0.1:{AUCTION_ENGINE_PORT}"

    if args.no_auction:
        log(YELLOW, "auction", "Skipped by --no-auction")
    elif auction_script:
        start_if_needed(
            label="auction",
            script=auction_script,
            cmd_args=["--port", str(AUCTION_PORT)],
            port=AUCTION_PORT,
            timeout=args.ready_timeout,
            # auction_backend.py verifies bearer tokens via the gateway's /api/auth/me
            # and reaches the auction engine via AUCTION_SERVICE_URL.
            env_extra={
                "GATEWAY_INTERNAL_URL": f"http://127.0.0.1:{args.gateway_port}",
                "AUCTION_SERVICE_URL": auction_engine_url,
            },
        )
    else:
        log(RED, "auction", "auction_backend.py not found; Auction tab will show BACKEND OFFLINE.")

    # 3d) Start cold-storage-intelligence backend.
    if args.no_cold_storage:
        log(YELLOW, "cold-storage", "Skipped by --no-cold-storage")
    elif cold_storage_script:
        start_if_needed(
            label="cold-storage",
            script=cold_storage_script,
            cmd_args=[],
            port=COLD_STORAGE_PORT,
            timeout=args.ready_timeout,
            env_extra={"GATEWAY_INTERNAL_URL": f"http://127.0.0.1:{args.gateway_port}"},
        )
    else:
        log(RED, "cold-storage", "cold_storage_backend.py not found; Cold Storage Intelligence API will be unavailable.")

    # 3e) Start mandi-prices backend (daily commodity market prices).
    if args.no_mandi_prices:
        log(YELLOW, "mandi-prices", "Skipped by --no-mandi-prices")
    elif mandi_prices_script:
        start_if_needed(
            label="mandi-prices",
            script=mandi_prices_script,
            cmd_args=[],
            port=MANDI_PRICES_PORT,
            timeout=args.ready_timeout,
        )
    else:
        log(RED, "mandi-prices", "mandi_prices_backend.py not found; Mandi Prices tab will show BACKEND OFFLINE.")

    # 3f) Start nearest-mandi backend.
    if args.no_nearest_mandi:
        log(YELLOW, "nearest-mandi", "Skipped by --no-nearest-mandi")
    elif nearest_mandi_script:
        start_if_needed(
            label="nearest-mandi",
            script=nearest_mandi_script,
            cmd_args=[],
            port=NEAREST_MANDI_PORT,
            timeout=args.ready_timeout,
        )
    else:
        log(RED, "nearest-mandi", "nearest_mandi_backend.py not found; Nearest Mandi tab will show BACKEND OFFLINE.")

    # 3g) Start advisory chatbot backend (Groq LLM orchestration layer).
    # Needs GROQ_API_KEY (same key disease_backend.py uses) and, since its
    # cold-storage tool calls require a farmer's bearer token to be
    # forwarded to cold_storage_backend.py, GATEWAY_INTERNAL_URL isn't
    # actually needed here (the token comes from the incoming request),
    # but ADVISORY_IRRIGATION_API_KEY is passed through in case
    # irrigation_backend2.py has IRRIGATION_API_KEYS configured.
    if args.no_advisory:
        log(YELLOW, "advisory", "Skipped by --no-advisory")
    elif advisory_script:
        start_if_needed(
            label="advisory",
            script=advisory_script,
            cmd_args=[],
            port=ADVISORY_PORT,
            timeout=args.ready_timeout,
            env_extra={
                "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),
                "ADVISORY_IRRIGATION_API_KEY": os.environ.get("ADVISORY_IRRIGATION_API_KEY", ""),
                "MANDI_PRICES_URL": f"http://127.0.0.1:{MANDI_PRICES_PORT}",
                "NEAREST_MANDI_URL": f"http://127.0.0.1:{NEAREST_MANDI_PORT}",
                "IRRIGATION_URL": f"http://127.0.0.1:{IRRIGATION_PORT}",
                "COLD_STORAGE_URL": f"http://127.0.0.1:{COLD_STORAGE_PORT}",
                "CROP_RECOMMENDER_TRIPURA": f"http://127.0.0.1:{CROP_RECOMMENDER_BACKENDS['tripura']}",
                "CROP_RECOMMENDER_MEGHALAYA": f"http://127.0.0.1:{CROP_RECOMMENDER_BACKENDS['meghalaya']}",
                "CROP_RECOMMENDER_RAJASTHAN": f"http://127.0.0.1:{CROP_RECOMMENDER_BACKENDS['rajasthan']}",
            },
        )
    else:
        log(RED, "advisory", "advisory_backend.py not found; Advisory chatbot tab will show BACKEND OFFLINE.")

    # 4) Start gateway last, after internal services are up.
    start_if_needed(
        label="gateway",
        script=gateway_script,
        cmd_args=[],
        port=args.gateway_port,
        timeout=args.ready_timeout,
    )

    public_url = f"http://localhost:{args.gateway_port}/dashboard"
    disease_url = f"http://localhost:{args.gateway_port}/disease"
    advisory_url = f"http://localhost:{args.gateway_port}/advisory"

    print("\n" + "=" * 70)
    log(GREEN, "main", "All requested services have been launched.")
    print(f"  Dashboard: {public_url}")
    print(f"  Disease:   {disease_url}")
    print(f"  Advisory:  {advisory_url}")
    print("  Gateway disease health check:")
    print(f"    http://localhost:{args.gateway_port}/api/disease/health?state=tripura")
    print("=" * 70 + "\n")

    if not args.no_browser:
        webbrowser.open(public_url)

    # Keep main process alive so Ctrl+C can stop all child servers.
    try:
        while True:
            # If a child process exits unexpectedly, report it.
            for proc in list(_procs):
                code = proc.poll()
                if code is not None:
                    log(RED, "main", f"A service exited with code {code}. Check terminal output above.")
                    _procs.remove(proc)
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()