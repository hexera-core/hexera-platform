# Responsibility: Verify an ECXML upload reaches the chat as what it is - an assembly of solid parts,
# each its own region, with the openings the file declares - and that its unit, fixed by the
# conversion, is never asked and never changed. On dev the naming step called a set-top box "a solid
# body the fluid flows around", the form could not say otherwise, and the chat then refused the
# multi-region mesher; and "metres" (true of the ECXML source) made the millimetre box 180 m long.
# Boundaries: pure functions and the routes / message authority with the store, the repositories
# and the session stood in for; no CAD, no database, no model.
from __future__ import annotations

import json
import re
import time
import typing
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import meshpipeline.agents.intake.message as msg
import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import unit_change as uch
from meshpipeline.application.geometry_confirmation import (
    confirmation_message,
    declared_case,
    patches_from,
    with_declaration,
)
from meshpipeline.contracts import geometry_fields as gf
from meshpipeline.contracts import object_storage
from meshpipeline.contracts.geometry_units import GeometryInterpretation, LengthUnit, scale_to_metres
from meshpipeline.contracts.intake_formats import format_for_key, unit_fixing_format

SID = uuid.UUID("dddd1111-2222-4222-b222-dddddddddddd")
ECXML = format_for_key("ecxml")
REPO = Path(__file__).resolve().parents[3]

#: The set-top box's six open domain sides, as cad/ingest's sidecar declares them and the scout
#: turns them into openings (declared_openings).
SIDES = [("domain_xmin", [-30.0, 45.0, 20.0], 150.0, 80.0), ("domain_xmax", [150.0, 45.0, 20.0], 150.0, 80.0),
         ("domain_ymin", [60.0, -30.0, 20.0], 180.0, 80.0), ("domain_ymax", [60.0, 120.0, 20.0], 180.0, 80.0),
         ("domain_zmin", [60.0, 45.0, -20.0], 180.0, 150.0), ("domain_zmax", [60.0, 45.0, 60.0], 180.0, 150.0)]
REGIONS = ["air", "Case", "PCB", "SoC", "DDR_1", "DDR_2", "PMIC", "Bulk_cap", "SoC_heat_sink"]


def _assembly_facts() -> dict:
    """What the scout stores for the set-top box: the file read as an assembly, its sides open."""
    openings = [{"id": i, "face": None, "name": n, "role": "outlet", "kind": "disc", "shape": "rectangle",
                 "confidence": 0.6, "centroid_mm": c, "width_mm": w, "height_mm": h, "diameter_mm": 120.0,
                 "normal": [1.0, 0.0, 0.0], "declared_by": {"object": None, "role": "domain_boundary", "name": n}}
                for i, (n, c, w, h) in enumerate(SIDES, 1)]
    return {"body_kind": "solid", "input_kind": "solid-assembly", "flow": "internal",
            "size_mm": [180.0, 150.0, 80.0], "seed_point_mm": [60.0, 45.0, 20.0], "scale_to_m": 0.001,
            "confidence": {"input_kind": 0.95}, "notes": [], "openings": openings,
            "source_format": "ecxml", "source_regions": REGIONS, "thermal_model": {"object_key": "k"}}


#: What the picture model answered on dev: a body in a flow, every sticker "not an opening".
NAMING_ON_DEV = {"part": "set-top box", "flow": "external", "input_kind": "solid-body", "confidence": 0.99,
                 "openings": [{"id": i, "name": f"face_{i}", "role": "not_an_opening", "confidence": 0.9}
                              for i in range(1, 7)],
                 "flow_axis": "+x", "real_length_m": 0.12, "notes": "the fluid flows around the box"}


# -------------------------------------------------------------- Bug 1: the assembly -----
def test_the_proposal_follows_the_file_not_the_picture_model_for_an_ecxml_assembly():
    p = gc._proposal(_assembly_facts(), NAMING_ON_DEV)
    assert p["input_kind"] == "solid-assembly" and p["flow"] == "internal"
    assert [o["role"] for o in p["openings"]] == ["outlet"] * 6                   # the file's, not "not an opening"
    assert [o["name"] for o in p["openings"]] == [s[0] for s in SIDES]           # the file's names
    assert p["part"] == "set-top box"                                            # the model still names the part
    assert "the fluid flows around the box" not in " ".join(p["notes"])


