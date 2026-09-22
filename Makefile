.PHONY: test check build test-rust check-rust build-rust test-python check-python build-python swift-test macos-package-check clean macos-pkg

# Rust is the active implementation. The default targets must not pull the
# legacy Python suite into every Rust iteration.
test: test-rust
check: check-rust
build: build-rust

test-rust:
	cargo test --workspace

check-rust: test-rust
	cargo fmt --all --check
	cargo clippy --workspace --all-targets --all-features -- -D warnings

build-rust:
	cargo build --workspace --release

# Legacy Python validation is explicit and remains available for compatibility
# and for cards whose acceptance criteria still cover the Python port.
# Prefer the checkout's virtual environment without requiring activation.
# Override explicitly when validating another interpreter, for example:
#   make PYTHON=python3.9 check-python
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

test-python:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

check-python: test-python
	$(PYTHON) -m compileall -q src tests
	$(PYTHON) -c "import ast, pathlib; [ast.parse(p.read_text(), filename=str(p), feature_version=(3, 9)) for p in pathlib.Path('src').rglob('*.py')]; print('Python 3.9 syntax check passed')"
	bash -n install.sh scripts/status-macos.sh scripts/uninstall-macos.sh

build-python: check-python
	$(PYTHON) -m pip wheel . --no-deps --no-build-isolation -w dist

# Native Swift packages are kept independent so each can also be built and
# tested directly with Swift Package Manager.
swift-test:
	cd macos/EdgeDiscoIPC && swift test
	cd macos/EdgeDiscoMenuBar && swift test

# Narrow macOS packaging gate. This is intentionally separate from the fast
# Rust loop and from the full legacy Python compatibility suite.
macos-package-check:
	$(PYTHON) -m pytest -q packaging/macos/tests/test_packaging.py packaging/macos/tests/test_uninstall.py

export VERSION
macos-pkg:
	@test -n "$${VERSION:-}" || (printf '%s\n' 'VERSION is required (example: make macos-pkg VERSION=0.1.0)' >&2; exit 64)
	./packaging/macos/build-pkg.sh "$$VERSION"

clean:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p) for p in pathlib.Path('.').glob('build')]; [shutil.rmtree(p) for p in pathlib.Path('src').glob('*.egg-info')]"
