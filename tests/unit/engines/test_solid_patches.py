# Responsibility: Verify MIXED-9, the solid patches of a shell run: a region the extraction keeps solid (a boss, a
# block) is meshed as solid and tied to the shell in the shell's own files, said in one line; nothing solid is today's
# shell exactly; "shell only" in the notes is today's shell, said; the decks carry a section and a material per patch;
# the stage says the same line; the same part twice gives the same files.
# Boundaries: the words, the record reading and the deck writers are pure; the runs mesh a small plate with a boss
# (about a minute each, in this process). The CalculiX accuracy cases are test_solid_patches_textbook.py.
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines import shell_run
from meshpipeline.engines import solid_patches as SP


# ------------------------------------------------------------------------------------------------ the words ----
def test_the_line_counts_the_regions_and_names_the_bodies_only_for_several():
    one = [{"body": "Part_1", "whole": False, "count": 2}]
    assert SP.words(one) == "2 regions are too thick for a shell and are meshed as solid, tied to the shell."
    assert SP.words([{"body": "Part_1", "whole": False, "count": 1}]) == (
        "1 region is too thick for a shell and is meshed as solid, tied to the shell.")
    many = [{"body": "Part_3", "whole": False, "count": 2}, {"body": "Part_7", "whole": True, "count": 1}]
    assert SP.words(many, bodies=9) == ("3 regions are too thick for a shell and are meshed as solid, tied to the shell "
                                        "(Part_3: 2 regions; Part_7: whole part).")
    assert SP.words([]) == ""


def test_what_stays_out_after_all_is_said_with_its_reason():
    assert SP.left_words([]) == ""
    assert SP.left_words([{"body": "Part_2", "count": 1, "why": "Part_2 does not meet the shell at any plate edge"}]) == (
        "1 region stays solid and is not in the shell files: Part_2 does not meet the shell at any plate edge.")


def test_the_record_s_solid_regions_and_whole_solid_bodies_are_the_patches():
    rec = {"surface_faces": ["Mid_1"], "bodies": [
        {"body": "Part_1", "verdict": "mixed", "solid_regions": [{"faces": ["F1"]}, {"faces": ["F2"]}]},
        {"body": "Part_2", "verdict": "shell"},
        {"body": "Part_3", "verdict": "solid"}]}
    assert SP.solid_rows(rec) == [{"body": "Part_1", "whole": False, "count": 2},
                                  {"body": "Part_3", "whole": True, "count": 1}]
    # a part with no shell anywhere has nothing to tie a block to: meshed solid as before
    assert SP.solid_rows({"surface_faces": [], "bodies": [{"body": "Part_1", "verdict": "solid"}]}) == []
    assert SP.solid_rows({"surface_faces": ["Mid_1"], "bodies": [{"body": "Part_1", "verdict": "shell"}]}) == []


# --------------------------------------------------------------------------------------- the notes: shell only ----
def _read(entries: list[dict], notes: str) -> dict:
    raw = {"stated_unit": "none", "sizes": [], "element_kind": entries, "limits": [], "other": []}
    return MR.check(json.dumps(raw), notes)


@pytest.mark.parametrize("notes", ["Shell only please.", "No solid elements anywhere.",
                                   "Leave out what can't be shelled.", "Plates only, skip the thick parts."])
def test_shell_only_is_read_from_the_notes_as_an_element_kind(notes):
    quote = notes.rstrip(".")
    req, dropped, _na = _read([{"what": "shell_only", "value": 0, "quote": quote}], notes)
    assert req["kind"]["shell_only"] is True and dropped == []
    assert SP.shell_only(req) and not MR.is_empty(req)
    (row,) = req["rows"]
    assert row["field"] == "shell_only" and row["asked"].startswith("a shell only")


