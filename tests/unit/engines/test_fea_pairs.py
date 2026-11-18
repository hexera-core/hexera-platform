# Responsibility: Verify PLAN-fea-19/20/21 on small assemblies of known geometry: each pair of parts gets its own joint type (Bonded / Contact / Off) by rule or as the user set it, the choice reaches the staging and every solver file, the smaller body keeps its full shape where parts pass into each other (the user may name the other), the stage lists every pair with what was measured, and a bonded pair the mesh leaves open is named in red - never a clean pass.
# Boundaries: parts are built in the test (tests/fea_fixtures.py) and meshed in-process by the REAL FEA driver where a mesh is needed; no solver runs here (tests/unit/engines/test_fea_textbook.py solves the Off-and-bonded case in CalculiX).
from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.cad import fea_pairs as P

# #
# the rule alone (no geometry)
# #

TOUCH = {"id": "Interface_1", "kind": "touching", "parts": ["Part_1", "Part_2"], "area_mm2": 100.0}
GAP = {"id": "Interface_2", "kind": "gap", "parts": ["Part_3", "Part_1"], "gap_mm": 0.2, "area_mm2": 40.0}
OVER = {"id": "Interface_3", "kind": "overlap", "parts": ["Part_1", "Part_4"], "overlap_share": 0.1}


def test_a_pair_takes_the_whole_assembly_answer_by_default():
    for conn in ("bonded", "contact"):
        rows = P.choose([TOUCH, GAP, OVER], connection=conn, volume_of={"Part_1": 10.0, "Part_4": 1.0})
        assert [r["type"] for r in rows] == [conn, conn, conn]
        assert all(r["source"] == "rule" for r in rows)
    # the small-gap switch off: a small-gap pair is Off, the others keep the answer
    rows = P.choose([TOUCH, GAP], connection="bonded", gap_joins=False)
    assert [r["type"] for r in rows] == ["bonded", "off"]
    assert "switched off" in rows[1]["why"]


def test_a_pair_the_user_set_stands_over_the_rule_and_is_said_as_theirs():
    rows = P.choose([TOUCH, GAP], connection="bonded", overrides={"Part_1|Part_2": {"type": "contact"},
                                                                   "Part_1|Part_3": {"type": "off"}})
    assert [(r["key"], r["type"], r["source"]) for r in rows] == [("Part_1|Part_2", "contact", "user"),
                                                                  ("Part_1|Part_3", "off", "user")]
    assert "on the stage" in rows[0]["why"]
    # the key is the two parts in number order, whatever order the interface names them in
    assert P.pair_key(["Part_10", "Part_9"]) == "Part_9|Part_10"


def test_the_smaller_body_keeps_its_shape_and_the_same_size_leaves_it_with_the_first():
    keep, why = P.default_keep(["Part_1", "Part_4"], {"Part_1": 10.0, "Part_4": 1.0})
    assert keep == "Part_4" and "smaller body" in why and "Part_1 gives way" in why
    keep, why = P.default_keep(["Part_2", "Part_1"], {"Part_1": 5.0, "Part_2": 5.0 * (1 + 1e-5)})
    assert keep == "Part_1" and "same size" in why
    rows = P.choose([OVER], connection="bonded", volume_of={"Part_1": 10.0, "Part_4": 1.0},
                    overrides={"Part_1|Part_4": {"keep": "Part_1"}})
    assert rows[0]["keep"] == "Part_1" and rows[0]["keep_default"] == "Part_4" and rows[0]["keep_source"] == "user"


