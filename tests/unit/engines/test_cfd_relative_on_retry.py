# Responsibility: Verify CFDREQ-33 as amended in rulebook 113 - a stack that may be written relative is written in
# metres on the first snappyHexMesh attempt and marked; its layer-stack refusal says so, snappyHexMesh is the lever for
# it, and the next attempt (reading the carried gate facts) writes the same plan relative; that one's refusal moves on.
# Boundaries: pure over dictionary text and small workspace files; no mesher is run.
from __future__ import annotations

import json
import re

import pytest
from tests.unit.engines.test_cfd_external_wall import SHM, _ask
from tests.unit.engines.test_cfd_layer_stack_switch import _Ctx, _ws
from tests.unit.engines.test_cfd_relative_stack import ONE_LEVEL

from meshpipeline.contracts.decision_record import RULES_VERSION
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import gate_facts as GF
from meshpipeline.engines import layer_census as LC
from meshpipeline.engines.snappy import flow_gates as FG
from meshpipeline.pipeline import engine_fallback as EF

WC = 0.0004                                                # the stack about 1.7 plain wall cells deep: may be relative


def _num(text: str, key: str) -> float | None:
    m = re.search(rf"\b{key}\s+([-+0-9.eE]+)\s*;", text)
    return float(m.group(1)) if m else None


def _dict(ws, text: str):
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "snappyHexMeshDict").write_text(text)
    return ws


def test_the_rulebook_is_113():
    assert RULES_VERSION == "113"


def test_the_first_attempt_writes_the_stack_in_metres_and_marks_it(tmp_path):
    rel = CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=WC, relative_ok=True)
    first = CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=WC, relative_ok=True,
                           relative_now=False)
    assert "relativeSizes false;" in first and f"{CR.RELATIVE_ON_RETRY_KEY} {WC:g};" in first
    assert LC.PLAIN_CELL_KEY not in first
    # the plain faces get the same heights either way; the count and growth are the same
    assert _num(first, "firstLayerThickness") == pytest.approx(_num(rel, "firstLayerThickness") * WC, rel=1e-5)
    assert _num(first, "nSurfaceLayers") == _num(rel, "nSurfaceLayers")
    assert _num(first, "minVol") > _num(rel, "minVol")         # the relative volume bar only when written relative
    # no reader takes the mark for a height: the stack is read in metres
    _dict(tmp_path, first)
    assert LC.planned_plain_cell_of(tmp_path) is None
    assert LC.planned_first_of(tmp_path) == pytest.approx(_num(first, "firstLayerThickness"))
    assert CR.relative_on_retry(tmp_path)
    _dict(tmp_path, rel)
    assert not CR.relative_on_retry(tmp_path)                  # the relative attempt has no retry left


def test_a_stack_never_written_relative_is_not_marked():
    # 0.42 of the wall cell deep (RELATIVE_MIN_DEPTH): absolute and unmarked, as before
    out = CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=0.0016, relative_ok=True,
                         relative_now=False)
    assert "relativeSizes false;" in out and CR.RELATIVE_ON_RETRY_KEY not in out
    internal = CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=WC, relative_now=False)
    assert CR.RELATIVE_ON_RETRY_KEY not in internal            # the internal case keeps its absolute stack


def test_the_gate_says_the_next_attempt_writes_it_relative(tmp_path):
    _ws(tmp_path)
    _dict(tmp_path, CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=WC, relative_ok=True,
                                   relative_now=False))
    ok, why = FG._gate_layer_stack(_Ctx(tmp_path, {"layer_coverage_pct": 79.5}))
    facts = getattr(why, "facts", {})
    assert not ok and facts.get("relative_next") is True and facts.get("coverage_pct") == 79.5
    assert "each face's own cells" in str(why)
    # the relative attempt refused: no retry on this engine
    _dict(tmp_path, CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=WC, relative_ok=True))
    ok, why = FG._gate_layer_stack(_Ctx(tmp_path, {"layer_coverage_pct": 79.5}))
    assert not ok and "relative_next" not in getattr(why, "facts", {})


def test_snappy_is_the_lever_only_for_the_stack_in_metres():
    yes = {"coverage_pct": 79.5, "layers": 10, "relative_next": True}
    assert EF.lever_bears("layer_stack", "snappy", yes)
    assert not EF.lever_bears("layer_stack", "cfmesh", yes)
    assert not EF.lever_bears("layer_stack", "snappy", {"coverage_pct": 79.5, "layers": 10})
    assert not EF.refusal_without_lever({"executor_success": False, "executor_failed_gate": "layer_stack",
                                         "engine": "snappy", "executor_failure_facts": yes})
    assert EF.refusal_without_lever({"executor_success": False, "executor_failed_gate": "layer_stack",
                                     "engine": "snappy", "executor_failure_facts": {"coverage_pct": 79.5}})


def test_the_next_attempt_reads_the_carried_refusal(tmp_path):
    assert not GF.relative_stack_next(tmp_path)                # nothing carried
    GF.write_gate_facts(tmp_path, "layer_stack", {"coverage_pct": 79.5, "relative_next": True, "first_m": 1e-5})
    assert json.loads((tmp_path / GF.GATE_FACTS_FILE).read_text())["facts"] == {"relative_next": True}
    assert GF.relative_stack_next(tmp_path)
    GF.write_gate_facts(tmp_path, "layer_stack", {"coverage_pct": 41.0})
    assert not GF.relative_stack_next(tmp_path)
    GF.write_gate_facts(tmp_path, "port_areas", {"relative_next": True})
    assert not GF.relative_stack_next(tmp_path)
    # the rendered case on both attempts: tests/unit/engines/test_snappy_external_layer_bridge.py


def test_the_relative_retry_comes_before_the_next_engine_even_on_the_last_regular_attempt():
    from tests.unit.pipeline.test_engine_fallback_ladder import _external
    for retry in (0, 1):
        st = _external(engine="snappy", retry_count=retry, executor_failed_gate="layer_stack",
                       executor_failure_cause="mesh_quality",
                       executor_failure_facts={"coverage_pct": 66.5, "layers": 10, "relative_next": True})
        d = EF.decide(st, record=EF.with_attempt(st, EF.classify(st)))
        assert not d.switch_to and "own cells" in d.why and d.why.startswith("a measured check failed")
        assert not EF.another_engine_takes_over(st)
    # the relative attempt refused: the next engine, as CFDREQ-28 always did
    st = _external(engine="snappy", retry_count=1, executor_failed_gate="layer_stack",
                   executor_failure_cause="mesh_quality", executor_failure_facts={"coverage_pct": 79.5, "layers": 10})
    assert EF.decide(st, record=EF.with_attempt(st, EF.classify(st))).switch_to == "cfmesh"
