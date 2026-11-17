# Responsibility: Every engine's tessellator accepts the per-attempt staging bound the promotion
#                 proof hands down - whether or not it has any use for it.
from __future__ import annotations

import inspect

import pytest

from meshpipeline.engines.runtime import get_engine

#: Every engine a repaired file can be promoted for. The proof stages for the engine that will
#: mesh, so each of these is called with the bound.
ENGINES = ("gmsh", "snappy", "cfmesh", "vmtk")


def test_the_shared_tessellator_accepts_the_staging_bound():
    """The one every in-process bundle re-exports, checked without importing an engine.

    snappy and cfMesh both reach `cad_tessellate.tessellate_to_stl`, and resolving either bundle
    pulls in native dependencies that a hermetic environment may not have - so the regression that
    actually bit (a missing keyword here) is pinned directly, where nothing can skip it.
    """
    from meshpipeline.cad.cad_tessellate import tessellate_to_stl

    assert "stage_timeout_s" in inspect.signature(tessellate_to_stl).parameters


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_tessellator_accepts_the_staging_bound(engine):
    """`prepare_surface` passes `stage_timeout_s` to the engine's tessellator, always.

    A bundle that stages in a guarded subprocess uses it; one that tessellates in-process ignores
    it. Either way it must be in the signature, because the caller does not know which it got -
    and a bundle that rejects the keyword fails the whole run, not just the repair: on the demo
    car, snappy re-exported the shared OCC tessellator, promotion raised TypeError, and the job
    ended as an internal failure with no mesh at all.
    """
    try:
        tessellate = get_engine(engine).tessellate_to_stl
    except (ImportError, ModuleNotFoundError) as exc:  # a bundle's native deps, not its contract
        pytest.skip(f"{engine}'s bundle does not import here: {exc}")
    parameters = inspect.signature(tessellate).parameters
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    assert "stage_timeout_s" in parameters or takes_kwargs, (
        f"{engine}'s tessellate_to_stl would raise TypeError when the promotion proof "
        f"hands it a per-attempt bound; its parameters are {sorted(parameters)}")
