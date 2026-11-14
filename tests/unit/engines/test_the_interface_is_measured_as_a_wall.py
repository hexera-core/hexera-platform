"""The conjugate interface is measured as a plain wall, and put back the moment it has been.

WHY THIS EXISTS. `splitMeshRegions` couples the two halves of a conjugate mesh with a `mappedWall`
pair, and `checkMesh -region fluid` loads only the fluid polyMesh. Every geometric check runs and
prints, and then the last step asks the mapped patch for a sample mesh that was never registered:

    --> FOAM FATAL ERROR: failed lookup of solid (objectRegistry cr_ws_p7bn1361)
        available objects of type polyMesh: 1(fluid)

It dies after the summary and before the verdict. The log then carries every number the parser
wants and neither "Mesh OK." nor "Failed N mesh checks", so `mesh_ok` reads False on a mesh
nothing judged, and `-writeFields` never writes the three fields the viewer's quality sidecar is
built from.

THE TEXT BELOW IS REAL: `constant/fluid/polyMesh/boundary` and `constant/solid/polyMesh/boundary`
as job b6762d5c wrote them on 2026-11-13 - the first customer plate to go through the whole
product - at 326,631,400 cells with the interface conformal at 21,618,626 faces on both sides.
Both regions took that fatal and both were reported to the customer as "checkMesh did not report
Mesh OK".
"""
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE

FLUID = """\
FoamFile
{
    version     2.0;
    format      ascii;
    arch        "LSB;label=32;scalar=64";
    class       polyBoundaryMesh;
    location    "constant/fluid/polyMesh";
    object      boundary;
}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

3
(
    fluid_to_solid
    {
        type            mappedWall;
        inGroups        2(wall mappedPatch);
        nFaces          21618626;
        startFace       191268879;
        sampleMode      nearestPatchFace;
        sampleRegion    solid;
        samplePatch     solid_to_fluid;
    }
    inlet
    {
        type            patch;
        nFaces          1714;
        startFace       212887505;
    }
    mouth_3
    {
        type            wall;
        inGroups        1(wall);
        nFaces          1743;
        startFace       212889219;
    }
)

// ************************************************************************* //
"""

SOLID = """\
FoamFile
{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    location    "constant/solid/polyMesh";
    object      boundary;
}
2
(
    wall_outer
    {
        type            wall;
        inGroups        1(wall);
        nFaces          546551;
        startFace       774385720;
    }
    solid_to_fluid
    {
        type            mappedWall;
        inGroups        2(wall mappedPatch);
        nFaces          21618626;
        startFace       774932271;
        sampleMode      nearestPatchFace;
        sampleRegion    fluid;
        samplePatch     fluid_to_solid;
    }
)
"""

#: What checkMesh printed on that run, cut off exactly where the fatal cut it off: every number
#: and no verdict. The whole point of the fix is that this tail stops happening.
TRUNCATED = """\
Mesh stats fluid
    points:           79934302
    faces:            212890962
    cells:            66624895

Checking geometry...
    Max aspect ratio = 3.79028 OK.
    Mesh non-orthogonality Max: 48.6248 average: 6.21686
    Max skewness = 1.66014 OK.

--> FOAM FATAL ERROR: (openfoam-2412 patch=260127)

    failed lookup of solid (objectRegistry cr_ws_p7bn1361)
"""


def test_a_mapped_interface_is_retyped_and_loses_every_key_only_it_may_carry():
    plain, retyped = FE.as_plain_walls(FLUID)
    assert retyped == ("fluid_to_solid",), retyped
    assert "mappedWall" not in plain
    assert "mappedPatch" not in plain
    for key in ("sampleMode", "sampleRegion", "samplePatch"):
        assert key not in plain, key


def test_the_other_patches_are_left_exactly_as_they_were():
    plain, _ = FE.as_plain_walls(FLUID)
    for kept in ("    inlet\n", "    mouth_3\n", "        type            patch;\n",
                 "        nFaces          1714;\n", "        startFace       212889219;\n"):
        assert kept in plain, kept


def test_not_one_face_moves():
    """The retype is a measurement convenience, so it may not change what is being measured."""
    def faces(text):
        return [ln.split() for ln in text.splitlines()
                if ln.split()[:1] in (["nFaces"], ["startFace"])]

    plain, _ = FE.as_plain_walls(FLUID)
    assert faces(plain) == faces(FLUID)
    assert faces(plain) == [["nFaces", "21618626;"], ["startFace", "191268879;"],
                            ["nFaces", "1714;"], ["startFace", "212887505;"],
                            ["nFaces", "1743;"], ["startFace", "212889219;"]]
    assert plain.count("{") == FLUID.count("{")
    assert plain.count("}") == FLUID.count("}")


