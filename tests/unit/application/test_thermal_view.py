# Responsibility: Verify an ECXML upload's geometry check is the file's own thermal model - parts as
# the file groups them, totals, room air, the air box's sides in words, fans, vents, sources, probes,
# plain warnings and the detail levels - and that it asks nothing the file answers: no pictures, no
# picture naming, no up question, no triangle notes, and no inlet/outlet rows. On dev the set-top
# box came up with six "outlet" rows at "60% sure" and a line about the fluid flowing through it.
# Boundaries: real ECXML files written by the repo's test models (tests/unit/cad/ecxml_models), read
# and placed for real; the object store, the session and the repositories are stood in for.
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.cad.ecxml_models import Ecxml, ducted_board, set_top_box, standard_materials, tiny_board

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import thermal_view as tv
from meshpipeline.application.geometry_confirmation import THERMAL_SIDES, confirmation_message, patches_from
from meshpipeline.cad.ingest import canonicalise
from meshpipeline.cad.ingest.ecxml import read_ecxml
from meshpipeline.cad.ingest.ecxml_build import read_ecxml_sidecar
from meshpipeline.cad.ingest.ecxml_place import place
from meshpipeline.contracts import object_storage
from meshpipeline.contracts.intake_formats import (
    INTAKE_FORMATS,
    THERMAL_CHECK,
    check_mode_of,
    format_for_key,
)

SID = uuid.UUID("eeee1111-2222-4222-b222-eeeeeeeeeeee")
#: The generic check's words that have no place on a thermal model's check (as words or the start
#: of one: "outlet_2", "Sure" - not the "sure" in "pressure").
FORBIDDEN = re.compile(r"(?<![a-z])(?:inlet|outlet|sure|triangle|sticker)", re.I)


