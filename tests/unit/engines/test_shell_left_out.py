# Responsibility: Verify SHSTAGE-9 and SHELL-60, A SHELL SAYS WHAT IT LEAVES OUT: a shell mesh has no element for a
# mixed body's thick regions or a body with no shell (no solid is made beside a shell, SHSTAGE-7), so the stage, the
# run's words, the result, every deck's header and the reviewer's facts say what is not in the mesh - how many, their
# share of the part's volume and surface, the largest one's size - and the way on, never that they "stay solid". The
# extraction measures each thick region's size and metal (reporting only: the mid-surface is unchanged).
# Boundaries: small built parts of known geometry (tests/shell_fixtures), the real extraction in process, one real
# shell run (gmsh) on the plate with a boss.
from __future__ import annotations

import json
import math

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402
from tests.fea_fixtures import _compound  # noqa: E402

from meshpipeline.application import shell_check as sc  # noqa: E402
from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad import shell_words as sw  # noqa: E402

OLD = ("stays solid", "stay solid", "kept solid", "meshed solid and tied")


def _no_old_words(text: str) -> None:
    for w in OLD:
        assert w not in text, w


@pytest.fixture(scope="module")
def boss():
    """A 200 x 100 x 4 plate with a 30 mm cube on top: 80,000 mm3 of plate, 27,000 of boss."""
    return ms.extract(sf.plate_with_boss(), mm_per_unit=1.0)


def test_a_thick_region_is_measured_by_the_metal_no_shell_element_stands_for(boss):
    rec, _ = boss
    (body,) = rec["bodies"]
    (reg,) = body["solid_regions"]
    assert body["verdict"] == "mixed" and reg["size_mm"] == [30.0, 30.0, 30.0]
    assert reg["volume_mm3"] == pytest.approx(27000.0, rel=0.05)
    # a round rod 16 mm across and 250 mm long hung under a 5 mm plate: all of it is left out
    rod, _ = ms.extract(sf.plate_with_rod(), mm_per_unit=1.0)
    (r,) = rod["bodies"][0]["solid_regions"]
    assert r["size_mm"] == pytest.approx([250.0, 16.0, 16.0], abs=0.05)
    assert r["volume_mm3"] == pytest.approx(math.pi * 8.0 ** 2 * 250.0, rel=0.08)


def test_the_measure_is_reporting_only(boss, monkeypatch):
    """With SHELL-60 off the record is the same but for the new numbers, and the mid-surface is the same."""
    rec, surface = boss
    monkeypatch.setattr(ms, "THICK_MEASURE", False)
    off, surface_off = ms.extract(sf.plate_with_boss(), mm_per_unit=1.0)

    def bare(r: dict) -> dict:
        r = json.loads(json.dumps(r))
        r.pop("seconds", None)
        for b in r["bodies"]:
            b.pop("size_mm", None)
            for g in b.get("solid_regions") or []:
                g.pop("size_mm", None)
                g.pop("volume_mm3", None)
        return r

    assert bare(rec) == bare(off)
    assert "volume_mm3" not in off["bodies"][0]["solid_regions"][0]
    assert ms._area(surface) == pytest.approx(ms._area(surface_off), rel=1e-12)
    assert len(ms._faces(surface)) == len(ms._faces(surface_off))


def test_the_words_say_what_is_not_in_the_mesh_with_its_numbers_and_the_way_on(boss):
    lo = sw.left_out(boss[0])
    assert lo["thick"] == 1 and lo["whole"] == [] and lo["largest_mm"] == [30.0, 30.0, 30.0]
    assert lo["volume_share"] == pytest.approx(27000.0 / 107000.0, rel=0.05)
    assert lo["area_share"] == pytest.approx(4500.0 / 46000.0, rel=1e-6)
    assert sw.left_out_words(lo) == (
        "1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh. For a mesh that includes it, ask for a solid mesh.")
    assert sw.left_out_short(lo) == "1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh."
    lines = sw.left_out_lines(lo)
    assert lines[:2] == ["NOT IN THIS MESH: 1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh.", "For a mesh that includes it, ask for a solid mesh."]
    assert lines[-1] == "- Part_1, thick region 1: 5 faces, about 30 x 30 x 30 mm, about 26% of the part's volume"
    # an all-shell part leaves nothing out and says nothing
    tee, _ = ms.extract(sf.t_joint(), mm_per_unit=1.0)
    assert sw.left_out(tee) == {} and sw.left_out_words(sw.left_out(tee)) == "" and sw.left_out_lines({}) == []
    # a record from before SHELL-60 (no metal measured) says the surface alone
    old = json.loads(json.dumps(boss[0]))
    old["bodies"][0]["solid_regions"][0].pop("volume_mm3")
    assert sw.left_out_short(sw.left_out(old)) == "1 thick region (about 30 x 30 x 30 mm) is not in this shell mesh."
    # several regions: no size, the share, "them"
    many = {**lo, "thick": 3, "regions": lo["regions"] * 3}
    assert sw.left_out_words(many) == ("3 thick regions (26% of the part's volume) are not in this shell mesh. For a "
                                       "mesh that includes them, ask for a solid mesh.")


