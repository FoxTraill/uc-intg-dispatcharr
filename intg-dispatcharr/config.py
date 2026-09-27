"""
Persistent driver configuration.

Stored in $UC_CONFIG_HOME/config.json (provided by the UC sandbox; only
this directory is writable and persisted).
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)


@dataclass
class DriverConfig:
    url: str = ""
    api_key: str = ""
    client_ip: str = ""
    poll_interval: int = 10
    # Channel cache is reloaded every X seconds
    cache_refresh_interval: int = 21600  # 6h
    # Local HTTP port of the logo proxy. UC reserves 8000-9200 and 13333.
    logo_proxy_port: int = 19191

    def is_configured(self) -> bool:
        return bool(self.url and self.api_key and self.client_ip)


def _config_path() -> Path:
    """
    UC_CONFIG_HOME is provided by the UC sandbox. For local testing,
    falls back to $HOME (or /tmp).
    """
    base = os.environ.get("UC_CONFIG_HOME") or os.environ.get("HOME") or "/tmp"
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p / "config.json"


def load_config() -> DriverConfig:
    path = _config_path()
    if not path.exists():
        _LOG.info("No config at %s, returning defaults", path)
        return DriverConfig()
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # Be lenient about extra keys / missing keys
        cfg = DriverConfig()
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        _LOG.info("Loaded config from %s (configured=%s)", path, cfg.is_configured())
        return cfg
    except Exception as exc:
        _LOG.error("Failed to load config %s: %s — using defaults", path, exc)
        return DriverConfig()


def save_config(cfg: DriverConfig) -> None:
    path = _config_path()
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(asdict(cfg), fh, indent=2)
        _LOG.info("Saved config to %s", path)
    except Exception as exc:
        _LOG.error("Failed to save config %s: %s", path, exc)