def _write(doc: Ecxml, tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.ecxml"
    path.write_bytes(doc.xml())
    return path


def _model(doc: Ecxml, tmp_path: Path, name: str = "model"):
    """The sidecar and the placement of one ECXML document, as the scout reads them."""
    src = _write(doc, tmp_path, name)
    work = tmp_path / f"{name}_work"
    canonical = canonicalise(src, work, stem="geometry", ecxml_form="placed")
    sidecar = read_ecxml_sidecar(canonical.path)
    assert sidecar is not None
    return src, canonical, sidecar, place(read_ecxml(src))


def _view(doc: Ecxml, tmp_path: Path, name: str = "model", sized: bool = True) -> tuple[dict, dict]:
    _src, _canonical, sidecar, placement = _model(doc, tmp_path, name)
    detail = tv.detail_levels(placement) if sized else None
    return tv.thermal_view(sidecar, placement, detail=detail), sidecar


def _own_names(view: dict) -> set[str]:
    """Every name the file itself gives (parts, assemblies, fans, vents, sides, ...): the file may
    call a fan "Inlet fan", and its names are shown as it writes them."""
    names: set[str] = set()
    for key in ("tree", "fans", "vents", "resistances", "sources", "compact_models", "probes", "materials"):
        for row in view.get(key) or []:
            names.add(str(row.get("name")))
            names.update(str(p) for p in row.get("path") or [])
    for side in (view.get("domain") or {}).get("sides") or []:
        names.update(side["devices"] + side["plates"])
    return {n for n in names if n}


def _our_words(payload, own: set[str]) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    for name in sorted(own, key=len, reverse=True):
        text = text.replace(name, "")
    return text


# ------------------------------------------------------------------ the format's check mode ----
def test_ecxml_is_the_one_format_whose_check_is_a_thermal_model():
    assert format_for_key("ecxml").check_mode == THERMAL_CHECK == check_mode_of("ecxml")
    assert [f.key for f in INTAKE_FORMATS if f.check_mode] == ["ecxml"]
    assert check_mode_of("step") == check_mode_of("stl") == check_mode_of("nonsense") == ""


# ---------------------------------------------------------------------- the set-top box ----
def test_the_set_top_box_reads_as_the_files_own_thermal_model(tmp_path):
    view, sidecar = _view(set_top_box(), tmp_path)
    assert view["title"] == "Set-top box, vented" and view["producer"] == "Flotherm"
    t = view["totals"]
    assert t["power_W"] == pytest.approx(sidecar["total_power_W"]) == pytest.approx(8.7)
    assert t["power_in_mesh_W"] == pytest.approx(8.7) and t["power_left_out_W"] == 0
    assert t["parts"] == 8 and t["materials"] == 5
    assert view["ambient"] == {"temperature_C": 35.0, "radiant_C": 35.0, "pressure_Pa": 0.0}
    assert view["headline"] == ("Set-top box, vented: a Flotherm thermal model with 8 parts, 8.7 W in "
                                "total and 35 °C room air.")
    # the air box: one line, open to the room, no roles
    dom = view["domain"]
    assert dom["summary"] == "180 × 150 × 80 mm, open to the room on all six sides."
    assert dom["source"] == "solutionDomain" and {s["state"] for s in dom["sides"]} == {"open"}


def test_the_parts_are_grouped_as_the_file_groups_them_with_material_power_and_size(tmp_path):
    view, sidecar = _view(set_top_box(), tmp_path)
    tree = {r["name"]: r for r in view["tree"]}
    assert list(tree) == ["Case", "PCB", "SoC", "DDR 1", "DDR 2", "PMIC", "Bulk cap", "SoC heat sink"]
    assert tree["Case"]["path"] == ["Housing"] and tree["Case"]["kind"] == "enclosure"
    assert tree["Case"]["detail"] == "2.5 mm walls" and tree["Case"]["thinnest_mm"] == 2.5
    assert tree["PCB"]["path"] == ["Mainboard"] and tree["PCB"]["kind"] == "PCB"
    assert tree["PCB"]["material"] == "FR4 board" and tree["PCB"]["size_mm"] == [90.0, 70.0, 1.6]
    assert tree["SoC"]["kind"] == "2R package" and tree["SoC"]["power_W"] == 6.0
    assert tree["SoC"]["detail"] == "θJC 0.3 K/W, θJB 4.5 K/W"
    assert tree["SoC heat sink"]["path"] == ["Mainboard"] and tree["SoC heat sink"]["kind"] == "heat sink"
    assert tree["PMIC"]["thinnest_mm"] == 1.0 and tree["PMIC"]["power_W"] == 0.9
    # every part is one region of the mesh, named as the sidecar names it
    regions = [r["name"] for r in sidecar["regions"] if r["type"] == "solid"]
    assert [r["region"] for r in view["tree"]] == regions
    assert all(len(r["color"]) == 3 for r in view["tree"])
    k = {m["name"]: m["k_W_mK"] for m in view["materials"]}
    assert k["FR4 board"] == [17.0, 17.0, 0.4] and k["Aluminium 6063"] == 201.0
    assert k["Silicon"] == "varies with T"


def test_fans_vents_sources_compact_models_and_probes_carry_the_files_data(tmp_path):
    view, _ = _view(set_top_box(), tmp_path)
    (fan,) = view["fans"]
    assert fan["name"] == "Exhaust fan" and fan["kind"] == "axial" and fan["hub_mm"] == 10.0
    assert fan["blows"] == "+x" and fan["flow"]["kind"] == "curve"
    assert fan["text"] == ("Exhaust fan: axial, 25 mm, hub 10 mm, blows +x, fan curve (4 points, up to "
                           "0.5 L/s and 45 Pa), inside the air box")
    (vent,) = view["vents"]
    assert vent["text"] == "Front vent: 60 × 12 mm, 50% open, loss coefficient 2.5, inside the air box"
    assert view["sources"] == [{"name": "SoC die heat", "path": ["Mainboard"], "kind": "volume", "power_W": 0.5}]
    assert view["compact_models"][0]["theta_jc_K_W"] == 0.3
    assert {p["name"]: p["at_mm"] for p in view["probes"]} == {"Tj SoC": [40.0, 35.0, 10.6],
                                                              "Air exhaust": [115.0, 45.0, 20.0]}


def test_the_detail_levels_show_cells_time_and_the_thinnest_layer(tmp_path):
    view, _ = _view(set_top_box(), tmp_path)
    d = view["detail"]
    assert d["default"] == "standard"
    assert [d[k]["label"] for k in ("draft", "standard", "max")] == ["Quick look", "Standard", "Fine"]
    assert [d[k]["min_cells_across"] for k in ("draft", "standard", "max")] == [1, 2, 3]
    cells = [d[k]["est_cells"] for k in ("draft", "standard", "max")]
    assert cells[0] < cells[1] < cells[2] <= d["max"]["max_cells"]
    assert all(d[k]["est_seconds"] > 0 and d[k]["thin_layers_short"] == [] for k in ("draft", "standard", "max"))
    assert d["standard"]["thinnest"] == {"part": "PMIC", "thickness_mm": 1.0, "cells_across": 2}
    # a clean model raises no warning (a Flotherm file's fan hub is a diameter, as the standard says)
    assert view["warnings"] == []


def test_the_thermal_view_never_speaks_of_openings_roles_or_triangles(tmp_path):
    for doc, name in ((set_top_box(), "stb"), (ducted_board(), "duct"), (tiny_board(), "tiny")):
        view, _ = _view(doc, tmp_path, name)
        words = _our_words(view, _own_names(view))
        assert not FORBIDDEN.search(words), (name, FORBIDDEN.findall(words))
        assert "flows through" not in words


# --------------------------------------------------------------- the sides of the air box ----
def test_a_ducted_boards_sides_say_what_the_file_puts_there(tmp_path):
    view, sidecar = _view(ducted_board(), tmp_path)
    sides = {s["face"]: s for s in view["domain"]["sides"]}
    assert {f: s["state"] for f, s in sides.items()} == {
        "-x": "walled", "+x": "walled", "-y": "vent", "+y": "fan", "-z": "walled", "+z": "walled"}
    assert sides["+y"]["devices"] == ["Inlet fan"] and sides["-y"]["devices"] == ["Outlet"]
    assert view["domain"]["summary"] == (
        "120 × 240 × 36 mm. −x: walled (Low X); +x: walled (High X); −y: a vent (Outlet); "
        "+y: a fan (Inlet fan); −z: walled (Low Z); +z: walled (High Z).")
    assert view["totals"]["power_W"] == pytest.approx(sidecar["total_power_W"])
    (fan,) = view["fans"]
    assert fan["kind"] == "rectangular" and fan["where"] == "on the +y side of the air box"
    assert fan["flow"]["text"] == "fixed flow 4 L/s (8.476 CFM)"
    assert [r["name"] for r in view["resistances"]] == ["Cable bundle"]
    assert {s["name"] for s in view["sources"]} == {"VRM top"}


def test_a_model_without_an_air_box_says_so(tmp_path):
    doc = Ecxml("Bare die")
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.block("Die", (0.0, 0.0, 0.0), (0.005, 0.005, 0.0003), "Si", 2.0)
    view, _ = _view(doc, tmp_path)
    assert view["domain"]["source"] == "derived" and view["ambient"] is None
    assert "The file sets no air box" in view["domain"]["summary"]
    assert view["headline"] == "Bare die: a Flotherm thermal model with 1 part, 2 W in total."


# ------------------------------------------------------------------------- the warnings ----
def _crafted() -> Ecxml:
    """A small model with everything a warning is for: a part and a probe outside the air box, a
    part and a fan switched off, an external CAD reference, a negative power and an Icepak fan."""
    doc = standard_materials(Ecxml("Crafted", producer="Icepak"))
    doc.domain((0.0, 0.0, 0.0), (0.1, 0.1, 0.05))
    doc.pcb("Board", (0.01, 0.01, 0.01), (0.08, 0.08, 0.0016), "+xy", "FR4 board")
    doc.block("Chip", (0.04, 0.04, 0.0116), (0.01, 0.01, 0.002), "Silicon", 3.0)
    doc.block("Cooler", (0.02, 0.02, 0.0116), (0.005, 0.005, 0.002), "Aluminium 6063", -0.5)
    doc.block("Far chip", (0.3, 0.3, 0.3), (0.01, 0.01, 0.002), "Silicon", 0.4)
    doc.block("Spare chip", (0.06, 0.06, 0.0116), (0.01, 0.01, 0.002), "Silicon", 1.0, active=False)
    doc.fan3d("Blower", (0.0, 0.03, 0.02), (0.01, 0.02, 0.02), "+yz", 0.005, 0.001)
    doc.monitor("Lost probe", (0.5, 0.5, 0.5))
    doc.mcad("Connector", (0.0, 0.0, 0.0), "connector.step", "Aluminium 6063", 0.2)
    return doc


def test_warnings_fire_only_on_what_is_really_wrong_in_plain_words(tmp_path):
    view, _ = _view(_crafted(), tmp_path, sized=False)
    by_part = {p: w for w in view["warnings"] for p in w["parts"]}
    assert by_part["Far chip"]["text"] == ("1 part is outside the air box, so it is left out, with 0.4 W: "
                                           "Far chip.")
    assert by_part["Spare chip"]["level"] == "info" and "switched off in the file" in by_part["Spare chip"]["text"]
    assert by_part["Connector"]["text"] == ("The file refers to CAD that is not in the upload, so it is left out: "
                                            "Connector (connector.step).")
    assert "Lost probe" in by_part and "outside the air box" in by_part["Lost probe"]["text"]
    assert "Negative power (heat taken out) on Cooler (-0.5 W)" in by_part["Cooler"]["text"]
    assert any("Icepak's export calls it a radius" in w["text"] for w in view["warnings"])
    left = [r for r in view["tree"] if r.get("left_out")]
    assert [r["name"] for r in left] == ["Far chip"] and left[0]["region"] is None
    assert view["totals"]["power_left_out_W"] == pytest.approx(0.4 + 0.2)
    assert view["totals"]["parts"] == 3
    words = _our_words(view["warnings"], _own_names(view))
    assert not FORBIDDEN.search(words)


def test_a_thin_layer_short_of_its_cells_is_a_warning_at_standard(tmp_path):
    view, _ = _view(set_top_box(), tmp_path, sized=False)
    detail = tv.limits_only()
    detail["standard"].update(est_cells=1000, thin_layers_short=[
        {"part": "PMIC", "thickness_mm": 1.0, "cells_across": 1}])
    _src, _c, sidecar, placement = _model(set_top_box(), tmp_path, "again")
    view = tv.thermal_view(sidecar, placement, detail=detail)
    (w,) = view["warnings"]
    assert w["text"] == "At Standard, 1 thin layer gets fewer than 2 cells through it: PMIC (1 mm, 1 cell). Fine gives more."
    assert w["parts"] == ["PMIC"]


# --------------------------------------------------------------- sized in a bounded child ----
def test_the_check_sizes_the_levels_in_a_child_with_the_same_answer(tmp_path):
    src, _c, _s, placement = _model(set_top_box(), tmp_path)
    here = tv.detail_levels(placement)
    there = tv.detail_levels_bounded(src)
    for level in ("draft", "standard", "max"):
        assert there[level]["est_cells"] == here[level]["est_cells"] > 0


def test_a_child_stopped_at_its_budget_keeps_the_levels_it_finished(tmp_path, monkeypatch):
    from meshpipeline.sandbox import safe_exec

    done = json.dumps({"level": "standard", "row": {**tv.limits_only()["standard"], "est_cells": 123,
                                                     "estimate": ""}})

    def _stopped(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"), output=(done + "\n{\"level\": \"dr").encode())
    monkeypatch.setattr(safe_exec, "run_guarded", _stopped)
    d = tv.detail_levels_bounded(tmp_path / "x.ecxml", budget_s=1.0)
    assert d["standard"]["est_cells"] == 123
    assert d["draft"]["est_cells"] is None and d["draft"]["estimate"] == tv.SIZED_AT_RUN
    assert d["max"]["estimate"] == tv.SIZED_AT_RUN


def test_a_level_not_started_within_the_budget_keeps_its_limits(tmp_path):
    _src, _c, _s, placement = _model(tiny_board(), tmp_path)
    ticks = iter([0.0, 0.0, 99.0, 99.0])
    d = tv.detail_levels(placement, budget_s=1.0, clock=lambda: next(ticks))
    assert d["standard"]["est_cells"] > 0                   # the default level is sized first
    assert d["draft"]["estimate"] == d["max"]["estimate"] == tv.SIZED_AT_RUN


# ------------------------------------------------------------------- the scout and naming ----
class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def download_file(self, *, object_key, destination):
        Path(destination).write_bytes(self.get_bytes(object_key=object_key))

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)

    def create_download_url(self, *, object_key, expires_in):
        return f"https://pictures/{object_key}"


