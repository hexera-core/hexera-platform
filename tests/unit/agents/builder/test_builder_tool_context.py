# Responsibility: Verify every engine's build driver and entry point accepts the same typed tool context.
from __future__ import annotations

from pathlib import Path

import pytest
from tests._geometry_support import materialized

from meshpipeline.agents.builder import tools as T
from meshpipeline.agents.builder.tool_context import (
    BuilderToolContext,
    GeometryContextMissing,
)
from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

#: Every engine bundle must accept the SAME context shape at its build driver.
ENGINES = ["cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"]


def _context(tmp_path, *, engine, unit=LengthUnit.millimetre,
             basis=ResolutionBasis.user_confirmed, generation=7, geometry=None):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    # The REAL run policy gates on the engine's own required files, and this suite is about the
    # context rather than that gate - so satisfy it honestly instead of stubbing the policy out.
    from meshpipeline.engines.registry import get_spec
    for required in get_spec(engine).run_policy.required_files:
        target = ws / required
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# placeholder for the run-policy gate\n")
    if geometry is None:
        geometry = materialized(tmp_path / "materialised", unit=unit, basis=basis)
    return BuilderToolContext(
        workspace=ws, geometry=geometry, job_id="job-1", execution_id="exec-1",
        execution_generation=generation, engine=engine, mesh_fidelity="standard",
        loop_deadline=None)


@pytest.fixture()
def driver_spy(monkeypatch):
    seen: dict = {}

    class _Engine:
        # The REAL engine name, because `_tool_run_mesh` resolves that engine's spec and policy.
        # A spy calling itself "spy" would only prove the registry rejects unknown names.
        def __init__(self, name: str) -> None:
            self.name = name

        def run_cartesian_mesh(self, workspace, *, timeout, context=None):
            seen["workspace"] = Path(workspace)
            seen["timeout"] = timeout
            seen["context"] = context
            return {"rc": 0, "timed_out": False, "log_tail": ""}

        def inspect_stl(self, workspace, geometry_file="input.stl", *, context=None):
            seen["inspect_context"] = context
            return {"extents": [1.0, 1.0, 1.0]}

        def check_mesh(self, workspace):
            return {"fatal": [], "cells": 1000}

        def _patch_face_counts(self, workspace):
            # snappy's result enricher reaches back through the engine seam for this; a double
            # without it fails inside the enricher rather than at the boundary under test.
            return {"body": 100, "farfield": 50}

    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine",
                        lambda n="": _Engine(n or "cfmesh"))
    return seen


# the context arrives, intact

def test_the_build_driver_receives_the_whole_typed_context(tmp_path, driver_spy, monkeypatch):
    monkeypatch.setattr(T.meshing, "input_contract_rejection", lambda *_a, **_k: "")
    ctx = _context(tmp_path, engine="cfmesh", unit=LengthUnit.inch)

    T._tool_run_mesh(ctx)

    got = driver_spy["context"]
    assert got is ctx, "the driver received something other than the context it was given"

    # identity
    assert got.source.source_id == ctx.geometry.ref.source_id
    assert got.interpretation.interpretation_id == ctx.geometry.interpretation.interpretation_id
    assert got.interpretation.geometry_source_id == got.source.source_id

    # physical meaning: the unit AND the exact factor, not one or the other
    assert got.interpretation.unit == "in"
    assert got.interpretation.scale_to_metres == 0.0254

    # coordinate state - what the numbers mean now, and the one conversion still owed
    assert got.prepared is not None
    assert got.prepared.to_metres == 0.0254

    # the process-local handle, and the identity the fence needs
    assert got.geometry_path == str(ctx.geometry.local_path)
    assert Path(got.geometry_path).exists()
    assert got.workspace == tmp_path / "ws"
    assert got.execution_generation == 7
    assert (got.job_id, got.execution_id) == ("job-1", "exec-1")


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_build_driver_accepts_the_same_context_shape(engine, tmp_path, driver_spy,
                                                                  monkeypatch):
    monkeypatch.setattr(T.meshing, "input_contract_rejection", lambda *_a, **_k: "")
    ctx = _context(tmp_path, engine=engine)

    T._tool_run_mesh(ctx)

    assert driver_spy["context"] is ctx


@pytest.mark.parametrize("engine", ENGINES)
def test_every_real_engine_entry_point_accepts_a_context_argument(engine):
    import inspect

    from meshpipeline.engines.runtime import get_engine

    R = get_engine(engine)
    for method in ("run_cartesian_mesh", "inspect_stl"):
        sig = inspect.signature(getattr(R, method))
        assert "context" in sig.parameters, (
            f"{engine}.{method} does not accept a builder tool context")
        assert sig.parameters["context"].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{engine}.{method} must take `context` keyword-only, so it cannot be filled "
            "positionally by accident")


@pytest.mark.parametrize("unit,factor", [
    (LengthUnit.metre, 1.0),
    (LengthUnit.millimetre, 1e-3),
    (LengthUnit.centimetre, 1e-2),
    (LengthUnit.inch, 0.0254),
])
def test_the_factor_that_arrives_is_the_one_the_unit_defines(unit, factor, tmp_path, driver_spy,
                                                             monkeypatch):
    monkeypatch.setattr(T.meshing, "input_contract_rejection", lambda *_a, **_k: "")
    ctx = _context(tmp_path, engine="cfmesh", unit=unit)

    T._tool_run_mesh(ctx)

    got = driver_spy["context"]
    assert got.interpretation.scale_to_metres == factor
    assert got.prepared.to_metres == factor


def test_geometry_report_also_carries_the_context(tmp_path, driver_spy):
    T._tool_geometry_report(_context(tmp_path, engine="cfmesh"))

    assert driver_spy["inspect_context"] is not None
    assert driver_spy["inspect_context"].interpretation.unit == "mm"


# what it refuses

@pytest.mark.parametrize("tool,args", [("run_mesh", {}), ("geometry_report", {})])
def test_a_geometric_tool_refuses_to_run_without_verified_geometry(tool, args, tmp_path,
                                                                   driver_spy):
    ctx = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh")

    out = T._dispatch_tool(ctx, tool, args)

    assert "error" in out
    assert "geometry" in out.lower()
    assert "context" not in driver_spy, "the driver was reached without verified geometry"


def test_require_geometry_names_the_problem():
    ctx = BuilderToolContext(workspace=Path("/tmp"), geometry=None, engine="cfmesh")
    with pytest.raises(GeometryContextMissing):
        ctx.require_geometry()
    assert ctx.source is None and ctx.interpretation is None
    assert ctx.prepared is None and ctx.geometry_path == ""


def test_the_context_cannot_hold_a_second_disagreeing_interpretation(tmp_path):
    import dataclasses

    ctx = _context(tmp_path, engine="cfmesh")
    fields = {f.name for f in dataclasses.fields(BuilderToolContext)}
    assert "interpretation" not in fields
    assert "source" not in fields
    assert "prepared" not in fields
    assert "geometry_path" not in fields

    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.geometry = None                      # type: ignore[misc]
