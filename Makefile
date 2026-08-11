.PHONY: help install install-dev test lint fmt sim preflight scout brain console clean backup

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
MCTB := $(VENV)/bin/mctb

help:
	@echo "MCTB — make targets"
	@echo
	@echo "  install       install the package into $(VENV)"
	@echo "  install-dev   install with dev + all optional extras"
	@echo "  test          run the test suite"
	@echo "  lint          ruff check"
	@echo "  fmt           ruff format + autofix"
	@echo "  sim           run the offline simulator"
	@echo "  preflight     check dependencies and endpoints"
	@echo "  scout/brain/console   run a service in the foreground"
	@echo "  backup        snapshot the trade database"
	@echo "  clean         remove build artifacts and caches"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip setuptools wheel

install: $(VENV)
	$(PIP) install -e .

install-dev: $(VENV)
	$(PIP) install -e ".[trade,console,dev]"

test:
	$(VENV)/bin/pytest -q

lint:
	$(VENV)/bin/ruff check src tests

fmt:
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

sim:
	$(MCTB) simulate --tokens 150

preflight:
	$(MCTB) preflight

scout:
	$(MCTB) scout

brain:
	$(MCTB) brain

console:
	$(MCTB) console

# The database is the bot's learned memory. Losing it means starting over.
backup:
	@mkdir -p backups
	@test -f data/mctb.db && \
	  sqlite3 data/mctb.db ".backup 'backups/mctb-$$(date +%Y%m%d-%H%M%S).db'" && \
	  echo "backed up to backups/" || echo "no database yet at data/mctb.db"

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
