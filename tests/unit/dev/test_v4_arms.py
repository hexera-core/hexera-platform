# Responsibility: Prove the four v4 arms are DISPATCHABLE from run_arm's own parser, that the AI arm reaches the real
#                 router with the shard's WaveLedger attached (and is refused without one), and that a file the
#                 checker could not measure is a row that SAYS so rather than one that failed.
# Boundaries: hermetic. Args come from `run_arm.parse_args` (never a hand-built Namespace). Faked neighbours: the
#             WorkerPool (no OCCT), the entry/staged lane (`FakeEngine`, 22a/22b), the Judge's verdicts (REAL
#             `Verdict`/`score` objects over a fake world), the provider client at `providers.client_for`. Real: run_arm,
#             arms.run_file, search.arm_generators, search.search, AIProposer, the ledger, the router.
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.cad.repair_v4 import test_search_wiring as W
from tests.unit.cad.repair_v4.test_model_route import Wire, wire  # noqa: F401  (wire is a fixture)

from meshpipeline.cad.repair_v4 import measurement
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.ops import REGISTRY
from meshpipeline.cad.repair_v4.verdict.assemble import Verdict
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import report  # noqa: E402
import run_arm  # noqa: E402

ev = run_arm.evalspec
SHA = "a" * 64
CLEAN_CANDIDATE = {"candidates": [{"op": "t18_set", "targets": [], "params": {"q": 3}, "rationale": "fix"}]}
INCONCLUSIVE_CANDIDATE = {"candidates": [{"op": "t18_set", "targets": [], "params": {"q": 2, "inc": 100},
                                          "rationale": "x"}]}


# ------------------------------------------------------------------ the fake world

def verdict3(q: int, tier: str, inconclusive: bool = False) -> Verdict:
    panel = [W.Member(f"m{i}", "pass" if i < q else "fail") for i in range(3)]
    return Verdict(ValidReport(True, [], [], "mm", 1, True), panel, FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1),
                   clean=tier == "full" and q >= 3 and not inconclusive, tier=tier, inconclusive=inconclusive)  # type: ignore[arg-type]


class World3(W.World):
    """A 3-member panel; `q` members pass. Sites are of a kind DetGenerator has no sweep for, so the deterministic
    arm proposes nothing here and only the AI can move the file."""

    broken_checker = False

    def __init__(self) -> None:
        super().__init__(target=3, sites_fn=lambda pool, ws, v: [W.site("S1", "no_such_kind", ["F1"])])

    def cheap(self, pool: Any, ws: Any, original: str) -> Verdict:
        self.cheap_calls += 1
        left = self._inc_left.setdefault(ws.brep_path, int(ws.labels["_q"]["inc"]))
        if left > 0:
            self._inc_left[ws.brep_path] = left - 1
            return verdict3(W._q(ws), "cheap", inconclusive=True)
        return verdict3(W._q(ws), "cheap")

    def full(self, pool: Any, ws: Any, original: str) -> Verdict:
        if World3.broken_checker:
            raise CheckerUnavailable("the checker is dead")
        return verdict3(W._q(ws), "full")


class FakeEngine:
    def __init__(self) -> None:
        self.world = World3()
        self.staged_calls = 0

    def open_workspace(self, pool: Any, source: str, workdir: str) -> Any:
        return W.make_ws(0)

    def judge(self, h_mm: float | None) -> Any:
        return self.world.judge()

    def run_staged(self, pool: Any, ws: Any) -> Any:
        self.staged_calls += 1
        return SimpleNamespace(ws=W.make_ws(W._q(ws) + 1))


class FakePool:
    created: list = []

    def __init__(self, n: int, **kw: Any) -> None:
        FakePool.created.append(self)

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        return CallResult(False, None, "fake pool: no kernel", "error", 0.0)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "t18_set", W.T18Op())  # type: ignore[arg-type]
    monkeypatch.setitem(REGISTRY, "orient_and_close", W.FinaliseHelper())  # type: ignore[arg-type]
    monkeypatch.setattr("meshpipeline.cad.repair_v4.worker_pool.WorkerPool", FakePool)
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "sk-test")
    World3.broken_checker = False
    FakePool.created = []


@pytest.fixture
def shard(tmp_path: Path):
    """A pool of one cleared file, its split, and the staged input; returns the argv common to every run."""
    pool_mod = run_arm.pool_mod
    entry = pool_mod.PoolEntry("a.step", "abc_fail", SHA, "u", None, "lic")
    pool_json = tmp_path / "pool.json"
    pool_mod.write_pool([entry], pool_json, sources_included=["abc_fail"])
    split = tmp_path / "dev.json"
    split.write_text(json.dumps({"split": "dev", "files": [
        {"name": "a.step", "sha256": SHA, "source": "abc_fail", "verdict_summary": {}, "first_failing_layer": "panel",
         "family": "x", "group": None, "kind": None, "kind_status": None}]}))
    inputs = tmp_path / "inputs" / SHA
    inputs.mkdir(parents=True)
    (inputs / "a.step").write_text("ISO-10303-21;")
    out = tmp_path / "out"
    out.mkdir()
    return SimpleNamespace(
        out=out, entry=entry,
        argv=["--mode", "arms", "--pool", str(pool_json), "--inputs", str(tmp_path / "inputs"), "--out", str(out),
              "--split", "dev", "--split-file", str(split), "--h-mm", "0.5", "--sources", "abc_fail"])


def ledgered(cap: float = 100.0) -> WaveLedger:
    return WaveLedger.for_test(cap_usd=cap)


def go(shard: Any, arms: str, ledger: WaveLedger | None, engine: Any = None, *, extra: list[str] = ()) -> tuple[list, list]:
    # a model arm's argv carries the ledger flags (the parser refuses it otherwise); run_arms is handed the object
    flags = ["--wave-type", "normal", "--wave-id", "w1", "--n-shards", "1", "--ledger-path", "/x/log",
             "--marker-bucket", "b"] if "search_ai" in arms else []
    args = run_arm.parse_args([*shard.argv, "--arms", arms, *flags, *extra])
    rc = run_arm.run_arms(args, shard.out, {}, ledger, engine=engine or FakeEngine())
    assert rc == 0
    rows = json.loads((shard.out / "rows.json").read_text())
    return rows, json.loads((shard.out / "EXCLUDED.json").read_text())


# ------------------------------------------------------------------ the arms are selectable

@pytest.mark.parametrize("arm", ["input_only", "staged", "search_det", "search_ai"])
def test_each_arm_is_selectable_through_the_real_parser(arm: str) -> None:
    ledger_flags = ["--wave-type", "normal", "--wave-id", "w1", "--n-shards", "1", "--ledger-path", "/x",
                    "--marker-bucket", "b"]
    args = run_arm.parse_args(["--arm", arm, "--split", "dev", "--h-mm", "0.5", *ledger_flags])
    assert args.mode == "arms" and args.arms_resolved == (arm,)


def test_a_comma_list_and_an_alias_resolve_to_canonical_arms() -> None:
    args = run_arm.parse_args(["--arms", "heal_det,input_only", "--split", "dev"])
    assert args.arms_resolved == ("staged", "input_only")


