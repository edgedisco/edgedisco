"""Versioned, non-destructive migrations for endpoint configuration."""
import json
from pathlib import Path

CONFIG_VERSION = 1


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Cannot read {path}; repair the configuration before reinstalling") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"Configuration must be an object: {path}")
    version = config.get("config_version", 0)
    if type(version) is not int or not 0 <= version <= CONFIG_VERSION:
        raise RuntimeError(f"Unsupported configuration version in {path}; use a compatible release")
    for key in ("device_token", "device_id", "enrollment_token", "server_url", "runtime_spool", "ca_file"):
        if key in config and (not isinstance(config[key], str) or not config[key]):
            raise RuntimeError(f"Invalid {key} in {path}; repair the configuration before reinstalling")
    interval = config.get("scan_interval_seconds", 300)
    if type(interval) is not int or interval < 60:
        raise RuntimeError(f"Invalid scan_interval_seconds in {path}; expected an integer of at least 60")
    return config


def migrate_config(config: dict, *, server_url: str, spool: Path) -> dict:
    migrated = dict(config)
    # Version 0 is the original unversioned format. Unknown custom settings
    # survive migrations; future field renames belong in explicit version steps.
    if migrated.get("config_version", 0) == 0:
        migrated.setdefault("scan_interval_seconds", 300)
        migrated.setdefault("runtime_spool", str(spool))
        migrated["config_version"] = 1
    migrated["server_url"] = server_url
    return migrated
