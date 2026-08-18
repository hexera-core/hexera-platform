#!/bin/bash
# Responsibility: Bring the API container up - apply migrations under an advisory lock, then exec the server.
# Boundaries: a failed migration exits non-zero, so this instance never serves a schema it did not reach.

# entrypoint.sh - API container entrypoint
# Runs Alembic migrations automatically before starting Uvicorn. A public service can
# cold-start several instances at once, so migrations run under a Postgres advisory lock
# (meshpipeline.runtime.migrate) - one instance migrates, the rest wait then no-op. A failed
# migration exits non-zero here, so `set -e` fails this instance and Cloud Run keeps the
# previous healthy revision serving.
# All other services (worker, etc.) bypass this via docker-compose command override.
set -e

echo "[entrypoint] Running database migrations (advisory-locked)..."
python -m meshpipeline.runtime.migrate
echo "[entrypoint] Migrations complete. Starting API..."

exec "$@"
