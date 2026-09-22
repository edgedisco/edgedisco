# Contributing

Contributions that improve detection quality, privacy, portability, tests, or enterprise deployment are welcome.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
make check
.venv/bin/edgedisco demo
```

The first command uses a system Python only to create the project environment. Every later Python
command uses that environment explicitly. `make` automatically prefers `.venv/bin/python` when it
exists; set `PYTHON=/path/to/python` to test a different interpreter.

The command above installs the core package. To work on the optional integrations, run this from
the repository root:

```bash
.venv/bin/python -m pip install -e '.[mcp,otlp]'
```

The `mcp` extra requires Python 3.10+; the core supports Python 3.9+.

Self-service installs keep the package inside `~/.edgedisco/venv` as an implementation detail and expose a stable `edgedisco` command for normal Terminal sessions.

The core runtime intentionally has no third-party Python dependencies. Discuss any new core runtime dependency before adding it.

## Pull requests

- Keep each change focused and explain its compliance or operational value.
- Add or update tests for behavior changes.
- Preserve Python 3.9 compatibility.
- Do not add content capture, browser history collection, keystroke logging, screenshots, or raw secrets.
- Update the README or relevant document when behavior or deployment changes.
- Run `make check` before opening the pull request.

## Detection rules

Detection must be allowlisted and evidence-based. Avoid broad substrings that can match ordinary shell commands or unrelated applications. New signatures should include tests for both positive matches and likely false positives.

## Privacy changes

Any change that adds a collected field must document its purpose, endpoint source, whether the raw value leaves the endpoint, retention implications, and redaction or hashing behavior. Privacy-expanding changes require explicit maintainer review.

## Security reports

Do not open public issues for vulnerabilities. Follow [SECURITY.md](SECURITY.md).