@pytest.mark.parametrize("argv, why", [
    (["--arm", "nope", "--split", "dev"], "unknown arm"),
    (["--arm", "v3_meshability_pass_ai", "--split", "dev"], "cannot run here"),
    (["--arm", "input_only", "--arms", "staged", "--split", "dev"], "not both"),
    (["--arm", "input_only"], "needs --split"),
    (["--arm", "search_det", "--split", "dev"], "--h-mm"),
    (["--arm", "search_ai", "--split", "dev", "--h-mm", "0.5"], "ledger"),
    (["--arm", "input_only", "--split", "heldout"], "held-out"),
    (["--wave", "dev_search", "--split", "clean", "--h-mm", "1"], "disagrees"),
    (["--mode", "reprobe", "--arm", "staged"], "--mode arms"),
])
def test_a_contradictory_selection_is_refused(argv: list[str], why: str, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit):
        run_arm.parse_args(argv)
    assert why in capsys.readouterr().err


def test_no_arm_flag_keeps_the_reprobe_default() -> None:
    assert run_arm.parse_args([]).mode == "reprobe"


@pytest.mark.parametrize("wave_name", sorted(ev.WAVES))
def test_every_waves_shard_argv_is_accepted_by_run_arms_own_parser(wave_name: str) -> None:
    """`evalspec.WavePlan.shard_argv` used to emit flags the parser rejected and had no caller."""
    markers = SimpleNamespace(get=lambda k: None, create=lambda k, v: True)
    plan = ev.plan_wave(ev.WAVES[wave_name], wave_id="wave-1", n_shards=4, markers=markers)
    argv = plan.shard_argv(2, ledger_path="/eval/ledger/log.jsonl", marker_bucket="b", h_mm=0.5)
    args = run_arm.parse_args(argv)
    assert args.arms_resolved == tuple(a for a in ev.WAVES[wave_name].arms if a in ev.DISPATCHABLE_ARMS)
    assert (args.split, args.wave_type, args.shard) == (ev.WAVES[wave_name].split, ev.WAVES[wave_name].wave_type, "2/4")


def test_shard_argv_of_a_model_wave_cannot_be_written_without_its_ledger() -> None:
    markers = SimpleNamespace(get=lambda k: None, create=lambda k, v: True)
    plan = ev.plan_wave(ev.WAVES["dev_search"], wave_id="wave-1", n_shards=4, markers=markers)
    with pytest.raises(ValueError, match="ledger_path"):
        plan.shard_argv(1)


# ------------------------------------------------------------------ the arms run

def test_input_only_staged_and_search_det_write_rows_the_report_accepts(shard: Any) -> None:
    engine = FakeEngine()
    rows, excluded = go(shard, "input_only,staged,search_det", None, engine)
    assert excluded == [] and [r["arm"] for r in rows] == ["input_only", "staged", "search_det"]
    by = {r["arm"]: r for r in rows}
    assert by["input_only"]["out_panel_passes"] == by["input_only"]["in_panel_passes"] == 0
    assert by["staged"]["out_panel_passes"] == 1 and engine.staged_calls == 2  # the staged arm, and search_det's start
    assert by["search_det"]["reason"] == "exhausted" and by["search_det"]["verdict_candidates"] == 0
    assert all(r["model_usd"] == 0.0 for r in rows)
    for r in rows:                                  # the report's own loader: the rows are a real ArmRow contract
        assert report._row_from_dict(r).inconclusive is False


def test_a_search_arm_starts_from_the_staged_healers_output_as_the_engine_does(shard: Any) -> None:
    """Spec 5.7 and the seam contract (04, call order): the search's start state is `run_staged`'s output. An eval arm
    that searched the raw input would measure a pipeline the engine never ships."""
    engine = FakeEngine()
    [row], excluded = go(shard, "search_det", None, engine)
    assert excluded == [] and engine.staged_calls == 1
    # DetGenerator has no sweep for this world's site kind, so the result IS the start: the staged state (q=1)
    assert row["in_panel_passes"] == 0 and row["out_panel_passes"] == 1


def test_a_search_arm_keeps_its_whole_trace_beside_its_work_states(shard: Any) -> None:
    """The row keeps a summary (the first evaluated deltas, the top reasons); triage needs the whole search, so every
    search arm writes its trace as `<workdir>/search-trace.json` for each file, and a non-search arm writes none."""
    rows, excluded = go(shard, "input_only,search_det", None, FakeEngine())
    assert excluded == [] and len(rows) == 2
    traces = sorted(shard.out.rglob("search-trace.json"))
    assert [t.parent.name for t in traces] == ["search_det"]
    events = json.loads(traces[0].read_text())["events"]
    assert any(e["kind"] == "stop" for e in events)


def test_every_arm_records_which_state_it_delivered_and_keeps_its_brep(shard: Any, tmp_path: Path) -> None:
    """A REPAIRED output cannot be measured after the wave unless its BREP is kept: each (file, arm) writes
    `<workdir>/delivered.json` (which state shipped, whether it is the input) and copies a non-input output to
    `<workdir>/delivered.brep`. A failed copy is recorded, never a lost row."""
    rows, excluded = go(shard, "input_only,staged,search_det", None, FakeEngine())
    assert excluded == [] and len(rows) == 3
    recs = {p.parent.name: json.loads(p.read_text()) for p in shard.out.rglob("delivered.json")}
    assert set(recs) == {"input_only", "staged", "search_det"}
    assert recs["input_only"]["is_input"] is True and recs["input_only"]["kept"] is None  # the input is input.brep
    for arm in ("staged", "search_det"):  # the fake world ships the staged state (q=1), not the input
        assert recs[arm]["is_input"] is False and recs[arm]["source_brep"]
        assert recs[arm]["kept"] is False and "error" in recs[arm]  # the fake's path is not a file: recorded, not raised
    real = tmp_path / "s.brep"
    real.write_text("brep")
    dst = tmp_path / "w"
    run_arm.arms_mod.keep_delivered(str(real), str(dst), is_input=False)
    assert (dst / "delivered.brep").read_text() == "brep" and json.loads((dst / "delivered.json").read_text())["kept"]