def test_an_ordinary_part_still_follows_whoever_is_surer():
    facts = {**_assembly_facts(), "input_kind": "body-surface", "flow": "internal",
             "confidence": {"input_kind": 0.6}, "source_regions": None, "thermal_model": None}
    for o in facts["openings"]:
        o.pop("declared_by")
    p = gc._proposal(facts, NAMING_ON_DEV)
    assert p["input_kind"] == "solid-body" and p["flow"] == "external"
    assert {o["role"] for o in p["openings"]} == {"not_an_opening"}


def test_the_picture_model_is_told_the_file_declares_the_assembly():
    text = gc._facts_text(_assembly_facts(), [], "conjugate heat transfer on this set-top box")
    assert "The file itself declares this: an assembly of 9 regions" in text


def test_an_assembly_is_a_kind_the_form_offers_and_the_confirm_accepts():
    kinds = [v for v, _ in gf.INPUT_KIND_OPTIONS]
    assert "solid-assembly" in kinds
    spec = next(s for s in gf.form_spec() if s["key"] == "input_kind")
    assert [o[0] for o in spec["options"]] == kinds
    # the confirm model takes exactly the registry's kinds
    assert set(typing.get_args(route.ConfirmIn.model_fields["input_kind"].annotation)) == set(kinds)
    # and so does the console's form, in both copies the parity rule keeps identical
    for copy in ("ui/js/render/geometry_form.js", "apps/console/public/static/js/render/geometry_form.js"):
        js = (REPO / copy).read_text(encoding="utf-8")
        kind_block = re.search(r"export const KIND = \[(.*?)\];", js, re.S).group(1)
        assert re.findall(r'\["([a-z-]+)",', kind_block) == kinds, copy


def _confirmed_assembly(**over):
    openings = [route.ConfirmedOpening(id=i, name=n, role="outlet", centroid_mm=c, width_mm=w, height_mm=h)
                for i, (n, c, w, h) in enumerate(SIDES, 1)]
    return route.ConfirmIn(**{"input_kind": "solid-assembly", "flow": "internal", "part": "set-top box",
                              "openings": openings, "size_mm": [180.0, 150.0, 80.0], "unit": "mm",
                              "regions": REGIONS, **over})


def test_the_chat_is_told_the_file_is_an_assembly_of_regions_with_its_openings():
    m = confirmation_message(_confirmed_assembly())
    assert ("the file is an assembly of solid parts, each its own region (set-top box), "
            "input_kind solid-assembly; the fluid flows through it.") in m
    assert "multi-region (conjugate heat transfer)" in m
    assert "Its 9 regions, named as in the file: air, Case, PCB, SoC" in m
    assert "domain_xmin (outlet), 150 x 80 mm" in m
    assert "a solid body" not in m and "single" not in m
    assert [p["name"] for p in patches_from(_confirmed_assembly())][:2] == ["domain_xmin", "domain_xmax"]


def test_a_confirmed_assembly_is_conjugate_heat_transfer_which_the_multi_region_mesher_takes():
    history = with_declaration([{"role": "user", "content": "Conjugate heat transfer on this set-top box."}],
                               confirmation_message(_confirmed_assembly()))
    assert declared_case(history) == ("conjugate_heat_transfer", "solid-assembly")
    from meshpipeline.agents.intake.validation import preview_admission

    assert preview_admission("snappy_multiregion", "conjugate_heat_transfer", "solid-assembly",
                             dimensionality="3D").get("verdict") != "impossible"
    # the multi-region mesher is not "impossible" for what was confirmed - the refusal the chat looped on
    assert preview_admission("snappy", "conjugate_heat_transfer", "solid-assembly",
                             dimensionality="3D").get("verdict") == "impossible"


def test_a_long_region_list_is_counted_not_spelled_out():
    many = [f"part_{i}" for i in range(1000)]
    m = confirmation_message(_confirmed_assembly(regions=many))
    assert "Its 1000 regions" in m and "part_23, and 976 more." in m and "part_24" not in m


