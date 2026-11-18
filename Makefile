PY=python3
run:
	PYTHONPATH=src $(PY) -m timetablegrab.cli
test:
	@$(PY) -c "import pytest" 2>/dev/null && PYTHONPATH=src $(PY) -m pytest -q \
	  || PYTHONPATH=src $(PY) -I tests/_pytest_shim.py
clean:
	rm -rf .pytest_cache **/__pycache__ *.db var
