"""Validation at the authenticated, but untrusted, endpoint boundary."""
from datetime import datetime, timezone, timedelta
import re


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid observation timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp requires timezone")
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid observation timestamp") from exc


def _object(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"unsupported {label} fields")


def observation_timestamp(value):
    normalized = timestamp(value)
    if datetime.fromisoformat(normalized) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("observation timestamp exceeds permitted clock skew (5 minutes)")
    return normalized


def _string(value, limit, label):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"invalid {label}")


def validate_device(device):
    limits = {"hostname": 255, "os": 64, "os_version": 255, "machine": 64, "agent_version": 32}
    _object(device, limits, "device")
    for key, value in device.items():
        if not isinstance(value, str) or len(value) > limits[key]:
            raise ValueError(f"invalid device {key}")


def validate_report(payload):
    _object(payload, {"schema_version", "scan_id", "observed_at", "device", "assets", "privacy"}, "report")
    if not {"scan_id", "observed_at", "device", "assets", "privacy"} <= set(payload):
        raise ValueError("missing report fields")
    schema_version = payload.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError("unsupported schema version")
    _string(payload["scan_id"], 128, "scan ID")
    observation_timestamp(payload["observed_at"])
    validate_device(payload["device"])
    privacy = payload["privacy"]
    expected = {"content_captured": False, "secrets_captured": False, "paths_hashed": True, "command_lines_hashed": True}
    if schema_version == 2:
        expected["binary_contents_hashed"] = True
    _object(privacy, expected, "privacy")
    if schema_version == 2 and set(privacy) != set(expected):
        raise ValueError("missing privacy flags")
    if any(type(value) is not bool or value != expected[key] for key, value in privacy.items()):
        raise ValueError("invalid privacy flags")
    assets = payload["assets"]
    if not isinstance(assets, list) or len(assets) > 10_000:
        raise ValueError("assets must be a list with at most 10000 entries")
    metadata_strings = {"executable", "package", "configured_in", "transport", "host_app", "runtime", "relationship", "evidence_label", "discovery_source"}
    seen = set()
    for asset in assets:
        allowed_asset_fields = {
            "fingerprint", "kind", "name", "vendor", "version", "running",
            "path_hash", "command_hash", "metadata",
        }
        if schema_version == 2:
            allowed_asset_fields |= {
                "binary_sha256", "binary_fingerprint_status", "fingerprint_library_version",
            }
        _object(asset, allowed_asset_fields, "asset")
        for key in ("fingerprint", "kind", "name", "vendor"):
            _string(asset.get(key), 255, key)
        if asset["kind"] not in {"application", "process", "agent_runtime", "mcp_server"}:
            raise ValueError("unsupported asset kind")
        if type(asset.get("running")) is not bool:
            raise ValueError("running must be boolean")
        for key in ("fingerprint", "path_hash", "command_hash", "binary_sha256"):
            value = asset.get(key)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
                raise ValueError(f"invalid {key}")
        binary_sha256 = asset.get("binary_sha256")
        binary_status = asset.get("binary_fingerprint_status")
        library_version = asset.get("fingerprint_library_version")
        if binary_sha256 is None:
            if binary_status is not None or library_version is not None:
                raise ValueError("binary fingerprint metadata requires binary_sha256")
        else:
            if binary_status not in {"matched", "unlisted"}:
                raise ValueError("invalid binary fingerprint status")
            _string(library_version, 64, "fingerprint library version")
        if asset["fingerprint"] in seen:
            raise ValueError("duplicate asset fingerprint")
        seen.add(asset["fingerprint"])
        if asset.get("version") is not None:
            _string(asset["version"], 128, "version")
        metadata = asset.get("metadata", {})
        _object(metadata, metadata_strings | {"instance_count", "demo_lab", "observed_running"}, "asset metadata")
        for key, value in metadata.items():
            if key in metadata_strings:
                _string(value, 255, key)
                if key in {"executable", "runtime", "package"} and ("/" in value or "\\" in value):
                    raise ValueError(f"{key} must be a basename")
            elif key == "instance_count":
                if type(value) is not int or not 1 <= value <= 1_000_000:
                    raise ValueError("invalid instance count")
            elif type(value) is not bool:
                raise ValueError(f"invalid {key}")