def _never(what: str):
    def _called(*a, **k):
        raise AssertionError(f"a thermal model's check must not {what}")
    return _called


@pytest.fixture
def scouted(tmp_path, monkeypatch):
    """The set-top box through the real scout (and naming), with every generic step that has no
    place on a thermal model made to fail the test if it runs."""
    import meshpipeline.cad.scout_mesh as scout_mesh_mod
    import meshpipeline.render.scout_snapshots as snaps_mod

    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    xml = set_top_box().xml()

    def _fetch(ref, work):
        dest = work / "geometry.ecxml"
        dest.write_bytes(xml)
        return dest
    monkeypatch.setattr(gc, "_fetch", _fetch)
    monkeypatch.setattr(snaps_mod, "render_snapshots", _never("draw sticker pictures"))
    monkeypatch.setattr(snaps_mod, "render_upright_sheet", _never("draw the up pictures"))
    monkeypatch.setattr(scout_mesh_mod, "scout_mesh", _never("measure openings from triangles"))
    monkeypatch.setattr(gc, "read_up_evidence", _never("read which way is up"))
    monkeypatch.setattr(gc, "declared_openings", _never("propose openings"))
    monkeypatch.setattr(gc, "_name_with_vision", _never("ask a model to name the part"))
    monkeypatch.setattr(gc, "_upright_by_pictures", _never("ask which way is up"))
    source = {"source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/set_top_box.ecxml",
              "sha256": "0" * 64, "size_bytes": len(xml), "original_filename": "set_top_box.ecxml",
              "suffix_hint": ".ecxml"}
    result = gc.run_geometry_check(session_id=str(SID), owner_id="o1", source=source)
    return store, result


