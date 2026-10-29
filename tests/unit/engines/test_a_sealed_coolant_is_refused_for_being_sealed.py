"""A coolant with no way in and no way out is refused, and refused for that reason.

WHY THIS EXISTS. `ConjugateReport.flowable` has been on the report since the engine was written -
"whether the coolant has a way in and a way out, which a solve needs before anything else: with
neither, nothing fixes its pressure and no solver will run" - and not one of the eight gates read
it. So a sealed coolant cleared every mesh gate and was refused further down for a patch-name
contract mismatch. That refusal is true and it sends the reader to the wrong lever: they go and
look at their patch names, and the problem is that their coolant is a closed box.

THE FIRST CUSTOMER PART IS EXACTLY THIS CASE. Their inlet and outlet are fitting bores that stop
inside the metal, so the block was sized to the fittings' own bodies and the coolant stops 2 mm
short of every face. The delivered fluid region carries ONE patch, `fluid_to_solid`, and the plan
ran with inlet='' and outlet=''. 102,630,992 cells, checkMesh clean on both regions, and nothing
said the thing that actually matters.

A SEALED PART IS STILL A LEGAL PLAN, which is the other half. `murray.py` builds a tree sealed
inside the metal deliberately and it is the hardest topology in the corpus. What is refused is a
part that ASKED for an inlet and an outlet and did not get them.
"""
import json

import pytest

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import gates as G
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION

from test_conjugate_row import REPORT, _Ctx, _manifest, case  # noqa: F401


def _with_ports(inlet_faces: int, outlet_faces: int, *, declared: bool = True) -> dict:
    """The run's own report, plumbed or not."""
    report = json.loads(json.dumps(REPORT))
    patches = dict(report["regions"][FLUID_REGION].get("patches") or {})
    if declared:
        # THE PORT IS NAMED TWICE, as the real report names it: `ports` carries the BLOCK FACE
        # and the delivered patch wears the user's name, because renameBoundary runs before the
        # split. A fixture that used one name for both would pass a lookup that cannot work on a
        # real run - which is what the first version of this did.
        report["ports"] = {"inlet": "ymin", "outlet": "ymax", "reason": ""}
        report["patch_names"] = {"ymin": "coolant_in", "ymax": "coolant_out"}
        patches["coolant_in"] = inlet_faces
        patches["coolant_out"] = outlet_faces
    else:
        report["ports"] = None
    report["regions"][FLUID_REGION]["patches"] = patches
    return report


def _gate():
    return next(g for g in G.CONJUGATE_GATES if g.key == "flowable")


def test_the_gate_exists_and_is_in_the_row():
    """It is not enough for the reading to exist; something has to ask for it."""
    assert _gate().key == "flowable"
    assert "way in and a way out" in _gate().proves


def test_a_plumbed_coolant_passes(case):
    q = F.quality_of(case, _with_ports(128, 128))
    assert q["coolant_ports_declared"] is True
    assert q["coolant_inlet_faces"] == 128 and q["coolant_outlet_faces"] == 128
    assert q["coolant_flowable"] is True
    man = {**_manifest(case), "quality": q}
    ok, said = _gate().check(_Ctx(case, man))
    assert ok, said


@pytest.mark.parametrize("inlet, outlet, words", [
    (0, 128, "no inlet"),
    (128, 0, "no outlet"),
    (0, 0, "no inlet and no outlet"),
])
def test_a_named_port_with_nothing_behind_it_is_refused(case, inlet, outlet, words):
    q = F.quality_of(case, _with_ports(inlet, outlet))
    assert q["coolant_flowable"] is False
    man = {**_manifest(case), "quality": q}
    ok, said = _gate().check(_Ctx(case, man))
    assert not ok
    assert words in said, said
    assert "pressure" in said, "the refusal should say why it cannot be solved, not just that"


def test_a_part_that_never_asked_for_ports_is_not_refused(case):
    """The sealed Murray tree is a deliberate test article, not a defect."""
    q = F.quality_of(case, _with_ports(0, 0, declared=False))
    assert q["coolant_ports_declared"] is False
    man = {**_manifest(case), "quality": q}
    ok, said = _gate().check(_Ctx(case, man))
    assert ok, said


def test_the_customer_case_is_refused_by_this_gate_and_not_by_a_patch_name(case):
    """THE WHOLE POINT. Their fluid region carries one patch and no port, and until now the row
    said their patch names were wrong."""
    report = json.loads(json.dumps(REPORT))
    report["ports"] = {"inlet": "ymin", "outlet": "ymax", "reason": ""}
    report["patch_names"] = {"ymin": "coolant_in", "ymax": "coolant_out"}
    # one patch only, as delivered: the interface, and nothing else
    report["regions"][FLUID_REGION]["patches"] = {"fluid_to_solid": 13_022_400}
    q = F.quality_of(case, report)
    man = {**_manifest(case), "quality": q}
    ok, said = _gate().check(_Ctx(case, man))
    assert not ok
    assert "no inlet and no outlet" in said
    assert _gate().cause.name == "REGION_SPLIT"


def test_the_counts_reach_the_manifest_so_a_reader_can_see_them(case):
    """A gate that refuses without leaving the numbers behind makes the next person measure
    again."""
    q = F.quality_of(case, _with_ports(0, 44))
    for key in ("coolant_ports_declared", "coolant_inlet_faces",
                "coolant_outlet_faces", "coolant_flowable"):
        assert key in q, key
    assert q["coolant_outlet_faces"] == 44
