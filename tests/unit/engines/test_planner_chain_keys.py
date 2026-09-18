# Responsibility: Verify the chain's and the coverage facts' keys reach the planner, each with its note, and that the note never predicts a mesh.
from __future__ import annotations

from tests.unit.engines.test_planner_geometry_agent_block import BLOCK, _captured_user_message

from meshpipeline.engines.snappy import planner

CHAIN = {"intake": {"confirmed": {"opening.role:o1": "inlet"}, "assumed": {}, "from_the_brief": {},
                    "still_open": [], "write_up": "CONFIRMED by the customer: o1 = inlet."},
         "flow_patches": {"o1": {"role": "inlet", "kind": "confirmed", "by": "customer"}},
         "plan_envelope": {"cells_high": 1_800_000, "source": "tools.estimate_builder_cells on the plan"},
         "places_refused": [{"kind": "gap_clusters", "why": "which side of the surface is the flow is not settled"}],
         "coverage": {"version": "geometry_agent.coverage.v1",
                      "outside": {"status": "measured", "points_m": [{"side": "x_min", "point": [-0.1, 0, 0]}]},
                      "refused": ["local_feature_size", "vertex_curvature"]}}


def test_the_chain_and_coverage_keys_are_on_the_allowlist():
    for key in ("places_refused", "coverage", "intake", "flow_patches", "plan_envelope", "survey"):
        assert key in planner.GEOMETRY_AGENT_BLOCK_KEYS, key
    out = planner._validated_agent_block({**BLOCK, **CHAIN}, "j")
    assert out is not None and all(k in out for k in CHAIN)


def test_each_note_rides_with_its_key_and_only_with_it(monkeypatch, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    bare = _captured_user_message(monkeypatch, tmp_path / "a", geometry_agent=dict(BLOCK))["user"]
    full = _captured_user_message(monkeypatch, tmp_path / "b", geometry_agent={**BLOCK, **CHAIN})["user"]
    assert 'ABOUT "intake"' not in bare and 'ABOUT "coverage"' not in bare
    assert 'ABOUT "intake", "flow_patches" AND "plan_envelope"' in full and 'ABOUT "coverage"' in full
    assert '"plan_envelope"' in full and '"places_refused"' in full


def test_the_block_note_describes_places_and_predicts_no_mesh():
    note = planner._AGENT_BLOCK_NOTE
    assert "stair-stepped" not in note and "layers collapse" not in note
    assert "places_refused" in note and "flow path first" in note
