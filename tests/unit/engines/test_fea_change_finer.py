# Responsibility: Verify PLAN-fea-36 on a change request (VER-9) - "finer" asked on a DELIVERED mesh at marked faces is
#       half the size that mesh had there (not the mesh round them), "coarser" twice it, a typed size still wins, only
#       those faces change, the plan step's reading A is unchanged, the note carries the run it was asked on, and
#       what is said back is truthful.
# Boundaries: the filleted bracket built in the test (tests/fea_fixtures.py: a 4 mm inside fillet), measured and planned
#       in-process; no model, no mesher.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.application import change_request as CR
from meshpipeline.contracts import user_notes as UN
from meshpipeline.engines import fea_requirements as FR
from meshpipeline.engines import mesh_requirements as MR

PARENT = "0f0f0f0f-1111-4222-8333-444455556666"


# the stamp: written by the change path, kept with the note, carried to the picked entry

def test_the_change_path_stamps_its_note_with_the_finished_run():
    faces = [{"id": "Face_100", "label": "Face 100 · round Ø4 mm", "sig": {"area": 1.0, "c": [0, 0, 0]}}]
    note = CR.change_note("make the mesh finer around this hole only, nowhere else", faces, of_job_id=PARENT)
    assert note["change_of"] == PARENT and UN.RULE_CHANGE_OF == "VER-9"
    kept = UN.clean([note])
    assert kept[0]["change_of"] == PARENT
    assert "change_of" not in UN.clean([UN.make("finer here", source=UN.SOURCE_CHAT, faces=faces)])[0]
    req = {"rows": [{"id": "r1", "field": "local", "quote": "make the mesh finer around this hole only"}],
           "local": [{"id": "r1", "where": "this hole", "what": "finer",
                      "quote": "make the mesh finer around this hole only"}]}
    got = MR.bind_picks(req, kept)
    assert got["local"][0]["faces"] == ["Face_100"] and got["local"][0]["change_of"] == PARENT
    # the same words typed with a pick at the plan step carry no stamp
    plain = MR.bind_picks({"rows": [], "local": [{"id": "r1", "what": "finer", "quote": "finer here"}]},
                          UN.clean([UN.make("finer here", source=UN.SOURCE_CHAT, faces=faces)]))
    assert "change_of" not in plain["local"][0]


# the reply when the change is accepted: the size change in plain words, nothing invented

@pytest.mark.parametrize("words,step", [
    ("make the mesh finer around this hole only, nowhere else", CR.SIZE_STEP_FINER),
    ("coarser here", CR.SIZE_STEP_COARSER), ("0.1 mm on this hole", ""), ("this looks odd", "")])
def test_the_size_step_of_the_words(words, step):
    assert CR.size_step(words) == step


def test_the_accepted_reply_says_the_size_change_and_nothing_else_changes():
    f = CR.ChangeFacts(kind=CR.KIND_CHANGE, words="make the mesh finer around this hole only, nowhere else",
                       faces=[{"id": "Face_100", "label": "Face 100"}], marks=1, spots=1, last_run_minutes=3,
                       size_change=CR.SIZE_STEP_FINER)
    assert CR.plain_reply(f) == ("Got it: finer on Face 100 only, about half the size the last mesh had there, and "
                                 "nothing else changes. Starting a new run; the last one took 3 min.")
    d = f.as_dict()
    assert d["size_change_there"] == CR.SIZE_STEP_FINER
    from meshpipeline.agents.intake import change_reply
    assert change_reply.check(CR.plain_reply(f), d) == ""


def test_the_asked_against_built_row_says_it_against_the_last_mesh():
    req = {"rows": [{"id": "r1", "field": "local", "asked": "finer"}],
           "local": [{"id": "r1", "where": "", "what": "finer", "quote": "finer around this hole"}]}
    row = {"id": "Pick_1", "quote": "finer around this hole", "faces": ["Face_3"], "target_mm": 0.26,
           "there_mm": 0.52, "there_low_mm": 0.52, "around_mm": 3.0, "yours": True, "changed": True, "change": True}
    mesh = SimpleNamespace(faces={}, node_ids=[], xyz=[])
    got = FR._finer_row(req["rows"][0], req, [row], mesh, face_tags=None, mm_per_unit=1000.0)
    assert got["delivered"] == "made about 0.26 mm there, half the 0.52 mm the last mesh had there"


# the plan: half the size the delivered mesh had at the marked faces

gmsh = pytest.importorskip("gmsh")


@pytest.fixture(scope="module")
def bracket(tmp_path_factory):
    from tests import fea_fixtures as fx

    from meshpipeline.engines import fea_driver as FD

    ws = tmp_path_factory.mktemp("bracket")
    src = fx.write_step(fx.bracket(4.0), ws / "upload.step")
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
    return FD.measure_staged(ws)


