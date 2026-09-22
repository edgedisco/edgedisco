"""Encode only EdgeDisco's sanitized outbox event as OTLP Logs protobuf.

Transport contract: POST /v1/logs with Content-Type: application/x-protobuf.
This module performs no HTTP requests and does not accept discovery or report objects.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .detector import HOST_APP_NAMES, SIGNATURES
from .otlp_events import EVENT_NAME, DEVICE_EVENT_NAME, SCHEMA_VERSION

OTLP_LOGS_PATH = "/v1/logs"
OTLP_CONTENT_TYPE = "application/x-protobuf"
_KNOWN_ASSETS = {name: vendor for name, vendor, _ in SIGNATURES}
_REQUIRED = {
    "edgedisco.schema.version", "edgedisco.observation.id", "device.id",
    "asset.kind", "asset.name", "asset.vendor", "asset.running",
    "edgedisco.simulated",
}
_OPTIONAL = {"asset.host_app", "asset.relationship", "asset.version"}
_RUNTIME_OPTIONAL = {"asset.host_app", "asset.relationship"}


def _timestamp_nanos(value: Any, label: str) -> int:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timestamp requires timezone")
        utc = timestamp.astimezone(timezone.utc)
        nanos = calendar.timegm(utc.timetuple()) * 1_000_000_000 + utc.microsecond * 1_000
        if not 0 < nanos < 2**64:
            raise ValueError("invalid timestamp")
        return nanos
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc


def _validated_event(payload_json: str) -> tuple[int, int, dict[str, Any]]:
    if not isinstance(payload_json, str):
        raise ValueError("OTLP encoder requires serialized sanitized outbox payload")
    try:
        event = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid sanitized outbox payload") from exc
    required_fields = {"timestamp", "event.name", "resource", "attributes"}
    if (not isinstance(event, dict) or not required_fields <= set(event)
            or set(event) - (required_fields | {"recorded_at"})):
        raise ValueError("unsupported outbox event fields")
    if event["event.name"] not in (EVENT_NAME, DEVICE_EVENT_NAME) or event["resource"] != {"service.name": "edgedisco"}:
        raise ValueError("unsupported outbox event identity")
    attrs = event["attributes"]
    if not isinstance(attrs, dict):
        raise ValueError("unsupported outbox attributes")
    schema = attrs.get("edgedisco.schema.version")
    if type(schema) is not int or schema not in (1, SCHEMA_VERSION):
        raise ValueError("unsupported outbox schema version")
    required, optional = _REQUIRED, _OPTIONAL
    if event["event.name"] == DEVICE_EVENT_NAME:
        required = {"edgedisco.schema.version", "edgedisco.observation.id", "device.id",
                    "inventory.asset_count", "inventory.simulated_asset_count"}
        optional = set()
        if schema != SCHEMA_VERSION or "recorded_at" not in event:
            raise ValueError("unsupported heartbeat schema")
    elif schema == SCHEMA_VERSION:
        required = _REQUIRED | {"asset.present"}
    if not required <= set(attrs) or set(attrs) - (required | optional):
        raise ValueError("unsupported outbox attributes")
    event_id = attrs["edgedisco.observation.id"]
    device_id = attrs["device.id"]
    if not isinstance(event_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", event_id):
        raise ValueError("invalid observation ID")
    if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{32}", device_id):
        raise ValueError("invalid device ID")
    event_nanos = _timestamp_nanos(event["timestamp"], "outbox timestamp")
    observed_nanos = _timestamp_nanos(event.get("recorded_at", event["timestamp"]),
                                      "outbox recorded_at")
    if event["event.name"] == DEVICE_EVENT_NAME:
        count, simulated = attrs["inventory.asset_count"], attrs["inventory.simulated_asset_count"]
        if type(count) is not int or type(simulated) is not int or not 0 <= simulated <= count <= 10_000:
            raise ValueError("invalid inventory counts")
        return event_nanos, observed_nanos, attrs
    kind, name, vendor = attrs["asset.kind"], attrs["asset.name"], attrs["asset.vendor"]
    if not isinstance(kind, str) or kind not in {"application", "process", "agent_runtime"}:
        raise ValueError("invalid asset kind")
    if not isinstance(name, str) or _KNOWN_ASSETS.get(name) != vendor:
        raise ValueError("invalid asset signature")
    if type(attrs["asset.running"]) is not bool or type(attrs["edgedisco.simulated"]) is not bool:
        raise ValueError("invalid asset flags")
    if schema == SCHEMA_VERSION and (type(attrs["asset.present"]) is not bool or
                                     (attrs["asset.running"] and not attrs["asset.present"])):
        raise ValueError("invalid asset presence")
    version = attrs.get("asset.version")
    if version is not None and (not isinstance(version, str) or not version or len(version) > 128):
        raise ValueError("invalid asset version")
    if kind == "agent_runtime":
        host = attrs.get("asset.host_app")
        relationship = attrs.get("asset.relationship")
        if not isinstance(host, str) or host not in HOST_APP_NAMES | {"Direct/local"}:
            raise ValueError("invalid host application")
        if relationship != ("local_process" if host == "Direct/local" else "spawned_by"):
            raise ValueError("invalid relationship")
    elif set(attrs) & _RUNTIME_OPTIONAL:
        raise ValueError("unexpected runtime attributes")
    return event_nanos, observed_nanos, attrs


def encode_outbox_event(payload_json: str) -> bytes:
    """Encode one validated outbox payload; reject all unknown fields and values."""
    event_nanos, observed_nanos, attrs = _validated_event(payload_json)
    try:
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    except ImportError as exc:
        raise RuntimeError("OTLP protobuf support requires the edgedisco otlp extra") from exc

    def key_value(key: str, value: Any) -> Any:
        if type(value) is bool:
            encoded = AnyValue(bool_value=value)
        elif type(value) is int:
            encoded = AnyValue(int_value=value)
        else:
            encoded = AnyValue(string_value=value)
        return KeyValue(key=key, value=encoded)

    request = ExportLogsServiceRequest()
    resource_logs = request.resource_logs.add()
    resource_logs.resource.attributes.extend((
        key_value("service.name", "edgedisco"),
        key_value("service.version", __version__),
    ))
    scope_logs = resource_logs.scope_logs.add()
    scope_logs.scope.name = "ai_asset_inventory.otlp_encoder"
    scope_logs.scope.version = __version__
    record = scope_logs.log_records.add()
    record.time_unix_nano = event_nanos
    record.observed_time_unix_nano = observed_nanos
    record.severity_number = 9  # OTLP SEVERITY_NUMBER_INFO.
    record.severity_text = "INFO"
    record.event_name = DEVICE_EVENT_NAME if "inventory.asset_count" in attrs else EVENT_NAME
    if record.event_name == DEVICE_EVENT_NAME:
        # Asset-only collector transforms leave heartbeat records untouched.
        record.body.string_value = DEVICE_EVENT_NAME
    for key in sorted(attrs):
        record.attributes.append(key_value(key, attrs[key]))
    return request.SerializeToString()