def test_a_plain_shell_request_is_not_read_as_shell_only():
    req, dropped, _na = _read([{"what": "shell_only", "value": 0, "quote": "FEA shell mesh"}], "FEA shell mesh of this part")
    assert req["kind"]["shell_only"] is None and not SP.shell_only(req)
    assert dropped and "do not ask for a shell only" in dropped[0]


def test_the_empty_set_asks_for_patches_and_the_report_says_shell_only():
    assert MR.empty()["kind"]["shell_only"] is None and MR.is_empty(MR.empty())
    assert not SP.shell_only(None) and not SP.shell_only(MR.empty())
    from meshpipeline.engines.shell_mesh import ShellMesh

    mesh = ShellMesh(xyz=np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float),
                     quads=np.array([[0, 1, 2, 3]]), trias=np.zeros((0, 3), dtype=np.int64), wall=np.array([2.0]),
                     mid=["Mid_1"], part=["Part_1"])
    rep = SR.report(mesh, {"rows": [{"id": "r1", "field": "shell_only", "quote": "shell only", "asked": "a shell only"}]})
    assert rep["rows"][0]["met"] is True and rep["rows"][0]["delivered"] == "only shell elements are in the files"


# ------------------------------------------------------------------------------------------ the run's words ----
def _made(**extra) -> dict:
    return {"made": True, "elements": 10, "quads": 10, "trias": 0, "walls_mm": [2.0], **extra}


def test_the_run_says_the_patches_or_why_there_are_none():
    plain = shell_run.words(_made())
    assert plain == ("Shell mesh: 10 elements (10 quads, 0 triangles) on the mid-surface, wall 2 mm. Files: Nastran "
                     ".bdf, CalculiX and Abaqus .inp, Ansys .cdb.")
    # nothing patched (no solid region): today's words exactly
    assert shell_run.words(_made(solid_bodies=2)) == plain + " 2 part(s) or region(s) stay solid and are not in the shell files."
    line = SP.words([{"body": "Part_1", "whole": False, "count": 2}])
    got = shell_run.words(_made(solid_bodies=2, patches=[{"body": "Part_1", "count": 2}], patch_words=line))
    assert got == f"{plain} {line}"
    assert shell_run.words(_made(solid_bodies=2, shell_only=True)).endswith(
        "2 part(s) or region(s) stay solid and are not in the shell files: your notes asked for a shell only.")
    assert "could not be meshed as solid and tied to the shell (the split failed)" in shell_run.words(
        _made(solid_bodies=1, patch_why="the split failed"))


# ------------------------------------------------------------------------------------------- the stage line ----
def test_the_stage_says_the_same_line_from_the_shell_check():
    from meshpipeline.application import shell_check as sc

    summary = {"bodies": [{"body": "Part_1", "verdict": "mixed"}],
               "solid_regions": [{"body": "Part_1", "whole": False}, {"body": "Part_1", "whole": False}]}
    assert sc.patch_words(summary) == "2 regions are too thick for a shell and are meshed as solid, tied to the shell."
    many = {"bodies": [{"body": "Part_1", "verdict": "shell"}, {"body": "Part_2", "verdict": "solid"}],
            "solid_regions": [{"body": "Part_2", "whole": True}]}
    assert sc.patch_words(many).endswith("(Part_2: whole part).")
    assert sc.patch_words({"bodies": [{"body": "Part_1", "verdict": "shell"}], "solid_regions": []}) == ""
    assert sc.patch_words({"bodies": [{"body": "Part_1", "verdict": "solid"}],
                           "solid_regions": [{"body": "Part_1", "whole": True}]}) == ""