def test_a_body_with_no_shell_is_left_out_whole():
    """A 5 mm plate and a 50 x 40 x 30 block in one file: the block is not in the shell mesh, said by name."""
    rec, _ = ms.extract(_compound(sf.flat_plate(), sf._box(300, 0, 0, 50, 40, 30)), mm_per_unit=1.0)
    verdicts = {b["body"]: b["verdict"] for b in rec["bodies"]}
    block = next(b for b in rec["bodies"] if b["verdict"] == "solid")
    assert sorted(verdicts.values()) == ["shell", "solid"] and block["size_mm"] == [50.0, 40.0, 30.0]
    lo = sw.left_out(rec)
    total = sum(b["volume_mm3"] for b in rec["bodies"])
    assert lo["whole"] == [block["body"]] and lo["thick"] == 0
    assert lo["volume_share"] == pytest.approx(60000.0 / total, rel=1e-6)
    assert sw.left_out_words(lo) == (f"{block['body']} (about 50 x 40 x 30 mm, {sw.pct(60000.0 / total)} of the part's "
                                     "volume) is not in this shell mesh. For a mesh that includes it, ask for a solid "
                                     "mesh.")
    assert sw.left_out_lines(lo)[-1].startswith(f"- {block['body']}: the whole part, about 50 x 40 x 30 mm")


def test_the_stage_says_what_the_shell_will_keep_as_solid_and_what_if_the_join_fails(boss):
    # MIXED-15: one mixed body MIXED-12 will try to keep whole - the stage said its boss was "not in this shell mesh"
    view = sc.shell_view({"status": sc.READY, "scale_to_m": 0.001, "summary": sc.summarise(boss[0])},
                         {"source_format": "step", "validity": {"kind": "brep"}, "scale_to_m": 0.001},
                         {"value": "shell", "by": "table"})
    assert view["mesh_as"] == "shell"
    assert view["reason"] == ("1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is kept as solid "
                              "elements in the same mesh. If it cannot be joined to the shell, it is left out and the "
                              "result says so.")
    head, row = view["not_shelled"]
    assert head["words"] == view["reason"] and "not in this shell mesh" not in json.dumps(view)
    assert row["words"] == ("A thick region of the part (5 faces, about 30 x 30 x 30 mm, 26% of the part's volume) is "
                            "kept as solid elements: it is 30 mm thick or more, too thick for a shell.")
    assert row["short"] == "Thick region · 30 mm+ thick, 30 x 30 x 30 mm · kept as solid"
    assert sc.settle(view)["why"] == f"{sc.SAID_SHELL} {view['reason']}"
    _no_old_words(json.dumps(view))


