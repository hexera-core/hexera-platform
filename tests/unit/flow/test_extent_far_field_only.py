# Responsibility: Verify far-field extents are read only from phrases that state one, and judged only where a far field exists.
from __future__ import annotations

import json
from pathlib import Path

import pytest

import meshpipeline.engines.cfmesh.flow_gates as fg
import meshpipeline.engines.contract as contract_mod
import meshpipeline.pipeline.executor as ex
import meshpipeline.settings.policy as polcfg
from meshpipeline.engines import domain_extent_gate as g

# The Fluent aorta export's request, as the lab sends it (an INTERNAL flow, ten capped openings).
# Main read 'ONE inlet and 9 outlets' as 9 chords downstream and blocked a good 6.4 M-cell mesh.
_AORTA = ("Internal flow through a thoraco-abdominal aorta with its side branches and iliac "
          "arteries (one inlet at the top, nine branch outlets), supplied as a CLOSED (capped) "
          "fluid-volume STL; the flat caps are the openings. There is no far-field box.\n"
          "Patches: there is ONE inlet and 9 outlets: inlet is the ROUND opening near (191.07, "
          "209.06, -47.67) mm, 20.14 mm in diameter (flow enters along -z); outlet_1 is the ROUND "
          "opening near (220.38, 187.78, -314.28) mm, 7.48 mm in diameter.")


@pytest.mark.parametrize("text", [
    _AORTA,
    "Air at 8 m/s, 1 inlet and 1 outlet. It goes in at the left end.",
    "a manifold with 2 inlets and 3 outlets",
    "a trunk with 5 side branches",
    "outlet_9 behind the bend is the smallest",
    "Water at 20 C inlet temperature",
    "the 4 sides of the duct are walls",
])
def test_a_count_of_openings_or_branches_is_not_an_extent(text):
    assert g.parse_requested_extents(text) == {}


@pytest.mark.parametrize("text, want", [
    ("20 chords upstream, 30 downstream, 20 above/below",
     {"upstream": 20.0, "downstream": 30.0, "lateral": 20.0}),
    ("20 chords upstream, 20 chords lateral, 30 chords downstream",
     {"upstream": 20.0, "downstream": 30.0, "lateral": 20.0}),
    ("in every direction: 5 of it upstream, 5 above, 5 below, 5 lateral, 8 downstream.",
     {"downstream": 8.0, "lateral": 5.0}),
    ("margins: 5 upstream, 10 downstream, 5 to each side, 5 above.",
     {"upstream": 5.0, "downstream": 10.0, "lateral": 5.0}),
    ("Domain must be 20 chords upstream, wake 50 chords downstream.",
     {"upstream": 20.0, "downstream": 50.0}),
    ("D_UPSTREAM=20 D_DOWNSTREAM=30 D_LATERAL=20 chord lengths",
     {"upstream": 20.0, "downstream": 30.0, "lateral": 20.0}),
    # the boundary words as far-field places, stated with their multiple
    ("10c inlet, 15c outlet, 5c side", {"upstream": 10.0, "downstream": 15.0, "lateral": 5.0}),
    ("2.5 chords in front and 1.5c behind", {"upstream": 2.5, "downstream": 1.5}),
])
def test_far_field_phrasing_reads_as_it_always_did(text, want):
    assert g.parse_requested_extents(text) == want


def test_a_sentence_end_does_not_hide_a_later_margin():
    # main's number pattern also took a lone '.', and the first '. Lateral' it met ended the search
    assert g.parse_requested_extents("Flow is along +x. Lateral room: 5 lateral.") == {"lateral": 5.0}


def _boxes(down: float) -> dict:
    body = {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 0.2, "zmin": 0.0, "zmax": 0.2}
    box = {"xmin": -5.0, "xmax": 1.0 + down, "ymin": -5.0, "ymax": 5.2, "zmin": -5.0, "zmax": 5.2}
    return {"chord": 1.0, "domain_box": box, "body_box": body}


def test_an_internal_flow_has_no_far_field_to_judge():
    internal = {"flow_topology": "internal", "geometry": _boxes(0.0)}
    assert not g.far_field_exists(manifest=internal)
    assert not g.far_field_exists("internal", {"geometry": _boxes(0.0)})
    assert g.extent_gate_for_request("8 downstream", internal) == (True, "")
    v = g.evaluate_domain_extents({"downstream": 8.0}, 1.0, internal, flow_axis="+x")
    assert v.status == "na"


def test_an_external_flow_is_still_judged():
    external = {"flow_topology": "external", "geometry": _boxes(0.0)}
    assert g.far_field_exists("external", external) and g.far_field_exists("", {})
    ok, diag = g.extent_gate_for_request("8 downstream", external)
    assert not ok and "[DOMAIN_EXTENT_MISMATCH]" in diag
    assert g.evaluate_domain_extents({"downstream": 8.0}, 1.0, external,
                                     flow_axis="+x").status == "block"


# ---- the executor: the gate runs only where a far field exists ----
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    # node_executor publishes through the ownership-checked port; these tests exercise the gate
    # sequence, not Redis or PostgreSQL, so the port is stood in for
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    install(monkeypatch, _ep)
    monkeypatch.setattr(ex, "execution_publisher", _ep.execution_publisher)


def _arm(monkeypatch, ws: Path, topology: str):
    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass

    class _Engine:
        name = "cfmesh"

        def finalize(self, *a, **k):
            return {"success": True, "output": "finalize-out"}

        def check_domain_extents(self, request_txt, manifest):
            return g.extent_gate_for_request(request_txt, manifest)

        def check_solvability(self, w, metrics_out=None):
            return True, ""

    manifest = {"schema_version": 1, "flow_topology": topology, "geometry": _boxes(0.0),
                "patches": ["wall", "inlet", "outlet"], "quality": {"fatal": [], "cells": 1000},
                "validation": {"has_wall": True, "has_inflow": True, "has_outflow": True}}
    (ws / "mesh_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    monkeypatch.setattr(ex, "get_engine", lambda n="": _Engine())
    monkeypatch.setattr(fg, "_validate_manifest", lambda *a, **k: (True, ""))
    monkeypatch.setattr(contract_mod, "check_contract", lambda **k: (True, ""))
    monkeypatch.setattr(polcfg, "DOMAIN_EXTENT_GATE_ENABLED", True)
    monkeypatch.setattr(polcfg, "SOLVABILITY_GATE_ENABLED", False)


def _state(ws: Path, topology: str, **over) -> dict:
    st = {"openfoam_workspace": str(ws), "job_id": "t", "engine": "cfmesh",
          "flow_topology": topology, "request_txt": "Keep 8 downstream of the part.",
          "intake_patches": [{"name": "wall", "type": "wall"}]}
    st.update(over)
    return st


async def test_the_executor_never_judges_an_internal_flows_extent(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, "internal")
    out = await ex.node_executor(_state(tmp_path, "internal"))
    assert out["executor_success"] is True
    assert out.get("executor_failed_gate", "") != "domain_extent"
    # nor through the typed request, should an internal job ever carry one
    out = await ex.node_executor(_state(tmp_path, "internal", requested_extents={"downstream": 8},
                                        reference_length_m=1.0, flow_axis="+x"))
    assert out["executor_success"] is True


async def test_the_executor_still_judges_an_external_flows_extent(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, "external")
    out = await ex.node_executor(_state(tmp_path, "external"))
    assert out["executor_success"] is False
    assert out["executor_failed_gate"] == "domain_extent"
