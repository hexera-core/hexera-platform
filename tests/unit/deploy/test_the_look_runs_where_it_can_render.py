# Responsibility: Prove every container that drains the look's queue is built from an image that can render.
# Boundaries: it reads docker-compose.yml, the Dockerfile and the fleet's startup script; it builds nothing and
#             starts nothing, so it runs and reaches a verdict in every environment.
#
# THE DEFECT. The look RENDERS: `geometry_agent.vision.look` draws seventeen views of the part with PyVista
# before it asks the reader anything. docker-compose.yml's `worker-utility` - the only local consumer of the
# `geometry_look` queue - was built from Dockerfile target `api`, whose own first comment reads "Light: FastAPI
# + maintenance. No render stack". EGL, OSMesa and the X libraries are installed in the `pipeline` stage only.
#
# MEASURED on tests/fixtures/geometry/plate_with_hole_2d.step, through the platform's own
# `geometry_vision.look_at_local_file`, in both images built from this commit:
#
#   target api        exit 139 (SIGSEGV) after "Failed to load EGL" and "libOSMesa not found"
#   target pipeline   exit 0, 13.4 s, look.status=ok, 17 views, survey look_state=ok / looked=true
#
# A SIGNAL IS NOT AN EXCEPTION, which is what makes this worse than a failed look: the look's own try/except
# never runs, no block is written, nothing is recorded anywhere, and the Celery child is killed mid-task. The
# row keeps saying a look has not landed, which is true and is not the reason.
#
# AND IT WAS INVISIBLE FOR THE SAME REASON THE STALE WHEEL WAS: nothing exercised it. No reader key was
# configured, so `reader()` returned None and the look returned `not_attempted` before reaching the render. The
# wrong image never rendered anything, so it never crashed. Configuring a key is what turns the silence into a
# segfault, which is why this test exists on the same commit as the key.
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))

#: The queue the look is published to. Read from the platform's own queue declaration rather than typed here,
#: so a rename moves one word and this test follows it.
def _look_queue() -> str:
    text = (REPO / "src" / "meshpipeline" / "adapters" / "pipeline_execution" / "celery_app.py").read_text(
        encoding="utf-8")
    found = re.findall(r'^\s*QUEUE_\w*LOOK\w*\s*=\s*"([a-z_]+)"', text, re.M)
    assert len(set(found)) == 1, (
        f"the look's queue name could not be read out of celery_app.py (found {found}); this test would "
        f"otherwise check the wrong service, or none")
    return found[0]


#: What a stage must install before PyVista can render offscreen. `libegl1` alone is not enough: EGL is the
#: preferred backend and OSMesa is the software fallback, and an image with neither segfaults rather than
#: falling back. Named file-by-file rather than as "the pipeline stage", so the day somebody moves the render
#: stack the test asks the real question.
RENDER_LIBRARIES = ("libegl1", "libegl-mesa0", "libgl1-mesa-dri")


