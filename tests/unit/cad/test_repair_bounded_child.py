# Responsibility: Verify that a bounded kernel analysis runs in a FRESH INTERPRETER, never a fork:
#                 it is stopped at its wall, a dead or raising child is silence, only allow-listed
#                 kernels run, and a shape crosses the boundary with its identity and face order.
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from meshpipeline.cad.repair import bounded_child
from meshpipeline.cad.repair.bounded_child import (
    _probe_die,
    _probe_echo,
    _probe_raise,
    _probe_sleep,
    _probe_slow_shape,
    _probe_wrap,
)
from meshpipeline.cad.repair.localize import _bounded


@pytest.fixture
def no_fork(monkeypatch):
    """`os.fork` patched to raise: a bounded analysis must never reach for it."""
    def forbidden(*_a, **_k):
        raise AssertionError("os.fork was called: a threaded worker must never fork")

    monkeypatch.setattr(os, "fork", forbidden)
    if hasattr(os, "forkpty"):
        monkeypatch.setattr(os, "forkpty", forbidden)


def test_a_hanging_kernel_is_stopped_at_a_one_second_wall(no_fork, monkeypatch):
    started_procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording(*a, **k):
        proc = real_popen(*a, **k)
        started_procs.append(proc)
        return proc

    monkeypatch.setattr(bounded_child.subprocess, "Popen", recording)
    started = time.monotonic()
    assert _bounded(_probe_sleep, 60.0, budget_s=1.0) is None
    elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 2.5, elapsed
    (proc,) = started_procs
    assert proc.poll() is not None, "the child outlived its budget"


def test_an_answer_crosses_back_without_a_fork(no_fork):
    assert _bounded(_probe_echo, [1, 2], {"a": None, "b": 1.5}, b"\x00png", budget_s=30.0) == [
        [1, 2], {"a": None, "b": 1.5}, b"\x00png"]


def test_a_child_that_dies_is_silence(no_fork):
    assert _bounded(_probe_die, budget_s=30.0) is None


def test_a_raising_kernel_is_silence(no_fork):
    assert _bounded(_probe_raise, budget_s=30.0) is None


def test_an_unregistered_function_never_starts_a_child(monkeypatch):
    def popen(*_a, **_k):
        raise AssertionError("a child was started for an unregistered function")

    monkeypatch.setattr(bounded_child.subprocess, "Popen", popen)
    assert _bounded(lambda: [(1, 2)], budget_s=5.0) is None


def test_a_failed_spawn_is_silence(monkeypatch):
    def popen(*_a, **_k):
        raise BlockingIOError("EAGAIN")

    monkeypatch.setattr(bounded_child.subprocess, "Popen", popen)
    assert _bounded(_probe_echo, 1, budget_s=5.0) is None


def test_an_argument_json_cannot_carry_is_silence_not_an_exception():
    assert _bounded(_probe_echo, object(), budget_s=5.0) is None


def test_the_child_refuses_a_kernel_off_the_allow_list(tmp_path):
    """The job names a kernel by key; a key not in the registry is never imported."""
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"kernel": "os.system", "args": [{"value": "true"}],
                               "operands": None, "result": "result.json"}))
    assert bounded_child._child_main([str(job)]) != 0
    answer = json.loads((tmp_path / "result.json").read_text())
    assert answer["ok"] is False and "allow-list" in answer["error"]


def test_every_registered_kernel_resolves_to_the_function_it_names():
    for key, (module, name) in bounded_child.KERNELS.items():
        fn = bounded_child._resolve(key)
        assert fn.__module__ == module and fn.__name__ == name


def test_the_child_command_is_a_fresh_interpreter_running_the_dispatcher():
    cmd = bounded_child._child_cmd()
    assert cmd[0] == sys.executable and cmd[-1] == "meshpipeline.cad.repair.bounded_child"


# --- the real kernels, through the real child. The kernel is in the venv, so these are cheap. ---

@pytest.fixture
def ocp():
    """The CAD kernel, skipped per test: the process tests above need no kernel and must still run
    where OCP is missing."""
    return pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel")


