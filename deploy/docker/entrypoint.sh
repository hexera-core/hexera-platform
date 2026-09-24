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

# THE SURVEYOR'S READER, checked before this container serves anything.
#
# A deployment whose configured reader has no key looks at NOTHING. Every upload stores "the configured
# reader (openai, gpt-5.6-luna) has no key in this environment, so nothing looked", once per job, in a row
# and a log, and the builder receives a survey with no look in it. That is half the Surveyor missing, and
# it used to be the default: GEOMETRY_VISION_PROVIDER defaulted to openai while the template carried only
# DEEPSEEK_API_KEY and DEEPINFRA_API_KEY.
#
# WHY HERE. This is the deployment boundary: the real values are in this process's environment, the check
# happens before the container serves, and it is the last point before a customer's upload is affected.
# It is deliberately NOT a refusal inside `import meshpipeline.settings.policy` - a look that cannot happen
# must never take a process down, which is the rule every other part of the look already obeys.
#
# A DEFAULT IS NOT A CONFIRMATION. In a hosted environment an unset key REFUSES: it is an accident, and
# nothing downstream can tell an accident from a decision. GEOMETRY_VISION_PROVIDER=off is the decision,
# and it starts. In a dev environment it says it once and carries on, because a developer with no OpenAI
# account must still be able to run the stack.
#
# The question is answered by settings/policy.py's vision_reader_has_no_key() - one place, so this and the
# deploy's own validate-config.sh cannot drift from what the application actually reads.
reader_verdict="$(python -c 'import meshpipeline.settings.policy as p; print(p.vision_reader_has_no_key())' 2>/dev/null || echo "")"
if [ -n "${reader_verdict}" ]; then
    if python -c 'import sys, meshpipeline.settings.policy as p; sys.exit(0 if p.requires_hardened_runtime(p.ENV) else 1)' 2>/dev/null; then
        echo "[entrypoint] REFUSING TO START: ${reader_verdict}" >&2
        echo "[entrypoint] Set the reader's key, or set GEOMETRY_VISION_PROVIDER=off to say this" >&2
        echo "[entrypoint] deployment takes no look. ENV=dev warns instead of refusing." >&2
        exit 1
    fi
    echo "[entrypoint] WARNING: ${reader_verdict}"
    echo "[entrypoint] WARNING: this is a dev environment so it is a warning, not a refusal."
fi

echo "[entrypoint] Running database migrations (advisory-locked)..."
python -m meshpipeline.runtime.migrate
echo "[entrypoint] Migrations complete. Starting API..."

exec "$@"