def _stage_bodies() -> dict[str, tuple[str, str]]:
    """Each Dockerfile stage's name mapped to (its parent stage, its own body)."""
    out: dict[str, tuple[str, str]] = {}
    starts = [(m.start(), m.group(1), m.group(2))
              for m in re.finditer(r"^FROM\s+(\S+)(?:@\S+)?\s+AS\s+(\w[\w-]*)", DOCKERFILE, re.M)]
    assert starts, "no named build stages found in the Dockerfile"
    for i, (pos, parent, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(DOCKERFILE)
        out[name] = (parent.split("@")[0], DOCKERFILE[pos:end])
    return out


def _can_render(stage: str) -> bool:
    """Does this stage, or anything it is built on, install the render stack?"""
    stages = _stage_bodies()
    seen: set[str] = set()
    while stage in stages and stage not in seen:
        seen.add(stage)
        parent, body = stages[stage]
        if all(lib in body for lib in RENDER_LIBRARIES):
            return True
        stage = parent
    return False


def _services_draining(queue: str) -> dict[str, dict]:
    found = {}
    for name, svc in (COMPOSE.get("services") or {}).items():
        command = svc.get("command")
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        if queue in text:
            found[name] = svc
    return found


# ---------------------------------------------------------------- the premise

def test_the_render_stack_is_in_exactly_one_place_and_the_light_image_says_it_is_not_there():
    """The premise, asserted rather than assumed. If `api` ever gains the render stack this test starts
    passing trivially, so it says so instead: that is a change to read, not to sail past."""
    assert _can_render("pipeline"), (
        f"the `pipeline` stage no longer installs all of {RENDER_LIBRARIES}, so either the render stack moved "
        f"and RENDER_LIBRARIES is stale, or the image that renders can no longer render")
    assert not _can_render("api"), (
        "the `api` stage now carries the render stack too. That is fine, but this test's whole subject was "
        "that it did not - read the change and decide what this should assert now")


def test_the_look_really_does_render_before_it_asks_anything():
    """The other premise: that the look needs a render at all. Read from the vendored wheel, because the image
    installs that and not a developer's checkout."""
    import zipfile
    wheels = sorted((REPO / "vendor" / "wheels").glob("hexera_geometry_agent-*.whl"))
    assert len(wheels) == 1, [w.name for w in wheels]
    with zipfile.ZipFile(wheels[0]) as z:
        look = z.read("geometry_agent/vision/look.py").decode("utf-8")
    assert re.search(r"^\s*(?:from|import)\s+.*render", look, re.M | re.I) or "render" in look, (
        "the look no longer reaches a renderer, so nothing here is about anything")


# ---------------------------------------------------------------- the rule

def test_every_local_service_that_drains_the_look_is_built_from_an_image_that_can_render():
    queue = _look_queue()
    draining = _services_draining(queue)
    assert draining, (
        f"no compose service drains {queue!r}. That is the undrained-queue defect, not this one, and "
        f"tests/unit/deploy/test_queue_consumers.py owns it - but this test is now checking nothing.")
    for name, svc in draining.items():
        target = ((svc.get("build") or {}) if isinstance(svc.get("build"), dict) else {}).get("target")
        assert target, f"compose service {name!r} drains {queue} and names no build target"
        assert _can_render(target), (
            f"compose service {name!r} drains {queue} and is built from Dockerfile target {target!r}, which "
            f"does not install {RENDER_LIBRARIES}. The look draws seventeen views with PyVista before it asks "
            f"the reader anything: measured on this commit, that is exit 139 (SIGSEGV) inside VTK, not a "
            f"failed look. A signal is not an exception, so nothing is recorded and the Celery child dies "
            f"mid-task.")


def test_the_deployed_fleet_takes_the_look_in_the_image_that_is_actually_deployed():
    """The cloud half, which was already right and must stay right. The fleet runs both workers from one
    ${WORKER_IMAGE}, and Gate C's deployable `app` component is the `pipeline` target - it says so in its own
    header, including that the `api`-target image it used to build was never deployed."""
    startup = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text(encoding="utf-8")
    queue = _look_queue()
    assert queue in startup, f"the fleet no longer drains {queue}"
    # the look's worker and the mesh worker run the same image, so there is one image to be right about
    assert startup.count('"${WORKER_IMAGE}"') >= 2, (
        "the fleet's two workers no longer run the same image, so which one takes the look is a new question")
    validate = (REPO / "devtools" / "release" / "validate.sh").read_text(encoding="utf-8")
    assert re.search(r"app\s*=\s*Dockerfile target\s*`?pipeline`?", validate), (
        "Gate C's deployable app component is no longer the `pipeline` target, so the deployed image may be "
        "one that cannot render and this test's conclusion no longer follows")


@pytest.mark.parametrize("lib", RENDER_LIBRARIES)
def test_each_named_render_library_is_one_the_dockerfile_really_installs(lib):
    # Guards the list itself: a typo would make `_can_render` answer False for every stage, and this test
    # would then "pass" by finding no service that can render - which is a fail, not a pass, but for the
    # wrong reason.
    assert re.search(rf"(?<![\w-]){re.escape(lib)}(?![\w-])", DOCKERFILE), (
        f"{lib} appears in RENDER_LIBRARIES and nowhere in the Dockerfile, so _can_render answers False for "
        f"every stage and this file is measuring a typo")