def test_what_the_stage_sends_is_checked_and_only_changes_are_kept():
    raw = {"Part_2|Part_1": {"type": "OFF", "keep": "Part_9", "reach_mm": "0.4"}, "nonsense": {"type": "bonded"},
           "Part_1|Part_4": {"type": "glued"}, "Part_5|Part_6": {"reach_mm": -1}}
    assert P.clean_overrides(raw) == {"Part_1|Part_2": {"type": "off", "reach_mm": 0.4}}
    rows = [{"key": "Part_1|Part_2", "parts": ["Part_1", "Part_2"], "type": "bonded"}]
    assert P.clean_overrides([{"parts": ["Part_2", "Part_1"], "type": "contact"}]) == {"Part_1|Part_2": {"type": "contact"}}
    # a pair left as the rule has it is not stored: it follows the rule when the run stages it
    assert P.changes_only({"Part_1|Part_2": {"type": "bonded"}}, rows) == {}
    assert P.changes_only({"Part_1|Part_2": {"type": "contact"}}, rows) == {"Part_1|Part_2": {"type": "contact"}}
    # a pair the assembly does not have is dropped
    assert P.changes_only({"Part_7|Part_8": {"type": "off"}}, rows) == {}


def test_every_pair_and_every_owner_is_a_decision_with_its_reason():
    rows = P.choose([TOUCH, OVER], connection="bonded", volume_of={"Part_1": 10.0, "Part_4": 1.0},
                    overrides={"Part_1|Part_2": {"type": "contact"}})
    got = P.decisions(rows, connection="bonded", gap_joins=True)
    assert [(d["rule"], d["value"], d["source"]) for d in got] == [
        ("PLAN-fea-19", "contact", "user"), ("PLAN-fea-19", "bonded", "rule"), ("PLAN-fea-20", "Part_4", "rule")]
    assert all(d["words"] for d in got)
    many = P.decisions(P.choose([{**TOUCH, "parts": [f"Part_{k}", f"Part_{k + 1}"]} for k in range(1, 260)],
                                connection="bonded"), connection="bonded", gap_joins=True)
    assert len(many) == P.CITE_EACH + 1 and "more pair" in many[-1]["words"]


def test_an_open_pair_is_said_with_its_faces_and_the_way_on():
    said = P.open_words({"name": "Tie2", "parts": ["Part_1", "Part_2"], "reach_m": 5e-5, "reach_from": "set",
                         "needed_m": 2.0e-4, "faces": [{"face": "Part_2_Face_1", "nearest_m": 2.0e-4}]})
    assert said.startswith("Tie2 (Part_1 and Part_2): Part_2_Face_1 (nearest 0.2 mm) has no mesh node")
    assert "Contact or Off" in said and "at least 0.21 mm" in said


def test_an_open_pair_that_overlaps_is_told_to_mend_the_overlap_never_to_widen_the_reach():
    # lab, 28 Oct: a mixed setup meshes the assembly apart, and a bonded pair that passes into the other
    # part is tied only where the surfaces cross (one needed a 17 mm "reach") - no reach mends that
    said = P.open_words({"name": "Tie_5", "parts": ["Part_3", "Part_4"], "reach_m": 3.5e-5,
                         "reach_from": "solver default", "needed_m": 0.017, "overlap": True,
                         "faces": [{"face": "Part_4_Face_2", "nearest_m": 0.017}]})
    assert said.startswith("Tie_5 (Part_3 and Part_4): Part_4_Face_2 (nearest 17 mm) has no mesh node")
    assert "pass into each other" in said and "remove the overlap in the CAD" in said
    assert "reach to at least" not in said


def test_a_reach_set_for_a_pair_counts_as_set_on_the_stage():
    rows = P.choose([{"id": "Interface_1", "kind": "gap", "parts": ["Part_1", "Part_2"], "gap_mm": 0.2}],
                    connection="bonded", overrides={"Part_1|Part_2": {"type": "bonded", "reach_mm": 0.3}})
    assert rows[0]["source"] == "rule" and rows[0]["reach_mm"] == 0.3
    assert P.counts(rows)["edited"] == 1


