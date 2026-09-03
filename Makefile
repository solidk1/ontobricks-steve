# Makefile for OntoBricks (FastAPI)
#
# OntoBricks runs as an ordinary container against an ordinary PostgreSQL
# database. Configuration is entirely environment-driven — see `.env.example`
# for the full contract. There is no bundle and no platform-specific deploy
# target; start the app with `make run` (or `python run.py`) and point
# PGHOST/PGDATABASE/PGUSER at your server.

.PHONY: help install test test-cov scenario-campaign run dev prod setup format lint clean

# Output dir for the live scenario campaign reports (JUnit + HTML).
SCENARIO_ARTIFACTS := artifacts/scenarios
# Target app for the campaign; override to hit a deployed instance:
#   make scenario-campaign ONTOBRICKS_LIVE_BASE=https://<app-url>
ONTOBRICKS_LIVE_BASE ?= http://localhost:8000

help:
	@echo "OntoBricks (FastAPI) - Available commands:"
	@echo ""
	@echo "  Development:"
	@echo "    make install      - Install dependencies"
	@echo "    make run          - Run the application locally"
	@echo "    make dev          - Run in development mode with auto-reload"
	@echo "    make setup        - Complete setup (install + configure)"
	@echo ""
	@echo "  Testing:"
	@echo "    make test              - Run tests"
	@echo "    make test-cov          - Run tests with coverage"
	@echo "    make scenario-campaign - Run the live E2E scenario campaign (opt-in, billable)"
	@echo "                             → JUnit + HTML reports in $(SCENARIO_ARTIFACTS)/"
	@echo "                             App must be running (make dev); override target with"
	@echo "                             ONTOBRICKS_LIVE_BASE=<url>"
	@echo ""
	@echo "  Code Quality:"
	@echo "    make format       - Format code with black"
	@echo "    make lint         - Lint code with flake8"
	@echo ""
	@echo "  Maintenance:"
	@echo "    make clean        - Remove generated files"
	@echo ""

install:
	@echo "Installing dependencies..."
	uv venv
	uv sync --frozen --extra lakebase --extra pitfalls

setup:
	@echo "Running setup..."
	chmod +x scripts/setup.sh
	scripts/setup.sh

run:
	@echo "Starting OntoBricks (FastAPI)..."
	. .venv/bin/activate && python run.py

test:
	@echo "Running tests..."
	. .venv/bin/activate && pytest

test-cov:
	@echo "Running tests with coverage..."
	. .venv/bin/activate && pytest --cov=src --cov-report=html --cov-report=term

# ── Integration test campaign (live, opt-in scenario suites) ─────────────
# Runs every `scenario`-marked suite under tests/e2e/scenarios/ end-to-end,
# in filename order (test_scenario_1 → 2 → 3 → … → test_scenario_validation),
# against a RUNNING app and writes machine + human reports to
# $(SCENARIO_ARTIFACTS)/ (campaign.xml for CI, campaign.html to open).
#
# These are billable (warehouse + LLM) and mutate the registry the app reads,
# so they stay opt-in: this target sets ONTOBRICKS_SCENARIO_LIVE=1 for you.
# Point at another instance with `ONTOBRICKS_LIVE_BASE=<url>`. Preflight the
# app health before spending money.
#
# Run the target app with auto-reload OFF (`scripts/start.sh --no-reload`):
# Auto-Map and the KG build run for minutes in background threads whose state
# lives in the in-memory TaskManager, so any src/ save mid-campaign restarts
# uvicorn, kills the thread and makes the run fail with a misleading timeout.
scenario-campaign:
	@echo "Scenario campaign → $(ONTOBRICKS_LIVE_BASE)"
	@curl -sf "$(ONTOBRICKS_LIVE_BASE)/health" >/dev/null 2>&1 \
	  || curl -sf "$(ONTOBRICKS_LIVE_BASE)/healthz" >/dev/null 2>&1 \
	  || { echo "ERROR: no app reachable at $(ONTOBRICKS_LIVE_BASE) — start it (make dev) or set ONTOBRICKS_LIVE_BASE"; exit 1; }
	@# uvicorn's reloader is a supervisor that forks the real server, so a
	@# reload-enabled `run.py` has child processes while a no-reload one does
	@# not. Matching on "--reload"/"watchfiles" misses it: the children are
	@# plain `python3` and reload is enabled in-code, not on the command line.
	@for pid in $$(pgrep -f "[r]un.py" 2>/dev/null); do \
	  if pgrep -P $$pid >/dev/null 2>&1; then \
	    echo "WARNING: app (pid $$pid) is running WITH auto-reload — a src/ edit mid-campaign will kill Auto-Map and fail the run with a misleading timeout."; \
	    echo "         Restart it first: scripts/stop.sh && ONTOBRICKS_NO_RELOAD=1 .venv/bin/python run.py"; \
	  fi; \
	done
	@mkdir -p $(SCENARIO_ARTIFACTS)
	@echo "Running live scenarios (JUnit + HTML → $(SCENARIO_ARTIFACTS)/)..."
	. .venv/bin/activate && \
	  ONTOBRICKS_SCENARIO_LIVE=1 ONTOBRICKS_SCENARIO_CHAIN=1 \
	  ONTOBRICKS_LIVE_BASE="$(ONTOBRICKS_LIVE_BASE)" \
	  pytest tests/e2e/scenarios -m scenario -v -s --no-cov -p no:randomly \
	    --junitxml=$(SCENARIO_ARTIFACTS)/campaign.xml \
	    --html=$(SCENARIO_ARTIFACTS)/campaign.html --self-contained-html
	@echo "Reports: $(SCENARIO_ARTIFACTS)/campaign.html  (JUnit: $(SCENARIO_ARTIFACTS)/campaign.xml)"
	@echo "         $(SCENARIO_ARTIFACTS)/campaign_report.md  (validation summary, if it ran)"

format:
	@echo "Formatting code..."
	. .venv/bin/activate && black src/ tests/

lint:
	@echo "Linting code..."
	. .venv/bin/activate && flake8 src/ tests/ --max-line-length=100

clean:
	@echo "Cleaning up..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage
	rm -rf $(SCENARIO_ARTIFACTS) artifacts
	rm -rf flask_session fastapi_session
	@echo "Clean complete!"

dev:
	@echo "Starting development server with auto-reload..."
	. .venv/bin/activate && python run.py

prod:
	@echo "Starting production server..."
	. .venv/bin/activate && ONTOBRICKS_CONTAINERIZED=true python run.py