# ------------------------------------------------------------------------------------------------ the decks ----
def _tiny():
    """One shell quad (Part_1) and two tetrahedra in two patches (Part_1 and Part_2, a body solid only), one tie."""
    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines import shell_links as sl
    from meshpipeline.engines.shell_mesh import ShellMesh

    shell = ShellMesh(xyz=np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float),
                      quads=np.array([[0, 1, 2, 3]]), trias=np.zeros((0, 3), dtype=np.int64), wall=np.array([2.0]),
                      mid=["Mid_1"], part=["Part_1"])
    corners = np.array([[2, 0, 0], [3, 0, 0], [2, 1, 0], [2, 0, 1]], float)
    mids = [(corners[a] + corners[b]) / 2 for a, b in ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))]
    xyz = np.vstack([corners, mids, corners + [0, 0, 5], np.array(mids) + [0, 0, 5]])
    tets = np.array([list(range(1, 11)), list(range(11, 21))], dtype=np.int64)
    solid = mm.SolidPart(xyz=xyz, tets=tets, faces={})
    ties = [sl.Tie(2, 1, [(5, 1, 1.0)])]
    m = mm.MixedMesh(shell=shell, solid=solid, part="Part_1", ties=ties, links=[], n_overlap=0)
    m.regions = [mm.SolidRegion("1_Part_1", "Part_1", 0, 1), mm.SolidRegion("2_Part_2", "Part_2", 1, 1)]
    return m


def test_every_deck_has_a_section_and_a_material_per_patch(tmp_path):
    from meshpipeline.engines import mixed_mesh as mm

    m = _tiny()
    tmp_path = tmp_path / "decks"
    tmp_path.mkdir()
    got = mm.write_all(tmp_path, m, units="mm", stem="shell")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shell.bdf", "shell.cdb", "shell.inp", "shell_abaqus.inp"]
    assert got["calculix"]["elements"] == 3 and got["nastran"]["ties"] == 1
    inp = (tmp_path / "shell.inp").read_text()
    assert "*SHELL SECTION, ELSET=P_1, MATERIAL=MAT_1" in inp
    assert "*SOLID SECTION, ELSET=SOLID_1_Part_1, MATERIAL=MAT_1" in inp
    assert "*SOLID SECTION, ELSET=SOLID_2_Part_2, MATERIAL=MAT_2" in inp     # a body solid only: numbered on
    assert "**   MAT_2 = Part_2 (solid only)" in inp and inp.count("TYPE=C3D10") == 2 and "*EQUATION" in inp
    bdf = (tmp_path / "shell.bdf").read_text()
    cards = [ln for ln in bdf.splitlines() if ln.startswith("CTETRA")]
    # each patch's tetrahedra in its own PSOLID (after the shell's PSHELL 1), naming its part's material
    assert [ln.split()[2] for ln in cards] == ["2", "3"]
    assert "PSOLID         2       1" in bdf and "PSOLID         3       2" in bdf
    assert "$ HEXERA GROUP 1 SOLID_1_Part_1" in bdf and "$   3 THRU 3" in bdf and "MPC*" in bdf
    assert "PSHELL" in bdf and "$   MAT1 2 = Part_2 (solid only)" in bdf
    cdb = (tmp_path / "shell.cdb").read_text()
    assert "ET,2,187" in cdb and "CE,NEXT,0,2,UX,1,5,UX,-1" in cdb and cdb.rstrip().endswith("FINISH")
    # the EBLOCK rows: material first, type 2 (SOLID187); the second patch carries MAT 2
    block = cdb.rsplit("EBLOCK,19,SOLID", 1)[1].splitlines()[2:]
    assert [(int(r[:9]), int(r[9:18])) for r in block[0:4:2]] == [(1, 2), (2, 2)]


def test_the_viewer_mesh_shows_each_patch_as_its_own_group(tmp_path):
    m = _tiny()
    patches = [{"name": "Solid_1", "body": "Part_1"}, {"name": "Solid_2", "body": "Part_2"}]
    rows = SP.write_msh(tmp_path / "shell.msh", m, patches)
    assert [r["name"] for r in rows] == ["Mid_1", "Solid_1", "Solid_2"] and rows[1]["solid"] is True
    text = (tmp_path / "shell.msh").read_text()
    assert '2 2 "Solid_1"' in text and '2 3 "Solid_2"' in text
    n_nodes = int(text.split("$Nodes\n", 1)[1].split("\n", 1)[0])
    assert n_nodes == 4 + 20
    # a lone tetrahedron's skin is its four faces
    els = text.split("$Elements\n", 1)[1].split("$EndElements")[0].strip().splitlines()
    assert int(els[0]) == 1 + 4 + 4 == len(els) - 1