def test_the_verdict_reads_an_open_pair_as_a_concern_and_an_unrun_check_as_unmeasured():
    from meshpipeline.contracts.decision_record import limit_met
    from meshpipeline.engines.verdict_limits import fea_limits

    def row(asm):
        q = {"fea": {"plan": True, "assembly": asm}, "element_order": 2}
        return next((r for r in fea_limits(q) if r["key"] == "fea_open_bonded_pairs"), None)

    opened = {"name": "Tie1", "parts": ["Part_1", "Part_2"], "reach_m": 1e-4, "needed_m": 3e-4, "faces": []}
    r = row({"bonded_check": {"checked": 2, "closed": 1}, "open_bonded": [opened]})
    assert limit_met(r["measured"], r["op"], r["limit"]) is False and "Tie1 (Part_1 and Part_2)" in r["measured"][0]
    r = row({"bonded_check": {"checked": 2, "closed": 2}, "open_bonded": []})
    assert limit_met(r["measured"], r["op"], r["limit"]) is True
    r = row({"bonded_check": {"checked": None, "closed": None, "error": "x"}, "open_bonded": []})
    assert limit_met(r["measured"], r["op"], r["limit"]) is None          # not measured, never taken as closed
    assert row({"bonded_check": {"checked": 0, "closed": 0}, "open_bonded": []}) is None   # no tie: no row


# #
# staging (geometry, no mesh)
# #

OCP = pytest.importorskip("OCP.BRepPrimAPI")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import fea_assembly as A  # noqa: E402


def _by_key(side) -> dict[str, dict]:
    return {p["key"]: p for p in side["pairs"]}


def test_half_welded_half_bolted_meshes_apart_with_a_tie_and_a_contact_pair(tmp_path):
    shape = fx.welded_and_bolted()
    plain = A.stage(shape, A.BONDED, tmp_path / "plain", mm_per_unit=1.0)
    # every pair bonded: one fused mesh, as before
    assert plain["mode"] == "bonded" and {p["kind"] for p in plain["pairs"]} == {"bonded"}
    mixed = A.stage(shape, A.BONDED, tmp_path / "mixed", mm_per_unit=1.0,
                    pairs={"Part_1|Part_3": {"type": "contact"}})
    assert mixed["mode"] == "tie"
    got = _by_key(mixed)
    assert got["Part_1|Part_2"]["kind"] == "tie" and got["Part_1|Part_3"]["kind"] == "contact"
    assert any("set not to be bonded" in n for n in mixed["notes"])
    assert mixed["joint_counts"] == {"bonded": 1, "contact": 1, "off": 0, "edited": 1}
    assert mixed["pair_choices"] == {"Part_1|Part_3": {"type": "contact"}}
    # a contact assembly with one pair set bonded: that pair tied, the other a contact pair
    other = A.stage(shape, A.CONTACT, tmp_path / "contact", mm_per_unit=1.0, pairs={"Part_1|Part_2": {"type": "bonded"}})
    got = _by_key(other)
    assert other["mode"] == "contact" and got["Part_1|Part_2"]["kind"] == "tie" and got["Part_1|Part_3"]["kind"] == "contact"


def test_a_pair_set_off_is_not_joined_and_said(tmp_path):
    side = A.stage(fx.welded_and_bolted(), A.BONDED, tmp_path / "off", mm_per_unit=1.0,
                   pairs={"Part_1|Part_3": {"type": "off"}})
    assert set(_by_key(side)) == {"Part_1|Part_2"}
    assert any("Part_1 and Part_3 are not joined" in n for n in side["notes"])
    assert any("Part_3 touches no other part it is joined to" in n for n in side["notes"])


def test_a_small_gap_pair_set_contact_keeps_the_fused_mesh(tmp_path):
    # a small-gap pair's sides are not one surface: it can be anything inside a fused bonded mesh
    side = A.stage(fx.gapped_blocks(10.0, 0.2), A.BONDED, tmp_path / "g", mm_per_unit=1.0,
                   pairs={"Part_1|Part_2": {"type": "contact"}})
    (pair,) = side["pairs"]
    assert side["mode"] == "bonded" and pair["kind"] == "contact" and pair["reach"] > 0.2
    off = A.stage(fx.gapped_blocks(10.0, 0.2), A.BONDED, tmp_path / "o", mm_per_unit=1.0, gap_joins=False)
    assert off["pairs"] == [] and off["joints"][0]["type"] == "off" and off["joints"][0]["source"] == "rule"
    # the switch off, and this one pair set bonded on its own: joined across its gap
    back = A.stage(fx.gapped_blocks(10.0, 0.2), A.BONDED, tmp_path / "b", mm_per_unit=1.0, gap_joins=False,
                   pairs={"Part_1|Part_2": {"type": "bonded"}})
    assert [p["kind"] for p in back["pairs"]] == ["tie"]


