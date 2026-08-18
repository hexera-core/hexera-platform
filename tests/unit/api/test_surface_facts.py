# Responsibility: Verify the viewer and the result read one authority, and a malformed manifest costs no mesh.
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
VIEWER = APP / "application" / "viewer_payload.py"


def test_the_payload_no_longer_deletes_the_outer_boundary():
    src = VIEWER.read_text()
    assert 'if r == "farfield"' not in src, \
        "the surface payload strips the farfield again - the viewer cannot show what it is not sent"
    assert re.search(r"_skip_names:\s*tuple\[str, \.\.\.\]\s*=\s*\(\)", src), \
        "the skip list is no longer empty by default"


def test_the_skip_mechanism_survives_for_genuinely_internal_objects():
    src = VIEWER.read_text()
    assert "skip_names=_skip_names" in src
    from meshpipeline.render.viewer_pack import stl_response
    out = stl_response({"a": [((0, 0, 0), (1, 0, 0), (0, 1, 0))],
                        "internal_helper": [((0, 0, 0), (1, 0, 0), (0, 1, 0))]},
                       {}, "m", ("internal_helper",))
    assert [p["name"] for p in out["patches"]] == ["a"]


def test_an_input_skin_declares_no_delivered_quality():
    from meshpipeline.application.viewer_payload import _attach_facts
    resp = {"kind": "stl", "is_mesh": False, "patches": [{"name": "body"}]}
    _attach_facts(resp, {"mesh_mode": "cfmesh", "quality": {"cells": 5}})
    assert "quality" not in resp
    assert resp["parts"] == []


def test_a_malformed_manifest_does_not_cost_the_user_their_mesh():
    from meshpipeline.application.viewer_payload import _attach_facts
    resp = {"kind": "polymesh", "is_mesh": True, "patches": [{"name": "w"}]}
    _attach_facts(resp, {"quality": "not a dict", "patch_types": None})
    assert resp["parts"] == [] or isinstance(resp["parts"], list)
    assert "patches" in resp, "the geometry was dropped because the metadata was bad"


def test_the_viewer_and_the_result_read_the_same_authority():
    src = VIEWER.read_text()
    assert "from meshpipeline.render.mesh_facts import parts as _parts" in src
    assert "from meshpipeline.render.mesh_facts import quality as _quality" in src
    assert src.count("def _attach_facts") == 1
    assert src.count("_attach_facts(resp, manifest)") == 2, \
        "a payload branch returns without the mesh facts"
