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

import inspect
from pathlib import Path

import pytest

ENGINES = Path(__file__).parents[3] / "src" / "meshpipeline" / "engines"

#: The metrics only `checkMesh` produces. An engine requiring one at review cannot get it from
#: anywhere but the container the mesh was built in.
FOAM_ONLY_METRICS = ("max_non_ortho", "max_skewness", "max_aspect_ratio")


def _engines_requiring_a_foam_metric() -> list[str]:
    out = []
    for crit in sorted(ENGINES.glob("*/criteria.py")):
        text = crit.read_text(encoding="utf-8")
        for metric in FOAM_ONLY_METRICS:
            if 'MetricRequirement("' + metric + '"' in text:
                out.append(crit.parent.name)
                break
    return out


def _resolved(engine: str):
    """The check_mesh and the local runner THIS ENGINE ACTUALLY USES, off the same registries the
    pipeline uses. The first version of this test guessed filenames - foam_exec.py and native.py -
    and so missed snappy_multiregion entirely, whose real check_mesh is the per-region aggregator in
    multiregion_runner.py. A test that assumes where code lives fails differently from a test that
    asks what is wired, and the wiring is the thing that matters."""
    from meshpipeline.engines.dispatch import engine_runners
    from meshpipeline.engines.runtime import get_engine
    return get_engine(engine).check_mesh, engine_runners()[engine]


def test_at_least_two_engines_are_in_scope():
    """If this drops to nothing the finder has broken and the rest of the file is vacuously green."""
    found = _engines_requiring_a_foam_metric()
    assert len(found) >= 2, found
    for expected in ("cfmesh", "snappy", "snappy_multiregion"):
        assert expected in found, (expected, found)


@pytest.mark.parametrize("engine", _engines_requiring_a_foam_metric())
def test_its_check_mesh_prefers_the_measurement_taken_where_the_mesh_was_built(engine):
    check_mesh, _runner = _resolved(engine)
    src = inspect.getsource(check_mesh)
    assert "mesh_quality.json" in src, (
        f"{engine}: the check_mesh the pipeline resolves never looks for mesh_quality.json, so on "
        "the Cloud Run path it shells out to a binary that is not there and returns nothing")
    before_shell = src.split("run_guarded")[0].split("_single_region_check_mesh")[0]
    assert "mesh_quality.json" in before_shell, (
        f"{engine}: check_mesh reads mesh_quality.json only AFTER measuring; the cached measurement "
        "has to win, because here the measurement may be impossible")


@pytest.mark.parametrize("engine", _engines_requiring_a_foam_metric())
def test_its_local_runner_writes_the_measurement_beside_the_mesh(engine):
    _check_mesh, runner = _resolved(engine)
    src = inspect.getsource(runner)
    assert "mesh_quality.json" in src, (
        f"{engine}: {runner.__name__} requires a checkMesh metric at review and never writes "
        "mesh_quality.json, so every remote run arrives with no quality at all and the reviewer "
        "dead-letters a mesh that built correctly")


@pytest.mark.parametrize("engine", _engines_requiring_a_foam_metric())
def test_it_does_not_hand_back_a_previous_attempts_numbers(engine):
    """check_mesh PREFERS the file, and the builder retries in place, so a runner that measures into
    a reused workspace without clearing it reports the last attempt's mesh as if it were this one.
    A fact that lies is worse than a missing one, and these numbers decide whether the customer gets
    the mesh. snappy had exactly this gap and nothing had noticed."""
    _check_mesh, runner = _resolved(engine)
    src = inspect.getsource(runner)
    assert "unlink()" in src or "missing_ok" in src, (
        f"{engine}: {runner.__name__} writes mesh_quality.json but never clears a stale one first")