# ------------------------------------------------------------------------------------------------ the runs ----
pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")


@pytest.fixture(scope="module")
def boss(tmp_path_factory):
    from tests import shell_fixtures as sf

    ws = tmp_path_factory.mktemp("boss")
    step = sf.write_step(sf.plate_with_boss(), ws / "part.step")
    return step


def _run(step: Path, ws: Path, requirements: dict | None = None) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    return shell_run.run(ws, step, mm_per_unit=1.0, source=step.name, requirements=requirements)


def test_a_boss_is_meshed_solid_and_tied_into_the_shell_files(boss, tmp_path):
    from meshpipeline.engines import shell_writers as sw

    ws = tmp_path / "a"
    rec = _run(boss, ws)
    assert rec["made"] is True and rec.get("patch_why") is None, rec.get("patch_why")
    (p,) = rec["patches"]
    assert p["body"] == "Part_1" and not p["whole"] and p["elements"] == rec["solid_elements"] > 0
    assert rec["ties"] > 0 and rec["links"] >= 1 and rec["overlap_elements"] > 0
    assert rec["words"].endswith("1 region is too thick for a shell and is meshed as solid, tied to the shell.")
    inp = (ws / "shell.inp").read_text()
    assert "TYPE=C3D10" in inp and "*SOLID SECTION, ELSET=SOLID_1_Part_1" in inp and "*EQUATION" in inp
    assert "CTETRA" in (ws / "shell.bdf").read_text() and "SOLID187" not in (ws / "shell.bdf").read_text()
    assert "ET,2,187" in (ws / "shell.cdb").read_text()
    # the shell part reads back as it always did: plates only, the patches after them
    assert len(sw.read_bdf(ws / "shell.bdf").elements) == rec["elements"]
    assert [f["name"] for f in rec["faces"] if f.get("solid")] == ["Solid_1"]
    assert rec["quality"]["valid"] is True
    # every link node is tied: the join says nothing missing
    assert not rec.get("left")


def test_shell_only_in_the_notes_leaves_the_boss_out_as_before(boss, tmp_path):
    req = {**MR.empty(), "kind": {**MR.empty()["kind"], "shell_only": True},
           "rows": [{"id": "r1", "field": "shell_only", "quote": "shell only", "asked": "a shell only"}]}
    rec = _run(boss, tmp_path / "b", req)
    assert rec["made"] is True and not rec.get("patches") and rec["shell_only"] is True
    assert "TYPE=C3D10" not in (tmp_path / "b" / "shell.inp").read_text()
    assert rec["words"].endswith("stay solid and are not in the shell files: your notes asked for a shell only.")


def test_the_same_part_twice_gives_the_same_files(boss, tmp_path):
    a, b = _run(boss, tmp_path / "x"), _run(boss, tmp_path / "y")
    assert a["solid_elements"] == b["solid_elements"] and a["ties"] == b["ties"]
    for f in (*shell_run.FILES, shell_run.MSH):
        assert (tmp_path / "x" / f).read_bytes() == (tmp_path / "y" / f).read_bytes(), f


def test_a_part_with_nothing_solid_is_today_s_shell(tmp_path):
    from tests import shell_fixtures as sf

    step = sf.write_step(sf.t_joint(), tmp_path / "t.step")
    rec = _run(step, tmp_path / "t")
    assert rec["made"] is True and not rec.get("solid_bodies")
    assert not {"patches", "patch_why", "shell_only", "solid_elements", "patch_s"} & set(rec)
    assert "C3D10" not in (tmp_path / "t" / "shell.inp").read_text()