def test_a_search_row_carries_the_cheap_full_table_of_the_states_it_confirmed(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    [row], _ = go(shard, "search_ai", ledgered())
    cf = row["cheap_full"]
    assert set(cf) == set(report.CHEAP_FULL_CELLS) and cf["cheap_pass_full_pass"] >= 1
    assert report._row_from_dict(row).cheap_full == cf
    [det], _ = go(shard, "search_det", None)
    assert det["cheap_full"] == dict.fromkeys(report.CHEAP_FULL_CELLS, 0)  # nothing confirmed: zeros, not absent


def test_every_conclusive_row_carries_a_triage_block_from_its_output_verdict(shard: Any) -> None:
    rows, _ = go(shard, "input_only,staged,search_det", None)
    by = {r["arm"]: r for r in rows}
    for r in rows:
        t = r["triage"]
        assert t["valid"] == {"ok": True, "hard": []}
        assert t["deviation"]["ok"] is True and t["deviation"]["used"] == 0.0
    assert [m["name"] for m in by["input_only"]["triage"]["panel_failing"]] == ["m0", "m1", "m2"]
    assert [m["name"] for m in by["staged"]["triage"]["panel_failing"]] == ["m1", "m2"]
    assert "search" not in by["staged"]["triage"]
    s = by["search_det"]["triage"]["search"]
    assert s["stop"]["reason"] == "exhausted" and s["open_sites"] == {"no_such_kind": 1}
    assert s["ops"] == {} and s["ai"] is None   # DetGenerator has no sweep here: nothing proposed, no proposer


def test_a_search_ai_row_triages_its_ops_and_its_model_use(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    [row], _ = go(shard, "search_ai", ledgered())
    s = row["triage"]["search"]
    assert s["stop"]["reason"] == "clean" and s["open_sites_at"] in ("best", "last_expanded")
    op = s["ops"]["t18_set"]
    assert op["ai"] >= 1 and op["evaluated"] >= 1 and op["det"] == 0
    ai = s["ai"]
    assert ai["turns"] >= 1 and ai["spent_usd"] == pytest.approx(row["model_usd"]) and ai["tool_errors"] == 0
    assert row["triage"]["panel_failing"] == []


def test_an_unmeasured_search_row_still_says_why_it_stopped(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = INCONCLUSIVE_CANDIDATE
    [row], _ = go(shard, "search_ai", ledgered())
    assert row["inconclusive"] is True and row["triage"]["search"]["stop"]["reason"] == "no_verdict"
    assert row["triage"]["search"]["ops"]["t18_set"]["inconclusive"] >= 1


def test_search_triage_counts_each_fate_and_keeps_the_top_reasons() -> None:
    from meshpipeline.cad.repair_v4.trace import Trace

    t = Trace()
    t.record("expand", state="s0", sites=[{"kind": "open"}, {"kind": "open"}, {"kind": "proximity"}])
    for outcome, extra in (("evaluated", {"ops_applied": 1}), ("duplicate", {}), ("rejected", {"stage": "declined", "errors": ["fill_too_large: span 9 mm"]}),
                           ("rejected", {"stage": "failed", "errors": ["timeout: worker killed"]}),
                           ("rejected", {"stage": "validate", "errors": ["F9 removed"]}), ("skipped_budget", {})):
        t.record("candidate", state="s0", outcome=outcome, op="fill_loop", source="det", params={}, **extra)
    t.record("candidate", state="s0", outcome="evaluated", op="run_stage", source="ai", params={"stage": "shells"},
             ops_applied=2)
    t.record("stop", reason="ops", ended_by="ops", best="s0", spent={"ops": 6})
    gen = SimpleNamespace(skipped={"no_solid": 2})
    res = SimpleNamespace(trace=t, ai=[], ai_capped=False, ai_stop_reason=None)
    s = run_arm.arms_mod.search_triage(res, [gen])
    assert s["stop"] == {"reason": "ops", "ended_by": "ops", "spent": {"ops": 6}, "reprompts": 0}
    assert s["open_sites"] == {"open": 2, "proximity": 1} and s["open_sites_at"] == "best"
    f = s["ops"]["fill_loop"]
    assert (f["det"], f["evaluated"], f["duplicate"], f["skipped_budget"]) == (6, 1, 1, 1)
    assert f["declined"] == {"fill_too_large: span 9 mm": 1} and f["failed"] == {"timeout: worker killed": 1}
    assert f["invalid"] == {"F9 removed": 1}
    assert s["ops"]["run_stage:shells"]["ai"] == 1
    # one candidate, but the chain it ran cost two ops of budget (Task 53); a plain op costs one each
    assert s["ops"]["run_stage:shells"]["ops_applied"] == 2 and f["ops_applied"] == 1
    assert s["not_proposed"] == {"no_solid": 2} and s["ai"] is None
    assert s["deltas"] == [{"op": "fill_loop", "source": "det", "parent": None, "score": None},
                           {"op": "run_stage", "source": "ai", "parent": None, "score": None}]


def test_search_triage_counts_sites_by_how_well_they_are_placed() -> None:
    from meshpipeline.cad.repair_v4.trace import Trace

    t = Trace()
    t.record("expand", state="s0", sites=[{"kind": "proximity", "entities": ["F1"], "coarse": False},
                                          {"kind": "invalid", "entities": ["B1"], "coarse": True},
                                          {"kind": "unlocated", "entities": [], "coarse": False},
                                          {"kind": "open", "entities": []}])
    t.record("stop", reason="exhausted", best="s0")
    s = run_arm.arms_mod.search_triage(SimpleNamespace(trace=t, ai=[], ai_capped=False, ai_stop_reason=None), [])
    assert s["sites_by_placement"] == {"located": 1, "coarse": 1, "unlocated": 2}


def test_search_triage_records_each_evaluated_candidates_verdict_delta() -> None:
    from meshpipeline.cad.repair_v4.trace import Trace

    t = Trace()
    t.record("candidate", state="s0", outcome="evaluated", op="sew", source="det", params={},
             score=(0, 2, 0.1, 0.0, 0), parent_score=(0, 1, 0.0, 0.0, 0))
    for i in range(12):
        t.record("candidate", state="s0", outcome="evaluated", op="split_angle", source="det", params={},
                 score=(0, 1, 0.0, 0.0, -i), parent_score=(0, 1, 0.0, 0.0, 0))
    t.record("stop", reason="exhausted", best="s0")
    s = run_arm.arms_mod.search_triage(SimpleNamespace(trace=t, ai=[], ai_capped=False, ai_stop_reason=None), [])
    assert s["deltas"][0] == {"op": "sew", "source": "det", "parent": [0, 1, 0.0, 0.0, 0], "score": [0, 2, 0.1, 0.0, 0]}
    assert len(s["deltas"]) == run_arm.arms_mod.TRIAGE_MAX_DELTAS and s["deltas_total"] == 13


def test_cheap_full_cells_reads_the_confirm_events() -> None:
    # a cheap PASS is every cheap member passing: 2 of 2 since lane B8 added gmsh_prod to the cheap tier
    cells = run_arm.arms_mod.cheap_full_cells([
        {"cheap_score": [1, 2, 0.3, 0.0, 0], "clean": True}, {"cheap_score": [1, 2, 0.3, 0.0, 0], "clean": False},
        {"cheap_score": [0, 2, 0.3, 0.0, 0], "clean": True}, {"cheap_score": [1, 1, 0.0, 0.0, 0], "clean": False},
        {"cheap_score": None, "clean": True}])
    assert cells == {"cheap_pass_full_pass": 1, "cheap_pass_full_fail": 1, "cheap_fail_full_pass": 1,
                     "cheap_fail_full_fail": 1}


def test_search_det_builds_no_proposer_and_so_never_reaches_a_model(shard: Any, wire: Wire) -> None:  # noqa: F811
    go(shard, "search_det", None)
    assert wire.requests == []


def test_a_dead_checker_is_an_excluded_file_never_a_row(shard: Any) -> None:
    World3.broken_checker = True
    rows, excluded = go(shard, "input_only,staged", None)
    assert rows == [] and [e["arm"] for e in excluded] == ["input_only", "staged"]
    assert all(e["reason"].startswith("checker_unavailable") for e in excluded)


def test_a_missing_input_is_excluded_not_failed(shard: Any) -> None:
    (Path(shard.argv[shard.argv.index("--inputs") + 1]) / SHA / "a.step").unlink()
    rows, excluded = go(shard, "input_only", None)
    assert rows == [] and excluded[0]["reason"].startswith("input_missing")


# ------------------------------------------------------------------ search_ai: the router and the ledger

def test_search_ai_reaches_the_real_router_with_the_ledger_attached_and_fixes_the_file(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    ledger = ledgered()
    admitted: list[float] = []
    real_admit = ledger.admit

    def spy(file_id: str, estimate_usd: float, **kw: Any) -> Any:
        admitted.append(estimate_usd)
        return real_admit(file_id, estimate_usd, **kw)

    ledger.admit = spy  # type: ignore[method-assign]
    rows, excluded = go(shard, "search_ai", ledger)
    [row] = rows
    proposals = [r for r in wire.requests if "propose" in [t["name"] for t in r.get("tools", [])]]
    assert excluded == [] and row["out_clean"] is True and row["reason"] == "clean"
    # EVERY request on the wire - the shard's route probe included (Task 49's sixth gap) - was admitted by THE ledger
    assert len(proposals) >= 1 and len(admitted) == len(wire.requests), "every model request was admitted by THE ledger"
    assert row["model_usd"] > 0 and row["model_usd"] == pytest.approx(ledger.file_spent_usd(SHA))
    from meshpipeline.cad.repair_v4.model_route import ROUTE_PROBE_FILE_ID
    probe = ledger.file_spent_usd(ROUTE_PROBE_FILE_ID)
    assert probe > 0 and ledger.spent_usd() == pytest.approx(row["model_usd"] + probe)
    assert report._row_from_dict(row).clean


def test_search_ai_with_no_ledger_is_refused_before_anything_starts(shard: Any, wire: Wire) -> None:  # noqa: F811
    args = run_arm.parse_args([*shard.argv, "--arms", "search_ai", "--wave-type", "normal", "--wave-id", "w1",
                               "--n-shards", "1", "--ledger-path", "/x", "--marker-bucket", "b"])
    with pytest.raises(run_arm.arms_mod.ModelArmWithoutLedger):
        run_arm.run_arms(args, shard.out, {}, None, engine=FakeEngine())
    assert wire.requests == [] and FakePool.created == []


def test_run_file_itself_refuses_a_model_arm_without_a_ledger() -> None:
    entry = SimpleNamespace(name="a", sha256=SHA, source="abc_fail")
    with pytest.raises(run_arm.arms_mod.ModelArmWithoutLedger):
        run_arm.arms_mod.run_file("search_ai", entry, "/x", pool=None, engine=FakeEngine(), ledger=None,
                                  arm=ev.ARMS["search_ai"], budget=ev.ARMS["search_ai"].budget, h_mm=0.5,
                                  route="openai:gpt-6-astra", max_turns=3, job_id="j", workdir="/w")


def test_room_left_after_the_route_probe_buys_a_reduced_round_within_the_cap(shard: Any, wire: Wire) -> None:  # noqa: F811
    """Room for the shard's route probe and NOT for a full-allowance round (~$0.23): before D12 no round was sent and
    the file got zero AI turns. The ~$0.22 left now buys one round with the output allowance it can pay for - sent
    with that smaller max_output_tokens - and the shard still never spends past its cap. A slice too small for even
    MIN_OUTPUT_TOKENS stops `no_affordable_turn` (tests/unit/cad/repair_v4/test_model_route.py)."""
    wire.candidate = CLEAN_CANDIDATE
    ledger = ledgered(cap=0.235)                  # the probe reserves ~$0.225 and settles $0.015; a full round ~$0.23
    rows, _ = go(shard, "search_ai", ledger)
    proposals = [r for r in wire.requests if "propose" in [t["name"] for t in r.get("tools", [])]]
    assert proposals and all(r["max_output_tokens"] < 4096 for r in proposals)
    assert ledger.spent_usd() + ledger.reserved_usd() <= 0.235
    assert rows and [e["kind"] for e in ledger.entries()][0] == "route_probe"


def test_a_ledger_with_no_room_for_the_route_probe_refuses_the_shard_before_anything_is_sent(
        shard: Any, wire: Wire) -> None:  # noqa: F811
    from meshpipeline.cad.repair_v4.proposer import NoUsableRoute

    args = run_arm.parse_args([*shard.argv, "--arms", "search_ai", "--wave-type", "normal", "--wave-id", "w1",
                               "--n-shards", "1", "--ledger-path", "/x", "--marker-bucket", "b"])
    with pytest.raises(NoUsableRoute, match="ledger"):
        run_arm.run_arms(args, shard.out, {}, ledgered(cap=0.001), engine=FakeEngine())
    assert wire.requests == []


def test_search_ai_routes_through_the_probed_route_and_records_it(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    wire.fail_models = {"gpt-6-astra"}                       # astra fails the probe: the chain falls to terra
    prov: dict = {}
    args = run_arm.parse_args([*shard.argv, "--arms", "search_ai", "--wave-type", "normal", "--wave-id", "w1",
                               "--n-shards", "1", "--ledger-path", "/x", "--marker-bucket", "b"])
    run_arm.run_arms(args, shard.out, prov, ledgered(), engine=FakeEngine())
    assert prov["route"] == "openai:gpt-5.6-terra" and prov["ledger_attached"] is True
    assert prov["route_probe"]["chosen"] == "openai:gpt-5.6-terra"


# ------------------------------------------------------------------ "was this file measured?"

def test_a_search_that_never_got_a_verdict_is_a_row_that_says_so(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = INCONCLUSIVE_CANDIDATE                  # every child's checker comes back inconclusive
    rows, excluded = go(shard, "search_ai", ledgered())
    [row] = rows
    assert excluded == []
    assert row["inconclusive"] is True and row["reason"] == "no_verdict" and row["infrastructure_failed"] is True
    assert row["verdict_candidates"] == 0 and row["inconclusive_candidates"] >= 1
    loaded = report._row_from_dict(row)
    assert loaded.derived_inconclusive and not loaded.counts            # excluded from the scoreboard


def test_the_report_and_the_search_share_one_definition_of_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    row = report.ArmRow("a", SHA, "abc_fail", "search_det", True, 0.0, 1.0, reason="no_verdict",
                        verdict_candidates=0, inconclusive_candidates=1, infrastructure_failed=True)
    assert row.derived_inconclusive is True
    monkeypatch.setattr(measurement, "file_unmeasured", lambda **kw: False)
    assert row.derived_inconclusive is False, "report.py carries its own copy of the rule instead of importing it"


def test_searchresult_and_a_row_agree_on_unmeasured() -> None:
    from meshpipeline.cad.repair_v4.search import SearchResult
    res = SearchResult(best=None, best_verdict=None, clean=False, trace=None,  # type: ignore[arg-type]
                       reason="no_verdict", inconclusive_candidates=2, verdict_candidates=0, infrastructure_failed=True)
    ok = SearchResult(best=None, best_verdict=None, clean=False, trace=None,  # type: ignore[arg-type]
                      reason="patience", inconclusive_candidates=1, verdict_candidates=3)
    assert res.unmeasured is True and ok.unmeasured is False


# ------------------------------------------------------------------ main(): the ledger main builds is the one passed

def _main_argv(shard: Any, arms: str) -> list[str]:
    return [*shard.argv, "--arms", arms, "--wave-type", "normal", "--wave-id", "w1", "--n-shards", "1",
            "--ledger-path", "/x/log", "--marker-bucket", "b", "--cache-uri", "gs://b/eval/w/verdict-cache"]


def test_main_hands_the_ledger_it_builds_to_the_arms(shard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    built = ledgered()
    monkeypatch.setattr(run_arm, "make_marker_store", lambda bucket: (object(), "gcs/b"))
    monkeypatch.setattr(run_arm, "check_marker_binding", lambda *a: None)
    monkeypatch.setattr(run_arm, "build_ledger", lambda args, markers: SimpleNamespace(
        cap_usd=1.0, provenance="shard", _real=built))
    seen: dict = {}

    def spy(args: Any, out: Any, prov: Any, ledger: Any = None, **kw: Any) -> int:
        seen["ledger"] = ledger
        return 0

    monkeypatch.setattr(run_arm, "run_arms", spy)
    assert run_arm.main(_main_argv(shard, "search_ai")) == 0
    assert seen["ledger"] is not None and seen["ledger"]._real is built, "main built a ledger and discarded it"


def test_main_refuses_a_model_arm_with_no_key_and_spends_nothing(shard: Any, monkeypatch: pytest.MonkeyPatch,
                                                                 wire: Wire) -> None:  # noqa: F811
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "")
    monkeypatch.setattr(run_arm, "make_marker_store", lambda bucket: (object(), "gcs/b"))
    monkeypatch.setattr(run_arm, "check_marker_binding", lambda *a: None)
    monkeypatch.setattr(run_arm, "build_ledger", lambda args, markers: ledgered())
    assert run_arm.main(_main_argv(shard, "search_ai")) == 6
    prov = json.loads((shard.out / "PROVENANCE.json").read_text())
    assert "OPENAI_API_KEY" in prov["error"] and wire.requests == [] and FakePool.created == []


def test_reprobe_mode_is_unchanged_and_attaches_no_ledger(shard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(run_arm, "run_reprobe", lambda args, out, prov: seen.setdefault("mode", args.mode) and 0)
    assert run_arm.main(["--out", str(shard.out), "--cache-uri", "gs://b/p"]) == 0 and seen["mode"] == "reprobe"


# ------------------------------------------------------------------ one shared cache per wave, reaching the engine's judges

@pytest.mark.parametrize("reprobe", [False, True], ids=["arms", "reprobe"])
@pytest.mark.parametrize("cache", [[], ["--cache-uri", ""], ["--cache-uri", "/tmp/per-vm"]], ids=["none", "blank", "dir"])
def test_a_shard_without_a_shared_cache_refuses_before_any_work(shard, monkeypatch, reprobe, cache) -> None:
    monkeypatch.setattr(run_arm, "run_arms", lambda *a, **k: pytest.fail("ran without a shared cache"))
    monkeypatch.setattr(run_arm, "run_reprobe", lambda *a, **k: pytest.fail("ran without a shared cache"))
    argv = ["--mode", "reprobe", "--out", str(shard.out), *cache] if reprobe else [*shard.argv, "--arms", "input_only", *cache]
    assert run_arm.main(argv) == 7
    assert "--cache-uri" in json.loads((shard.out / "PROVENANCE.json").read_text())["error"]


def test_the_wave_cache_reaches_the_engines_own_judges_and_is_restored(shard, monkeypatch) -> None:
    """`default_judge` and `stages` take no cache flag: they read `default_verdict_cache()`. Whatever
    `--cache-uri` the shard got must be that cache, and the SAME object the re-probe path holds."""
    from meshpipeline.cad.repair_v4.verdict import cache as cache_mod

    cache_mod.reset_default_verdict_cache()
    monkeypatch.delenv(cache_mod.CACHE_URI_ENV, raising=False)
    seen: dict = {}

    def fake_run_arms(args, out, prov, ledger=None, **k):
        seen["judge_cache"] = cache_mod.default_verdict_cache()
        seen["reprobe_cache"] = run_arm.make_cache(args.cache_uri)
        return 0

    monkeypatch.setattr(run_arm, "run_arms", fake_run_arms)
    uri = "gs://dev-crsv3-x/eval/w/verdict-cache"
    assert run_arm.main([*shard.argv, "--arms", "input_only", "--cache-uri", uri]) == 0
    assert seen["judge_cache"] is seen["reprobe_cache"]
    assert isinstance(seen["judge_cache"].store, cache_mod.GcsStore)
    assert cache_mod.CACHE_URI_ENV not in __import__("os").environ  # restored: no leak into the next caller
    cache_mod.reset_default_verdict_cache()


# ------------------------------------------------------------------ the model goes away mid-wave

def test_a_search_ai_file_whose_model_is_unreachable_is_inconclusive_not_a_failed_repair(shard: Any, wire: Wire) -> None:  # noqa: F811
    """The probe passes, then every proposer round fails (the router's retries included). The file ran
    deterministic-only, so its outcome measures search_det, not the AI arm: the row must be INCONCLUSIVE with the
    reason named - never a conclusive failure charged to the AI, and never a crash."""
    real_client = wire.client

    def client(target: Any) -> Any:
        inner = real_client(target)

        async def create(**kw: Any) -> Any:
            if "propose" in [t["name"] for t in kw.get("tools", [])]:
                wire.requests.append(kw)
                raise ValueError("the provider is gone")        # terminal: the router returns a failure marker
            return await inner.responses.create(**kw)
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    import meshpipeline.adapters.model_inference.providers as providers_mod
    providers_mod.client_for = client  # restored by the wire fixture's monkeypatch on teardown
    ledger = ledgered()
    rows, excluded = go(shard, "search_ai", ledger)
    [row] = rows
    assert excluded == []
    assert row["inconclusive"] is True and row["ai_stop_reason"] == "model_unavailable", (row, len(wire.requests))
    assert row["model_usd"] == 0.0 and ledger.reserved_usd() == 0.0
    assert [e for e in ledger.entries() if e["file_id"] == SHA] == []        # only the shard's route probe was paid
    parsed = report._row_from_dict(row)
    assert parsed.inconclusive and not parsed.counts and parsed.ai_stop_reason == "model_unavailable"


def test_search_det_rows_carry_no_ai_stop_reason(shard: Any) -> None:
    rows, _ = go(shard, "search_det", None)
    assert "ai_stop_reason" not in rows[0]


def test_run_arm_slices_the_wave_throttle_per_shard(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    prov: dict = {}
    args = run_arm.parse_args([*shard.argv, "--arms", "search_ai", "--wave-type", "normal", "--wave-id", "w1",
                               "--n-shards", "1", "--ledger-path", "/x", "--marker-bucket", "b",
                               "--model-max-in-flight", "4"])
    run_arm.run_arms(args, shard.out, prov, ledgered(), engine=FakeEngine())
    assert prov["model_throttle"]["max_in_flight"] == 4 and prov["model_throttle"]["n_shards"] == 1
# ------------------------------------------------------------------ Task 49: the operator's tighter limits

def test_ops_and_usd_flags_lower_the_arm_budget_and_never_raise_it() -> None:
    base = ["--arms", "search_ai", "--split", "dev", "--h-mm", "auto", "--wave-type", "normal", "--wave-id", "w1",
            "--n-shards", "1", "--ledger-path", "/x", "--marker-bucket", "b"]
    args = run_arm.parse_args([*base, "--ops", "24", "--file-usd-cap", "0.5", "--model-usd-cap", "5"])
    ai = run_arm.effective_arm(ev.ARMS["search_ai"], args)
    det = run_arm.effective_arm(ev.ARMS["search_det"], args)
    assert (ai.budget.ops, ai.budget.model_usd) == (24, 0.5)
    assert (det.budget.ops, det.budget.model_usd) == (24, 0.0)          # a non-model arm never gains a ceiling
    assert args.h_mm == run_arm.arms_mod.H_MM_AUTO
    higher = run_arm.parse_args([*base, "--file-usd-cap", "5"])
    assert run_arm.effective_arm(ev.ARMS["search_ai"], higher).budget.model_usd == 1.0   # min(), never raises
    plain = run_arm.parse_args(base)
    assert run_arm.effective_arm(ev.ARMS["search_ai"], plain) is ev.ARMS["search_ai"]


@pytest.mark.parametrize("flag,value", [("--ops", "0"), ("--model-usd-cap", "0"), ("--model-usd-cap", "nan"),
                                        ("--file-usd-cap", "-1"), ("--file-usd-cap", "inf")])
def test_a_nonsense_limit_is_refused_at_the_command_line(flag: str, value: str) -> None:
    with pytest.raises(SystemExit):
        run_arm.parse_args(["--arms", "input_only", "--split", "dev", flag, value])


# ------------------------------------------------------------------ recipe memory (CAD_REPAIR_V4_RECIPES)

def _recipes_on(monkeypatch: pytest.MonkeyPatch, where: Path) -> None:
    monkeypatch.setenv("CAD_REPAIR_V4_RECIPES", "true")
    monkeypatch.setenv("CAD_REPAIR_V4_RECIPES_URI", str(where))


def _recipe_objects(where: Path) -> list[Path]:
    return sorted(where.rglob("*.json"))


def test_recipes_off_by_default_change_nothing(shard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAD_REPAIR_V4_RECIPES", raising=False)
    prov: dict = {}
    args = run_arm.parse_args([*shard.argv, "--arms", "search_det"])
    run_arm.run_arms(args, shard.out, prov, None, engine=FakeEngine())
    assert prov["recipes"] == {"enabled": False}


def test_a_clean_dev_file_teaches_the_det_arm_a_fix_it_could_not_find(shard: Any, wire: Wire, tmp_path: Path,  # noqa: F811
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The A/B in miniature. The fake part's site is of a kind the deterministic sweep has no row for, so search_det
    alone never fixes it. search_ai fixes it on the dev split, which writes a recipe; a LATER shard's search_det,
    reading that recipe, replays the op and ends clean."""
    store = tmp_path / "recipes"
    _recipes_on(monkeypatch, store)
    rows, _ = go(shard, "search_det", None)
    assert rows[0]["out_clean"] is False and _recipe_objects(store) == []          # nothing to learn from

    wire.candidate = CLEAN_CANDIDATE
    rows, _ = go(shard, "search_ai", ledgered())
    assert rows[0]["out_clean"] is True
    [obj] = _recipe_objects(store)
    body = json.loads(obj.read_text())
    assert body["witness"] == {"file_sha256": SHA, "split": "dev", "wave_id": "w1"}
    assert [s["op"] for s in body["steps"]] == ["t18_set"] and "a.step" not in obj.read_text()

    prov: dict = {}
    args = run_arm.parse_args([*shard.argv, "--arms", "search_det"])
    run_arm.run_arms(args, shard.out, prov, None, engine=FakeEngine())
    [row] = json.loads((shard.out / "rows.json").read_text())
    assert row["out_clean"] is True, "search_det with the recipe should replay the fix"
    assert prov["recipes"]["recipes"] == 1 and prov["recipes"]["writes"] is True


@pytest.mark.parametrize("split,wave_type", [("heldout", "held_out"), ("clean", "normal")])
def test_held_out_and_clean_control_files_never_write_a_recipe(shard: Any, wire: Wire, tmp_path: Path,  # noqa: F811
                                                               monkeypatch: pytest.MonkeyPatch, split: str,
                                                               wave_type: str) -> None:
    store = tmp_path / "recipes"
    _recipes_on(monkeypatch, store)
    split_file = tmp_path / f"{split}.json"
    split_file.write_text(json.dumps({"split": split, "files": [
        {"name": "a.step", "sha256": SHA, "source": "abc_fail", "verdict_summary": {}, "first_failing_layer": "panel",
         "family": "x", "group": None, "kind": None, "kind_status": None}]}))
    argv = list(shard.argv)
    argv[argv.index("--split") + 1] = split
    argv[argv.index("--split-file") + 1] = str(split_file)
    wire.candidate = CLEAN_CANDIDATE
    prov: dict = {}
    args = run_arm.parse_args([*argv, "--arms", "search_ai", "--wave-type", wave_type, "--wave-id", "w1",
                               "--n-shards", "1", "--ledger-path", "/x/log", "--marker-bucket", "b"])
    run_arm.run_arms(args, shard.out, prov, ledgered(), engine=FakeEngine())
    [row] = json.loads((shard.out / "rows.json").read_text())
    assert row["out_clean"] is True                       # the file DID come out clean...
    assert _recipe_objects(store) == []                   # ...and still taught the memory nothing
    assert prov["recipes"]["writes"] is False and prov["recipes"]["written"] == 0


def test_recipes_on_without_a_location_is_refused(shard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAD_REPAIR_V4_RECIPES", "true")
    monkeypatch.delenv("CAD_REPAIR_V4_RECIPES_URI", raising=False)
    args = run_arm.parse_args([*shard.argv, "--arms", "search_det"])
    with pytest.raises(ValueError, match="CAD_REPAIR_V4_RECIPES_URI"):
        run_arm.run_arms(args, shard.out, {}, None, engine=FakeEngine())


# ------------------------------------------------------------------ the staged regression (Task 49's clean control)

class BreakingEngine(FakeEngine):
    """A CLEAN input (all 3 members pass) whose staged healer breaks one member - the cheap tier never sees it."""

    def __init__(self, staged_q: int = 2) -> None:
        super().__init__()
        self.staged_q = staged_q
        self.search_start: list[int] = []

    def open_workspace(self, pool: Any, source: str, workdir: str) -> Any:
        return W.make_ws(3)

    def run_staged(self, pool: Any, ws: Any) -> Any:
        self.staged_calls += 1
        return SimpleNamespace(ws=W.make_ws(self.staged_q))


def test_a_staged_result_that_loses_a_panel_member_is_not_what_the_staged_arm_ships(shard: Any) -> None:
    engine = BreakingEngine(staged_q=2)
    rows, excluded = go(shard, "staged", None, engine)
    [row] = rows
    assert excluded == [] and row["in_panel_passes"] == 3
    assert row["out_panel_passes"] == 3 and row["out_clean"] is True, "the regression was shipped"
    assert row["triage"]["stages"]["full_panel"]["fell_back_to"] == "input"


def test_a_clean_input_is_never_replaced_by_the_staged_arm_even_when_the_result_is_as_good(shard: Any) -> None:
    """The engine's delivery gate (`deliverable`) passes a clean input through; the staged arm must measure that."""
    engine = BreakingEngine(staged_q=3)
    [row], _ = go(shard, "staged", None, engine)
    assert row["out_clean"] is True and row["out_panel_passes"] == 3
    assert row["triage"]["stages"]["full_panel"]["fell_back_to"] is None


def test_the_search_arms_start_from_the_confirmed_staged_state_not_the_damaged_one(shard: Any,
                                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4 import search as S

    starts: list[int] = []
    real = S.search

    def spy(pool: Any, start: Any, *a: Any, **k: Any) -> Any:
        starts.append(W._q(start))
        return real(pool, start, *a, **k)

    monkeypatch.setattr(S, "search", spy)
    go(shard, "search_det", None, BreakingEngine(staged_q=2))
    assert starts == [3], "the search started from the state the healer damaged"


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_a_clean_input_the_healer_damages_costs_no_model_call_and_comes_out_clean(shard: Any, wire: Wire,  # noqa: F811
                                                                                  arm: str) -> None:
    """LEAD-2's snapshot-v1 data point (00614313_..._003 on 5dc953ea): search_ai spent $0.044 on a CLEAN input,
    because the search started from the healer's damaged state. With the full-panel confirmation the search starts
    from the clean state, the proposer stops at once (it never calls a model on a clean start), and the row is
    clean. The delivery gate kept the OUTPUT clean even before the fix; what the fix removes is the wasted spend
    and the search working on a damaged part."""
    wire.candidate = CLEAN_CANDIDATE
    ledger = ledgered() if arm == "search_ai" else None
    [row], _ = go(shard, arm, ledger, BreakingEngine(staged_q=2))
    proposals = [r for r in wire.requests if "propose" in [t["name"] for t in r.get("tools", [])]]
    assert row["out_clean"] is True and row["out_panel_passes"] == 3
    assert proposals == [], f"{arm} called the model {len(proposals)} time(s) on a clean input"
    if ledger is not None:
        assert ledger.file_spent_usd(SHA) == 0.0

# ------------------------------------------------------------------ a wall stop is not a verdict on the file

def test_a_search_that_stopped_on_its_wall_was_not_measured() -> None:
    """Task 49's smoke: search_det on 0052... stopped on its own wall at 900.9 s with 0 verdict candidates and was
    written as a CONCLUSIVE failure. Time is the machine's (the smoke ran at load 18 on 16 vCPU), not the file's: a
    wall stop that did not end clean is inconclusive, with or without candidates. Count stops (ops, exhausted,
    patience) stay conclusive: they are the same on any machine."""
    smoke = {"reason": "wall", "infrastructure_failed": False, "inconclusive_candidates": 0, "verdict_candidates": 0,
                 "out_clean": False}
    assert measurement.file_unmeasured(**smoke) is True
    assert measurement.file_unmeasured(**{**smoke, "verdict_candidates": 7}) is True
    assert measurement.file_unmeasured(**{**smoke, "out_clean": True}) is False      # clean is clean, however late
    for counted in ("ops", "exhausted", "patience"):
        assert measurement.file_unmeasured(**{**smoke, "reason": counted, "verdict_candidates": 3}) is False


def test_a_wall_stop_written_as_a_conclusive_failure_is_refused_by_the_report() -> None:
    row = {"name": "0052_00528484_step_003.step", "sha256": SHA, "source": "abc_fail", "arm": "search_det",
           "inconclusive": False, "model_usd": 0.0, "wall_s": 900.9, "reason": "wall", "verdict_candidates": 0,
           "inconclusive_candidates": 0, "infrastructure_failed": False, "in_clean": False, "in_panel_passes": 3,
           "in_valid": True, "in_faithful_ok": True, "out_valid": True, "out_faithful_ok": True,
           "out_panel_passes": 3, "panel_size": 4, "out_tier": "full", "out_clean": False}
    with pytest.raises(report.ReportRefused, match="NOT a conclusive failure"):
        report._row_from_dict(row)
    assert report._row_from_dict({**row, "inconclusive": True}).counts is False


def test_a_wall_stop_through_the_real_arm_path_is_an_inconclusive_row(shard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same stop through run_arms -> arms.run_file -> search: a search whose wall is already spent stops on `wall`;
    its row must say inconclusive, and the report must accept it as such (not count it as a failure)."""
    import dataclasses

    det = ev.ARMS["search_det"]
    monkeypatch.setitem(ev.ARMS, "search_det", dataclasses.replace(det, budget=dataclasses.replace(det.budget, wall_s=1e-9)))
    rows, excluded = go(shard, "search_det", None)
    [row] = rows
    assert excluded == [] and row["reason"] == "wall"
    assert row["inconclusive"] is True
    loaded = report._row_from_dict(row)
    assert loaded.derived_inconclusive and not loaded.counts


def test_the_triage_names_each_failing_members_cause() -> None:
    """The report counts repeated member timeouts by cause (timeout_twice / after_timeout_*), so the row must carry it."""
    member = SimpleNamespace(name="gmsh_alt", status="timeout", error_kind="timeout", cause="after_timeout_crashed")
    v = SimpleNamespace(faithful=None, panel=[member, SimpleNamespace(name="gmsh_prod", status="pass")],
                        valid=SimpleNamespace(ok=True, hard=[]))
    failing = run_arm.arms_mod.verdict_triage(v)["panel_failing"]
    assert failing == [{"name": "gmsh_alt", "status": "timeout", "error_kind": "timeout", "cause": "after_timeout_crashed",
                        "located": [], "located_total": 0}]


def test_the_triage_keeps_where_each_failing_member_said_it_went_wrong() -> None:
    """A row must carry the face / point a mesher named (C5, Lane D's padding fix): without it a wave cannot say which
    face failed. Capped per member so one row cannot blow up; the total is kept, so the cap is visible."""
    from meshpipeline.cad.repair_v4.verdict.panel import Located

    cap = run_arm.arms_mod.LOCATED_CAP
    many = [Located(f"fp{i}", (float(i), 0.5, -1.25), f"S{i}") for i in range(cap + 7)]
    member = SimpleNamespace(name="gmsh_prod", status="fail", error_kind="other", cause=None, located=many)
    unplaced = SimpleNamespace(name="netgen_occ", status="fail", error_kind="netgen_meshing_failed", cause=None,
                               located=[Located(None, None, "?")])
    v = SimpleNamespace(faithful=None, panel=[member, unplaced, SimpleNamespace(name="gmsh_alt", status="pass")],
                        valid=SimpleNamespace(ok=True, hard=[]))
    prod, ng = run_arm.arms_mod.verdict_triage(v)["panel_failing"]
    assert prod["located_total"] == cap + 7 and len(prod["located"]) == cap
    assert prod["located"][0] == {"entity": "fp0", "xyz": [0.0, 0.5, -1.25], "mesher_tag": "S0"}
    assert ng["located"] == [{"entity": None, "xyz": None, "mesher_tag": "?"}] and ng["located_total"] == 1
    json.dumps(prod)  # a row is JSON


# --- the owner's overlap ruling: either cut computable from rows -------------------------------------------------


class _OverlapPool:
    def __init__(self, record: dict | None = None, fail: bool = False) -> None:
        self.record, self.fail, self.calls = record, fail, []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload))
        if self.fail:
            return CallResult(False, None, "boom", "error", 0.0)
        return CallResult(True, self.record, None, "ok", 0.0)


def _overlapping_input(*pairs: tuple[str, str]) -> Any:
    from meshpipeline.cad.repair_v4.verdict.valid import Finding

    hard = [Finding("body_overlap", [a, b], {}) for a, b in pairs]
    return SimpleNamespace(valid=SimpleNamespace(hard=hard))


def test_the_inputs_overlap_depth_and_bound_are_recorded_from_both_cuts() -> None:
    pool = _OverlapPool({"diag_mm": 120.0, "bound_mm": 0.1, "pairs": [
        {"bodies": ["S1", "S2"], "a_by_b": {"depth_mm": 4.6, "method": "measured", "volume_mm3": 43.0},
         "b_by_a": {"depth_mm": 3.1, "method": "mean_thickness", "volume_mm3": 43.0}},
        {"bodies": ["S2", "S3"], "a_by_b": {"depth_mm": 0.05, "method": "measured", "volume_mm3": 1.0},
         "b_by_a": {"depth_mm": 0.07, "method": "measured", "volume_mm3": 1.0}}]})
    o = run_arm.arms_mod.overlap_triage(pool, SimpleNamespace(brep_path="/w/in.brep"),
                                        _overlapping_input(("S1", "S2"), ("S2", "S3"), ("S2", "S1")))
    assert pool.calls == [("v4_overlap_depths", {"brep_path": "/w/in.brep", "pairs": [[1, 2], [2, 3]]})]
    # per pair the cheaper cut; for the file the worst pair: the file is within the bound only if every pair is
    assert o["max_depth_mm"] == 3.1 and o["method"] == "mean_thickness" and o["bound_mm"] == 0.1
    assert o["within_bound"] is False and [p["required_mm"] for p in o["pairs"]] == [3.1, 0.05]


def test_an_input_with_no_overlap_records_none_and_calls_nothing() -> None:
    pool = _OverlapPool()
    assert run_arm.arms_mod.overlap_triage(pool, SimpleNamespace(brep_path="x"), _overlapping_input()) is None
    assert pool.calls == []


def test_a_failed_measurement_is_recorded_as_such_never_breaks_the_row() -> None:
    o = run_arm.arms_mod.overlap_triage(_OverlapPool(fail=True), SimpleNamespace(brep_path="x"),
                                        _overlapping_input(("S1", "S2")))
    assert o["max_depth_mm"] is None and "error" in o

def test_a_wave_with_more_shards_than_in_flight_calls_is_refused_without_the_override(shard: Any) -> None:
    """B11 (3): 96 shards against an in-flight ceiling of 37 used to floor each slice to one call silently. Now the
    launch is refused unless --allow-in-flight-floor is passed, and the override is recorded."""
    from meshpipeline.cad.repair_v4.model_throttle import ThrottleConfigError

    base = [*shard.argv, "--arms", "search_ai", "--wave-type", "normal", "--wave-id", "w1", "--n-shards", "96",
            "--shard", "1/96", "--ledger-path", "/x", "--marker-bucket", "b"]
    with pytest.raises(ThrottleConfigError, match="allow-in-flight-floor"):
        run_arm.build_throttle(run_arm.parse_args(base), "openai:gpt-6-astra", {})
    prov: dict = {}
    t = run_arm.build_throttle(run_arm.parse_args([*base, "--allow-in-flight-floor"]), "openai:gpt-6-astra", prov)
    assert t.slice.floored and prov["model_throttle"]["in_flight_floor_override"] is True
    assert prov["model_throttle"]["in_flight_floored"] is True


def test_the_row_carries_the_output_fidelity_numbers_the_report_flags_on() -> None:
    """Layer 3 gates nothing, so the row must still CARRY it: report.py's gaming flag reads these."""
    from types import SimpleNamespace

    import arms as arms_mod

    f = SimpleNamespace(ok=False, max_dev_mm=4.2, bound_mm=0.1, reasons=["volume_changed"], volume_ratio=3.0,
                        area_ratio=1.1, bbox_ratio=0.4, diag_after_mm=40.0, bodies_before=1, bodies_after=1,
                        declared_added=[], declared_removed=[])
    v = SimpleNamespace(valid=SimpleNamespace(ok=True, hard=[]), panel=[], faithful=f, clean=True, tier="full")
    fields = arms_mod._layers(v, "out")
    assert fields["out_volume_ratio"] == 3.0 and fields["out_bbox_ratio"] == 0.4
    assert fields["out_max_dev_mm"] == 4.2 and fields["out_faithful_ok"] is False


def test_a_row_without_a_layer_three_measurement_carries_none_not_a_number() -> None:
    from types import SimpleNamespace

    import arms as arms_mod

    v = SimpleNamespace(valid=SimpleNamespace(ok=True, hard=[]), panel=[], faithful=None, clean=True, tier="full")
    fields = arms_mod._layers(v, "out")
    assert fields["out_volume_ratio"] is None and fields["out_bbox_ratio"] is None
    assert fields["out_max_dev_mm"] is None and fields["out_faithful_ok"] is False


def test_the_row_carries_every_fidelity_quantity_the_decision_says_to_report() -> None:
    """Owner 2026-11-24: deviation, volume/area change, bbox and body count are REPORTED on every row."""
    from types import SimpleNamespace

    import arms as arms_mod

    f = SimpleNamespace(ok=False, max_dev_mm=4.2, bound_mm=0.1, reasons=["volume_changed"], volume_ratio=3.0,
                        area_ratio=1.1, bbox_ratio=0.4, diag_after_mm=40.0, bodies_before=1, bodies_after=7,
                        declared_added=[], declared_removed=[])
    v = SimpleNamespace(valid=SimpleNamespace(ok=True, hard=[]), panel=[], faithful=f, clean=True, tier="full")
    out = arms_mod._layers(v, "out")
    assert out["out_area_ratio"] == 1.1
    assert out["out_bodies_before"] == 1 and out["out_bodies_after"] == 7
    # and the input side stays as it was: these are the OUTPUT's numbers
    assert "in_area_ratio" not in arms_mod._layers(v, "in")


def test_search_wall_s_only_lowers_the_search_wall(shard: Any) -> None:
    spec = run_arm.evalspec.ARMS["search_det"]
    low = run_arm.effective_arm(spec, run_arm.parse_args([*shard.argv, "--arms", "search_det", "--search-wall-s", "240"]))
    assert low.budget.wall_s == 240.0 and low.budget.ops == spec.budget.ops
    high = run_arm.effective_arm(spec, run_arm.parse_args([*shard.argv, "--arms", "search_det", "--search-wall-s", "99999"]))
    assert high.budget.wall_s == spec.budget.wall_s
    with pytest.raises(SystemExit):
        run_arm.parse_args([*shard.argv, "--arms", "search_det", "--search-wall-s", "0"])