def _overlapping_boxes():
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    both = TopoDS_Compound()
    builder.MakeCompound(both)
    builder.Add(both, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    builder.Add(both, BRepPrimAPI_MakeBox(gp_Pnt(5.0, 5.0, 5.0), 10.0, 10.0, 10.0).Shape())
    return both


def test_the_face_pairs_are_the_same_in_the_child_as_in_process(ocp, no_fork):
    from meshpipeline.cad.repair.localize import _kernel_face_pairs

    shape = _overlapping_boxes()
    here = _kernel_face_pairs(shape)
    assert here, "the fixture must have crossing faces"
    there = _bounded(_kernel_face_pairs, shape, budget_s=60.0)
    assert [tuple(p) for p in there] == here


def test_a_face_operand_keeps_its_identity_with_its_shape(ocp, no_fork):
    """`_thickness_kernel` finds the face's owner and neighbours by IsSame, so the face must still
    BE a face of the shape in the child: both are written into one BREP, sharing topology."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.investigator_tools import _thickness_kernel, _thickness_of
    from meshpipeline.cad.repair.localize import _indexed

    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 3.0).Shape()
    face = _indexed(shape, TopAbs_FACE).FindKey(5)
    here = _thickness_kernel(shape, face)
    assert here["thickness_mm"] is not None
    assert _thickness_of(shape, face, budget_s=60.0) == here


def test_the_common_volume_is_measured_in_the_child(ocp, no_fork):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.repair.investigator_tools import _common_volume_bounded

    a = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(5.0, 5.0, 5.0), 10.0, 10.0, 10.0).Shape()
    assert _common_volume_bounded(a, b, budget_s=60.0) == pytest.approx(125.0)


def test_the_heal_analysis_maps_the_childs_pairs_back_onto_the_part(ocp, no_fork):
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed, _kernel_face_pairs
    from meshpipeline.cad.repair.operations import _analyze_faces

    shape = _overlapping_boxes()
    faces = _indexed(shape, TopAbs_FACE)
    every = [faces.FindKey(i) for i in range(1, faces.Extent() + 1)]
    assert _analyze_faces(shape, every) == _kernel_face_pairs(shape)


def test_operands_cross_without_the_parents_triangulation(ocp, tmp_path):
    """The operand write is the one part of a bounded call the child's wall cannot stop, so it
    carries only what a kernel reads: the B-rep. A meshed part's triangulation is most of its BREP
    (measured 5x on a corpus hinge) and no kernel uses the parent's mesh - render meshes its own."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import _indexed

    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 3.0).Shape()
    BRepMesh_IncrementalMesh(shape, 0.1, False, 0.2, False)
    path = tmp_path / "operands.brep"
    bounded_child._write_operands([shape], path)
    (back,) = bounded_child._read_operands(path)
    faces = _indexed(back, TopAbs_FACE)
    for i in range(1, faces.Extent() + 1):
        assert BRep_Tool.Triangulation_s(TopoDS.Face_s(faces.FindKey(i)), TopLoc_Location()) is None


def test_slow_operand_preparation_never_starts_a_child(ocp, monkeypatch):
    """A write that used the whole budget is an honest None, and no child is started after it."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    def slow_write(_shapes, _path):
        time.sleep(0.3)

    def popen(*_a, **_k):
        raise AssertionError("a child was started after the budget went on the operands")

    monkeypatch.setattr(bounded_child, "_write_operands", slow_write)
    monkeypatch.setattr(bounded_child.subprocess, "Popen", popen)
    assert bounded_child.run(_probe_echo, (BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape(),), 0.1) is None


# --- an answer may carry SHAPES, not only JSON (audit row 6) ----------------------------------


def _box(dx=10.0, dy=20.0, dz=3.0):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return abs(float(props.Mass()))


def test_a_shape_comes_back_from_the_child(ocp, no_fork):
    """ROW 6 NEEDS THIS AND NOTHING ELSE DID. `_general_fuse` answers with the bodies' PIECES, and
    until now the wire was JSON only - `_encode` refused a shape, so no kernel that builds
    geometry could run in a child at all. Operands already cross as a BREP compound; answers now
    come back the same way, written beside the JSON."""
    box = _box()

    (back,) = bounded_child.run(_probe_echo, (box,), budget_s=60.0)

    assert _volume(back) == pytest.approx(_volume(box))


def test_a_shape_nested_in_the_answer_comes_back_too(ocp, no_fork):
    """The fuse's answer is `((pieces, pieces), history)` - shapes inside tuples inside a tuple -
    so a shape has to be found wherever it sits, not only at the top."""
    box = _box()

    answer = bounded_child.run(_probe_wrap, (box,), budget_s=60.0)

    assert answer["faces"] == 6
    assert _volume(answer["piece"]) == pytest.approx(_volume(box))
    assert [_volume(s) for s in answer["both"]] == [pytest.approx(_volume(box))] * 2


def test_a_shape_answer_is_still_silence_when_the_budget_runs_out(ocp, no_fork, monkeypatch):
    """THE CONTRACT THAT MATTERS FOR AN OPERATION. A child killed mid-answer gives None, so
    `fragment_bodies` declines rather than committing a half-built fuse. The BREP is written
    BEFORE the result JSON, and the JSON is renamed into place, so there is no window in which a
    reader sees an answer whose shapes are not all there."""
    started = time.monotonic()

    assert bounded_child.run(_probe_slow_shape, (0.5,), budget_s=1.0) is None
    assert time.monotonic() - started < 20.0


def test_an_answer_naming_a_shape_it_did_not_carry_is_refused(ocp, no_fork, monkeypatch):
    """A corrupt or truncated answer is not something to guess at. A reference with no shape
    behind it must read as NO ANSWER, never as some other index's geometry - which is the
    difference between an operation declining and an operation committing the wrong solid."""
    monkeypatch.setattr(bounded_child, "_read_operands", lambda path: [])

    assert bounded_child.run(_probe_wrap, (_box(),), budget_s=60.0) is None


def test_the_shape_reference_is_resolved_against_what_the_answer_carried():
    """The decode rule on its own: in range it resolves, out of range it raises rather than
    returning something plausible."""
    assert bounded_child._decode({"a": {"__shape__": 1}}, ["first", "second"]) == {"a": "second"}
    with pytest.raises(bounded_child._NotEncodable):
        bounded_child._decode({"__shape__": 0}, [])
    with pytest.raises(bounded_child._NotEncodable):
        bounded_child._decode({"__shape__": 0}, None)
