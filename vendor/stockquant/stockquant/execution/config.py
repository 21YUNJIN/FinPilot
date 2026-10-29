from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Dict, List, Mapping, Optional

REQUIRED_CREDENTIAL_KEYS = (
    "STOCKQUANT_API_KEY",
    "STOCKQUANT_API_SECRET",
    "STOCKQUANT_API_PASSPHRASE",
)
SIMULATED_KEY = "STOCKQUANT_OKX_SIMULATED"


def parse_env_file(path: str) -> Dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return {}
    values: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_env_file(path: str, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    target = os.environ if environ is None else environ
    values = parse_env_file(path)
    for key, value in values.items():
        if hasattr(target, "setdefault"):
            target.setdefault(key, value)  # type: ignore[attr-defined]
    return values


DEFAULT_TUSHARE_ENV = "~/.config/stockquant/tushare.env"


def resolve_tushare_token(
    explicit: Optional[str] = None,
    path: str = DEFAULT_TUSHARE_ENV,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve a Tushare token without ever logging it.

    Precedence: explicit argument, then the ``TUSHARE_TOKEN`` environment
    variable, then the local ``~/.config/stockquant/tushare.env`` file.
    """
    if explicit:
        return str(explicit).strip()
    target = dict(os.environ if environ is None else environ)
    token = str(target.get("TUSHARE_TOKEN", "") or "").strip()
    if token:
        return token
    values = load_env_file(path, target)
    return str(target.get("TUSHARE_TOKEN", "") or values.get("TUSHARE_TOKEN", "") or "").strip()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_broker_config(
    path: str,
    live: bool,
    environ: Optional[Mapping[str, str]] = None,
    require_live_confirmation: bool = False,
) -> Dict[str, object]:
    """Validate local execution settings without making a network request."""
    file_values = parse_env_file(path)
    runtime = dict(os.environ if environ is None else environ)
    merged = {**file_values, **runtime}
    missing = [key for key in REQUIRED_CREDENTIAL_KEYS if not merged.get(key)]
    errors: List[str] = []
    warnings: List[str] = []

    if missing:
        errors.append("missing required credential fields: " + ", ".join(missing))

    simulated_raw = merged.get(SIMULATED_KEY)
    simulated = _truthy(simulated_raw)
    if live:
        if simulated:
            errors.append("simulation mode is enabled while live mode was requested")
        if require_live_confirmation and merged.get("STOCKQUANT_LIVE_CONFIRM") != "YES_I_ACCEPT_RISK":
            errors.append("live confirmation token is missing")
    elif not simulated:
        errors.append("simulation mode marker is missing or disabled")

    mode = None
    env_path = Path(path).expanduser()
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            warnings.append("configuration file is readable or writable by group/others")

    return {
        "ok": not errors,
        "live": bool(live),
        "simulation": bool(simulated),
        "env_file": str(env_path),
        "env_file_exists": env_path.exists(),
        "mode": mode,
        "required_fields_present": not missing,
        "missing_fields": missing,
        "errors": errors,
        "warnings": warnings,
    }
