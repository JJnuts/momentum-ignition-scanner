"""Configuration: config.json (tunables, never secrets) + .env (secrets only).

Rules:
  - Every threshold lives in config.json. Code never hard-codes a number.
  - Secrets live in .env (or the process environment). Never in config.json.
  - Loading fails loudly on anything missing or malformed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .plans import PLANS

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"
# Any of these names is accepted for the secrets file (first one found wins).
ENV_FALLBACKS: tuple[str, ...] = (".env", "secrets.txt", "secrets.env", "env.txt")


def resolve_env_path(path: Path = ENV_PATH) -> Path:
    """Return the first existing secrets file: the given path, else a sibling fallback name."""
    if path.exists():
        return path
    for name in ENV_FALLBACKS:
        candidate = path.parent / name
        if candidate.exists():
            return candidate
    return path

REQUIRED_ENV: tuple[str, ...] = ("BIRDEYE_API_KEY",)
OPTIONAL_ENV: dict[str, str | None] = {
    "DISCORD_WEBHOOK_TEST": None,
    "DISCORD_WEBHOOK_LIVE": None,
    "SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com",
    "ROBINHOOD_RPC_URL": "https://rpc.mainnet.chain.robinhood.com",
}


class ConfigError(RuntimeError):
    """Raised for any missing/malformed configuration. Message is user-facing."""


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ConfigError(f".env line {lineno}: expected KEY=VALUE, got {raw!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def load_env(path: Path = ENV_PATH) -> dict[str, str | None]:
    """Merge .env file (if present) with the process environment (which wins).

    Fails loudly if any REQUIRED_ENV key is missing from both sources.
    """
    env: dict[str, str | None] = {}
    path = resolve_env_path(path)
    if path.exists():
        env.update(_parse_env_file(path))
    for key in list(REQUIRED_ENV) + list(OPTIONAL_ENV):
        if os.environ.get(key):
            env[key] = os.environ[key]
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        where = f"{path}" if path.exists() else f"{path} (file not found)"
        raise ConfigError(
            f"missing required secret(s) {missing} - add them to {where} "
            f"(any of {', '.join(ENV_FALLBACKS)} in the project folder) or set them in the environment."
        )
    for key, default in OPTIONAL_ENV.items():
        if not env.get(key):
            env[key] = default
    return env


@dataclass(frozen=True)
class ChainConfig:
    name: str
    enabled: bool
    birdeye_chain: str
    scan_interval_s: int
    stage0: dict[str, Any]
    stage1: dict[str, Any]


@dataclass(frozen=True)
class Config:
    root: Path
    birdeye_plan: str
    birdeye_base_url: str
    db_path: Path
    raw_dir: Path
    log_dir: Path
    recorder_enabled: bool
    discord_use_test: bool
    chains: dict[str, ChainConfig]
    env: dict[str, str | None]
    raw: dict[str, Any]
    timezone: str = "UTC"           # IANA name; every daily budget rolls at local midnight (T15a)

    @property
    def enabled_chains(self) -> list[ChainConfig]:
        return [c for c in self.chains.values() if c.enabled]

    def secret(self, key: str) -> str | None:
        return self.env.get(key)


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"config.json: {ctx} is missing required key {key!r}")
    return d[key]


def load_config(config_path: Path = CONFIG_PATH, env_path: Path = ENV_PATH) -> Config:
    if not config_path.exists():
        raise ConfigError(f"config.json not found at {config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json is not valid JSON: {e}") from e

    env = load_env(env_path)

    birdeye = _require(data, "birdeye", "top level")
    plan = str(_require(birdeye, "plan", "birdeye")).lower()
    if plan not in PLANS:
        raise ConfigError(f"birdeye.plan {plan!r} unknown; valid: {sorted(PLANS)}")
    base_url = str(birdeye.get("base_url", "https://public-api.birdeye.so")).rstrip("/")

    chains_raw = _require(data, "chains", "top level")
    if not isinstance(chains_raw, dict) or not chains_raw:
        raise ConfigError("config.json: chains must be a non-empty object")
    chains: dict[str, ChainConfig] = {}
    for name, c in chains_raw.items():
        ctx = f"chains.{name}"
        interval = int(_require(c, "scan_interval_s", ctx))
        if interval < 10:
            raise ConfigError(f"{ctx}.scan_interval_s must be >= 10 (got {interval})")
        chains[name] = ChainConfig(
            name=name,
            enabled=bool(_require(c, "enabled", ctx)),
            birdeye_chain=str(_require(c, "birdeye_chain", ctx)),
            scan_interval_s=interval,
            stage0=dict(_require(c, "stage0", ctx)),
            stage1=dict(_require(c, "stage1", ctx)),
        )
    if not any(ch.enabled for ch in chains.values()):
        raise ConfigError("config.json: no chain is enabled")

    root = config_path.resolve().parent
    tz = str(data.get("timezone", "UTC") or "UTC")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"config.json: timezone {tz!r} is not a known IANA timezone") from e

    def _path(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else root / p

    db = data.get("db", {})
    rec = data.get("recorder", {})
    logs = data.get("logging", {})
    discord = data.get("discord", {})

    return Config(
        root=root,
        birdeye_plan=plan,
        birdeye_base_url=base_url,
        db_path=_path(db.get("path", "data/scanner.sqlite")),
        raw_dir=_path(rec.get("dir", "raw")),
        log_dir=_path(logs.get("dir", "logs")),
        recorder_enabled=bool(rec.get("enabled", True)),
        discord_use_test=bool(discord.get("use_test_channel", True)),
        chains=chains,
        env=env,
        raw=data,
        timezone=tz,
    )


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return "<unset>"
    return f"{value[:keep]}...({len(value)} chars)"
