# Inventory sync over MCP

EdgeDisco's read-only MCP service exposes two tools for external inventory consumers:

- `inventory_snapshot(watermark?, after?, limit?)` returns at most 500 current asset records per page. Begin with no watermark and no `after`. Reuse the returned `watermark` on each later page, passing `next_after` as `after`, until `next_after` is null.
- `inventory_changes(cursor?, limit?)` returns ordered `upsert` and `delete` changes after the cursor. Begin at the snapshot watermark. Save `next_cursor` after processing a page; continue immediately while `has_more` is true, then poll later.

The snapshot watermark fixes a consistent point in the change history, so a scan occurring during pagination appears in the change feed. Records have a stable `source_asset_id` and contain only the allowlisted fields from EdgeDisco's outbound asset projection. An absent asset produces a `delete` change. Simulated demo assets are explicitly labeled.

The MCP server listens on localhost only. For a remote consumer, place it behind an authenticated HTTPS proxy, restrict the network path, and use a credential dedicated to the consumer. EdgeDisco records each tool invocation in the MCP audit log.

This feed is for inventory synchronization. It does not publish directly into a third-party inventory API or provide prompt and response traces. The consuming platform maps EdgeDisco records to its own asset model.
