# Responsibility: Verify every event type has one typed constructor and one publisher, and survives the wire round trip.
from __future__ import annotations

import json
from pathlib import Path

import meshpipeline.events as E

ROOT = Path(__file__).parent.parent.parent.parent
UI = ROOT / "ui"
APP = ROOT / "src" / "meshpipeline"


# the closed contract


def test_every_event_type_has_a_typed_constructor_and_exactly_one_publisher():
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    for name in E.EVENT_TYPES:
        assert callable(getattr(E, name, None)), f"{name} has no typed constructor"
        assert hasattr(JobPublisher, name), f"{name} has no publisher method"


def test_the_delivery_filter_admits_only_the_closed_set():
    ws = (APP / "api" / "v1" / "ws.py").read_text()
    assert 'data.get("type") not in E.EVENT_TYPES' in ws


def test_the_contract_has_no_untyped_escape_hatch():
    src = (APP / "events" / "__init__.py").read_text()
    assert "def custom(" not in src and "def raw(" not in src
    # every constructor returns a UiEvent, whose __post_init__ closes the set
    for name in ("stage", "attempt", "note", "check", "action", "search",
                 "screenshot", "file", "meshing", "meshed", "verdict", "closing"):
        assert f"def {name}(" in src


def test_an_event_survives_the_wire_round_trip():
    w = E.file("builder", "system/meshDict", 2418, "created").wire()
    restored = json.loads(json.dumps(w))
    assert restored == w
    assert restored["type"] in E.EVENT_TYPES


# privacy


def test_the_brief_that_reaches_the_browser_carries_no_authorization():
    from meshpipeline.application.intake_brief import NEVER_EXPOSED, build_brief

    class S:
        request_txt = "x" * 100
        review_brief_txt = "y" * 80
        purpose = "external_cfd"; mesh_engine = "cfmesh"; input_kind = "body-surface"
        dimensionality = "3D"; requested_mesh_fidelity = None; domain = "d"
        intake_patches = [{"name": "w", "type": "wall"}]
        engine_params = {}; intake_submitted = True
        intake_gate = {"admission": {"token": "tok_LEAK"}}
        llm_metadata = [{"model": "m"}]; messages = []; owner_id = "o"

    flat = json.dumps(build_brief(S()))
    assert "tok_LEAK" not in flat
    for column in NEVER_EXPOSED:
        assert column not in flat


def test_an_engine_the_payload_builder_has_never_heard_of_still_renders():
    from meshpipeline.application.viewer_payload import _attach_facts

    manifest = {
        "mesh_mode": "voxelflow_ng",          # no such engine, deliberately
        "mesh_units": "m", "cell_count": 4242,
        "patch_types": {"skin": "wall", "far": "farfield"},
        "patch_face_counts": {"skin": 900, "far": 120},
        "quality": {"cells": 4242, "faces": 12726, "regions": 2, "hexahedra": 4000,
                    "max_non_ortho": 31.5},
        "inspection_regions": [{"name": "cut_x", "kind": "slice"}],
    }
    resp = {"kind": "polymesh", "is_mesh": True,
            "patches": [{"name": "skin"}, {"name": "far"}]}
    _attach_facts(resp, manifest)

    assert resp["quality"]["cells"] == 4242
    assert resp["quality"]["engine"] == "voxelflow_ng", "the engine is reported as data"
    assert resp["quality"]["units"] == "m"
    ids = [p["id"] for p in resp["parts"]]
    assert "boundary:skin" in ids and "boundary:far" in ids
    assert any(i.startswith("region:") for i in ids), "regions were dropped for an unknown engine"
    assert any(i.startswith("cellGroup:") for i in ids)
    assert any(i.startswith("inspection:") for i in ids)
    # the farfield behaviour is derived from the declared patch ROLE, not the engine
    far = next(p for p in resp["parts"] if p["id"] == "boundary:far")
    assert far["default_visibility"] == "hidden" and far["metadata"]["encloses_domain"] is True
