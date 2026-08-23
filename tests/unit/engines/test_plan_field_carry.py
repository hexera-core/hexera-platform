# Responsibility: Pin plan-field carry-forward across retry attempts. The heat-sink retries
# (job 3240fc78) lost reference_length_m and max_cells when the planner revised: the domain
# gate then measured with a wrong fallback ruler and rejected two production-grade meshes.
# Durable facts must survive re-planning; per-attempt choices must not be touched.
from __future__ import annotations

import json

from meshpipeline.engines.snappy.drivers import (
    _inherit_durable_plan_fields,
    _sibling_plan_memory,
)


def make_attempt(tmp_path, n: int, plan: dict | None):
    ws = tmp_path / f"attempt_{n}"
    ws.mkdir(parents=True, exist_ok=True)
    if plan is not None:
        (ws / ".last_plan.json").write_text(json.dumps(plan))
    return ws


class TestDurableFieldsSurviveReplanning:
    def test_the_heat_sink_trajectory_keeps_its_ruler(self, tmp_path):
        # attempt 1 planned with the user's ruler and a real budget; the revision dropped both
        make_attempt(tmp_path, 1, {"reference_length_m": 0.06, "max_cells": 5_000_000,
                                   "approach": "external aero"})
        ws2 = make_attempt(tmp_path, 2, None)
        revised = {"approach": "bigger far-field box"}
        out = _inherit_durable_plan_fields(revised, ws2)
        assert out["reference_length_m"] == 0.06
        assert out["max_cells"] == 5_000_000
        assert out["approach"] == "bigger far-field box"  # choices stay the revision's own

    def test_a_revision_that_states_its_own_values_is_untouched(self, tmp_path):
        make_attempt(tmp_path, 1, {"reference_length_m": 0.06, "max_cells": 5_000_000})
        ws2 = make_attempt(tmp_path, 2, None)
        revised = {"reference_length_m": 0.06, "max_cells": 2_000_000}
        assert _inherit_durable_plan_fields(revised, ws2) == revised

    def test_inheritance_walks_past_an_attempt_that_also_lost_the_field(self, tmp_path):
        # attempt 2 recorded the already-broken plan; attempt 3 must reach back to attempt 1
        make_attempt(tmp_path, 1, {"reference_length_m": 0.06, "max_cells": 5_000_000})
        make_attempt(tmp_path, 2, {"reference_length_m": None, "max_cells": None})
        ws3 = make_attempt(tmp_path, 3, None)
        out = _inherit_durable_plan_fields({}, ws3)
        assert out["reference_length_m"] == 0.06
        assert out["max_cells"] == 5_000_000

    def test_the_first_attempt_has_nothing_to_inherit(self, tmp_path):
        ws1 = make_attempt(tmp_path, 1, None)
        assert _inherit_durable_plan_fields({}, ws1) == {}

    def test_a_corrupt_sibling_memory_is_skipped_not_fatal(self, tmp_path):
        make_attempt(tmp_path, 1, {"reference_length_m": 0.06, "max_cells": 5_000_000})
        ws2 = make_attempt(tmp_path, 2, None)
        (tmp_path / "attempt_1" / ".last_plan.json").write_text("{not json")
        assert _inherit_durable_plan_fields({}, ws2) == {}

    def test_a_non_attempt_workspace_is_left_alone(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        assert _inherit_durable_plan_fields({}, ws) == {}


class TestPlannerSeeding:
    def test_a_retry_seeds_its_planner_from_the_newest_sibling_plan(self, tmp_path):
        make_attempt(tmp_path, 1, {"approach": "first try", "max_cells": 4_000_000})
        make_attempt(tmp_path, 2, {"approach": "second try", "max_cells": 5_000_000})
        ws3 = make_attempt(tmp_path, 3, None)
        seeded = _sibling_plan_memory(ws3)
        assert seeded is not None and seeded["approach"] == "second try"

    def test_no_siblings_means_no_seed(self, tmp_path):
        assert _sibling_plan_memory(make_attempt(tmp_path, 1, None)) is None