def test_the_scout_attaches_the_thermal_view_and_skips_every_generic_step(scouted):
    store, result = scouted
    assert result.get("reason") is None, result
    assert result["status"] == "scouted" and result["check_mode"] == THERMAL_CHECK
    stored = json.loads(store.objects[f"sessions/{SID}/geometry_check/scout.json"])
    assert stored["thermal"]["title"] == "Set-top box, vented"
    assert stored["thermal"]["detail"]["standard"]["est_cells"] > 0          # sized by the child
    assert stored["snapshots"] == [] and "upright_sheet" not in stored
    facts, p = stored["facts"], stored["proposal"]
    assert facts["check_mode"] == THERMAL_CHECK and "up_evidence" not in facts
    assert facts["openings"] == [] and facts["source_regions"][0] == "air"
    assert p["openings"] == [] and p["input_kind"] == "solid-assembly" and p["part"] == "Set-top box, vented"
    assert p["up_axis"] == "+z" and p["size_mm"] == [180.0, 150.0, 80.0]
    assert f"sessions/{SID}/geometry_check/thermal_model.json" in store.objects
    skin = json.loads(store.objects[f"sessions/{SID}/geometry_check/skin.json"])
    assert skin["parts"] is True and len(skin["patches"]) == 8


def test_the_naming_of_a_thermal_model_asks_no_model_and_keeps_the_files_title(scouted):
    store, _ = scouted
    named = gc.run_geometry_naming(session_id=str(SID), owner_id="o1", purpose_text="CHT on this box")
    assert named["status"] == "ready" and named["named"] is True
    p = named["proposal"]
    assert p["part"] == "Set-top box, vented" and p["openings"] == [] and p["named"] is True
    assert named["thermal"]["title"] == "Set-top box, vented"


