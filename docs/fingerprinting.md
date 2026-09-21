# Binary fingerprinting

EdgeDisco keeps logical identity, privacy fingerprints, and binary integrity evidence separate:

| Evidence | Current method | Purpose |
| --- | --- | --- |
| Logical asset fingerprint | SHA-256 over product, evidence kind, and a hashed location key | Keep two installations distinct without using content as identity |
| Path fingerprint | SHA-256 of the local path | Correlate repeated observations without transmitting the path |
| Command fingerprint | SHA-256 after positional values and option values are removed | Group equivalent process shapes without retaining prompts or arguments |
| Binary content fingerprint | SHA-256 of the exact regular file | Compare executable bytes across scans and against reviewed catalog entries |

Plain path and command hashes reduce exposure but are not anonymization: predictable low-entropy values can be guessed offline. A future schema should use HMAC-SHA-256 with a separate random per-device key when only endpoint continuity is needed, or a tenant key when fleet-wide correlation is required. Binary content SHA-256 must remain unkeyed so it can match a shared library.

SHA-256 is the initial content algorithm because it is widely interoperable, available in the Python standard library, and specified by [NIST's Secure Hash Standard](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) for change detection. SHA-512 adds digest size without a practical benefit here. BLAKE3 may be faster but adds a dependency and is less suitable for environments requiring standardized algorithms. Fuzzy hashes such as ssdeep or TLSH can support investigation but must not make allow/block or integrity decisions.

## Static fingerprint library

[`fingerprints.json`](../src/ai_asset_inventory/fingerprints.json) is packaged with the collector and loaded without a network request. It contains product identity rules, executable names, package markers, source links, and versioned binary hashes where available. Factory Droid 0.223.0 hashes come from the checksums published by its official installer download service; other products currently have empty hash lists. Maintainers may add a binary entry only when its provenance is an official release artifact or checksum.

Each binary entry records:

- SHA-256 digest
- Exact product version and distribution
- Platform and architecture
- First-party provenance URL

A digest is `matched` only when it appears under that product. A readable artifact without a reviewed catalog digest is `unlisted`, not `mismatch` or malicious. Before introducing a `mismatch` state, matching must also require the exact version, distribution, platform, and architecture so package-manager wrappers and legitimate rebuilds do not create false tamper alerts.

Factory Droid is an identification exception: the ambiguous `droid` filename must match a published Factory hash before an asset is emitted. An unknown hash is skipped, not reported as malicious. This requirement applies to both installed entry points and running processes.

Library updates must keep the schema version explicit, use lowercase 64-character SHA-256 values, retain first-party provenance, update the review date, and pass the catalog validation tests. Runtime scans never download or update the library themselves.

## Least-privilege hashing boundary

Content hashing happens only after discovery and only for regular files in approved install roots such as `/Applications`, `~/Applications`, `/opt/homebrew`, `/usr/local`, and the documented user CLI directories. EdgeDisco:

- Never recursively hashes an application bundle or home directory.
- Checks each symlink destination before following it; the allowlist itself is never expanded by resolving a symlinked root. The same path policy guards application metadata, MCP discovery, static-cache watchers, and setup's bounded adapter detection.
- Refuses special files and files larger than 512 MiB.
- Enforces the total byte limit during reading, even if the file grows, and verifies file identity and metadata before and after reading. POSIX file opens refuse symlink traversal and use nonblocking mode so a substituted FIFO cannot hang hashing.
- Caches a digest by resolved path, device, inode, size, modification time, and change time for the collector process.
- Treats permission errors and disappearing or changing files as a skipped observation; it never retries with `sudo` or asks for broader access.

MCP configuration contents, prompts, transcripts, workspaces, shell history, browser/editor state, process memory, Desktop, Documents, Downloads, iCloud Drive, network volumes, removable media, and other users' files are not content-hashed.

On macOS, ordinary reads in protected user-content locations can invoke [Files & Folders privacy controls](https://support.apple.com/guide/security/secddd1d86a6/web), so those locations are explicitly outside the scan. The per-user collector does not request Full Disk Access, Accessibility, Automation, Screen Recording, Input Monitoring, or Endpoint Security access. macOS can still show its normal Background Items notice when the LaunchAgents are installed.

## Additional validation signals

Content hashes are strong byte-level evidence but change on every legitimate release. Useful future corroboration includes:

- macOS code-signing validity, bundle identifier, Team ID, designated requirement, and CodeDirectory hash.
- Windows Authenticode publisher and certificate identity.
- Mach-O UUID, ELF build ID, or PE version resource.
- Package-manager integrity data such as npm integrity values or Python wheel `RECORD` entries.
- A signed fingerprint-library release so the catalog itself has provenance.

These signals should be collected only from already-discovered artifacts and must not trigger recursive searches, protected-directory access, process-memory inspection, or elevated privileges. On macOS, Apple's documented [`codesign` verification](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/Procedures/Procedures.html) can confirm sealed code and its designated requirement, but it should be applied only to the discovered path and treated as complementary evidence rather than a replacement for SHA-256.
