"""INS-39: a case meshed as several passages says which cell region holds which opening, and refuses a mesh that cut
one passage in two. TIME-16: the passage measure is stopped at its guard even inside a call Python cannot interrupt."""
from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest

from meshpipeline.engines import passage
from meshpipeline.engines.cfmesh import native

_HEAD = "FoamFile\n{{\n    version 2.0;\n    format ascii;\n    class {cls};\n    object {obj};\n}}\n"


def _write(pm: Path, name: str, cls: str, body: str) -> None:
    (pm / name).write_text(_HEAD.format(cls=cls, obj=name) + body)


def _two_volumes(ws: Path) -> Path:
    """Three cells: 0 and 1 joined through face 0, cell 2 on its own (a second fluid volume). inlet on cell 0,
    outlet_1 on cell 1, outlet_2 on cell 2, wall on cell 2."""
    pm = ws / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    _write(pm, "owner", "labelList", "5\n(\n0 0 1 2 2\n)\n")
    _write(pm, "neighbour", "labelList", "1\n(\n1\n)\n")
    rows = [("inlet", "patch", 1), ("outlet_1", "patch", 2), ("outlet_2", "patch", 3), ("wall", "wall", 4)]
    body = "".join(f"    {n}\n    {{\n        type {t};\n        nFaces 1;\n        startFace {s};\n    }}\n"
                   for n, t, s in rows)
    _write(pm, "boundary", "polyBoundaryMesh", f"4\n(\n{body})\n")
    return pm


def test_opening_regions_reads_each_opening_s_region(tmp_path):
    read = native.opening_regions(_two_volumes(tmp_path), ["inlet", "outlet_1", "outlet_2"])
    assert read == {"cells": [2, 1], "openings": {"inlet": [1], "outlet_1": [1], "outlet_2": [2]}}


def test_unreadable_mesh_is_not_judged(tmp_path):
    assert native.opening_regions(tmp_path / "nothing", ["inlet"]) is None


def test_separate_passages_each_whole_are_told_side_by_side():
    read = {"cells": [2, 1], "openings": {"inlet": [1], "outlet_1": [1], "outlet_2": [2]}}
    refusal, account = native.regions_judged(read, [["inlet", "outlet_1"], ["outlet_2"]])
    assert refusal == ""
    assert "2 separate fluid volume(s), side by side and not joined" in account
    assert "region 1 (2 cells): inlet, outlet_1" in account and "region 2 (1 cells): outlet_2" in account


def test_a_passage_cut_in_two_is_refused_in_words():
    read = {"cells": [2, 1], "openings": {"inlet": [1], "outlet_2": [2]}}
    refusal, _ = native.regions_judged(read, [["inlet", "outlet_2"]])
    assert "passage 1 came out as 2 separate pieces (region 1: inlet; region 2: outlet_2)" in refusal
    assert native.PORT_REGION_WAY_OUT in refusal


def test_a_region_with_no_opening_is_named():
    read = {"cells": [2, 1], "openings": {"inlet": [1], "outlet_1": [1]}}
    refusal, account = native.regions_judged(read, [["inlet", "outlet_1"]])
    assert refusal == "" and "1 region(s) hold no opening" in account


def _passages(ws: Path, ports: list[list[str]]) -> None:
    from meshpipeline.cad.fluid_passages import PASSAGES_FILE
    (ws / PASSAGES_FILE).write_text(json.dumps({"ports": ports, "runs": []}))


def test_mesh_passages_records_the_regions_of_the_merged_mesh(tmp_path):
    _two_volumes(tmp_path)
    _passages(tmp_path, [["inlet", "outlet_1"], ["outlet_2"]])
    assert native.mesh_passages(tmp_path) == (0, False, "")
    rec = json.loads((tmp_path / native.PASSAGE_REGIONS_FILE).read_text())
    assert rec["rule"] == "INS-39" and rec["cells"] == [2, 1] and rec["refusal"] == ""


def test_mesh_passages_refuses_a_merged_mesh_that_cuts_a_passage(tmp_path):
    # the plan says inlet and outlet_2 share one passage; the mesh holds them in two separate volumes
    _two_volumes(tmp_path)
    _passages(tmp_path, [["inlet", "outlet_2"], ["outlet_1"]])
    rc, timed_out, why = native.mesh_passages(tmp_path)
    assert rc == 1 and not timed_out
    assert "passage 1 came out as 2 separate pieces" in why


def _stuck(_pm):
    # a call Python cannot interrupt: the guard's signal is held off until it returns
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    time.sleep(4.0)
    return {"passage_radius": {"p05": 1.0}}


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="POSIX only")
def test_the_passage_measure_is_stopped_at_its_guard(tmp_path, monkeypatch, caplog):
    _two_volumes(tmp_path)
    monkeypatch.setattr(passage, "_measure_polymesh", _stuck)
    t0 = time.monotonic()
    with caplog.at_level("WARNING", logger="meshpipeline.engines.passage"):
        assert passage.passage_of_polymesh(tmp_path, budget_s=0.5) == {}
    assert time.monotonic() - t0 < 4.5
    # the log names where the measure was when it was stopped
    assert "in _stuck" in caplog.text


def test_the_bounded_measure_returns_its_result_and_its_error():
    assert passage._bounded(lambda: {"a": 1}, budget_s=5.0) == {"a": 1}

    def boom():
        raise ValueError("no surface")
    with pytest.raises(ValueError, match="no surface"):
        passage._bounded(boom, budget_s=5.0)
