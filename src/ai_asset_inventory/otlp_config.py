"""Explicit, secret-safe configuration for the OTLP/HTTP Logs worker."""
from __future__ import annotations

import ipaddress
import os
import re
import ssl
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ExportConfig:
    endpoint: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    context: ssl.SSLContext | None = field(default=None, repr=False)
    compression: str = ""
    timeout: float = 10
    batch_records: int = 100
    batch_bytes: int = 1048576
    poll_interval: float = 1
    lease_seconds: int = 60
    shutdown_grace: int = 15
    delivered_days: int = 1
    failed_days: int = 7
    enabled: bool = False

    @classmethod
    def from_env(cls, env=None, *, require_enabled=True):
        env = os.environ if env is None else env

        def fail(key):
            raise ConfigurationError(f"Invalid {key}") from None

        def setting(suffix, default=""):
            specific = "OTEL_EXPORTER_OTLP_LOGS_" + suffix
            general = "OTEL_EXPORTER_OTLP_" + suffix
            key = specific if specific in env else general
            return key, env.get(key, default)

        def integer(key, default, low=1, high=2147483647):
            try:
                value = int(env.get(key, default))
                if not low <= value <= high:
                    fail(key)
                return value
            except (ValueError, TypeError):
                fail(key)

        for key in ("EDGEDISCO_OTLP_EXPORT_ENABLED", "EDGEDISCO_OTLP_OUTBOX_ENABLED"):
            if env.get(key, "false") not in ("true", "false"):
                fail(key)
        enabled = env.get("EDGEDISCO_OTLP_EXPORT_ENABLED") == "true"
        if require_enabled and not enabled:
            fail("EDGEDISCO_OTLP_EXPORT_ENABLED (must be true)")
        if enabled and env.get("EDGEDISCO_OTLP_OUTBOX_ENABLED") != "true":
            fail("EDGEDISCO_OTLP_OUTBOX_ENABLED (required for export)")
        key, endpoint = setting("ENDPOINT")
        if not endpoint and (enabled or require_enabled):
            fail(key)
        if endpoint:
            try:
                parts = urlsplit(endpoint)
                host = parts.hostname
                if (not host or parts.username is not None or parts.password is not None
                        or "?" in endpoint or "#" in endpoint or "\\" in endpoint
                        or any(ord(c) <= 32 or ord(c) >= 127 for c in endpoint)
                        or parts.scheme not in ("http", "https")):
                    fail(key)
                if parts.port is not None and not 1 <= parts.port <= 65535:
                    fail(key)
                if parts.scheme == "http" and not ipaddress.ip_address(host).is_loopback:
                    fail(key)
                if key == "OTEL_EXPORTER_OTLP_ENDPOINT":
                    endpoint = urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/v1/logs"))
            except ValueError:
                fail(key)
        key, protocol = setting("PROTOCOL", "http/protobuf")
        if protocol != "http/protobuf":
            fail(key)
        key, compression = setting("COMPRESSION")
        if compression not in ("", "gzip"):
            fail(key)
        key, raw_headers = setting("HEADERS")
        headers = {}
        reserved = {"content-type", "content-length", "content-encoding", "host", "connection",
                    "user-agent", "transfer-encoding", "trailer", "te", "upgrade", "cookie",
                    "proxy-authorization", "proxy-connection"}
        if raw_headers:
            for item in raw_headers.split(","):
                name, sep, value = item.strip().partition("=")
                name = name.strip().lower()
                if (not sep or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name)
                        or name in headers or name in reserved or ";" in value
                        or re.search(r"%(?![0-9a-fA-F]{2})", value)):
                    fail(key)
                value = unquote(value.strip())
                if any(ord(c) < 32 or ord(c) > 126 for c in value):
                    fail(key)
                headers[name] = value
        timeout_key, _ = setting("TIMEOUT", "10000")
        timeout = integer(timeout_key, 10000) / 1000
        lease = integer("EDGEDISCO_OTLP_LEASE_SECONDS", 60)
        if lease <= timeout:
            fail("EDGEDISCO_OTLP_LEASE_SECONDS (must exceed request timeout)")

        def certificate(suffix, private=False):
            key, value = setting(suffix)
            if value:
                try:
                    mode = Path(value).stat().st_mode
                    if not stat.S_ISREG(mode) or mode & (0o077 if private else 0o022):
                        fail(key)
                except OSError:
                    fail(key)
            return key, value

        ca_key, ca = certificate("CERTIFICATE")
        cert_key, cert = certificate("CLIENT_CERTIFICATE")
        key_key, client_key = certificate("CLIENT_KEY", True)
        if bool(cert) != bool(client_key):
            fail(cert_key + " / " + key_key)
        context = None
        if endpoint.startswith("https:") or ca or cert:
            try:
                context = ssl.create_default_context(cafile=ca or None)
            except (OSError, ValueError):
                fail(ca_key)
            if cert:
                try:
                    # Explicit password avoids an interactive prompt on encrypted keys.
                    context.load_cert_chain(cert, client_key, password="")
                except (OSError, ValueError):
                    fail(cert_key + " / " + key_key)
        return cls(endpoint=endpoint, headers=headers, context=context, compression=compression,
                   timeout=timeout, lease_seconds=lease, enabled=enabled,
                   batch_records=integer("EDGEDISCO_OTLP_BATCH_RECORDS", 100, high=1000),
                   batch_bytes=integer("EDGEDISCO_OTLP_BATCH_BYTES", 1048576),
                   poll_interval=integer("EDGEDISCO_OTLP_POLL_INTERVAL_MS", 1000) / 1000,
                   shutdown_grace=integer("EDGEDISCO_OTLP_SHUTDOWN_GRACE_SECONDS", 15, low=0),
                   delivered_days=integer("EDGEDISCO_OTLP_DELIVERED_RETENTION_DAYS", 1),
                   failed_days=integer("EDGEDISCO_OTLP_FAILED_RETENTION_DAYS", 7))
