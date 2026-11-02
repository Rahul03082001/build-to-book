# Build-to-Book -- Manufacturing Data Pipeline & Inventory Reconciliation
#
# `make all` rebuilds everything from scratch. Each target below is also
# runnable on its own, in the order shown by `make help`.
#
# Targets marked PHASE n are stubs until that phase lands. They exit non-zero
# with a pointer to the phase, so a half-built pipeline can never look green.

.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# ---------------------------------------------------------------------------
# Configuration. Override on the command line: `make data PROFILE=full`
# ---------------------------------------------------------------------------
PROFILE       ?= demo
SEED          ?= 20261101
TARGET_SCHEMA ?= reporting
# Models build here and are swapped into reporting only if every critical
# check passes (ADR-014, ADR-021).
CANDIDATE_SCHEMA ?= publish_candidate
DBT_DIR       := warehouse/dbt
VENV          := .venv
PY            := $(VENV)/bin/python
PYTEST        := $(VENV)/bin/pytest
RUFF          := $(VENV)/bin/ruff
SQLFLUFF      := $(VENV)/bin/sqlfluff
DBT           := $(VENV)/bin/dbt

-include .env
export

GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo "nogit")

# A stub target prints what it will do and fails, so `make all` cannot report
# success for a phase that has not been built yet.
define NOT_YET
	@printf '\033[33m[not yet built]\033[0m %s\n' "$(1)"; \
	printf '  Scheduled for %s. See docs/DECISIONS.md and backlog/jira_backlog.csv.\n' "$(2)"; \
	exit 1
endef

.PHONY: help
help: ## Show this help
	@printf '\nBuild-to-Book -- make targets\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\nProfile: %s   Target schema: %s   Seed: %s\n\n' "$(PROFILE)" "$(TARGET_SCHEMA)" "$(SEED)"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
.PHONY: venv
venv: ## Create the virtualenv and install dependencies (uses uv if present)
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --python 3.12 $(VENV); \
		VIRTUAL_ENV=$(VENV) uv pip install -e '.[dbt,app,dev]'; \
	else \
		python3 -m venv $(VENV); \
		$(VENV)/bin/pip install --upgrade pip; \
		$(VENV)/bin/pip install -e '.[dbt,app,dev]'; \
	fi
	@printf '\nVirtualenv ready. Activate with: source $(VENV)/bin/activate\n'

.PHONY: up
up: ## Start the local PostgreSQL warehouse
	docker compose up -d --wait
	@printf 'Warehouse up on localhost:%s\n' "$${POSTGRES_PORT:-5432}"

.PHONY: down
down: ## Stop the warehouse (keeps the data volume)
	docker compose down

.PHONY: clean-db
clean-db: ## Stop the warehouse and DESTROY the data volume
	docker compose down -v

.PHONY: roles
roles: ## Create warehouse roles and grants (warehouse/sql/roles.sql)
	@scripts/psql.sh \
		-v dbname="$${POSTGRES_DB:-btb}" \
		-v loader_password="$${BTB_LOADER_PASSWORD:?set BTB_LOADER_PASSWORD in .env}" \
		-v transformer_password="$${BTB_TRANSFORMER_PASSWORD:?set BTB_TRANSFORMER_PASSWORD in .env}" \
		-v reporter_password="$${BTB_REPORTER_PASSWORD:?set BTB_REPORTER_PASSWORD in .env}" \
		-v auditor_password="$${BTB_AUDITOR_PASSWORD:?set BTB_AUDITOR_PASSWORD in .env}" \
		-f - < warehouse/sql/roles.sql
	@printf 'Roles applied: loader, transformer, reporter, auditor\n'

.PHONY: psql
psql: ## Open a psql shell on the warehouse
	@scripts/psql.sh

.PHONY: schemas
schemas: roles raw-schema audit-schema ## Apply roles and all schemas to a running warehouse
	@printf 'Schemas and roles up to date\n'

.PHONY: init
init: up schemas ## Start the warehouse, then apply roles and schemas

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
.PHONY: data
data: ## Generate synthetic source extracts + control totals
	$(PY) -m generator --profile $(PROFILE) --seed $(SEED)

