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

# OCP (OpenCASCADE Python binding, used for CAD tessellation on the cfMesh path)
# loads bundled VTK shared libs at import. Put the vtkmodules dir on the loader
# path for the Celery process (OpenFOAM is sourced per-subprocess, so it does
# not reset this for the worker itself).
export LD_LIBRARY_PATH="/usr/local/lib/python3.11/dist-packages/vtkmodules:${LD_LIBRARY_PATH}"

exec "$@"
