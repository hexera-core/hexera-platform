#!/bin/bash
# Responsibility: Prepare the worker container's render environment, then exec the process it was given.
# Owns: the loader path OCP needs, and the removal of DISPLAY, which forces VTK onto EGL rather than GLX.
# Boundaries: environment preparation only; what the worker runs is decided by the command passed to it.

# worker_entrypoint.sh - Worker container entrypoint
#
# Xvfb is started as a defensive fallback in case any tool needs an X server,
# but DISPLAY is NOT exported to the Celery worker process. PyVista/VTK use
# EGL for off-screen rendering (VTK_DEFAULT_RENDER_BACKEND=egl set in
# docker-compose.yml). With DISPLAY exported, PyVista's auto-detect picked the
# X11/GLX path even though EGL was requested, producing intermittent
# `X_GLXMakeCurrent BadAccess` crashes (~75s into the reviewer's second tool
# call) that aborted the worker process and triggered Celery task re-delivery.
# Unsetting DISPLAY for the worker forces VTK to use the EGL render window
# exclusively. Gmsh is configured headless (General.Terminal=0) so it does
# not need DISPLAY either.
set -e

echo "[worker_entrypoint] Starting Xvfb on :99 (defensive fallback only)..."
# Remove stale socket and lock file from a previous container run (persists in writable layer)
rm -f /tmp/.X11-unix/X99 /tmp/.X99-lock 2>/dev/null || true
Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Give Xvfb a moment to initialise.
sleep 2

if kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[worker_entrypoint] Xvfb is running (pid=$XVFB_PID) - DISPLAY NOT exported (EGL is used)"
else
    echo "[worker_entrypoint] Xvfb did not start - EGL must succeed for rendering"
fi

# Explicitly unset DISPLAY so PyVista cannot pick the X11/GLX path.
# VTK_DEFAULT_RENDER_BACKEND=egl (set in docker-compose.yml) drives the
# render-window selection to vtkEGLRenderWindow.
unset DISPLAY

mkdir -p /data/training /data/beat || true

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
        echo "[worker_entrypoint] REFUSING TO START: ${reader_verdict}" >&2
        echo "[worker_entrypoint] Set the reader's key, or set GEOMETRY_VISION_PROVIDER=off to say this" >&2
        echo "[worker_entrypoint] deployment takes no look. ENV=dev warns instead of refusing." >&2
        exit 1
    fi
    echo "[worker_entrypoint] WARNING: ${reader_verdict}"
    echo "[worker_entrypoint] WARNING: this is a dev environment so it is a warning, not a refusal."
fi


# OCP (OpenCASCADE Python binding, used for CAD tessellation on the cfMesh path)
# loads bundled VTK shared libs at import. Put the vtkmodules dir on the loader
# path for the Celery process (OpenFOAM is sourced per-subprocess, so it does
# not reset this for the worker itself).
export LD_LIBRARY_PATH="/usr/local/lib/python3.11/dist-packages/vtkmodules:${LD_LIBRARY_PATH}"

exec "$@"