def _global(mm: float) -> dict:
    req = MR.empty()
    req["sizes"]["global_mm"] = mm
    return MR.section("used", requirements=req)


def _fillets(record: dict, entry: dict | None = None) -> dict:
    spot = next(h for h in record.get("hot_spots") or [] if h.get("type") == "fillet")
    return {"status": "used", "features": {"Pick_1": list(spot["faces"])},
            "parts": [{"id": "Pick_1", "feature": True, "name": "the fillets"}],
            "entries": [entry or {"ask": "refine", "parts": ["Pick_1"], "quote": "finer around the fillets",
                                  "change_of": PARENT}]}


def _plan(record, surface, diag, asked, parts):
    from meshpipeline.engines import fea_ai as FA
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_part_sizes as PS

    sizes, scaled, _controls = FD.requirement_sizes(record, asked, parts, diag)
    rec = {**record, FA.SETTLED_KEY: PS.settled(scaled)} if PS.finer_outcomes(scaled) else record
    return FD.choose_plan(rec, surface, diag, sizes=sizes or None), PS.finer_outcomes(scaled)


def _zone(plan: dict, zid: str) -> float:
    return next(float(z["size_m"]) for z in plan["zones"] if z["id"] == zid)


def test_finer_on_a_change_request_is_half_what_the_last_mesh_had_there(bracket):
    """The live case: 3 mm overall, then on the delivered mesh "finer" at a place the rules already make finer than
    half of 3 mm. At the plan step it keeps its size (reading A); on a change request it is half what was built."""
    record, surface, diag = bracket
    last, _ = _plan(record, surface, diag, _global(3.0), None)            # the finished run
    plan, finer = _plan(record, surface, diag, _global(3.0), _fillets(record))
    was = _zone(last, "H1")
    assert _zone(plan, "H1") == pytest.approx(0.5 * was, rel=1e-3)
    (row,) = finer
    assert row["change"] and row["changed"] and row["there_mm"] == pytest.approx(was * 1e3, rel=1e-3)
    assert row["words"] == (f"You asked for finer elements at the fillets: the last mesh had about "
                            f"{float(f'{was * 1e3:.2g}'):g} mm there; this one has about "
                            f"{float(f'{0.5 * was * 1e3:.2g}'):g} mm.")
    assert row["words"] in plan["words"]
    assert plan["estimate"]["elements"] > last["estimate"]["elements"]


def test_the_same_words_at_the_plan_step_keep_reading_a(bracket):
    record, surface, diag = bracket
    from meshpipeline.engines import fea_driver as FD
    entry = {"ask": "refine", "parts": ["Pick_1"], "quote": "finer around the fillets"}
    plan, finer = _plan(record, surface, diag, _global(3.0), _fillets(record, entry))
    rules = FD.choose_plan(record, surface, diag)
    assert _zone(plan, "H1") == _zone(rules, "H1") and [r["changed"] for r in finer] == [False]
    assert "already about" in finer[0]["words"] and not finer[0]["change"]


def test_only_there_nothing_else_changes(bracket):
    record, surface, diag = bracket
    last, _ = _plan(record, surface, diag, _global(3.0), None)
    plan, _finer = _plan(record, surface, diag, _global(3.0), _fillets(record))
    marked = set(_fillets(record)["features"]["Pick_1"])
    for z in last["zones"]:
        if not marked & set(z.get("faces") or []):
            assert _zone(plan, z["id"]) == float(z["size_m"]), z["id"]
    assert plan["base_size_m"] == last["base_size_m"]


def test_coarser_on_a_change_request_is_twice_what_the_last_mesh_had_there(bracket):
    record, surface, diag = bracket
    last, _ = _plan(record, surface, diag, _global(3.0), None)
    entry = {"ask": "coarsen", "parts": ["Pick_1"], "quote": "coarser around the fillets", "change_of": PARENT}
    plan, finer = _plan(record, surface, diag, _global(3.0), _fillets(record, entry))
    assert _zone(plan, "H1") == pytest.approx(2.0 * _zone(last, "H1"), rel=1e-3)
    assert finer[0]["words"].startswith("You asked for coarser elements at the fillets: the last mesh had about")


def test_a_typed_size_still_wins_on_a_change_request(bracket):
    record, surface, diag = bracket
    typed = {"ask": "size", "parts": ["Pick_1"], "min_mm": 0.2, "max_mm": 0.2, "quote": "0.2 mm here",
             "change_of": PARENT}
    plan, finer = _plan(record, surface, diag, _global(3.0), _fillets(record, typed))
    assert _zone(plan, "H1") == 0.0002 and finer == []