def test_a_gap_is_said_in_millimetres_whatever_unit_the_model_is_in(tmp_path):
    # the mesher stages in metres: the punch's 0.5 mm gaps once read "across 0.0005 mm" (4 Nov)
    side = A.stage(fx.gapped_blocks(0.01, 0.0002), A.BONDED, tmp_path / "m", mm_per_unit=1000.0)
    (joint,) = side["joints"]
    assert "across 0.2 mm" in joint["why"], joint["why"]


def test_the_pin_keeps_its_full_shape_in_its_bracket_and_the_user_can_say_otherwise(tmp_path):
    side = A.stage(fx.pin_in_bracket(), A.BONDED, tmp_path / "pin", mm_per_unit=1.0)
    assert side["mode"] == "bonded"
    (ring,) = [v for v in side["volumes"] if len(v["parts"]) > 1]
    assert ring["parts"] == [1, 2] and ring["owner"] == 2            # the pin (Part_2), not the first part
    (choice,) = [c for c in side["joints"] if c["kind"] == "overlap"]
    assert choice["keep"] == "Part_2" and choice["keep_source"] == "rule"
    assert any("Part_2 keeps it from Part_1" in n and "smaller body keeps its full shape" in n for n in side["notes"])
    mine = A.stage(fx.pin_in_bracket(), A.BONDED, tmp_path / "mine", mm_per_unit=1.0,
                   pairs={"Part_1|Part_2": {"keep": "Part_1"}})
    (ring,) = [v for v in mine["volumes"] if len(v["parts"]) > 1]
    assert ring["owner"] == 1 and any("(your choice)" in n for n in mine["notes"])


def test_a_part_every_piece_of_which_another_keeps_is_said_never_dropped_quietly(tmp_path):
    # lab, 28 Oct (g_c0282): a bar drawn whole AND as three pieces - each piece is the smaller body, keeps
    # its share, and the bar is left with no elements; the files must say so
    bar = fx._compound(fx._box(0, 0, 0, 30, 10, 10), fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10),
                       fx._box(20, 0, 0, 10, 10, 10))
    side = A.stage(bar, A.BONDED, tmp_path / "bar", mm_per_unit=1.0)
    assert side["mode"] == "bonded"
    owned = {v["parts"][0] if len(v["parts"]) == 1 else v["owner"] for v in side["volumes"]}
    assert 1 not in owned
    (said,) = [n for n in side["notes"] if n.startswith("Part_1 has no elements of its own")]
    assert "Part_2, Part_3 and Part_4" in said and "remove the doubled material" in said
    # the pieces given to the bar instead: the bar keeps every piece, and each piece is said as before
    back = A.stage(bar, A.BONDED, tmp_path / "back", mm_per_unit=1.0,
                   pairs={f"Part_1|Part_{k}": {"keep": "Part_1"} for k in (2, 3, 4)})
    assert not any(n.startswith("Part_1 has no elements") for n in back["notes"])
    assert sum("has no elements of its own" in n for n in back["notes"]) == 3


def test_the_overlap_is_measured_for_the_card():
    rec = A.record(fx.pin_in_bracket(), mm_per_unit=1.0)
    (itf,) = [i for i in rec["interfaces"] if i["kind"] == "overlap"]
    # a 1 mm ring, 10 mm tall: about 0.9 mm deep (the piece's volume over half its surface)
    assert itf["depth_mm"] == pytest.approx(0.91, rel=0.05)
    assert all(p["volume_mm3"] > 0 for p in rec["parts"])