def test_the_stage_says_what_the_shell_leaves_out_and_settles_it(boss, monkeypatch):
    from meshpipeline.engines import shell_run

    monkeypatch.setattr(shell_run, "KEEP_THICK", False)      # (MIXED-12 off: the boss is left out, SHSTAGE-9)
    view = sc.shell_view({"status": sc.READY, "scale_to_m": 0.001, "summary": sc.summarise(boss[0])},
                         {"source_format": "step", "validity": {"kind": "brep"}, "scale_to_m": 0.001},
                         {"value": "shell", "by": "table"})
    assert view["mesh_as"] == "shell"
    assert view["reason"] == sw.left_out_words(sw.left_out(boss[0]))
    head, row = view["not_shelled"]
    assert head == {"id": "", "kind": "solid_head", "at_mm": None, "faces": [],
                    "words": view["reason"], "short": "1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh."}
    assert row["id"] == "S1" and row["words"] == (
        "A thick region of the part (5 faces, about 30 x 30 x 30 mm, 26% of the part's volume) is not in this "
        "shell mesh: it is 30 mm thick or more, too thick for a shell.")
    assert row["short"] == "Thick region · 30 mm+ thick, 30 x 30 x 30 mm · not in the mesh"
    settled = sc.settle(view)
    assert settled["mesh_as"] == "shell" and settled["why"] == f"{sc.SAID_SHELL} {view['reason']}"
    assert sc.declaration_words(settled).startswith(sc.SHELL_HEAD)
    _no_old_words(json.dumps(view))
    # a body with no shell beside a shell body: named, not in the mesh
    summary = {"bodies": [{"body": "Part_1", "verdict": "shell"}, {"body": "Part_2", "verdict": "solid"}],
               "solid_regions": [{"body": "Part_2", "whole": True, "why": "no wall of it is a plate"}]}
    (whole,) = sc.not_shelled(summary)
    assert whole["words"] == "Part_2 is not in this shell mesh: no wall of it is a plate."
    _no_old_words(json.dumps(sc.not_shelled({**summary, "solid_more": 3})))


def test_the_run_the_result_the_decks_and_the_reviewer_say_it(tmp_path, monkeypatch):
    gmsh = pytest.importorskip("gmsh")  # noqa: F841
    from meshpipeline.engines import fea_results, fea_review, shell_delivery, shell_run
    from meshpipeline.engines import shell_writers as wr

    # the shell without its thick region: what a run makes when MIXED-12 cannot keep it solid (tested on its own in
    # test_shell_keeps_thick)
    monkeypatch.setattr(shell_run, "KEEP_THICK", False)

    step = sf.write_step(sf.plate_with_boss(), tmp_path / "boss.step")
    ws = tmp_path / "ws"
    ws.mkdir()
    rec = shell_run.run(ws, step, mm_per_unit=1.0)
    assert rec["made"] is True and rec["left_out"]["thick"] == 1
    said = sw.left_out_words(rec["left_out"])
    assert said in rec["words"]
    _no_old_words(rec["words"])
    for name in shell_run.FILES:
        text = (ws / name).read_text()
        assert "NOT IN THIS MESH: 1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh." in text, name
        assert "- Part_1, thick region 1: 5 faces, about 30 x 30 x 30 mm" in text, name
    # the decks still read back whole
    assert len(wr.read_bdf(ws / "shell.bdf").elements) == rec["elements"]
    assert len(wr.read_inp(ws / "shell.inp").elements) == rec["elements"]
    assert len(wr.read_cdb(ws / "shell.cdb").elements) == rec["elements"]
    assert shell_delivery.deliver(ws, rec)
    got = fea_results.results(ws)
    assert got["summary"].endswith(" 1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh.")
    assert got["left_out"]["words"] == said and got["left_out"]["regions"] == 1
    assert any(said in str(w) for w in got["plan_words"])
    _no_old_words(json.dumps(got))
    facts = fea_review.shell_facts_text(ws)
    assert "- LEFT OUT OF THIS SHELL, and told to the user in the result and the decks: " + said in facts


def test_a_record_from_before_says_the_count_truthfully():
    from meshpipeline.engines import shell_run

    said = shell_run.words({"made": True, "elements": 10, "quads": 10, "trias": 0, "solid_bodies": 2})
    assert said.endswith(" 2 part(s) or region(s) are not in this shell mesh.")
    assert "solid" not in shell_run.PLATES_OUT.replace("no solid", "") and "left out" in shell_run.PLATES_OUT


def test_where_plates_do_not_meet_is_said_open_not_solid():
    rec, _ = ms.extract(sf.plate_with_boss(), mm_per_unit=1.0)
    for note in rec["notes"]:
        _no_old_words(note)
    from meshpipeline.engines import shell_places as SP

    for target in ({"id": "Part_2"}, {"area": True, "kind": "boss"}):
        _no_old_words(SP._why_not(target))