# ----------------------------------------------------------- Bug 2: the fixed unit -----
def test_ecxml_fixes_its_unit_at_the_millimetres_its_solids_are_built_in():
    from meshpipeline.cad.ingest.ecxml_build import UNIT
    from meshpipeline.cad.ingest.units import declared_unit

    assert ECXML.fixed_unit == "mm" and scale_to_metres(LengthUnit(ECXML.fixed_unit)) == pytest.approx(1.0 / UNIT)
    assert declared_unit(Path("input.ecxml"), "ecxml").unit is LengthUnit(ECXML.fixed_unit)
    assert unit_fixing_format(".ecxml") is ECXML and unit_fixing_format(".xml") is ECXML
    for other in (".stl", ".step", ".igs", ".brep", ".3mf", ".obj", ""):
        assert unit_fixing_format(other) is None, other


class _Sources:
    suffix = ".ecxml"

    async def get_for_owner(self, db, source_id, owner_id, *, organization_id=""):
        return SimpleNamespace(id=source_id, suffix_hint=self.suffix)


@pytest.fixture
def ecxml_source(monkeypatch):
    from meshpipeline.persistence.repositories import geometry_source_repository as gsr

    monkeypatch.setattr(gsr, "GeometrySourceRepository", _Sources)
    return _Sources


@pytest.mark.asyncio
async def test_the_upload_format_says_whether_the_unit_is_fixed(ecxml_source, monkeypatch):
    session = SimpleNamespace(id=SID, geometry_source_id=uuid.uuid4())
    assert await gh.unit_fixing_format(object(), session, "alice", "org") is ECXML
    monkeypatch.setattr(ecxml_source, "suffix", ".stl")
    assert await gh.unit_fixing_format(object(), session, "alice", "org") is None
    assert await gh.unit_fixing_format(object(), SimpleNamespace(id=SID, geometry_source_id=None), "a", "") is None


class _Store:
    def __init__(self, objects: dict):
        self.objects = {f"sessions/{SID}/geometry_check/{k}": json.dumps(v).encode() for k, v in objects.items()}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = local_path.read_bytes()

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)

    def create_download_url(self, *, object_key, expires_in):
        return f"https://pictures/{object_key}"


DECLARED_MM = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
               "basis": "file_declared", "evidence": "ECXML states every length in metres"}


@pytest.mark.asyncio
async def test_metres_named_in_the_chat_changes_nothing_on_a_converted_ecxml_model(ecxml_source, monkeypatch):
    """"What unit does the ECXML model use?" - "metres" is true of the source, never of the
    millimetre geometry it was converted into: refused before anything is read or written."""
    store = _Store({"scout.json": {"facts": {"size_mm": [180.0, 150.0, 80.0], "scale_to_m": 0.001}}})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    recorded: list = []
    from meshpipeline.persistence.repositories import geometry_interpretation_repository as girmod

    class _Interps:
        async def record(self, *a, **k):
            recorded.append(k)
    monkeypatch.setattr(girmod, "GeometryInterpretationRepository", _Interps)

    async def _interp(db, sess, owner_id, organization_id):
        return DECLARED_MM
    monkeypatch.setattr(gh, "interpretation_payload", _interp)
    session = SimpleNamespace(id=SID, geometry_source_id=uuid.uuid4(), geometry_interpretation_id=uuid.uuid4(),
                              intake_gate={}, messages=[])
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    repo.bind_geometry_interpretation = AsyncMock()
    with pytest.raises(uch.UnitFixed) as fixed:
        await uch.change_unit(object(), session, owner_id="alice", organization_id="org", unit=LengthUnit.metre,
                              session_repo=repo)
    assert recorded == [] and not repo.bind_geometry_interpretation.await_count
    reply = fixed.value.reply()
    assert reply.startswith("This file's unit is fixed, so I changed nothing: an ECXML model states every length")
    assert "the part is 180 mm long" in reply

    # through the message authority: the reply is the application's, the unit stays millimetres
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="metres", organization_id="org")
    outcome = await msg._settle(inbound, object(), gate={}, locked=session, messages=[], revision="r1",
                                session_repo=repo, logger=MagicMock())
    assert outcome.status is msg.MessageStatus.unit_changed and outcome.reply == reply
    assert recorded == [] and repo.append_message.await_args.args[2:] == ("assistant", reply)
    repo.set_intake_gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_converted_ecxml_model_with_no_unit_recorded_is_never_asked_for_one(ecxml_source, monkeypatch):
    """An upload whose unit could not be recorded then: the fixed unit is recorded as the file's,
    silently, instead of the unit question."""
    from meshpipeline.persistence.repositories import geometry_interpretation_repository as girmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    recorded: list = []

    class _Interps:
        async def record(self, db, *, owner_id, geometry_source_id, unit, basis, evidence, organization_id=""):
            recorded.append((unit, basis.value, evidence))
            return GeometryInterpretation(interpretation_id=str(uuid.uuid4()), owner_id=owner_id,
                                          geometry_source_id=str(geometry_source_id), unit=unit,
                                          scale_to_metres=scale_to_metres(unit), basis=basis, evidence=evidence)
    monkeypatch.setattr(girmod, "GeometryInterpretationRepository", _Interps)
    bound: list = []

    class _Sessions:
        async def bind_geometry_interpretation(self, db, session_id, interpretation_id):
            bound.append(interpretation_id)
    monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

    async def _none(db, sess, owner_id, organization_id):
        return None
    monkeypatch.setattr(gh, "interpretation_payload", _none)
    monkeypatch.setattr(gh, "hold_applies", lambda sid: None)
    session = SimpleNamespace(id=SID, geometry_source_id=uuid.uuid4(), geometry_interpretation_id=None,
                              intake_gate={}, messages=[])
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="Conjugate heat transfer on this.",
                                 organization_id="org")
    outcome = await msg._settle(inbound, object(), gate={}, locked=session, messages=[], revision="r1",
                                session_repo=repo, logger=MagicMock())
    assert outcome.status is msg.MessageStatus.unit_recorded
    assert recorded == [(LengthUnit.millimetre, "file_declared",
                         "fixed by the conversion: " + ECXML.fixed_unit_reason)]
    assert len(bound) == 1
    repo.append_message.assert_not_awaited()                     # no question was asked


