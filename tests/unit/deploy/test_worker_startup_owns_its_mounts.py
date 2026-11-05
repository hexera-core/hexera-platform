# Responsibility: Verify the worker startup script hands its bind-mounted directories to the user the
# image runs as, before the container that needs them starts.
# Boundaries: a static read of the script; the fleet is not started here.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
STARTUP = REPO / "deploy" / "gcp" / "worker" / "startup.sh"
DOCKERFILE = REPO / "Dockerfile"

# THE DEFECT THIS PINS. The container runs as the image's non-root user, but a bind mount keeps
# the host's ownership and docker creates a missing mount point as root. Every job on the fleet
# therefore died on its first mkdir with "Permission denied" - shared dev and the personal
# environments alike had never run a mesh job. The fix is ownership on the host side, applied
# every boot, before `docker run`.

MOUNTS = ("/var/lib/hexera/workspaces", "/var/lib/hexera/data")


def _script() -> str:
    return STARTUP.read_text(encoding="utf-8")


def test_the_startup_script_chowns_every_bind_mount_it_hands_the_worker():
    s = _script()
    for mount in MOUNTS:
        assert f"-v {mount}:" in s, f"{mount} is no longer bind-mounted; update MOUNTS"
    chown = [line for line in s.splitlines() if line.lstrip().startswith("chown ")]
    assert chown, "the startup script never chowns the worker's directories"
    for mount in MOUNTS:
        assert any(mount in line for line in chown), f"{mount} is mounted but never chowned"


def test_ownership_is_settled_before_the_worker_container_starts():
    s = _script()
    assert s.index("chown ") < s.index("docker run -d --name hexera-worker"), \
        "the worker started before its directories were handed to it"
    assert s.index("mkdir -p /var/lib/hexera/workspaces") < s.index("chown "), \
        "chown ran on a directory the script had not created yet"


def test_the_uid_comes_from_the_image_not_from_a_number_in_the_script():
    # The image decides which user it runs as (Dockerfile: useradd -u 1000). Reading the uid off
    # the image keeps the two from drifting apart; 1000 only remains as the fallback.
    s = _script()
    assert 'docker run --rm --entrypoint id "${WORKER_IMAGE}" -u' in s
    assert 'chown "${WORKER_UID}:${WORKER_UID}"' in s
    assert "useradd -m -u 1000" in DOCKERFILE.read_text(encoding="utf-8"), \
        "the Dockerfile's user changed; the startup fallback should follow it"