# -------------------------------------------------------------------------- the routes ----
class _Sources:
    async def get_for_owner(self, db, source_id, owner_id, *, organization_id=""):
        return SimpleNamespace(id=source_id, suffix_hint=".ecxml")


@pytest.fixture
def served(scouted, monkeypatch):
    from meshpipeline.persistence import session as dbmod
    from meshpipeline.persistence.repositories import geometry_source_repository as gsr

    monkeypatch.setattr(gsr, "GeometrySourceRepository", _Sources)
    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    session = SimpleNamespace(id=SID, job_id=None, geometry_source_id=uuid.uuid4(),
                              geometry_interpretation_id=uuid.uuid4(), messages=[], intake_gate={},
                              input_kind=None, intake_patches=None)

    async def _owned(session_id, owner_id, organization_id):
        return session
    monkeypatch.setattr(route, "_owned_session", _owned)

    @asynccontextmanager
    async def _db():
        yield SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(dbmod, "get_db", _db)

    async def _interp(db, sess, owner_id, organization_id):
        return {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm",
                "scale_to_metres": 0.001, "basis": "file_declared", "evidence": "ECXML"}
    monkeypatch.setattr(gh, "interpretation_payload", _interp)
    return session, scouted[0]


@pytest.mark.asyncio
async def test_an_ecxml_check_serves_the_thermal_block_and_no_generic_words(served):
    _session, store = served
    for step in ("scouted", "ready"):
        if step == "ready":
            gc.run_geometry_naming(session_id=str(SID), owner_id="o1", purpose_text="CHT")
        d = await route.get_check(SID, "alice", "org-1")
        assert d["status"] == step and d["check_mode"] == THERMAL_CHECK
        assert d["thermal"]["headline"].startswith("Set-top box, vented: a Flotherm thermal model with 8 parts")
        assert d["proposal"]["openings"] == [] and d["proposal"]["unit_fixed"] is True
        assert "fields" not in d and "facts" not in d and d.get("pictures", []) == []
        words = _our_words(d, _own_names(d["thermal"]))
        assert not FORBIDDEN.search(words), FORBIDDEN.findall(words)
        assert "flows through" not in words


