.PHONY: test check build clean

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

check: test
	python3 -m compileall -q src tests
	python3 -c "import ast, pathlib; [ast.parse(p.read_text(), filename=str(p), feature_version=(3, 9)) for p in pathlib.Path('src').rglob('*.py')]; print('Python 3.9 syntax check passed')"
	bash -n install.sh scripts/status-macos.sh scripts/uninstall-macos.sh

build: check
	python3 -m pip wheel . --no-deps --no-build-isolation -w dist

clean:
	python3 -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p) for p in pathlib.Path('.').glob('build')]; [shutil.rmtree(p) for p in pathlib.Path('src').glob('*.egg-info')]"
