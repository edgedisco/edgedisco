.PHONY: test check build clean

# Prefer the checkout's virtual environment without requiring activation.
# Override explicitly when validating another interpreter, for example:
#   make PYTHON=python3.9 check
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# Canonical test entrypoint for a source checkout.
# The package uses a src/ layout, so bare
#   $(PYTHON) -m unittest discover -s tests -v
# fails with ModuleNotFoundError unless the package is installed into the
# active environment. This target sets the import path for you.
test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m compileall -q src tests
	$(PYTHON) -c "import ast, pathlib; [ast.parse(p.read_text(), filename=str(p), feature_version=(3, 9)) for p in pathlib.Path('src').rglob('*.py')]; print('Python 3.9 syntax check passed')"
	bash -n install.sh scripts/status-macos.sh scripts/uninstall-macos.sh

build: check
	$(PYTHON) -m pip wheel . --no-deps --no-build-isolation -w dist

clean:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p) for p in pathlib.Path('.').glob('build')]; [shutil.rmtree(p) for p in pathlib.Path('src').glob('*.egg-info')]"
