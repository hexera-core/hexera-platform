# Responsibility: Verify the platform forwards the measurement package's block and never recomputes one, and that every failure is an absence.
from __future__ import annotations

import asyncio
import sys

import pytest

from meshpipeline.cad import regions as cad_regions
from meshpipeline.contracts import geometry_agent_block as gab

STORED = {"schema": "geometry_agent.planner_block.v1", "status": "ok",
          "representation": "wall_shell", "agent_forecast_cells": 904_514,
          "inlet_bore_m": 0.35694, "units": "m"}

STATE = {"geometry": {"ref": {"source_id": "11111111-1111-4111-8111-111111111111",
                             "owner_id": "o", "object_key": "sources/x", "sha256": "b" * 64,
                             "size_bytes": 10, "original_filename": "p.step",
                             "suffix_hint": ".step"}}}


def test_the_stored_block_is_forwarded_verbatim():
    """The platform does no arithmetic. A second spelling of the conversion on this side of the
    boundary is a second thing to keep in step across two independently pinned distributions."""
    document = {"status": "ok", "planner_block": STORED}
    assert gab.block_for_document(document) is STORED


def test_a_row_written_before_the_block_existed_is_given_one_by_the_package(monkeypatch):
    """A row from last week has no `planner_block` key, and re-measuring the file to add one would
    cost seconds for a value already derivable from what the row holds."""
    called: list = []

    class _Hexera:
        @staticmethod
        def planner_block(document):
            called.append(document)
            return {**STORED, "status": "degraded"}

    monkeypatch.setitem(sys.modules, "geometry_agent", type(sys)("geometry_agent"))
    monkeypatch.setitem(sys.modules, "geometry_agent.agent", type(sys)("geometry_agent.agent"))
    monkeypatch.setitem(sys.modules, "geometry_agent.agent.hexera", _Hexera)
    out = gab.block_for_document({"status": "ok", "forecast": {"cells_high": 1}})
    assert out["status"] == "degraded" and len(called) == 1


@pytest.mark.parametrize("document", [None, {}, 0, "x"])
def test_nothing_to_read_is_no_block(document):
    assert gab.block_for_document(document) is None


def test_an_image_without_the_measurement_package_adds_no_key(monkeypatch):
    """The direction of the dependency is the one rule that does not bend: the platform may import
    the package, and an image that does not carry it behaves as one with no measurement."""
    real = __import__

    def _no_package(name, *a, **k):
        if name.startswith("geometry_agent"):
            raise ModuleNotFoundError(name)
        return real(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_package)
    assert gab.block_for_document({"status": "ok", "forecast": {}}) is None


def test_a_package_that_raises_is_an_absence_not_a_failure(monkeypatch):
    class _Hexera:
        @staticmethod
        def planner_block(_document):
            raise RuntimeError("the block could not be assembled")

    monkeypatch.setitem(sys.modules, "geometry_agent", type(sys)("geometry_agent"))
    monkeypatch.setitem(sys.modules, "geometry_agent.agent", type(sys)("geometry_agent.agent"))
    monkeypatch.setitem(sys.modules, "geometry_agent.agent.hexera", _Hexera)
    assert gab.block_for_document({"status": "ok"}) is None


def test_a_run_with_no_geometry_gets_no_block():
    assert asyncio.run(cad_regions.agent_block_for_state({})) is None
    assert asyncio.run(cad_regions.agent_block_for_state({"geometry": {}})) is None
    assert asyncio.run(cad_regions.agent_block_for_state(None)) is None


def test_a_state_with_geometry_reads_the_row_and_forwards_the_block(monkeypatch):
    seen: list = []

    async def _read(ref, **_kw):
        seen.append(ref.sha256)
        return {"status": "ok", "planner_block": STORED}

    monkeypatch.setattr("meshpipeline.cad.regions.stored_document_for_source", _read)
    block = asyncio.run(cad_regions.agent_block_for_state(STATE))
    # EVERY NUMBER IS THE PACKAGE'S AND IS FORWARDED UNTOUCHED, which is what this module is for. It is no
    # longer the same object: the document here carries no `passage_ends` key, so nothing measured where this
    # part's passages stop at a wall with no mouth, and `cad/regions.py` adds the refusal that says so rather
    # than letting an empty place list read as a measured zero. See
    # `tests/unit/cad/test_geometry_block_skipped_stage.py`.
    assert {k: v for k, v in block.items() if k != "places_refused"} == STORED
    assert [r["kind"] for r in block["places_refused"]] == ["closed_end"]
    assert seen == ["b" * 64]


def test_a_read_that_raises_never_fails_a_plan(monkeypatch):

    async def _boom(_ref, **_kw):
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr("meshpipeline.cad.regions.stored_document_for_source", _boom)
    assert asyncio.run(cad_regions.agent_block_for_state(STATE)) is None
