set dotenv-load := true

python := "python3"
pytest := "python3 -m pytest"

# ── Default ───────────────────────────────────────────────────────────────────

default:
    @just --list

# ── Environment ───────────────────────────────────────────────────────────────

# Create venv and install all dev dependencies
bootstrap:
    {{ python }} -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    @echo "Run: source .venv/bin/activate"

# Install/sync dependencies into current environment
install:
    pip install -e ".[dev]"

# ── Linting and types ─────────────────────────────────────────────────────────

lint:
    ruff check cygnet tests

lint-fix:
    ruff check --fix cygnet tests

fmt:
    ruff format cygnet tests

fmt-check:
    ruff format --check cygnet tests

typecheck:
    mypy cygnet

# ── Tests ─────────────────────────────────────────────────────────────────────

# Unit + builder + mapping tests (no database required)
test:
    {{ pytest }} tests/ --ignore=tests/integration \
        -v --cov=cygnet --cov-report=term-missing

# Integration tests — expects CYGNET_TEST_DSN in environment or .env
test-integration:
    {{ pytest }} tests/integration -v -m integration

# Full suite — spins up Docker PostgreSQL, runs everything, tears it down
test-all: pg-up
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'just pg-down' EXIT
    export CYGNET_TEST_DSN="postgresql://cygnet:cygnet@localhost:5555/cygnet_test"
    {{ pytest }} tests/ -v --cov=cygnet --cov-report=term-missing

# Full pre-push check: fmt, lint, types, unit tests
check: fmt-check lint typecheck test

# ── Benchmarks ────────────────────────────────────────────────────────────────
# Bench dependencies live in the [bench] extra so regular dev installs
# stay light.  Run `just bootstrap-bench` once to install them.

# Add the bench dependency group to the existing venv.
bootstrap-bench:
    .venv/bin/pip install -e ".[bench]"

# Render + overhead benchmarks (no DB needed).  Fast (~5s); good for
# regression-checking Cygnet's hot rendering paths during development.
bench:
    {{ pytest }} bench/test_render.py bench/test_overhead.py \
        --benchmark-only

# E2E benchmarks against a Dockerised PG.  Spins up the container,
# runs against it, tears it down.  Slower (~30s) — these measure
# total wall time including PG round-trip.
bench-e2e: pg-up
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'just pg-down' EXIT
    export CYGNET_TEST_DSN="postgresql://cygnet:cygnet@localhost:5555/cygnet_test"
    {{ pytest }} bench/test_e2e.py --benchmark-only

# Full bench suite: render + overhead + e2e + cross-ORM comparison.
bench-all: pg-up
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'just pg-down' EXIT
    export CYGNET_TEST_DSN="postgresql://cygnet:cygnet@localhost:5555/cygnet_test"
    {{ pytest }} bench/ --benchmark-only \
        --benchmark-json=bench-result.json \
        --benchmark-columns=median,min,max,iqr,ops

# ── Docker PostgreSQL ─────────────────────────────────────────────────────────

pg-up:
    docker run --rm -d \
        --name cygnet-test-pg \
        -e POSTGRES_USER=cygnet \
        -e POSTGRES_PASSWORD=cygnet \
        -e POSTGRES_DB=cygnet_test \
        -p 5555:5432 \
        postgres:16-alpine \
        postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off
    @echo "Waiting for PostgreSQL..."
    @until docker exec cygnet-test-pg pg_isready -U cygnet -q; do sleep 0.2; done
    @echo "PostgreSQL ready."

pg-down:
    docker stop cygnet-test-pg 2>/dev/null || true

pg-psql:
    docker exec -it cygnet-test-pg psql -U cygnet -d cygnet_test

# ── Build and publish ─────────────────────────────────────────────────────────

build:
    hatch build

publish-test:
    hatch publish --repo test

publish:
    hatch publish
