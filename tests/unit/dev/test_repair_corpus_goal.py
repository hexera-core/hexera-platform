# Responsibility: Verify the corpus runner scores a goal profile per file, counts tier-2b routing,
#                 and draws a deterministic stratified sample of the mesh-failure-real manifest.
# Boundaries: hermetic - the repair, the inspection and the triage are stubbed; no CAD is read and
#             no gmsh, OCP or fTetWild process is started. The manifest is the committed one
#             (names and hashes only), so the sampler is tested against real strata.
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "devtools" / "validation" / "corpora" / "mesh-failure-real.json"


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_goal_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load()


# A verdict as goal.py records it in measurements["goal"] (Tasks 2-4): the meshability block, the
# score, the grind prediction and - only when the tier-2b flag is live - the routing entry.
def _verdict(*, profile="standalone", meshes=True, kind="", score=1.0, routing=None,
             seconds=3.5, kernel_clean=True):
    verdict = {
        "profile": profile,
        "kernel_clean": kernel_clean,
        "meshability": {"tier": "2", "meshes": meshes, "kind": kind, "stage": "3d",
                        "volume_elements": 500, "over_element_cap": False,
                        "budget_s": 60.0, "seconds": seconds, "skipped": ""},
        "score": score,
        "grind": {"tier": "heavy", "rule": "faces>400"},
        "review": [],
    }
    if routing is not None:
        verdict["routing"] = {"tier": "2b", "verdict": routing, "tets": 1200, "seconds": 1.5,
                              "stopped": False, "budget_s": 30.0, "message": ""}
    return verdict


@pytest.fixture
def kernel(monkeypatch, tmp_path):
    """A stub repair whose report carries whatever verdict the test asks for, plus the kwargs the
    runner handed `repair_step_file`, so "no --goal means no kwarg" is observable."""
    import meshpipeline.cad.repair.conservative as cons
    import meshpipeline.cad.repair.contracts as contracts
    import meshpipeline.cad.repair.triage as triage

    state = SimpleNamespace(seen=[], verdict=None, raise_refused=False)

    def repair(source, destination, **kwargs):
        state.seen.append(dict(kwargs))
        Path(destination).write_text("repaired")
        measurements = [SimpleNamespace(name="operations_attempted", value=0)]
        if state.verdict is not None:
            measurements.append(SimpleNamespace(name="goal", value=state.verdict))
        if state.raise_refused:
            raise cons.RepairRefused("nothing was kept", measurements={
                "goal": state.verdict, "operations": []})
        return SimpleNamespace(operations=[], measurements=measurements)

    def inspect(path):
        return {"report": SimpleNamespace(defects=[], to_dict=lambda: {}),
                "defects": [], "entities": [{"entity": "f1", "code": "small_edge"}],
                "valid": True, "counts": {}, "located": {"by_code": {"small_edge": 1}}}

    monkeypatch.setattr(cons, "repair_step_file", repair)
    monkeypatch.setattr(contracts, "status_from_defects",
                        lambda defects: SimpleNamespace(value="repairable"))
    monkeypatch.setattr(triage, "recommend", lambda **kw: SimpleNamespace(
        route=triage.ROUTE_CONSERVATIVE_REPAIR, confidence=1.0, targets=[], abstain_reason=""))
    monkeypatch.setattr(rc, "_inspect", inspect)
    part = tmp_path / "part.step"
    part.write_text("x")
    state.part = part
    return state


def test_without_goal_rows_are_unchanged(kernel, tmp_path):
    kernel.verdict = _verdict()  # recorded by the stub, but nothing asked for it
    plain = rc._repair_row(kernel.part, tmp_path)
    explicit = rc._repair_row(kernel.part, tmp_path, goal=None)
    assert plain.keys() == explicit.keys()
    assert all("goal" not in kwargs for kwargs in kernel.seen)
    assert not [k for k in plain if k.startswith("goal_") or k.startswith("tier2_")]
    assert "grind_tier" not in plain and "tolerant_route" not in plain


def test_goal_columns_come_from_the_report(kernel, tmp_path):
    kernel.verdict = _verdict(meshes=False, kind="overlapping_facets", score=0.0)
    row = rc._repair_row(kernel.part, tmp_path, goal="standalone")
    assert kernel.seen[-1]["goal"] == "standalone"
    assert row["goal_profile"] == "standalone"
    assert row["kernel_clean"] is True
    assert row["tier2_meshes"] is False
    assert row["tier2_kind"] == "overlapping_facets"
    assert row["tier2_score"] == 0.0
    assert row["grind_tier"] == "heavy"
    assert "tolerant_route" not in row
    assert row["goal_verdict_seconds"] == 3.5


def test_refused_row_reads_the_verdict_from_the_refusal(kernel, tmp_path):
    kernel.verdict = _verdict(profile="benchmark", meshes=None, score=None)
    kernel.raise_refused = True
    row = rc._repair_row(kernel.part, tmp_path, goal="benchmark")
    assert row["outcome"] == "refused"
    assert row["goal_profile"] == "benchmark"
    assert row["tier2_meshes"] is None and row["tier2_score"] is None
    assert row.get("goal_verdict") != "not_run"