.PHONY: load
load: ## Validate and incrementally load extracts into raw
	$(PY) -m ingestion --data-dir $(or $(DATA_DIR),data)

.PHONY: audit-schema
audit-schema: ## Create the append-only audit tables
	@scripts/psql.sh -q -f - < warehouse/sql/audit.sql
	@printf 'Audit schema applied (append-only enforced)\n'

.PHONY: raw-schema
raw-schema: ## Create the raw landing tables
	@scripts/psql.sh -q -f - < warehouse/sql/raw.sql
	@printf 'Raw schema applied\n'

DBT_FLAGS = --project-dir $(DBT_DIR) --profiles-dir $(DBT_DIR)

.PHONY: dbt-deps
dbt-deps: ## Install dbt package dependencies
	$(DBT) deps $(DBT_FLAGS)

.PHONY: build
build: ## Run dbt build (models + tests) into the candidate schema
	@# `dbt build`, not `dbt run`: it interleaves tests with models and fails
	@# on a failing test, so a model that breaks a business rule cannot be
	@# published (ADR-010).
	BTB_TARGET_SCHEMA=$(CANDIDATE_SCHEMA) $(DBT) build $(DBT_FLAGS)

.PHONY: dbt-test
dbt-test: ## Run dbt tests only, without rebuilding
	BTB_TARGET_SCHEMA=$(CANDIDATE_SCHEMA) $(DBT) test $(DBT_FLAGS)

.PHONY: controls
controls: ## Run reconciliations and record results  [PHASE 1]
	$(call NOT_YET,Run reconciliations into audit.reconciliation_results,Phase 1 (BTB-22))

.PHONY: publish
publish: ## Swap a validated build into reporting if all checks pass  [PHASE 1]
	$(call NOT_YET,Publish gate: swap publish_candidate into reporting,Phase 1 (BTB-23))

.PHONY: lineage
lineage: ## Generate column-level KPI lineage with sqlglot  [PHASE 5]
	$(call NOT_YET,Trace certified KPI columns to raw source columns,Phase 5 (BTB-58))

.PHONY: export
export: ## Write certified reporting tables to exports/ with a manifest  [PHASE 6]
	$(call NOT_YET,Export certified datasets as CSV + Parquet,Phase 6 (BTB-66))

.PHONY: evidence
evidence: ## Generate an IPE evidence pack for a run  [PHASE 4]
	$(call NOT_YET,Write evidence/<run_id>/ for a certified run,Phase 4 (BTB-53))

.PHONY: app
app: ## Run the Streamlit data trust console locally  [PHASE 1]
	$(call NOT_YET,Serve the trust console on localhost:8501,Phase 1 (BTB-24))

.PHONY: dbt-docs
dbt-docs: ## Generate and serve dbt docs  [PHASE 5]
	$(call NOT_YET,dbt docs generate + serve,Phase 5 (BTB-57))

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
.PHONY: lint
lint: ## Lint Python (ruff) and SQL (sqlfluff)
	$(RUFF) check .
	$(RUFF) format --check .
	@if [ -n "$$(find $(DBT_DIR)/models -name '*.sql' -print -quit 2>/dev/null)" ]; then \
		$(SQLFLUFF) lint $(DBT_DIR)/models $(DBT_DIR)/tests; \
	else \
		printf 'sqlfluff: no dbt models yet, skipping\n'; \
	fi

.PHONY: format
format: ## Auto-fix formatting
	$(RUFF) check --fix .
	$(RUFF) format .

.PHONY: test
test: ## Run the test suite (unit tests; excludes integration)
	$(PYTEST) -m "not integration"

.PHONY: test-all
test-all: ## Run every test, including those needing a live database
	$(PYTEST)

.PHONY: check
check: lint test ## Lint and test -- what CI runs on every PR

# ---------------------------------------------------------------------------
# Full rebuild
# ---------------------------------------------------------------------------
.PHONY: all
all: init data load build controls publish lineage export evidence test ## Rebuild everything from scratch
	@printf '\n\033[32mFull rebuild complete.\033[0m Profile=%s Commit=%s\n' "$(PROFILE)" "$(GIT_SHA)"
