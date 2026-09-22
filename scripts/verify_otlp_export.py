#!/usr/bin/env python3
"""Opt-in integration test against the local otel-stack (writes simulated logs)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler

from ai_asset_inventory.database import Database
from ai_asset_inventory.otlp_encoder import encode_outbox_event
from ai_asset_inventory.otlp_config import ExportConfig
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4318/v1/logs")
    parser.add_argument("--loki", default="http://127.0.0.1:3100")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = args.output_dir or Path(tempfile.mkdtemp(prefix="edgedisco-export-integration-"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / "inventory.db"
    if path.exists():
        parser.error("output directory already contains inventory.db")
    # Isolate the test from ambient credentials and production exporter settings.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OTEL_EXPORTER_OTLP_", "EDGEDISCO_OTLP_"))}
    env.update(EDGEDISCO_OTLP_EXPORT_ENABLED="true", EDGEDISCO_OTLP_OUTBOX_ENABLED="true",
               OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=args.endpoint)
    ExportConfig.from_env(env)
    db = Database(path, otlp_enabled=True)
    device, _ = db.enroll({"hostname": "exporter-integration-test", "os": "test"})
    expected = {}
    for index, (running, version) in enumerate(((True, None), (False, None), (True, "1.2.3"), (True, "2.0.0"), (False, "absent"), (True, "2.0.0"))):
        db.ingest(device, {"scan_id": f"export-integration-{index}",
            "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            "assets": [] if version == "absent" else [{"fingerprint": "integration-runtime", "kind": "agent_runtime", "name": "CrewAI", "vendor": "CrewAI",
                        "version": version, "running": running, "metadata": {"host_app": "Cursor", "relationship": "spawned_by",
                        "demo_lab": True, "evidence_label": "SIMULATED TEST WORKLOADS"}}]})
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM otlp_outbox WHERE status='pending'").fetchall()
        for row in rows:
            record = ExportLogsServiceRequest.FromString(encode_outbox_event(row['payload_json'])).resource_logs[0].scope_logs[0].log_records[0]
            attrs = {a.key.replace('.', '_'): str(getattr(a.value, a.value.WhichOneof('value'))).lower()
                     if a.value.WhichOneof('value') == 'bool_value' else str(getattr(a.value, a.value.WhichOneof('value')))
                     for a in record.attributes}
            expected[row['id']] = {"attributes": attrs, "timestamp": str(record.time_unix_nano),
                                   "observed_timestamp": str(record.observed_time_unix_nano),
                                   "event_name": record.event_name}
        env['OTEL_EXPORTER_OTLP_LOGS_COMPRESSION'] = "gzip" if index % 2 else ""
        subprocess.run([sys.executable, '-m', 'ai_asset_inventory', 'otlp-export', '--db', str(path), '--once'], env=env, check=True)
        with db.connect() as conn:
            for row in rows:
                status = conn.execute("SELECT status FROM otlp_outbox WHERE id=?", (row['id'],)).fetchone()[0]
                assert status == 'delivered', f"Expected delivered, got {status}"
    opener = build_opener(ProxyHandler({}))
    query = '{service_name="edgedisco"} | device_id = "' + device + '"'
    url = args.loki.rstrip('/') + '/loki/api/v1/query_range?' + urlencode({'query': query, 'since': '15m', 'limit': 100})
    deadline = time.monotonic() + 30
    found = {}
    while time.monotonic() < deadline:
        with opener.open(url, timeout=5) as response:
            result = json.load(response)
        for stream in result['data']['result']:
            for value in stream['values']:
                metadata = {**stream['stream'], **(value[2] if len(value) > 2 else {})}
                event_id = metadata.get('edgedisco_observation_id')
                if event_id in expected:
                    wanted = expected[event_id]
                    for key, expected_value in wanted['attributes'].items():
                        assert metadata.get(key) == expected_value, f"Metadata mismatch: {key}"
                    assert metadata.get('observed_timestamp') == wanted['observed_timestamp'], 'Observed timestamp mismatch'
                    assert value[0] == wanted['timestamp'], 'Event timestamp mismatch'
                    assert value[0] != metadata['observed_timestamp'], 'Timestamps should be distinct'
                    if wanted['event_name'] == 'edgedisco.asset.observed':
                        assert 'edgedisco.asset.observed: CrewAI' in value[1], 'Missing transformed body'
                    else:
                        assert 'edgedisco.device.inventory' in value[1], 'Missing device heartbeat body'
                    found[event_id] = metadata
        if len(found) == len(expected):
            break
        time.sleep(0.5)
    assert len(found) == len(expected), f"Loki stored {len(found)}/{len(expected)} events"
    summary = {"endpoint": args.endpoint, "device_id": device, "delivered": len(expected), "verified_in_loki": len(found),
               "checks": ["running", "stopped", "absent", "reappeared", "device_heartbeat", "versions", "simulation", "host_relationship", "event_timestamp", "observed_timestamp", "gzip"],
               "database": str(path), "logql": query}
    (root / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