# #
# the stage: every pair listed, and the answer stored
# #

def _facts(shape):
    from meshpipeline.cad.fea_measure import FACT_KEY

    return {"solids": 3, FACT_KEY: {A.FACT_KEY: A.record(shape, mm_per_unit=1.0)}}


def test_the_stage_lists_every_pair_with_what_was_measured_and_its_defaults():
    from meshpipeline.application import fea_check

    asm = fea_check.assembly_view(_facts(fx.welded_and_bolted()))
    rows = {r["key"]: r for r in asm["pairs"]}
    assert set(rows) == {"Part_1|Part_2", "Part_1|Part_3"}
    r = rows["Part_1|Part_2"]
    assert r["kind"] == "touching" and r["area_mm2"] == pytest.approx(400.0, rel=1e-3) and r["type"] == "bonded"
    assert r["defaults"] == {"bonded|on": "bonded", "bonded|off": "bonded", "contact|on": "contact", "contact|off": "contact"}
    assert [t["key"] for t in asm["pair_types"]] == ["bonded", "contact", "off"] and asm["pair_note"]
    over = {r["key"]: r for r in fea_check.assembly_view(_facts(fx.pin_in_bracket()))["pairs"]}["Part_1|Part_2"]
    assert over["keep"] == "Part_2" and "smaller body" in over["keep_why"] and over["depth_mm"] > 0
    gap = fea_check.assembly_view(_facts(fx.gapped_blocks(10.0, 0.2)))["pairs"][0]
    assert gap["defaults"]["bonded|off"] == "off" and gap["gap_mm"] == pytest.approx(0.2, abs=1e-3)


def test_the_stage_keeps_only_the_pairs_the_user_changed_and_says_them():
    from meshpipeline.application import fea_check

    asm = fea_check.assembly_view(_facts(fx.welded_and_bolted()))
    sent = {"Part_1|Part_2": {"type": "bonded"}, "Part_1|Part_3": {"type": "contact"}}
    got = fea_check.pairs_answer(sent, asm, connection="bonded", gap_joins=True)
    assert got == {"Part_1|Part_3": {"type": "contact"}}
    # under Contact for the whole assembly the same choice is the rule, so nothing is kept
    assert fea_check.pairs_answer(sent, asm, connection="contact", gap_joins=True) == {"Part_1|Part_2": {"type": "bonded"}}
    assert fea_check.pairs_summary(got, asm) == "Set for single pairs: Part_1 and Part_3: Contact."


def test_the_run_reads_the_pairs_off_the_stored_confirmation(monkeypatch):
    from meshpipeline.application import fea_check

    stored = {"fea_pairs": {"Part_1|Part_3": {"type": "off"}, "bad": {"type": "x"}}}

    class _Store:
        def get_bytes(self, object_key):
            return json.dumps(stored).encode()

    monkeypatch.setattr("meshpipeline.contracts.object_storage.get_object_store", lambda: _Store())
    assert fea_check.confirmed_pairs("sid") == {"Part_1|Part_3": {"type": "off"}}
    assert fea_check.confirmed_pairs("") == {}


# #
# meshed: the decks, and the open-pair check
# #

gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter  # noqa: E402


def _stage_ws(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    return ws


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _drive(ws: Path, monkeypatch, **state):
    from meshpipeline.engines import runtime

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {"purpose": "structural", **state}, job_id=f"p-{ws.name}", publish=None,
                                          run=_Run(), source_path=str(ws / "upload.step")))
    assert ok, marker
    return json.loads((ws / "quality.json").read_text())