def test_the_patch_count_line_is_untouched():
    """It counts patches, and no patch is added or removed - only one is retyped."""
    plain, _ = FE.as_plain_walls(FLUID)
    assert "\n3\n(\n" in plain


def test_the_solid_side_is_the_same_story_with_the_names_the_other_way_round():
    plain, retyped = FE.as_plain_walls(SOLID)
    assert retyped == ("solid_to_fluid",)
    assert "sampleRegion    fluid;" in SOLID        # what it faulted on
    assert "sampleRegion" not in plain              # and what it is handed instead
    assert "        type            wall;\n        inGroups        1(wall);\n" in plain


def test_a_boundary_with_no_mapped_patch_is_handed_back_word_for_word():
    """Nothing is rewritten on a single-region mesh, and the caller is told so by an empty tuple -
    which is what stops `check_mesh` restoring a file it never changed."""
    plain, _ = FE.as_plain_walls(FLUID)
    again, retyped = FE.as_plain_walls(plain)
    assert retyped == ()
    assert again == plain


def _case(tmp_path: Path, region: str, text: str) -> Path:
    p = tmp_path / "constant" / region / "polyMesh"
    p.mkdir(parents=True)
    (p / "boundary").write_text(text, encoding="utf-8")
    return p / "boundary"


def _measured(monkeypatch, written: str, *, blow_up: bool = False) -> list[str]:
    """Stub the subprocess: record what the boundary file said WHILE checkMesh ran."""
    seen: list[str] = []

    def fake(argv, **kw):
        seen.append(Path(kw["cwd"], "constant", "fluid", "polyMesh", "boundary")
                    .read_text(encoding="utf-8"))
        kw["stdout"].write(written)
        if blow_up:
            raise RuntimeError("checkMesh was stopped")
        return 0

    monkeypatch.setattr(FE, "run_guarded", fake)
    monkeypatch.setattr(FE, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(FE, "collect_poor_sets", lambda ws, region="": [])
    return seen


def test_checkmesh_sees_a_wall_and_the_mesh_keeps_its_coupling(tmp_path, monkeypatch):
    boundary = _case(tmp_path, "fluid", FLUID)
    seen = _measured(monkeypatch, "Mesh OK.\n")
    out = FE.check_mesh(tmp_path, region="fluid")
    assert len(seen) == 1
    assert "mappedWall" not in seen[0], "checkMesh was handed the mapped patch after all"
    assert boundary.read_text(encoding="utf-8") == FLUID, "the coupling was not put back"
    assert out["mesh_ok"] is True
    assert out["checked_as_walls"] == ["fluid_to_solid"]


def test_the_coupling_goes_back_even_when_the_check_is_stopped(tmp_path, monkeypatch):
    """A timeout used to be the end of the run; it must not also be the end of the mesh."""
    boundary = _case(tmp_path, "fluid", FLUID)
    _measured(monkeypatch, "", blow_up=True)
    out = FE.check_mesh(tmp_path, region="fluid")
    assert out["fatal"] == ["checkMesh did not finish"]
    assert boundary.read_text(encoding="utf-8") == FLUID


def test_the_pre_split_mesh_is_measured_as_it_stands(tmp_path, monkeypatch):
    """With no region named there is no mapped patch to trip over - `checkMesh -constant` reads
    the mesh from before the split - so nothing is rewritten and nothing has to be put back."""
    boundary = _case(tmp_path, "fluid", FLUID)
    monkeypatch.setattr(FE, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(FE, "collect_poor_sets", lambda ws, region="": [])
    monkeypatch.setattr(FE, "run_guarded",
                        lambda argv, **kw: kw["stdout"].write("Mesh OK.\n") and 0)
    out = FE.check_mesh(tmp_path)
    assert "checked_as_walls" not in out
    assert boundary.read_text(encoding="utf-8") == FLUID


def test_the_reading_that_the_fatal_produced_is_the_one_this_fix_is_for():
    """Proof the truncated log is the shape claimed: every number, no verdict, mesh_ok False."""
    q = FE.parse_check_mesh(TRUNCATED)
    assert q["cells"] == 66624895
    assert q["max_non_ortho"] == pytest.approx(48.6248)
    assert q["mesh_ok"] is False