def test_the_declaration_says_the_unit_is_settled_when_the_conversion_fixed_it():
    m = confirmation_message(_confirmed_assembly(unit_fixed=True))
    assert "The file is in millimetres: the part is 18 cm long." in m
    assert "never ask the user what unit the file is in" in m
    assert "never ask" not in confirmation_message(_confirmed_assembly())


# ------------------------------------------------- the routes, end to end of the form -----
@pytest.fixture
def served(monkeypatch, ecxml_source):
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
    from meshpipeline.persistence import session as dbmod
    monkeypatch.setattr(dbmod, "get_db", _db)

    async def _interp(db, sess, owner_id, organization_id):
        return DECLARED_MM
    monkeypatch.setattr(gh, "interpretation_payload", _interp)
    facts = _assembly_facts()
    check = {"status": "ready", "named": True, "written_at": time.time(), "facts": facts,
             "proposal": {**gc._proposal(facts, NAMING_ON_DEV), "real_length_m": 120.0},
             "snapshots": [], "skin_key": "k"}
    store = _Store({"scout.json": check})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    return session, store


@pytest.mark.asyncio
async def test_the_stage_shows_the_fixed_unit_and_never_offers_another(served):
    d = await route.get_check(SID, "alice", "org-1")
    p = d["proposal"]
    assert p["unit"] == "mm" and p["unit_fixed"] is True and "unit_suggestion" not in p
    assert p["input_kind"] == "solid-assembly" and p["flow"] == "internal"


@pytest.mark.asyncio
async def test_the_confirm_declares_the_assembly_and_refuses_another_unit(served, monkeypatch):
    from fastapi import HTTPException

    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    session, store = served
    with pytest.raises(HTTPException) as refused:
        await route.confirm_check(SID, _confirmed_assembly(unit="m", regions=None), "alice", "org-1")
    assert refused.value.status_code == 409 and "fixed at mm" in refused.value.detail

    class _Sessions:
        async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
            return session
    monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

    async def _turn(body, owner_id, organization_id):
        raise RuntimeError("no model here")
    monkeypatch.setattr(chatmod, "chat_message", _turn)
    out = await route.confirm_check(SID, _confirmed_assembly(regions=None, unit_fixed=False), "alice", "org-1")
    assert "input_kind solid-assembly" in out["message"]
    assert "Its 9 regions, named as in the file: air, Case" in out["message"]     # from the check
    assert "never ask the user what unit the file is in" in out["message"]          # set by the server
    assert session.input_kind == "solid-assembly"
    stored = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert stored["unit_fixed"] is True and stored["regions"] == REGIONS
