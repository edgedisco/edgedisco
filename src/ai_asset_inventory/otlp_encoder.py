"""Encode only EdgeDisco's sanitized outbox event as OTLP Logs protobuf.

Future transport contract: POST /v1/logs with Content-Type: application/x-protobuf.
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
from .otlp_events import EVENT_NAME, SCHEMA_VERSION

OTLP_LOGS_PATH = "/v1/logs"
OTLP_CONTENT_TYPE = "application/x-protobuf"
_KNOWN_ASSETS = {name: vendor for name, vendor, _ in SIGNATURES}
_REQUIRED = {
    "edgedisco.schema.version", "edgedisco.observation.id", "device.id",
    "asset.kind", "asset.name", "asset.vendor", "asset.running",
    "edgedisco.simulated",
}
_OPTIONAL = {"asset.host_app", "asset.relationship"}


def _validated_event(payload_json: str) -> tuple[int, dict[str, Any]]:
    if not isinstance(payload_json, str):
        raise ValueError("OTLP encoder requires serialized sanitized outbox payload")
    try:
        event = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid sanitized outbox payload") from exc
    if not isinstance(event, dict) or set(event) != {"timestamp", "event.name", "resource", "attributes"}:
        raise ValueError("unsupported outbox event fields")
    if event["event.name"] != EVENT_NAME or event["resource"] != {"service.name": "edgedisco"}:
        raise ValueError("unsupported outbox event identity")
    attrs = event["attributes"]
    if not isinstance(attrs, dict) or not _REQUIRED <= set(attrs) or set(attrs) - (_REQUIRED | _OPTIONAL):
        raise ValueError("unsupported outbox attributes")
    if type(attrs["edgedisco.schema.version"]) is not int or attrs["edgedisco.schema.version"] != SCHEMA_VERSION:
        raise ValueError("unsupported outbox schema version")
    event_id = attrs["edgedisco.observation.id"]
    device_id = attrs["device.id"]
    if not isinstance(event_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", event_id):
        raise ValueError("invalid observation ID")
    if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{32}", device_id):
        raise ValueError("invalid device ID")
    kind, name, vendor = attrs["asset.kind"], attrs["asset.name"], attrs["asset.vendor"]
    if not isinstance(kind, str) or kind not in {"application", "process", "agent_runtime"}:
        raise ValueError("invalid asset kind")
    if not isinstance(name, str) or _KNOWN_ASSETS.get(name) != vendor:
        raise ValueError("invalid asset signature")
    if type(attrs["asset.running"]) is not bool or type(attrs["edgedisco.simulated"]) is not bool:
        raise ValueError("invalid asset flags")
    if kind == "agent_runtime":
        host = attrs.get("asset.host_app")
        relationship = attrs.get("asset.relationship")
        if not isinstance(host, str) or host not in HOST_APP_NAMES | {"Direct/local"}:
            raise ValueError("invalid host application")
        if relationship != ("local_process" if host == "Direct/local" else "spawned_by"):
            raise ValueError("invalid relationship")
    elif set(attrs) & _OPTIONAL:
        raise ValueError("unexpected runtime attributes")
    try:
        timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timestamp requires timezone")
        utc = timestamp.astimezone(timezone.utc)
        nanos = calendar.timegm(utc.timetuple()) * 1_000_000_000 + utc.microsecond * 1_000
        if nanos <= 0:
            raise ValueError("invalid timestamp")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid outbox timestamp") from exc
    return nanos, attrs


def encode_outbox_event(payload_json: str) -> bytes:
    """Encode one validated outbox payload; reject all unknown fields and values."""
    nanos, attrs = _validated_event(payload_json)
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
    record.time_unix_nano = nanos
    record.observed_time_unix_nano = nanos
    record.severity_number = 9  # OTLP SEVERITY_NUMBER_INFO.
    record.severity_text = "INFO"
    record.event_name = EVENT_NAME
    for key in sorted(attrs):
        record.attributes.append(key_value(key, attrs[key]))
    return request.SerializeToString()
