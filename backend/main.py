"""
main.py

One-command launcher for the Crop Analytics suite.

Starts:
  • gateway.py                  -> http://0.0.0.0:8085  (single public entry point)
  • backend_2.py Tripura         -> http://127.0.0.1:5000 (crop yield / stats API)
  • backend_2.py Meghalaya       -> http://127.0.0.1:5002 (crop yield / stats API)
• backend_2.py Rajasthan       -> http://127.0.0.1:5006 (crop yield / stats API)
  • irrigation_backend2.py       -> http://127.0.0.1:5001 (irrigation advisory API, if present)
  • disease_backend.py           -> http://127.0.0.1:5004 (crop disease detection API)

Public URLs:
  http://localhost:8085/dashboard
  http://localhost:8085/irrigation
  http://localhost:8085/recommender
  http://localhost:8085/alerts
  http://localhost:8085/disease

Usage:
  python main.py                         # launch all services
  python main.py --state tripura          # launch only Tripura crop backend + shared services
  python main.py --state meghalaya        # launch only Meghalaya crop backend + shared services
python main.py --state rajasthan        # launch only Rajasthan crop backend + shared services
  python main.py --states tripura,meghalaya,rajasthan
  python main.py --no-browser
  python main.py --no-disease             # skip disease backend

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

# ── DEFAULT CONFIG ─────────────────────────────────────────────────────────────

DEFAULT_GATEWAY_PORT = 8085
READY_TIMEOUT = 30

# Must match gateway.py state-aware targets.
CROP_BACKENDS = {
    "tripura": 5000,
    "meghalaya": 5002,
    "rajasthan": 5006,
}

# Existing single irrigation backend; gateway.py uses 5001.
IRRIGATION_PORT = 5001

# Disease backend; gateway.py forwards /api/disease/* to this port.
DISEASE_PORT = 5004

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
    parser.add_argument("--no-disease", action="store_true", help="Skip disease detection backend")

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
    """Find script whether main.py is run from backend/ or project root."""
    candidates = [
        start_dir / filename,
        start_dir / "backend" / filename,
        start_dir.parent / "backend" / filename,
        start_dir.parent / filename,
    ]

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
    irrigation_script = find_script(base_dir, "irrigation_backend2.py")
    disease_script = find_script(base_dir, "disease_backend.py")
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
            env_extra={"DISEASE_PORT": str(DISEASE_PORT)},
        )
    else:
        log(RED, "disease", "disease_backend.py not found; disease detection will show BACKEND OFFLINE.")

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

    print("\n" + "=" * 70)
    log(GREEN, "main", "All requested services have been launched.")
    print(f"  Dashboard: {public_url}")
    print(f"  Disease:   {disease_url}")
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
