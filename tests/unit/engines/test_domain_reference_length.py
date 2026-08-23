# Responsibility: Verify the domain-extent gate judges the far-field in the user's own reference length.
from __future__ import annotations

from meshpipeline.engines.domain_extent_gate import (
    check_domain_extents,
    parse_requested_extents,
)

# The CRM wind-tunnel model, verbatim: body 1.738 m long, MAC 0.2758 m. The planner honoured the
# user's "20 chords" by building 5.561 m of lateral clearance = 20.2 MAC = 3.2 body lengths.
_BODY = {"xmin": 0.063, "xmax": 1.801, "ymin": -0.795, "ymax": 0.795, "zmin": -0.019, "zmax": 0.235}
_BOX = {"xmin": 0.063 - 5.560, "xmax": 1.801 + 8.342,
        "ymin": -0.795 - 5.561, "ymax": 0.795 + 5.561,
        "zmin": -0.019 - 5.561, "zmax": 0.235 + 5.561}
_REQUEST = "20 chords upstream, 20 chords lateral, 30 chords downstream"


def _geom(**extra) -> dict:
    g = {"domain_box": dict(_BOX), "body_box": dict(_BODY), "chord": 1.738}
    g.update(extra)
    return g


def test_a_domain_sized_in_the_users_chord_passes_when_judged_in_it():
    # This exact mesh was rejected twice - "requested 20c, mesh has 3.2c" - because the request's
    # "20" is in MACs and the gate divided by the body length. Same numbers, one ruler: it passes.
    req = parse_requested_extents(_REQUEST)
    assert req == {"upstream": 20.0, "lateral": 20.0, "downstream": 30.0}
    ok, diag = check_domain_extents(
        req, {"geometry": _geom(reference_length=0.2758,
                                reference_length_source="user_stated")})
    assert ok, diag


def test_without_a_stated_reference_the_body_length_still_judges_it():
    # No reference quoted -> the old behaviour exactly: divided by the body length, 3.2 vs 20,
    # rejected. Fail closed to the unit the planner's margins are natively in.
    ok, diag = check_domain_extents(
        parse_requested_extents(_REQUEST), {"geometry": _geom()})
    assert not ok
    assert "3.2" in diag, diag


def test_the_rejection_names_the_ruler_it_measured_with():
    # A rejection the planner can act on must say which unit the verdict is in - the revision
    # loop was moving the box on one ruler while being judged on another, and could not converge.
    ok, diag = check_domain_extents(
        parse_requested_extents(_REQUEST),
        {"geometry": _geom(reference_length=0.5, reference_length_source="user_stated")})
    assert not ok
    assert "user_stated" in diag and "0.5" in diag, diag


def test_manifest_records_the_ruler_beside_the_chord(tmp_path):
    from tests.engine_workspaces import build_workspace  # noqa: PLC0415

    import json

    from meshpipeline.engines.manifest import write_manifest

    ws = build_workspace(tmp_path, "snappy")
    write_manifest(ws, patch_types={"body": "wall"}, patch_entities={"body": []},
                   bbox=(0,0,0,1.738,1.0,0.3), quality={"cells": 10}, domain="d",
                   body_bbox=[[0, 0, 0], [1.738, 1.0, 0.3]],
                   reference_length=0.2758, mesh_units="m")
    g = json.loads((ws / "mesh_manifest.json").read_text())["geometry"]
    assert g["chord"] == 1.738
    assert g["reference_length"] == 0.2758
    assert g["reference_length_source"] == "user_stated"

    write_manifest(ws, patch_types={"body": "wall"}, patch_entities={"body": []},
                   bbox=(0,0,0,1.738,1.0,0.3), quality={"cells": 10}, domain="d",
                   body_bbox=[[0, 0, 0], [1.738, 1.0, 0.3]], mesh_units="m")
    g = json.loads((ws / "mesh_manifest.json").read_text())["geometry"]
    assert g["reference_length"] == g["chord"]
    assert g["reference_length_source"] == "body_streamwise_extent"
