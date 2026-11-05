# Responsibility: Pin that openings the uploaded file declares carry the file's reason - on the check, in the declaration and in the brief - and only while the user keeps the file's roles.
# Boundaries: real ECXML sidecars from the test models; the declaration and the brief are built from plain objects.
from __future__ import annotations

from types import SimpleNamespace

from tests.unit.cad.ecxml_models import ducted_board, tiny_board

from meshpipeline.application.geometry_check import declared_openings, declared_openings_reason
from meshpipeline.application.geometry_confirmation import (
    CONFIRMED_MARK,
    ROLES_MARK,
    confirmation_message,
    declared_reason_kept,
    roles_reason_of,
)
from meshpipeline.application.intake_brief import build_brief
from meshpipeline.cad.ingest.ecxml import read_ecxml
from meshpipeline.cad.ingest.ecxml_build import build

# THE TAKE (dev-areen set_top_box, 2026-10-11): all six domain sides came up "outlet", and a user
# who knew the box had a vent and a fan inside corrected it three times. The file declares every
# side of its solution domain open - the stage, the declaration and the brief never said so.

OPEN_ALL = "the file declares every side of its solution domain open"


def _facts(model):
    facts = {"scale_to_m": 1.0, "notes": [], "openings": []}
    declared_openings(facts, build(read_ecxml(model().xml())).sidecar)
    return facts


def test_a_model_open_on_every_side_says_so():
    assert _facts(tiny_board)["openings_reason"] == OPEN_ALL


def test_fans_vents_and_walls_are_named_as_the_file_declares_them():
    why = _facts(ducted_board)["openings_reason"]
    assert why.startswith("the file declares ")
    assert "Inlet_fan, a fan blowing in" in why and "Outlet, a vent" in why
    assert "-x, +x, -z, +z sides walled" in why


def test_nothing_declared_says_nothing():
    assert declared_openings_reason([], [], []) == ""


def _openings(facts, **changed):
    return [SimpleNamespace(id=o["id"], role=changed.get(o["name"], o["role"])) for o in facts["openings"]]


def test_the_reason_stands_only_while_the_files_roles_are_kept():
    facts = _facts(tiny_board)
    assert declared_reason_kept(facts, _openings(facts)) == OPEN_ALL
    assert declared_reason_kept(facts, _openings(facts, domain_xmin="inlet")) == ""
    assert declared_reason_kept(facts, _openings(facts)[:-1]) == ""        # one removed


def _body(facts, reason):
    ops = [SimpleNamespace(id=o["id"], name=o["name"], role=o["role"], diameter_mm=None,
                           width_mm=o.get("width_mm"), height_mm=o.get("height_mm"),
                           centroid_mm=o.get("centroid_mm")) for o in facts["openings"]]
    return SimpleNamespace(input_kind="solid-assembly", flow="internal", part="board", openings=ops,
                           regions=["air", "Board"], seed_point_mm=None, size_mm=None, unit=None,
                           unit_fixed=False, openings_reason=reason)


def test_the_declaration_says_why_and_the_brief_carries_it():
    facts = _facts(tiny_board)
    said = confirmation_message(_body(facts, OPEN_ALL))
    assert f"{ROLES_MARK} {OPEN_ALL}." in said
    messages = [{"role": "assistant", "content": said}]
    patches = [{"name": o["name"], "type": o["role"]} for o in facts["openings"]]
    assert roles_reason_of(messages, [{"name": p["name"], "role": p["type"]} for p in patches]) == OPEN_ALL
    session = SimpleNamespace(request_txt="mesh it", purpose="conjugate_heat_transfer", mesh_engine="snapgrid",
                              input_kind="solid-assembly", dimensionality="3D", requested_mesh_fidelity="standard",
                              domain="board", intake_patches=patches, engine_params=None, review_brief_txt="r",
                              intake_submitted=True, messages=messages)
    assert build_brief(session)["boundaries_reason"] == OPEN_ALL
    # a role the user changed in the chat afterwards is theirs: the brief no longer claims the file
    session.intake_patches = [{**patches[0], "type": "inlet"}] + patches[1:]
    assert "boundaries_reason" not in build_brief(session)


def test_without_a_reason_the_declaration_is_as_before():
    facts = _facts(tiny_board)
    said = confirmation_message(_body(facts, ""))
    assert ROLES_MARK not in said and said.startswith(CONFIRMED_MARK)
