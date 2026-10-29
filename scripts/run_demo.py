"""Run a loopback-only FinPilot demo as a foreground process; Ctrl+C stops its children.

Requires pre-migrated, explicitly selected PostgreSQL demo database. This launcher
does not create, reset, migrate, or seed a database. All financial data is simulated.
"""
from __future__ import annotations
import os
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
FP = ROOT / "finpilot"
QUANT = ROOT / "services" / "quant-service"
RUNTIME = ROOT / ".runtime"
PY_FP = Path(os.environ.get("FINPILOT_PYTHON", ROOT / ".venv-finpilot" / "Scripts" / "python.exe"))
PY_QUANT = Path(os.environ.get("QUANT_PYTHON", ROOT / ".venv-quant" / "Scripts" / "python.exe"))


def wait_ready(url: str, child: subprocess.Popen, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Service exited before becoming ready: {url}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(.2)
    raise RuntimeError(f"Service did not become ready: {url}")


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("Set DATABASE_URL to a dedicated, already migrated and seeded demo database.", file=sys.stderr)
        return 2
    for python in (PY_FP, PY_QUANT):
        if not python.is_file():
            print(f"Missing Python environment: {python}. Run scripts/setup.ps1 first.", file=sys.stderr)
            return 2
    for port in (58000, 58101, 58102, 8770):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                print(f"Port {port} is already in use; no service was started.", file=sys.stderr)
                return 2
    RUNTIME.mkdir(exist_ok=True)
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    mode = os.environ.get("APP_MODE", "mock")
    if mode not in {"mock", "live"}:
        print("APP_MODE must be mock or live; replay is not implemented.", file=sys.stderr)
        return 2
    env.update(APP_MODE=mode, QUANT_BACKEND="remote", PROVIDER_MODE="http", PYTHONUTF8="1",
        NO_PROXY="127.0.0.1,localhost", STOCKQUANT_SERVICE_URL="http://127.0.0.1:8770",
        STOCKQUANT_SERVICE_TOKEN=token, SQ_BANK_TOKEN=token,
        BANK_SANDBOX_STATE=str(RUNTIME / "bank.sqlite3"),
        MERCHANT_SANDBOX_STATE=str(RUNTIME / "merchant.sqlite3"),
        PYTHONPATH=str(FP / "services" / "api" / "src"))
    quant_env = dict(env, PYTHONPATH=str(ROOT / "vendor" / "stockquant"))
    processes: list[subprocess.Popen] = []
    logs = []

    def start(name, args, cwd, settings):
        log = (RUNTIME / (name + ".log")).open("a", encoding="utf-8")
        logs.append(log)
        p = subprocess.Popen([str(x) for x in args], cwd=cwd, env=settings, stdout=log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(p)
        return p

    try:
        q = start("quant", [PY_QUANT, "-m", "stockquant_bank", "serve", "--port", "8770"], QUANT, quant_env)
        wait_ready("http://127.0.0.1:8770/healthz", q)
        for name, port in (("bank", 58101), ("merchant", 58102)):
            p = start(name, [PY_FP, "-m", "uvicorn", f"{name}:app", "--app-dir",
                FP / "services" / "provider-sandbox", "--host", "127.0.0.1", "--port", str(port)], FP, env)
            wait_ready(f"http://127.0.0.1:{port}/{name}/health", p)
        api = start("api", [PY_FP, "-m", "uvicorn", "finpilot.app:app", "--host", "127.0.0.1", "--port", "58000"], FP, env)
        wait_ready("http://127.0.0.1:58000/healthz", api)
        with urlopen("http://127.0.0.1:58000/healthz", timeout=3) as response:
            health = json.load(response)
        if health.get("status") != "ok" or not health.get("database", {}).get("ok"):
            raise RuntimeError("FinPilot database health check failed; verify the dedicated database.")
        adapters = health.get("adapters", {}).get("registered", [])
        if "bank_sandbox_v1" not in adapters or "merchant_sandbox_v1" not in adapters:
            raise RuntimeError("Independent provider adapters were not registered.")
        for worker in ("worker_outbox.py", "worker_scheduler.py"):
            start(worker, [PY_FP, FP / "scripts" / worker], FP, env)
        print(f"Demo ready (mode={mode}): http://127.0.0.1:58000/app/ ; IM: http://127.0.0.1:58000/im/", flush=True)
        print("Synthetic data only. Press Ctrl+C to stop. See .runtime for logs.", flush=True)
        while True:
            for p in processes:
                if p.poll() is not None:
                    raise RuntimeError("A service stopped; inspect .runtime logs.")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping demo services.")
        return 0
    except Exception as exc:
        print(f"Demo startup/runtime failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
        for p in reversed(processes):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