def test_unrun_rows_are_not_run_not_failed(kernel, monkeypatch, tmp_path):
    import meshpipeline.cad.repair.contracts as contracts

    monkeypatch.setattr(contracts, "status_from_defects",
                        lambda defects: contracts.RepairStatus.clean)
    monkeypatch.setattr(rc, "_inspect", lambda path: {
        "report": SimpleNamespace(defects=[], to_dict=lambda: {}), "defects": [],
        "entities": [], "valid": True, "counts": {}, "located": {}})
    row = rc._repair_row(kernel.part, tmp_path, goal="standalone")
    assert row["outcome"] == "nothing_to_repair"
    assert row["goal_profile"] == "standalone"
    assert row["goal_verdict"] == "not_run"
    assert row["tier2_meshes"] is None
    assert rc.goal_summary([row])["not_run"] == 1
    assert rc.goal_summary([row])["tier2_no_answer"] == 0


def test_score_mean_ignores_no_answers():
    rows = [{"goal_profile": "benchmark", "tier2_meshes": True, "tier2_score": 1.0},
            {"goal_profile": "benchmark", "tier2_meshes": False, "tier2_score": 0.0},
            {"goal_profile": "benchmark", "tier2_meshes": None, "tier2_score": None}]
    out = rc.goal_summary(rows)
    assert out["files"] == 3 and out["profile"] == "benchmark"
    assert out["score_mean"] == 0.5
    assert out["tier2_meshes"] == 1 and out["tier2_refuses"] == 1
    assert out["tier2_no_answer"] == 1 and out["not_run"] == 0


def test_tolerant_routing_counts_appear_only_when_recorded():
    quiet = [{"goal_profile": "standalone", "tier2_meshes": True, "tier2_score": 1.0}]
    assert "tolerant_routing" not in rc.goal_summary(quiet)
    routed = quiet + [
        {"goal_profile": "standalone", "tier2_meshes": False, "tier2_score": 0.0,
         "tolerant_route": "tolerant_rung_eats_it"},
        {"goal_profile": "standalone", "tier2_meshes": False, "tier2_score": 0.0,
         "tolerant_route": "tolerant_rung_eats_it"},
        {"goal_profile": "standalone", "tier2_meshes": False, "tier2_score": 0.0,
         "tolerant_route": "no_answer"}]
    assert rc.goal_summary(routed)["tolerant_routing"] == {
        "no_answer": 1, "tolerant_rung_eats_it": 2}


def test_goal_summary_is_merged_only_when_a_row_carries_a_profile():
    plain = [{"outcome": "repaired", "repaired_defects": {}}]
    assert "goal" not in rc.corpus_summary(plain)
    scored = plain + [{"outcome": "repaired", "repaired_defects": {},
                       "goal_profile": "standalone", "tier2_meshes": True, "tier2_score": 1.0}]
    assert rc.corpus_summary(scored)["goal"]["files"] == 1


def test_grind_counts_group_by_tier():
    rows = [{"goal_profile": "benchmark", "tier2_meshes": True, "tier2_score": 1.0,
             "grind_tier": t} for t in ("heavy", "heavy", "light")]
    assert rc.goal_summary(rows)["grind_by_tier"] == {"heavy": 2, "light": 1}


# ---- the stratified sampler, over the committed 9,956-entry manifest ------------------------
def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_stratified_sample_is_deterministic_and_exact():
    manifest = _manifest()
    first = rc.stratified_sample(manifest, 300, seed=20261120)
    again = rc.stratified_sample(manifest, 300, seed=20261120)
    other = rc.stratified_sample(manifest, 300, seed=7)
    assert len(first) == 300
    assert [e["sha256"] for e in first] == [e["sha256"] for e in again]
    assert [e["sha256"] for e in first] != [e["sha256"] for e in other]
    assert len({e["sha256"] for e in first}) == 300


def test_stratified_sample_covers_every_stratum():
    manifest = _manifest()
    sample = rc.stratified_sample(manifest, 300, seed=20261120)
    assert {rc.stratum_of(e) for e in sample} == {rc.stratum_of(e) for e in manifest["files"]}


def test_stratified_sample_carries_no_cad():
    manifest = _manifest()
    by_sha = {e["sha256"]: e for e in manifest["files"]}
    for entry in rc.stratified_sample(manifest, 20, seed=1):
        assert entry == by_sha[entry["sha256"]]
        assert set(entry) <= set(by_sha[entry["sha256"]])


def test_shards_partition_the_sample():
    manifest = _manifest()
    sample = rc.stratified_sample(manifest, 300, seed=20261120)
    shards = [rc.shard_of(sample, f"{i}/3") for i in (1, 2, 3)]
    union = [e["sha256"] for shard in shards for e in shard]
    assert sorted(union) == sorted(e["sha256"] for e in sample)
    assert len(set(union)) == len(union)
    assert sum(len(s) for s in shards) == 300


def test_exclude_draws_a_disjoint_sample(tmp_path):
    manifest = _manifest()
    held = rc.stratified_sample(manifest, 300, seed=20261120)
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"track": manifest["track"], "files": [
        {"name": e["name"], "sha256": e["sha256"]} for e in held]}))
    remaining = rc.without_excluded(manifest, rc.excluded_sha256s([path]))
    drawn = rc.stratified_sample(remaining, 500, seed=20261120)
    assert len(drawn) == 500
    assert not {e["sha256"] for e in drawn} & {e["sha256"] for e in held}