def test_each_pairs_type_reaches_every_solver_file(tmp_path, monkeypatch):
    from meshpipeline.contracts import decision_record as dr

    ws = _stage_ws(fx.welded_and_bolted(), tmp_path / "wb")
    q = _drive(ws, monkeypatch, fea_connection="bonded", fea_pairs={"Part_1|Part_3": {"type": "contact"}})
    inp = (ws / "mesh.inp").read_text()
    # the tie as settled (PLAN-fea-18): tolerance and ADJUST=NO on the card, the tied slave nodes as <slave>_Tied
    ties = [(n, s.removesuffix("_Tied"), m)
            for n, s, m in re.findall(r"^\*TIE, NAME=(\w+)[^\n]*ADJUST=NO\n(\w+), (\w+)", inp, flags=re.M)]
    contacts = re.findall(r"\*CONTACT PAIR, INTERACTION=\w+, TYPE=SURFACE TO SURFACE\n(\w+), (\w+)", inp)
    assert len(ties) == 1 and len(contacts) == 1
    assert {"Part_1", "Part_2"} == {re.search(r"_(Part_\d+)$", s).group(1) for s in ties[0][1:]}
    assert {"Part_1", "Part_3"} == {re.search(r"_(Part_\d+)$", s).group(1) for s in contacts[0]}
    # Nastran: one glued (IGLUE 1) and one touching (IGLUE 0) pair; Ansys: one bonded and one standard CONTA174
    bdf = (ws / "mesh.bdf").read_text()
    iglue = [int(ln[56:64]) for ln in bdf.splitlines() if ln.startswith(" " * 8 + "SLAVE")]
    assert sorted(iglue) == [0, 1]
    cdb = (ws / "mesh.cdb").read_text()
    assert "KEYOPT,5,12,5" in cdb and "KEYOPT,3,12,0" in cdb
    asm = q["fea"]["assembly"]
    assert {(p["key"], p["type"], p["source"]) for p in asm["pairs"]} == {("Part_1|Part_2", "bonded", "rule"),
                                                                          ("Part_1|Part_3", "contact", "user")}
    # a touching tie: closed, so no open pair and no concern from it
    assert asm["open_bonded"] == [] and asm["bonded_check"]["checked"] == 1
    cited = [e for e in dr.cited_for(f"p-{ws.name}") if e.get("rule") in ("PLAN-fea-19", "PLAN-fea-21")]
    assert {e["rule"] for e in cited} == {"PLAN-fea-19", "PLAN-fea-21"}
    assert any(e["source"] == "user" and e["value"] == "contact" for e in cited)


def test_the_smaller_body_keeps_its_whole_volume_in_the_mesh(tmp_path, monkeypatch):
    ws = _stage_ws(fx.pin_in_bracket(), tmp_path / "pin")
    q = _drive(ws, monkeypatch, fea_connection="bonded")
    assert q["fea"]["assembly"]["mode"] == "bonded"
    inp = (ws / "mesh.inp").read_text()
    nodes = {int(p[0]): np.array([float(v) for v in p[1:4]]) for p in
             (ln.split(",") for ln in inp.split("*NODE\n", 1)[1].split("*", 1)[0].strip().splitlines())}

    def volume(elset: str) -> float:
        m = re.search(rf"\*ELEMENT, TYPE=C3D10, ELSET={elset}\n((?:[^*][^\n]*\n)*)", inp)
        total = 0.0
        for ln in m.group(1).splitlines():
            ids = [int(v) for v in ln.split(",")[1:5]]
            a, b, c, d = (nodes[i] for i in ids)
            total += abs(float(np.dot(np.cross(b - a, c - a), d - a))) / 6.0
        return total * 1e9                                     # m^3 -> mm^3

    pin = math.pi * fx.PIN["r_pin"] ** 2 * fx.PIN["length"]
    ring = math.pi * (fx.PIN["r_pin"] ** 2 - fx.PIN["r_hole"] ** 2) * fx.PIN["plate"][2]
    got = volume("Part_2")
    # THE PIN KEEPS ITS WHOLE SECTION: its elements fill the full pin, ring and all (the file's order
    # would have left it short by the ring, 18% of it); straight-sided tets read a little under
    assert abs(got - pin) < 0.25 * ring, (got, pin, ring)
    assert abs(volume("Part_1") + got - (np.prod(fx.PIN["plate"]) - math.pi * fx.PIN["r_hole"] ** 2
                                          * fx.PIN["plate"][2] + pin - ring)) < 0.25 * ring


