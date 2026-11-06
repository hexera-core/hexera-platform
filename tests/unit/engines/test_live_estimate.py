# Responsibility: Verify what the run's live line is allowed to count against: the engine's OWN
# planned cells, read back out of the record that engine wrote beside its case, and the engine's
# OWN word for one of the things it makes - with nothing invented where an engine records neither,
# and no failure of either reaching the mesh.
# Boundaries: the engine declarations and engines/live_estimate; no mesher runs here.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.live_estimate import planned_live_count
from meshpipeline.engines.registry import get_spec

# WHICH WORD, PER ENGINE - the engine's own declaration, never the cell shape's

@pytest.mark.parametrize(("engine", "unit"), [
    ("snappy", "cells"),                 # an OpenFOAM polyMesh
    ("cfmesh", "cells"),
    ("snappy_multiregion", "cells"),
    ("snapgrid", "cells"),
    ("vmtk", "cells"),                   # a VTK volume mesh counts cells, tetrahedral or not
    ("gmsh", "elements"),                # an FEA deck counts elements
])
def test_each_engine_says_what_it_makes_in_its_own_word(engine, unit, tmp_path):
    assert get_spec(engine).counted_as == unit
    assert planned_live_count(engine, tmp_path)[1] == unit


# THE ESTIMATE IS THE ENGINE'S OWN RECORD, OR NOTHING

def test_snappy_counts_against_the_plan_record_its_own_case_carries(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "plan_cells.json").write_text(json.dumps({"plan_cells": 1_900_000}))
    assert planned_live_count("snappy", tmp_path) == (1_900_000, "cells")


def test_cfmesh_counts_against_the_run_plan_its_own_case_carries(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "run_plan.json").write_text(json.dumps({"estimated_cells": 101_392}))
    assert planned_live_count("cfmesh", tmp_path) == (101_392, "cells")


def test_a_case_with_no_record_is_counted_against_nothing(tmp_path):
    # NOT zero, and NOT another engine's plan: the line says only what it has counted so far.
    for engine in ("snappy", "cfmesh", "gmsh", "vmtk", "snapgrid", "snappy_multiregion"):
        assert planned_live_count(engine, tmp_path)[0] is None


@pytest.mark.parametrize("written", ["{", "[]", json.dumps({"plan_cells": 0}),
                                     json.dumps({"plan_cells": -4}), json.dumps({"plan_cells": True}),
                                     json.dumps({"plan_cells": "lots"})])
def test_a_record_that_does_not_read_as_a_count_is_no_estimate(tmp_path, written):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "plan_cells.json").write_text(written)
    assert planned_live_count("snappy", tmp_path) == (None, "cells")


def test_an_unnamed_or_unknown_engine_never_borrows_the_default_engines_plan(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "plan_cells.json").write_text(json.dumps({"plan_cells": 1_900_000}))
    assert planned_live_count("", tmp_path) == (None, "")
    assert planned_live_count("not_a_real_engine", tmp_path) == (None, "")


# A COURTESY, NEVER LOAD-BEARING

def test_an_estimate_that_cannot_be_read_at_all_is_still_only_a_missing_estimate(monkeypatch):
    import dataclasses

    from meshpipeline.engines import registry

    def _explode(_workspace):
        raise RuntimeError("the plan record is on fire")

    angry = dataclasses.replace(get_spec("snappy"), _load_planned_cells_on_record=lambda: _explode)
    assert angry.planned_cells_on_record("/does/not/exist") is None
    monkeypatch.setitem(registry.ENGINE_CATALOG, "snappy", angry)
    assert planned_live_count("snappy", "/does/not/exist") == (None, "cells")


def test_a_workspace_that_is_not_a_path_is_no_estimate_and_no_exception():
    assert planned_live_count("snappy", None) == (None, "cells")
    assert planned_live_count("cfmesh", object()) == (None, "cells")


# THE WHOLE PLUMBING, SHORT OF THE EXECUTOR: a log the mesher wrote, the engine's own record
# beside it, and what the live line is then published with.

def _said(workspace, engine):
    import asyncio

    from meshpipeline.contracts import mesh_progress
    from meshpipeline.engines.mesher_stage import read_stage

    said: list[dict] = []

    class _Rec:
        async def anarrate(self, kind, text, *, n=None, of=None, unit="", op_id=""):
            said.append({"n": n, "of": of, "unit": unit})

        async def amesh_stage(self, key, label, op_id=""):
            pass

    async def go():
        of, unit = planned_live_count(engine, workspace)
        sink = mesh_progress.publishing_sink(_Rec(), asyncio.get_running_loop(), op_prefix="p",
                                             of=of, unit=unit)

        def remote():
            with mesh_progress.relaying(sink):
                mesh_progress.report(read_stage(workspace))
        await asyncio.to_thread(remote)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    return said


def _fixture(name, workspace):
    from pathlib import Path
    src = Path(__file__).parent / "fixtures" / "mesher_logs" / name
    (workspace / name).write_text(src.read_text())


def test_a_snappy_run_says_its_cells_out_of_the_cells_its_case_planned(tmp_path):
    _fixture("snappyHexMesh_layers.log", tmp_path)
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "plan_cells.json").write_text(json.dumps({"plan_cells": 3_000_000}))
    assert _said(tmp_path, "snappy") == [{"n": 2_679_296, "of": 3_000_000, "unit": "cells"}]


def test_the_same_run_past_its_plan_says_only_what_it_counted(tmp_path):
    _fixture("snappyHexMesh_layers.log", tmp_path)
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "plan_cells.json").write_text(json.dumps({"plan_cells": 2_000_000}))
    assert _said(tmp_path, "snappy") == [{"n": 2_679_296, "of": None, "unit": "cells"}]


def test_a_cfmesh_run_counts_cells_and_an_fea_run_counts_elements(tmp_path):
    cf = tmp_path / "cf"
    (cf / "system").mkdir(parents=True)
    _fixture("cartesianMesh_cells.log", cf)
    (cf / "system" / "run_plan.json").write_text(json.dumps({"estimated_cells": 120_000}))
    assert _said(cf, "cfmesh") == [{"n": 101_384, "of": 120_000, "unit": "cells"}]

    fea = tmp_path / "fea"
    fea.mkdir()
    _fixture("gmshfea_volume.log", fea)
    rows = _said(fea, "gmsh")
    assert rows and rows[-1]["unit"] == "elements" and rows[-1]["of"] is None   # gmsh records none
    assert rows[-1]["n"]
