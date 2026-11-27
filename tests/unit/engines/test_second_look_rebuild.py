# Responsibility: Verify the rebuild after a second look (VER-18) makes the mesh finer at each mark and says so, and that "finer" on a delivered mesh is against the size the last mesh REALLY had there (VER-19).
# Boundaries: the bracket built in the test (tests/fea_fixtures.py), measured and planned in-process; no model, no mesher.
from __future__ import annotations

import pytest

from meshpipeline.engines import marked_spots as MS
from meshpipeline.engines import mesh_requirements as MR

PARENT = "0f0f0f0f-1111-4222-8333-444455556666"


# the marks, carried in the dispute, become "finer at the spot you marked" asks - only on a rebuild

def _state(**kw) -> dict:
    d = {"of_job_id": PARENT, "mode": "rebuild", "comment": "",
         "flags": [{"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 9.0, "y": 9.0, "z": 9.0}],
         "marks": [{"ordinal": 1, "faces": [{"id": "Face_2", "label": "Face 2 · flat", "sig": {"a": 1}}]}]}
    d.update(kw)
    return {"user_dispute": d, "dispute_flag_findings": [
        {"ordinal": 1, "status": "not_confirmed", "observation": "elements look even", "explanation": "",
         "measurements": "aspect ratio max 3.2 at the hole; scaled Jacobian 0.64"}]}


def test_each_mark_on_the_part_is_one_finer_ask_against_the_delivered_mesh():
    req = MS.with_marks(None, _state())
    (e,) = req["local"]
    assert e["what"] == "finer" and e["faces"] == ["Face_2"] and e["change_of"] == PARENT and e["marked"] == 1
    assert e["finding"] == ("the second look found nothing past a limit there (worst aspect ratio there 3.2, limit 20; "
                            "worst scaled Jacobian there 0.64, limit 0.01)")
    # the user's own requirements stay as they were
    own = {"local": [{"id": "r1", "what": "size", "mm": 2.0, "faces": ["Face_9"]}], "sizes": {"global_mm": 3.0}}
    both = MS.with_marks(own, _state())
    assert [x["id"] for x in both["local"]] == ["r1", "mark_1"] and both["sizes"] == {"global_mm": 3.0}
    assert own["local"] == [{"id": "r1", "what": "size", "mm": 2.0, "faces": ["Face_9"]}]


def test_an_accept_or_a_normal_run_asks_nothing():
    assert MS.with_marks(None, _state(mode="accept")) is None
    assert MS.with_marks({"local": []}, {}) == {"local": []}


def test_every_mark_no_step_spoke_of_is_told_with_what_the_second_look_found():
    said = ["From your marks: spot 1, on Face 2 · flat - the second look found nothing past a limit there; the last "
            "mesh had about 3 mm there, this one has about 1.5 mm."]
    lines = MS.result_lines(_state(), said)
    assert lines == ["From your marks: spot 2 - the second look gave no finding of its own there; nothing was made "
                     "finer there (the mark is not on a face of the part)."]
    assert len(MS.result_lines(_state(), [])) == 2


def test_a_confirmed_finding_is_said_as_found():
    st = _state()
    st["dispute_flag_findings"] = [{"ordinal": 1, "status": "confirmed", "observation": "two slivers at the hole edge",
                                    "explanation": "", "measurements": "aspect_ratio = 31.5; jacobian ratio 4"}]
    assert MS.finding_words(st, 1) == "the second look found a problem there: worst aspect ratio there 31.5, limit 20"
    st["dispute_flag_findings"][0]["measurements"] = "sicn_low_fraction = 0.002"
    assert MS.finding_words(st, 1) == "the second look found a problem there"


def test_the_note_never_carries_the_second_looks_own_wording():
    """The live note (dev, 26 Nov) put the reviewer's raw text in brackets, render settings and a field name, cut off
    with "...". Only figures the result screen names reach it; with none, the plain sentence alone."""
    raw = ("Marked-circle radius 0.00576 m; centered inspection span 0.0144 m; close render magnification 8x; "
           "sicn_low_fraction = 0.0012 over 41 elements in view; elements look even round the bore")
    st = _state()
    st["dispute_flag_findings"] = [{"ordinal": 1, "status": "not_confirmed", "observation": raw, "explanation": raw,
                                    "measurements": raw}]
    said = MS.finding_words(st, 1)
    assert said == "the second look found nothing past a limit there"
    line = MS.result_lines(st, [])[0]
    for internal in ("radius", "span", "render", "magnification", "sicn", "_", "...", "0.00576", "("):
        assert internal not in line, (internal, line)
    st["dispute_flag_findings"][0]["measurements"] = raw + "; worst aspect ratio 2.7"
    assert MS.finding_words(st, 1) == ("the second look found nothing past a limit there (worst aspect ratio there "
                                       "2.7, limit 20)")


def test_the_second_looks_reply_is_never_empty_and_passes_the_guard():
    from meshpipeline.agents.intake import change_reply
    from meshpipeline.application import change_request as CR

    f = CR.ChangeFacts(kind=CR.KIND_REVIEW, words="", faces=[{"id": "Face_66", "label": "Face 66 · round Ø12 mm"}],
                       marks=1, spots=1, last_run_minutes=9)
    said = CR.plain_reply(f)
    assert said == ("I'll take a second look at the spot you marked on Face 66 · round Ø12 mm. If nothing there is "
                    "past a limit, I'll rebuild with a finer mesh at that spot and keep everything else you asked for. "
                    "The last run took 9 min.")
    assert change_reply.check(said, f.as_dict()) == ""
    assert change_reply.check("Got it, I'll rebuild it finer.", f.as_dict()) != "", "no second look said"
    assert change_reply.check("The mesh looks fine, I'll take a second look and rebuild.", f.as_dict()) != ""


# the plan: the bracket, its big flat face (the rules give it 15 mm; 3 mm overall holds it at 3 mm)

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


FACE = "Face_2"


def _asked(global_mm: float, min_mm: float | None = None) -> dict:
    req = MR.empty()
    req["sizes"]["global_mm"] = global_mm
    if min_mm is not None:
        req["sizes"]["min_edge_mm"] = min_mm
    return MR.section("used", requirements=req)


def _area_finer() -> dict:
    """The first run's "make the mesh finer at the big face" (an area found by the parts reading, reading A)."""
    return {"ask": "refine", "parts": ["Feature_1"], "quote": "make the mesh finer at the big face"}


def _reading(*entries: dict, features: dict) -> dict:
    return {"status": "used", "features": features,
            "parts": [{"id": k, "feature": True, "name": "the big face" if k == "Feature_1" else "the face you picked"}
                      for k in features],
            "entries": list(entries)}


def _plan(record, surface, diag, asked, parts):
    from meshpipeline.engines import fea_ai as FA
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_part_sizes as PS

    sizes, scaled, _controls = FD.requirement_sizes(record, asked, parts, diag)
    rec = {**record, FA.SETTLED_KEY: PS.settled(scaled)} if PS.finer_outcomes(scaled) else record
    return FD.choose_plan(rec, surface, diag, sizes=sizes or None), PS.finer_outcomes(scaled)


def _at(plan: dict, face: str) -> float:
    return min(float(z["size_m"]) for z in plan["zones"] if z.get("kind") != "part" and face in (z.get("faces") or []))


def test_finer_on_a_delivered_mesh_is_half_what_the_last_run_really_built_there(bracket):
    """The live case (dev, 26 Nov, the knuckle): "3mm global", "finer in/around the holes" put the holes at 1.5 mm;
    then "finer around this hole only" on the delivered mesh read the hole's last size as 3 mm and built 1.5 mm again,
    while the note claimed 3 -> 1.5. The last size there is what the last plan held there, after every ask."""
    record, surface, diag = bracket
    first = _reading(_area_finer(), features={"Feature_1": [FACE]})
    last, _ = _plan(record, surface, diag, _asked(3.0), first)                    # the finished run
    assert _at(last, FACE) == pytest.approx(0.0015, rel=1e-3)
    change = {"ask": "refine", "parts": ["Pick_1"], "quote": "make the mesh finer around this face only",
              "change_of": PARENT}
    plan, finer = _plan(record, surface, diag, _asked(3.0),
                        _reading(_area_finer(), change, features={"Feature_1": [FACE], "Pick_1": [FACE]}))
    assert _at(plan, FACE) == pytest.approx(0.00075, rel=1e-3)
    row = next(r for r in finer if r["change"])
    assert row["there_mm"] == pytest.approx(1.5, rel=1e-3) and row["target_mm"] == pytest.approx(0.75, rel=1e-3)
    assert row["words"] == ("You asked for finer elements at the face you picked: the last mesh had about 1.5 mm "
                            "there; this one has about 0.75 mm.")


def test_a_floor_the_user_set_is_built_and_said(bracket):
    record, surface, diag = bracket
    change = {"ask": "refine", "parts": ["Pick_1"], "quote": "finer here", "change_of": PARENT}
    plan, finer = _plan(record, surface, diag, _asked(3.0, min_mm=1.0),
                        _reading(_area_finer(), change, features={"Feature_1": [FACE], "Pick_1": [FACE]}))
    row = next(r for r in finer if r["change"])
    assert row["target_mm"] == pytest.approx(1.0, rel=1e-3) and _at(plan, FACE) == pytest.approx(0.001, rel=1e-3)
    assert row["words"].endswith("this one has about 1 mm, held at your smallest element size of 1 mm.")


def test_a_marked_spot_with_no_words_is_made_finer_there_and_told(bracket):
    """VER-18: one mark, no words, the second look found nothing past a limit: the rebuild keeps the first run's asks
    and makes the mesh finer at the mark - half what it had there - and the line says what the look found."""
    from meshpipeline.cad import face_map as FM
    from meshpipeline.engines import fea_part_sizes as PS

    record, surface, diag = bracket
    fm = FM.from_surface(surface, scale_to_m=1.0)
    sig = FM.signature(fm, fm.names.index(FACE))
    st = _state(marks=[{"ordinal": 1, "faces": [{"id": FACE, "label": "Face 2 · flat", "sig": sig}]}])
    first = _reading(_area_finer(), features={"Feature_1": [FACE]})
    last, _ = _plan(record, surface, diag, _asked(3.0), first)
    parts = PS.with_picks(first, MS.with_marks(MR.requirements_of(_asked(3.0)), st), surface)
    plan, finer = _plan(record, surface, diag, _asked(3.0), parts)
    assert _at(plan, FACE) == pytest.approx(0.5 * _at(last, FACE), rel=1e-3)
    (row,) = [r for r in finer if r.get("marked")]
    assert row["words"] == ("From your marks: spot 1, on Face 2 · flat - the second look found nothing past a limit "
                            "there (worst aspect ratio there 3.2, limit 20; worst scaled Jacobian there 0.64, "
                            "limit 0.01); the last mesh had about 1.5 mm there, this one has about "
                            "0.75 mm.")
    # nothing else of the first run's plan changes
    now = {z["id"]: float(z["size_m"]) for z in plan["zones"]}
    for z in last["zones"]:
        if FACE not in (z.get("faces") or []):
            assert now[z["id"]] == float(z["size_m"]), z["id"]
    assert plan["base_size_m"] == last["base_size_m"]