def test_a_bonded_pair_the_mesh_leaves_open_is_named_in_red_never_a_clean_pass(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_results

    ws = _stage_ws(fx.gapped_blocks(10.0, 0.2), tmp_path / "open")
    # a reach far short of the 0.2 mm gap: the solver could tie no node of this pair
    q = _drive(ws, monkeypatch, fea_connection="bonded", fea_pairs={"Part_1|Part_2": {"reach_mm": 0.05}})
    (opened,) = q["fea"]["assembly"]["open_bonded"]
    assert opened["parts"] == ["Part_1", "Part_2"] or opened["parts"] == ["Part_2", "Part_1"]
    assert opened["reach_from"] == "set" and opened["needed_m"] == pytest.approx(2e-4, rel=0.05)
    assert opened["tied_share"] == 0.0 and opened["faces"]
    # the tie writer (settle_ties, PLAN-fea-18) writes no *TIE for a tie with no node in reach: it says so
    deck = (ws / "mesh.inp").read_text()
    assert not re.search(r"^\*TIE,", deck, flags=re.M) and "nothing to tie" in deck
    out = fea_results.results(ws)
    assert out["check"]["outcome"] != "pass"
    row = next(r for r in out["check"]["rows"] if r["key"] == "fea_open_bonded_pairs")
    assert row["ok"] is False and "Contact or Off" in row["measured"][0]
    (red,) = out["assembly"]["open_bonded"]
    assert "Part_1 and Part_2" in red["words"] or "Part_2 and Part_1" in red["words"]


def test_a_bonded_pair_across_its_measured_gap_is_closed(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_results

    ws = _stage_ws(fx.gapped_blocks(10.0, 0.2), tmp_path / "closed")
    q = _drive(ws, monkeypatch, fea_connection="bonded")
    asm = q["fea"]["assembly"]
    assert asm["open_bonded"] == [] and asm["bonded_check"] == {"checked": 1, "closed": 1}
    row = next(r for r in fea_results.results(ws)["check"]["rows"] if r["key"] == "fea_open_bonded_pairs")
    assert row["ok"] is True


def test_an_open_pair_whose_nodes_lie_beside_the_face_is_not_told_to_widen_the_reach():
    words = P.open_words({"name": "Tie_18", "parts": ["Part_3", "Part_5"], "reach_m": 3e-4, "needed_m": 1e-4,
                          "reach_from": "tie tolerance", "faces": [], "beside": True})
    assert "over the other part's surface" in words and "Contact or Off" in words and "reach to at least" not in words


def test_a_settled_tie_that_ties_no_node_is_open_however_near_its_nodes(tmp_path, monkeypatch):
    # the measured 0.2 mm gap, tied: closed; then the same deck with the settled tie emptied (as settle_ties
    # leaves a tie whose near nodes all lie beside the master) - the check must read it OPEN, never closed
    from meshpipeline.engines import fea_assembly_deck as AD

    ws = _stage_ws(fx.gapped_blocks(10.0, 0.2), tmp_path / "beside")
    captured = {}
    real = AD.open_joints

    def spy(prep):
        captured["prep"] = prep
        return real(prep)

    monkeypatch.setattr(AD, "open_joints", spy)
    _drive(ws, monkeypatch, fea_connection="bonded")
    prep = captured["prep"]
    (tie,) = [j for j in prep.joints if j.kind == "tie"]
    assert tie.tied is not None and len(tie.tied)
    assert real(prep)["closed"] == 1
    tie.tied = np.zeros(0, dtype=np.int64)
    got = real(prep)
    (row,) = got["open"]
    assert got["closed"] == 0 and row["tied_share"] == 0.0 and row.get("beside") is True
