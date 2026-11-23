"""v4_eval re-probe, splits, collector and cost gate: hermetic (fake members, no kernel, no mesher)."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict.cache import DirStore, VerdictCache
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, Finding, ValidReport

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import run_arm  # noqa: E402  (loads evalspec / reprobe / pool under stable module names)

rp = run_arm.reprobe_mod
evalspec = run_arm.evalspec


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"v4t_{name}", V4 / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"v4t_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


collect = _load("collect")
launch = _load("launch_reprobe")


@dataclass(frozen=True)
class Entry:
    name: str
    sha256: str
    source: str = "abc_fail"
    group: str | None = None


@dataclass(frozen=True)
class FakeResult:
    name: str
    status: str
    min_sicn: float | None = 0.3
    message: str = ""
    error_kind: str | None = None
    located: list = field(default_factory=list)


@dataclass
class FakeMember:
    name: str
    status: str = "pass"
    message: str = ""
    error_kind: str | None = None
    runs: int = 0
    config: dict = field(default_factory=dict)

    def run(self, pool, step, *, timeout_s):
        self.runs += 1
        return FakeResult(self.name, self.status, 0.3 if self.status == "pass" else None, self.message, self.error_kind)


def run_panel(pool, step, members, *, timeout_s):
    return [m.run(pool, step, timeout_s=timeout_s) for m in members]


def _sha(i: int) -> str:
    return f"{i:064x}"


@pytest.fixture
def layers(monkeypatch):
    state = {"valid": ValidReport(True, [], [], "mm", 1, True), "unavailable": False}

    def fake_valid(pool, step, *, original_units):
        if state["unavailable"]:
            raise CheckerUnavailable("worker died")
        return state["valid"]

    monkeypatch.setattr(assemble, "check_valid", fake_valid)
    monkeypatch.setattr(
        assemble, "check_faithful_steps",
        lambda pool, o, s, *, declared_added, declared_removed: FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1),
    )
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "4.15"})
    return state


def _probe(entries, tier="cheap", members=None, **kw):
    excluded: list = []
    rows = rp.reprobe(entries, tier=tier, pool=object(), resolve=lambda e: f"/x/{e.name}", members=members,
                      run_panel=run_panel, excluded=excluded, **kw)
    return rows, excluded


# ------------------------------------------------------------------ the verdict -> row mapping

def test_layers_and_families(layers) -> None:
    fail = FakeMember("gmsh_default", "fail", "PLC Error: segment intersects")
    rows, _ = _probe([Entry("a", _sha(1))], members=[fail])
    assert (rows[0].first_failing_layer, rows[0].family) == ("panel", "plc_intersection")
    ok, _ = _probe([Entry("b", _sha(2))], members=[FakeMember("gmsh_default")])
    assert (ok[0].first_failing_layer, ok[0].family) == (None, "")
    layers["valid"] = ValidReport(False, [Finding("open_shell")], [], "mm", 1, True)
    bad, _ = _probe([Entry("c", _sha(3))], members=[FakeMember("gmsh_default")])
    assert (bad[0].first_failing_layer, bad[0].family) == ("valid", "open_shell")


def test_cheap_pass_is_never_clean_and_full_pass_is(layers) -> None:
    cheap, _ = _probe([Entry("a", _sha(1))], members=[FakeMember("gmsh_default")])
    assert cheap[0].verdict_summary["clean"] is False and cheap[0].verdict_summary["tier"] == "cheap"
    full, _ = _probe([Entry("a", _sha(1))], tier="full", members=[FakeMember("p"), FakeMember("d")])
    assert full[0].verdict_summary["clean"] is True


# ------------------------------------------------------------------ inconclusive stays out

def test_inconclusive_verdict_is_excluded_not_counted(layers) -> None:
    infra = FakeMember("gmsh_default", "crashed", "worker replaced", error_kind="infrastructure")
    rows, excluded = _probe([Entry("a", _sha(1))], members=[infra])
    assert rows == []
    assert [x.reason.split(":")[0] for x in excluded] == ["inconclusive"]


def test_checker_unavailable_is_excluded_not_recorded_as_unclean(layers) -> None:
    layers["unavailable"] = True
    rows, excluded = _probe([Entry("a", _sha(1))], members=[FakeMember("gmsh_default")])
    assert rows == [] and excluded[0].reason.startswith("checker_unavailable")


def test_a_genuine_mesher_timeout_is_a_panel_failure_not_an_exclusion(layers) -> None:
    rows, excluded = _probe([Entry("a", _sha(1))], members=[FakeMember("gmsh_default", "timeout", "budget")])
    assert excluded == [] and rows[0].first_failing_layer == "panel" and rows[0].family == "timeout_crash"


def test_file_wall_clock_excludes_the_file(layers) -> None:
    def slow(pool, step, members, *, timeout_s):
        time.sleep(5)
        return []

    excluded: list = []
    rows = rp.reprobe([Entry("a", _sha(1))], tier="cheap", pool=object(), resolve=lambda e: "/x",
                      members=[FakeMember("m")], run_panel=slow, file_timeout_s=0.2, excluded=excluded)
    assert rows == [] and excluded[0].reason.startswith("file_timeout")


def test_missing_input_is_excluded(layers) -> None:
    def resolve(e):
        raise FileNotFoundError("nope")

    excluded: list = []
    rows = rp.reprobe([Entry("a", _sha(1))], tier="cheap", pool=object(), resolve=resolve,
                      members=[FakeMember("m")], run_panel=run_panel, excluded=excluded)
    assert rows == [] and excluded[0].reason.startswith("input_missing")


def test_an_uncounted_row_never_reaches_splits_or_counts() -> None:
    uncounted = _row(1, "panel", "other", extra={"counts_for_scoreboard": False})
    splits = rp.build_splits([uncounted, *[_row(i, "panel", "other") for i in range(2, 12)]], dev=3, heldout=2, clean=0)
    assert splits.excluded_uncounted == 1
    assert all(r.sha256 != uncounted.sha256 for r in splits.dev + splits.heldout)


# ------------------------------------------------------------------ the cache really prevents a re-mesh

def test_cache_prevents_a_second_mesh(layers, tmp_path) -> None:
    step = tmp_path / "a.step"
    step.write_text("ISO-10303-21; one")
    cache = VerdictCache(DirStore(tmp_path / "cache"), result_factory=lambda d: FakeResult(**d))
    member = FakeMember("gmsh_default")
    entry = Entry("a.step", _sha(1))
    for _ in range(3):
        rp.reprobe([entry], tier="cheap", pool=object(), resolve=lambda e: str(step), members=[member],
                   run_panel=run_panel, cache=cache)
    assert member.runs == 1  # hit twice, meshed once


# ------------------------------------------------------------------ quarantine

def test_default_kinds_are_what_the_gate_allows() -> None:
    inject = rp.load_inject()
    assert set(inject.DEFAULT_KINDS).isdisjoint(inject.QUARANTINED_KINDS)
    for kind in inject.DEFAULT_KINDS:
        rp.check_not_quarantined([Entry(f"part.{kind}.7.step", _sha(1), "inject")])


@pytest.mark.parametrize("kind", ["loose_reapprox", "tolerance_inflate", "seam_variant", "precision_truncate",
                                  "pcurve_corrupt"])
def test_quarantined_and_infeasible_kinds_are_refused_before_any_mesh(layers, kind) -> None:
    member = FakeMember("gmsh_default")
    with pytest.raises(rp.QuarantinedKind):
        _probe([Entry(f"part.{kind}.7.step", _sha(1), "inject")], members=[member])
    assert member.runs == 0
    with pytest.raises(rp.QuarantinedKind):
        rp.build_splits([_row(1, "panel", "other", source="inject", name=f"part.{kind}.7.step")], dev=1, heldout=0, clean=0)


def test_an_injected_entry_with_no_readable_kind_is_refused() -> None:
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([Entry("mystery.step", _sha(1), "inject")])


# ------------------------------------------------------------------ the plan

def test_full_tier_runs_on_cheap_failures_plus_a_sample_of_passes_and_never_no_solids() -> None:
    entries = [Entry(f"f{i}", _sha(i)) for i in range(1, 2001)]
    fails = {e.sha256 for e in entries[:100]}
    no_solids = entries[0]
    cheap = [
        _row(i, "valid", "no_solids") if e is no_solids
        else _row(i, "panel", "other") if e.sha256 in fails else _row(i, None, "")
        for i, e in enumerate(entries, start=1)
    ]
    chosen = rp.full_tier_entries(entries, cheap)
    shas = {e.sha256 for e in chosen}
    assert no_solids.sha256 not in shas
    assert fails - {no_solids.sha256} <= shas
    sampled = shas - fails
    assert 100 < len(sampled) < 300  # ~10% of 1900
    assert rp.full_tier_entries(entries, cheap) == chosen  # deterministic


def test_agreement_counts_the_cheap_tiers_misses() -> None:
    cheap = [_row(1, None, ""), _row(2, None, ""), _row(3, "panel", "other"), _row(4, "panel", "other")]
    full = [_row(1, None, ""), _row(2, "panel", "other"), _row(3, "panel", "other"), _row(4, None, "")]
    a = rp.agreement(cheap, full)
    assert (a["cheap_pass_full_pass"], a["cheap_pass_full_fail"], a["cheap_fail_full_fail"],
            a["cheap_fail_full_pass"]) == (1, 1, 1, 1)
    assert a["cheap_miss_rate_among_cheap_passes"] == 0.5 and a["agree"] == 0.5


# ------------------------------------------------------------------ splits

def _row(i, layer, family, source="abc_fail", name=None, group=None, tier="cheap", clean=False, extra=None):
    summary = {"tier": tier, "clean": clean, "counts_for_scoreboard": True, "hard": [family] if layer == "valid" else []}
    summary.update(extra or {})
    return rp.ProbeRow(name or f"f{i}.step", _sha(i), source, summary, layer, family, group)


def _population():
    rows, i = [], 0
    for layer, family, source, n in [
        ("panel", "plc_intersection", "abc_fail", 400), ("panel", "overlapping_facets", "abc_fail", 200),
        ("panel", "open_loop", "f360", 150), ("valid", "open_shell", "abc_fail", 100),
        ("valid", "open_shell", "nist", 6), ("panel", "no_volume", "occt", 10),
    ]:
        for _ in range(n):
            i += 1
            rows.append(_row(i, layer, family, source, group=f"nist-{i % 3}" if source == "nist" else None))
    for _ in range(300):
        i += 1
        rows.append(_row(i, None, "", "abc_pass", tier="full", clean=True))
    for _ in range(50):
        i += 1
        rows.append(_row(i, None, "", "f360", tier="cheap"))  # a cheap pass: not certified clean
    for _ in range(40):
        i += 1
        rows.append(_row(i, "valid", "no_solids", "f360"))
    return rows


def test_no_solids_excluded_from_failing() -> None:
    rows = _population()
    splits = rp.build_splits(rows, dev=100, heldout=60, clean=50)
    ids = {r.sha256 for r in splits.dev + splits.heldout}
    assert not [r for r in rows if rp.is_no_solids(r) and r.sha256 in ids]
    assert splits.excluded_no_solids == 40


def test_no_solids_found_in_the_hard_codes_is_excluded_too() -> None:
    odd = _row(1, "valid", "brep_invalid", extra={"hard": ["brep_invalid", "no_solids"]})
    splits = rp.build_splits([odd, *[_row(i, "panel", "other") for i in range(2, 12)]], dev=3, heldout=2, clean=0)
    assert odd.sha256 not in {r.sha256 for r in splits.dev + splits.heldout}


def test_splits_disjoint_and_stratified(tmp_path) -> None:
    rows = _population()
    splits = rp.build_splits(rows, dev=100, heldout=60, clean=50, out_dir=tmp_path)
    assert (len(splits.dev), len(splits.heldout), len(splits.clean)) == (100, 60, 50)
    sets = [{r.sha256 for r in s} for s in (splits.dev, splits.heldout, splits.clean)]
    assert not (sets[0] & sets[1]) and not (sets[0] & sets[2]) and not (sets[1] & sets[2])
    # proportional: the biggest stratum (400/866) holds ~46% of dev+heldout
    pop = splits.strata["failing_population"]
    total = sum(pop.values())
    for key, n in pop.items():
        got = splits.strata["dev"].get(key, 0) + splits.strata["heldout"].get(key, 0)
        assert abs(got - 160 * n / total) <= 1.0, key
    # every populous stratum is represented in both splits
    assert splits.strata["dev"]["panel/plc_intersection/abc_fail"] > 0
    assert splits.strata["heldout"]["panel/overlapping_facets/abc_fail"] > 0
    # clean: full-tier passes only, never the cheap passes
    assert all(r.verdict_summary["tier"] == "full" and r.first_failing_layer is None for r in splits.clean)
    for name in ("dev", "heldout", "clean"):
        assert json.loads((tmp_path / f"{name}.json").read_text())["split"] == name
    assert [r.sha256 for r in rp.read_split(tmp_path / "dev.json")] == [r.sha256 for r in splits.dev]


def test_splits_do_not_depend_on_input_order() -> None:
    rows = _population()
    a = rp.build_splits(rows, dev=100, heldout=60, clean=50)
    b = rp.build_splits(list(reversed(rows)), dev=100, heldout=60, clean=50)
    assert [r.sha256 for r in a.dev] == [r.sha256 for r in b.dev]
    assert [r.sha256 for r in a.heldout] == [r.sha256 for r in b.heldout]
    c = rp.build_splits(rows, dev=100, heldout=60, clean=50, seed=1)
    assert [r.sha256 for r in a.dev] != [r.sha256 for r in c.dev]


@pytest.mark.parametrize("seed", [20261122, 1, 2, 3])
def test_heldout_never_in_dev(seed) -> None:
    rows = _population() + [_row(10_000 + i, "panel", "plc_intersection", "abc_fail") for i in range(5)]
    splits = rp.build_splits(rows, dev=100, heldout=60, clean=50, seed=seed)
    assert {r.sha256 for r in splits.dev}.isdisjoint({r.sha256 for r in splits.heldout})
    assert {r.group for r in splits.dev if r.group}.isdisjoint({r.group for r in splits.heldout if r.group})
    # and the guard itself bites: a hand-built overlap is refused
    with pytest.raises(AssertionError):
        rp.assert_disjoint(rp.Splits(splits.dev, splits.dev[:1], [], seed))


def test_too_small_a_failing_set_is_refused_not_silently_shrunk() -> None:
    with pytest.raises(ValueError):
        rp.build_splits([_row(i, "panel", "other") for i in range(1, 6)], dev=3, heldout=3, clean=0)


# ------------------------------------------------------------------ collector

def _shard(root: Path, i: int, n: int, rows, *, exit_code=0, commit="c" * 40, excluded=()):
    d = root / f"shard-{i}-of-{n}"
    d.mkdir(parents=True)
    from dataclasses import asdict
    (d / "rows.json").write_text(json.dumps([asdict(r) for r in rows]))
    rp.write_rows([r for r in rows if r.verdict_summary["tier"] == "cheap"], d / "cheap.jsonl")
    (d / "EXCLUDED.json").write_text(json.dumps([asdict(x) for x in excluded]))
    (d / "PROVENANCE.json").write_text(json.dumps({"exit_code": exit_code, "commit": commit, "image": "img"}))


def test_collect_refuses_a_partial_wave(tmp_path) -> None:
    _shard(tmp_path, 1, 2, [_row(1, "panel", "other")])
    with pytest.raises(collect.CollectRefused, match="missing shard"):
        collect.load_shards(tmp_path)


def test_collect_refuses_a_failed_shard_and_mixed_commits(tmp_path) -> None:
    _shard(tmp_path, 1, 2, [_row(1, "panel", "other")], exit_code=4)
    _shard(tmp_path, 2, 2, [_row(2, "panel", "other")])
    with pytest.raises(collect.CollectRefused, match="exit_code"):
        collect.load_shards(tmp_path)
    (tmp_path / "shard-1-of-2/PROVENANCE.json").write_text(json.dumps({"exit_code": 0, "commit": "d" * 40, "image": "img"}))
    with pytest.raises(collect.CollectRefused, match="commit"):
        collect.load_shards(tmp_path)


def test_collect_reports_excluded_separately_and_counts_only_conclusive(tmp_path) -> None:
    ex = rp.Excluded("x", _sha(99), "abc_fail", "inconclusive: pool")
    _shard(tmp_path, 1, 2, [_row(1, "panel", "other"), _row(2, None, "")], excluded=[ex])
    _shard(tmp_path, 2, 2, [_row(3, "valid", "no_solids")])
    merged = collect.merge(collect.load_shards(tmp_path, 2))
    rep = collect.report(merged, None)
    assert rep["probed_conclusive"] == 3 and rep["excluded_inconclusive"] == 1
    assert rep["failing"] == 1 and rep["no_solids_not_repairable"] == 1
    assert rep["excluded_by_reason"] == {"inconclusive": 1}


# ------------------------------------------------------------------ run_arm: the wall kill and the shard

def test_harness_argv_always_carries_the_wall_kill() -> None:
    argv = evalspec.harness_argv(inputs="/in", out="/out", shard="1/4")
    assert argv[argv.index("--file-timeout-s") + 1] == str(int(evalspec.DEFAULT_FILE_TIMEOUT_S))
    assert "--isolate-files" in argv
    for bad in (0, -1, 0.0):
        with pytest.raises(ValueError):
            evalspec.harness_argv(inputs="/in", out="/out", shard="1/4", file_timeout_s=bad)


def test_run_arm_harness_mode_passes_file_timeout_to_repair_corpus(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeRc:
        @staticmethod
        def main():
            seen["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(run_arm, "_load", lambda name, path: FakeRc if name == "repair_corpus" else sys.modules[name])
    assert run_arm.main(["--mode", "harness", "--shard", "1/2", "--out", str(tmp_path), "--inputs", "/in",
                         "--file-timeout-s", "123"]) == 0
    assert seen["argv"][seen["argv"].index("--file-timeout-s") + 1] == "123"
    assert json.loads((tmp_path / "PROVENANCE.json").read_text())["file_timeout_s"] == 123


def test_run_arm_refuses_a_zero_wall_kill(tmp_path) -> None:
    with pytest.raises(SystemExit):
        run_arm.main(["--out", str(tmp_path), "--file-timeout-s", "0"])


def test_shards_partition_the_pool_exactly() -> None:
    entries = [Entry(f"f{i}", _sha(i * 7919)) for i in range(1, 500)]
    parts = [run_arm.shard_entries(entries, f"{i}/7") for i in range(1, 8)]
    assert sorted(e.sha256 for p in parts for e in p) == sorted(e.sha256 for e in entries)
    assert all(parts)


# ------------------------------------------------------------------ the cost gate

def test_the_real_pool_at_the_ceiling_is_refused_and_a_sane_wave_is_not() -> None:
    real = evalspec.project(53_799, shards=96, max_run_hours=24)
    assert evalspec.check_budget(real) == []
    huge = evalspec.project(53_799, shards=96, max_run_hours=300, full_min=40.0)
    reasons = evalspec.check_budget(huge)
    assert huge.vm_hours > evalspec.MAX_VM_HOURS and any("VM-hour ceiling" in r for r in reasons)
    with pytest.raises(evalspec.BudgetRefused):
        evalspec.enforce_budget(huge)
    assert evalspec.check_budget(huge, allow_over_budget=True) == []


def test_override_does_not_waive_the_run_duration_or_vcpu_limits() -> None:
    short = evalspec.project(53_799, shards=96, max_run_hours=3)
    assert any("deleted" in r for r in evalspec.check_budget(short, allow_over_budget=True))
    wide = evalspec.project(53_799, shards=200, max_run_hours=24)
    assert any("vCPU" in r for r in evalspec.check_budget(wide, allow_over_budget=True))


def test_projection_formula() -> None:
    p = evalspec.project(600, shards=8, machine_type="c2d-standard-8", cheap_fail_fraction=0.5,
                         cheap_min=1.0, full_min=3.0)
    # 300 failing + ceil(300 * 0.1) = 330 full files; (600*1 + 330*3)/60 = 26.5 vCPU-h; 3.3125 h wall; 1 VM
    assert (p.full_files, p.vms) == (330, 1)
    assert p.cpu_hours == pytest.approx(26.5) and p.wall_hours == pytest.approx(26.5 / 8)
    assert p.vm_hours == pytest.approx(26.5 / 8)


def test_launcher_preflight_prints_and_refuses(capsys) -> None:
    assert launch.main(["--files", "53799", "--shards", "96", "--max-run-hours", "24"]) == 0
    assert "PROJECTED" in capsys.readouterr().out
    assert launch.main(["--files", "5000000", "--shards", "96", "--max-run-hours", "24"]) == 1
    err = capsys.readouterr().err
    assert "REFUSE" in err and "ceiling" in err
    assert launch.main(["--files", "5000000", "--shards", "96", "--max-run-hours", "9999",
                        "--allow-over-budget"]) == 0
