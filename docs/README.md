# EdgeDisco documentation

The main [README](../README.md) is the product overview and quick start. This index points to the canonical guide for each task so setup commands and operational details do not need to be repeated across the documentation.

## Install and operate

- [macOS installation and operation](deployment-macos.md): prerequisites, installation, verification, upgrades, MCP, OTLP, services, logs, and removal.
- [Linux and WSL installation and operation](deployment-linux.md): prerequisites, installation, verification, upgrades, MCP, OTLP, systemd services, logs, and removal.

The platform guides own the commands for managed installations. Component references link back to those guides for routine setup.

## Integrations and interfaces

- [MCP inventory access](inventory-sync-mcp.md): tool contracts, authentication, pagination, freshness, and manual source setup.
- [OpenTelemetry integration](otel-integration.md): exporter behavior, configuration reference, payload mapping, delivery semantics, and troubleshooting.
- [Runtime adapters](runtime-adapters.md): optional local runtime connections and their security boundaries.

## Architecture, privacy, and security

- [Architecture and collected fields](architecture.md): components, data flow, storage, trust boundaries, and field inventory.
- [Fingerprinting](fingerprinting.md): path, command, and binary fingerprint behavior.
- [Production hardening](production-hardening.md): authentication, network exposure, proxying, and operational controls.
- [Security policy](../SECURITY.md): supported versions and private vulnerability reporting.

## Discovery behavior

- [Detection catalog](detection-catalog.md): supported tools and evidence sources.
- [Scanning performance](scanning-performance.md): scan scope, timing, and performance considerations.

## Plans

- [Container and VM discovery roadmap](container-vm-discovery-roadmap.md): planned container and virtual-machine evidence levels.

For source setup, test commands, and pull-request expectations, see [CONTRIBUTING.md](../CONTRIBUTING.md).
