# Responsibility: A17 - memoising layer 1's two expensive checks on `geometry_key` + tolerances changes NO finding,
#                 and unlike the digest it HITS across a STEP round trip, which is the only reason to do it.
# Boundaries: the native tier, real OCCT shapes. The corpus-wide proof (Lane C's stored states) is a separate run.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: E402,F401  (import order: as a worker does)
from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk  # noqa: E402


def _write(shape, path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _compound(*shapes):
    c, b = TopoDS_Compound(), BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _overlapping_boxes():
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(5, 5, 5), 10.0, 10.0, 10.0).Shape()   # really shares volume
    return _compound(a, b)


def _touching_boxes():
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10.0, 10.0, 10.0).Shape()  # conformal contact, no shared volume
    return _compound(a, b)


def _without_memos(monkeypatch) -> None:
    """Both memos bypassed: every call recomputes. The comparison target."""
    class _Passthrough:
        hits = misses = 0

        def get_or_compute(self, key, compute):  # noqa: ANN001, ARG002
            return compute()

    monkeypatch.setattr(vk, "SELF_INTERSECTS", _Passthrough())
    monkeypatch.setattr(vk, "OVERLAP_PAIRS", _Passthrough())


@pytest.mark.parametrize("build,name", [(_overlapping_boxes, "overlap"), (_touching_boxes, "contact")])
def test_the_memo_changes_no_finding(build, name, tmp_path, monkeypatch) -> None:
    """The whole claim rests on this: the memo is a speed change, never an answer change."""
    path = _write(build(), tmp_path / f"{name}.brep")
    with_memo = vk.check_shape(path, booleans=True)
    _without_memos(monkeypatch)
    without = vk.check_shape(path, booleans=True)
    assert with_memo == without, (name, with_memo, without)


def test_the_overlap_really_is_found_so_the_test_is_not_vacuous(tmp_path) -> None:
    """A memo that changes nothing is worthless as evidence if nothing was found either way."""
    found = vk.check_shape(_write(_overlapping_boxes(), tmp_path / "ov.brep"), booleans=True)
    codes = [f.get("code") for f in (found.get("hard") or [])]
    assert "body_overlap" in codes, found
    clean = vk.check_shape(_write(_touching_boxes(), tmp_path / "ct.brep"), booleans=True)
    assert "body_overlap" not in [f.get("code") for f in (clean.get("hard") or [])], clean


def test_the_memo_HITS_across_a_real_step_round_trip_where_the_digest_did_not(tmp_path) -> None:
    """A17's reason to exist, with an ACTUAL export and re-read rather than a re-read of the same bytes.

    The old key was `digest`, which a STEP round trip changes (C16: 109/311 solids survived one); `geometry_key`
    survives 290/311. If the round-tripped shape missed the memo, the memo could never help BETWEEN judgements -
    which is exactly what it did before this change.
    """
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
    from OCP.TopAbs import TopAbs_SOLID

    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v4.verdict.shape_digest import digest, geometry_key

    shape = _overlapping_boxes()
    brep = _write(shape, tmp_path / "before.brep")
    first = vk.check_shape(brep, booleans=True)

    step = str(tmp_path / "rt.step")
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert writer.Write(step) == IFSelect_RetDone, "the round trip needs a STEP file to come back from"
    reader = STEPControl_Reader()
    assert reader.ReadFile(step) == IFSelect_RetDone
    reader.TransferRoots()
    after = reader.OneShape()
    after_brep = _write(after, tmp_path / "after.brep")

    before_solids = list(vk._children(ws_kernel.read_brep(brep), TopAbs_SOLID))
    after_solids = list(vk._children(after, TopAbs_SOLID))
    assert before_solids and len(before_solids) == len(after_solids), (len(before_solids), len(after_solids))

    # the point of the key: it survives what the digest does not
    keys_same = sum(geometry_key(a) == geometry_key(b) for a, b in zip(before_solids, after_solids, strict=True))
    digests_same = sum(digest(a) == digest(b) for a, b in zip(before_solids, after_solids, strict=True))
    assert keys_same == len(before_solids), f"geometry_key survived {keys_same}/{len(before_solids)}"
    # and the memo HITS on the re-read shape, which is what buys the time
    hits_before = vk.SELF_INTERSECTS.hits
    second = vk.check_shape(after_brep, booleans=True)
    assert vk.SELF_INTERSECTS.hits > hits_before, (
        f"the round-tripped shape must hit the memo (digests survived {digests_same}/{len(before_solids)}, "
        f"so a digest-keyed memo would have missed)")
    # same answer on both sides of the trip
    assert [f.get("code") for f in (second.get("hard") or [])] == [f.get("code") for f in (first.get("hard") or [])]


def test_a_shape_whose_key_cannot_be_built_still_gets_an_answer(monkeypatch, tmp_path) -> None:
    """No key, no memo - never a wrong hit and never a crash."""
    monkeypatch.setattr(vk, "geometry_key", lambda shape: (_ for _ in ()).throw(RuntimeError("no key")))
    out = vk.check_shape(_write(_overlapping_boxes(), tmp_path / "nk.brep"), booleans=True)
    assert "body_overlap" in [f.get("code") for f in (out.get("hard") or [])], out