@pytest.mark.asyncio
async def test_confirming_a_thermal_model_declares_no_openings_and_asks_nothing_about_them(served, monkeypatch):
    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    session, store = served

    class _Sessions:
        async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
            return session
    monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

    async def _turn(body, owner_id, organization_id):
        raise RuntimeError("no model here")
    monkeypatch.setattr(chatmod, "chat_message", _turn)
    # the form as the console sends it today: the kind, the flow, no openings - and a check_mode
    # it has no say in
    body = route.ConfirmIn(input_kind="solid-assembly", flow="internal", part="Set-top box, vented",
                           openings=[], size_mm=[180.0, 150.0, 80.0], unit="mm", check_mode="")
    out = await route.confirm_check(SID, body, "alice", "org-1")
    assert THERMAL_SIDES in out["message"] and out["patches"] == []
    assert "ask the user where the fluid enters" not in out["message"]
    assert not re.search(r"inlet|outlet", out["message"], re.I)
    assert session.intake_patches == [] and session.input_kind == "solid-assembly"
    stored = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert stored["check_mode"] == THERMAL_CHECK and stored["openings"] == []
    # the intake is told what that sentence means: no patches for the sides, no question about them
    from meshpipeline.agents.intake.agent import _block_geometry_check

    assert THERMAL_SIDES.startswith("The sides of the air box are the file's own")
    assert "the sides of the air box are the file's own, the thermal model sets them itself: send no "            "patches for them" in _block_geometry_check()


def test_a_generic_confirmation_with_no_openings_still_asks_where_the_fluid_goes():
    body = route.ConfirmIn(input_kind="fluid-domain", flow="internal", openings=[], check_mode="")
    m = confirmation_message(body)
    assert "ask the user where the fluid enters and leaves" in m and THERMAL_SIDES not in m
    assert patches_from(body) == []


# ------------------------------------------- nothing downstream demands an inlet or outlet ----
def test_a_heat_transfer_assembly_with_no_openings_passes_intake_validation_and_admission():
    from meshpipeline.agents.intake.admission_token import approved_intent_canonical
    from meshpipeline.agents.intake.validation import preview_admission, validate_submission

    errors = validate_submission({
        "domain": "set-top box thermal mesh",
        "request_txt": "Mesh the set-top box (Flotherm ECXML, 8 parts and the air, 8.7 W, 35 C ambient) "
                       "for a conjugate heat-transfer solve, Standard detail, as an OpenFOAM multi-region case.",
        "review_brief_txt": "Every part is its own region with at least two cells through each layer; "
                            "the air fills the rest of the box.",
        "dimensionality": "3D", "mesh_engine": "snapgrid", "engine_source": "suggested_confirmed",
        "purpose": "conjugate_heat_transfer", "input_kind": "solid-assembly", "engine_params": {},
        "patches": []})
    assert not [e for e in errors if re.search(r"patch|inlet|outlet|inflow|outflow", e, re.I)], errors
    assert preview_admission("snapgrid", "conjugate_heat_transfer", "solid-assembly",
                             dimensionality="3D").get("verdict") != "impossible"
    canonical = approved_intent_canonical(
        engine="snapgrid", purpose="conjugate_heat_transfer", input_kind="solid-assembly",
        dimensionality="3D", patches=[], engine_params={}, request_txt="x", requested_mesh_fidelity="standard")
    assert canonical["port_declaration"] == []


def test_the_check_is_quick_on_a_small_model(tmp_path):
    started = time.perf_counter()
    _view(set_top_box(), tmp_path)
    assert time.perf_counter() - started < 20.0
