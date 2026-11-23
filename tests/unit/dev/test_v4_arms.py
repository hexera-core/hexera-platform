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
    assert by["staged"]["out_panel_passes"] == 1 and engine.staged_calls == 1
    assert by["search_det"]["reason"] == "exhausted" and by["search_det"]["verdict_candidates"] == 0
    assert all(r["model_usd"] == 0.0 for r in rows)
    for r in rows:                                  # the report's own loader: the rows are a real ArmRow contract
        assert report._row_from_dict(r).inconclusive is False


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
    assert len(proposals) >= 1 and len(admitted) == len(proposals), "every model request was admitted by THE ledger"
    assert row["model_usd"] > 0 and row["model_usd"] == pytest.approx(ledger.file_spent_usd(SHA))
    assert row["model_usd"] == pytest.approx(ledger.spent_usd())
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


def test_an_exhausted_ledger_stops_the_model_before_the_wire_and_the_file_still_gets_a_row(shard: Any, wire: Wire) -> None:  # noqa: F811
    wire.candidate = CLEAN_CANDIDATE
    ledger = ledgered(cap=0.001)
    rows, _ = go(shard, "search_ai", ledger)
    proposals = [r for r in wire.requests if "propose" in [t["name"] for t in r.get("tools", [])]]
    assert proposals == [], "the ledger had no room, yet a proposer round reached the provider"
    assert ledger.spent_usd() == 0.0 and rows[0]["model_usd"] == 0.0 and rows[0]["out_clean"] is False


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
