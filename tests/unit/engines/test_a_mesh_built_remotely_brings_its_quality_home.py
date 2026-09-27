# Responsibility: an engine whose review criteria require a checkMesh metric must measure it where the mesh was built.
# Boundaries: source-level invariant over the engine bundles; the reviewer and the Cloud Run client are other tests.
"""A finished, paid-for cfMesh mesh was destroyed for a number nothing on this side could produce.

MEASURED 2026-09-27, job 1457d15f, in this order:

    cloud_run_client   - cfmesh ran on Cloud Run Job - rc=0 timed_out=False
    builder.executor   - submit_mesh succeeded - polyMesh confirmed, exiting loop
    pipeline.executor  - solvability gate PASSED
    pipeline.executor  - success=True
    reviewer.visual    - required deterministic evidence missing ('metric:max_non_ortho',)
    errors             - DEAD_LETTER failure_class=review_evidence_missing

`checkMesh` is an OpenFOAM binary. On the Cloud Run path the mesh is built in a container that has one
and read back by a worker that does not, so measuring on the worker side returns an empty dict every
time. `engines/snappy` fixed this by measuring beside the mesh and shipping `mesh_quality.json` home
with it, and said so in its own comments. cfMesh was never given the same treatment, and
`cfmesh/criteria.py` requires `max_non_ortho`, so this was EVERY cfMesh run on the remote path.

Two things made it worse than a missing number. Solvability reads the same empty dict and PASSES,
because it only reports the metric when it is present - two readers of one missing fact reaching
opposite verdicts, which is this codebase's signature defect. And the mesh was already built and paid
for, so the customer was charged for a mesh they never received.

THIS TEST PINS THE INVARIANT, NOT THE INSTANCE. Fixing cfMesh only fixes cfMesh; the next engine
wired to the remote path would repeat it. Any engine that both requires a checkMesh metric at review
and runs its own native binary must (a) prefer a measurement taken where the mesh was built, and
(b) actually take one. It reads the source rather than the behaviour deliberately: the behaviour needs
OpenFOAM and a Cloud Run container, so a test of it would be skipped exactly where it matters.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ENGINES = Path(__file__).parents[3] / "src" / "meshpipeline" / "engines"

#: The metrics that only `checkMesh` produces. An engine requiring one of these at review cannot get
#: it from anywhere but the container the mesh was built in.
FOAM_ONLY_METRICS = ("max_non_ortho", "max_skewness", "max_aspect_ratio")


def _engines_requiring_a_foam_metric() -> list[str]:
    out = []
    for crit in sorted(ENGINES.glob("*/criteria.py")):
        text = crit.read_text(encoding="utf-8")
        for metric in FOAM_ONLY_METRICS:
            if re.search(r'MetricRequirement\(\s*"' + metric + r'"', text):
                out.append(crit.parent.name)
                break
    return out


def test_at_least_two_engines_are_in_scope():
    """If this drops to nothing the finder has broken and the rest of the file is vacuously green."""
    found = _engines_requiring_a_foam_metric()
    assert len(found) >= 2, found
    assert "cfmesh" in found and "snappy" in found, found


@pytest.mark.parametrize("engine", _engines_requiring_a_foam_metric())
def test_its_check_mesh_prefers_the_measurement_taken_where_the_mesh_was_built(engine):
    foam = ENGINES / engine / "foam_exec.py"
    if not foam.is_file():
        pytest.skip(f"{engine} has no foam_exec bundle")
    text = foam.read_text(encoding="utf-8")
    assert "mesh_quality.json" in text, (
        f"{engine}/foam_exec.py never looks for mesh_quality.json, so on the Cloud Run path it shells "
        "out to a binary that is not there and returns nothing")
    head = text[text.index("def check_mesh"):]
    body = head[: head.index("run_guarded")] if "run_guarded" in head else head
    assert "mesh_quality.json" in body, (
        f"{engine}/check_mesh reads mesh_quality.json only AFTER shelling out; the cached measurement "
        "has to win, because here the measurement may be impossible")


@pytest.mark.parametrize("engine", _engines_requiring_a_foam_metric())
def test_its_native_runner_writes_the_measurement_beside_the_mesh(engine):
    candidates = [p for p in (ENGINES / engine).glob("*.py")
                  if p.name in ("native.py", f"{engine}_runner.py")]
    assert candidates, f"{engine} has no native runner to check"
    wrote = [p.name for p in candidates if "mesh_quality.json" in p.read_text(encoding="utf-8")]
    assert wrote, (
        f"{engine} requires a checkMesh metric at review and none of {[p.name for p in candidates]} "
        "writes mesh_quality.json, so every remote run arrives with no quality at all and the "
        "reviewer dead-letters a mesh that built correctly")


@pytest.mark.parametrize("engine", _engines_requiring_a_foam_metric())
def test_it_does_not_hand_back_a_previous_attempts_numbers(engine):
    """`check_mesh` prefers the file, so a runner that measures into a reused workspace without
    clearing it would report the last attempt's mesh as if it were this one. A fact that lies is worse
    than a missing one, and these numbers decide whether the customer gets the mesh."""
    for p in (ENGINES / engine).glob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "mesh_quality.json" not in text or "def check_mesh" in text:
            continue
        assert "unlink()" in text or "missing_ok" in text, (
            f"{p.name} writes mesh_quality.json but never clears a stale one before measuring")
