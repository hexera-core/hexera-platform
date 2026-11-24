"""v4_eval re-probe, splits, collector and cost gate: hermetic (fake members, no kernel, no mesher)."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

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
    kw.setdefault("engine_form", lambda pool, step: step)  # no kernel here: the file is its own engine form
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
                      members=[FakeMember("m")], run_panel=slow, file_timeout_s=0.2, excluded=excluded,
                      engine_form=lambda pool, step: step)
    assert rows == [] and excluded[0].reason.startswith("file_timeout")


def test_missing_input_is_excluded(layers) -> None:
    def resolve(e):
        raise FileNotFoundError("nope")

    excluded: list = []
    rows = rp.reprobe([Entry("a", _sha(1))], tier="cheap", pool=object(), resolve=resolve,
                      members=[FakeMember("m")], run_panel=run_panel, excluded=excluded,
                      engine_form=lambda pool, step: step)
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
                   run_panel=run_panel, cache=cache, engine_form=lambda pool, s: s)
    assert member.runs == 1  # hit twice, meshed once


# ------------------------------------------------------------------ quarantine
# Fixtures are REAL: a real `inject()` output wrapped by the real `pool.inject_entry` into a real `PoolEntry`.
# (An earlier fixture invented a `kind_status` attribute no production type had, and so passed while the real
# path never fed the check a kind.)

pool_mod = run_arm.pool_mod


@pytest.fixture(scope="module")
def clean_source() -> object:
    """The clean pool entry the injected variants derive from; its licence is what they must carry."""
    return pool_mod.PoolEntry("clean_part.step", "abc_pass", "c" * 64, "https://x/abc.7z", "d" * 64,
                              pool_mod.LICENCE["abc"])


@pytest.fixture(scope="module")
def clean_step(tmp_path_factory) -> Path:
    # Both gating tiers (meshpipeline-validation:local, meshpipeline-mesh:native) are known to carry OCP, so this runs
    # where merges are gated. Anywhere without OCP this skip would silently hide ALL the quarantine coverage below.
    pytest.importorskip("OCP.BRepCheck", reason="the CAD kernel is a native-tier dependency")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0, 0, 1)), 4.0, 22.0).Shape()
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(3.3, 1.7, 0.9), gp_Dir(1, 2, 3)), 0.37)
    part = BRepBuilderAPI_Transform(BRepAlgoAPI_Cut(box, hole).Shape(), turn, True).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(part, STEPControl_AsIs)
    path = tmp_path_factory.mktemp("clean") / "clean_part.step"
    assert writer.Write(str(path)) == IFSelect_RetDone
    return path


@pytest.fixture(scope="module")
def real_entries(clean_step, clean_source, tmp_path_factory) -> dict:
    """kind -> a real PoolEntry built from real inject() output, for every kind that applies to the fixture."""
    inject = rp.load_inject()
    out_dir = tmp_path_factory.mktemp("injected")
    entries = {}
    for kind in inject.KINDS:
        try:
            case = inject.inject(clean_step, kind, 1, out_dir)
        except (inject.InjectionNotApplicable, inject.InjectionInfeasible):
            continue
        entries[kind] = pool_mod.inject_entry_from_case(clean_source, case, url="gs://bucket/inj.tar")
    return entries


def _clean_named(entry):
    """The same real entry as a pool adapter might rename it: no kind string in the name."""
    import dataclasses
    renamed = dataclasses.replace(entry, name="clean_looking_part.step")
    assert rp.banned_kind_in(renamed.name) is None and rp.injected_kind(renamed.name) is None
    return renamed


def test_the_real_fixture_covers_both_structural_and_quarantined_kinds(real_entries) -> None:
    inject = rp.load_inject()
    assert set(real_entries) & set(inject.DEFAULT_KINDS) and set(real_entries) & set(inject.QUARANTINED_KINDS)


def test_inject_entries_carry_the_injectors_own_kind_and_status(real_entries) -> None:
    inject = rp.load_inject()
    for kind, entry in real_entries.items():
        assert isinstance(entry, pool_mod.PoolEntry)
        assert (entry.kind, entry.kind_status) == (kind, inject.KIND_STATUS[kind])


def test_default_kinds_are_what_the_gate_allows(real_entries) -> None:
    inject = rp.load_inject()
    assert set(inject.DEFAULT_KINDS).isdisjoint(inject.QUARANTINED_KINDS)
    structural = [e for k, e in real_entries.items() if k in inject.DEFAULT_KINDS]
    assert structural
    rp.check_not_quarantined(structural)
    rp.check_not_quarantined([_clean_named(e) for e in structural])


def test_a_quarantined_real_entry_is_refused_by_status_though_its_name_never_says_so(real_entries) -> None:
    """THE LEAK: real quarantined inject output, renamed so the name check cannot see it."""
    inject = rp.load_inject()
    quarantined = [e for k, e in real_entries.items() if k in inject.QUARANTINED_KINDS]
    assert quarantined
    for entry in quarantined:
        with pytest.raises(rp.QuarantinedKind):
            rp.check_not_quarantined([entry])
        with pytest.raises(rp.QuarantinedKind):
            rp.check_not_quarantined([_clean_named(entry)])


def test_a_quarantined_real_entry_is_refused_before_any_mesh_and_by_build_splits(layers, real_entries) -> None:
    inject = rp.load_inject()
    for kind, entry in real_entries.items():
        if kind not in inject.QUARANTINED_KINDS:
            continue
        member = FakeMember("gmsh_default")
        with pytest.raises(rp.QuarantinedKind):
            _probe([_clean_named(entry)], members=[member])
        assert member.runs == 0
        with pytest.raises(rp.QuarantinedKind):
            rp.build_splits([_row(1, "panel", "other", source="inject", name="clean_looking_part.step",
                                  kind=entry.kind, kind_status=entry.kind_status)], dev=1, heldout=0, clean=0)


@pytest.mark.parametrize("missing", ["kind", "kind_status", "both"])
def test_an_inject_entry_missing_its_declared_kind_or_status_is_refused(real_entries, missing) -> None:
    """Refused, not inferred from the name: even a structural kind whose name reads fine."""
    import dataclasses
    inject = rp.load_inject()
    entry = next(e for k, e in real_entries.items() if k in inject.DEFAULT_KINDS)
    gaps = {"kind": {"kind": None}, "kind_status": {"kind_status": None},
            "both": {"kind": None, "kind_status": None}}[missing]
    stripped = dataclasses.replace(entry, **gaps)
    assert rp.injected_kind(stripped.name) is not None   # the name WOULD have told the old check the kind
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([stripped])


def test_an_inject_entry_whose_status_contradicts_its_kind_is_refused(real_entries) -> None:
    import dataclasses
    inject = rp.load_inject()
    entry = next(e for k, e in real_entries.items() if k in inject.QUARANTINED_KINDS)
    with pytest.raises(rp.QuarantinedKind):   # a quarantined kind relabelled structural
        rp.check_not_quarantined([dataclasses.replace(entry, kind_status="structural")])


def test_the_name_check_stays_as_a_second_layer(real_entries) -> None:
    """A structural declaration on a file NAMED as a quarantined kind is still refused."""
    import dataclasses
    inject = rp.load_inject()
    entry = next(e for k, e in real_entries.items() if k in inject.DEFAULT_KINDS)
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([dataclasses.replace(entry, name="part.loose_reapprox.7.step")])


def test_the_probe_row_carries_the_declared_kind_and_status(layers, real_entries) -> None:
    inject = rp.load_inject()
    entry = next(e for k, e in real_entries.items() if k in inject.DEFAULT_KINDS)
    rows, _ = _probe([entry], members=[FakeMember("gmsh_default")])
    assert (rows[0].kind, rows[0].kind_status) == (entry.kind, entry.kind_status)
    assert rp.asdict(rows[0])["kind"] == entry.kind
    rp.check_not_quarantined(rows)


def test_write_pool_round_trips_the_declared_kind_and_leaves_other_rows_alone(real_entries, tmp_path) -> None:
    inject = rp.load_inject()
    inj = list(real_entries.values())
    plain = pool_mod.PoolEntry("p.step", "abc_fail", _sha(9), "u", None, "lic", None)
    path = tmp_path / "pool.json"
    pool_mod.write_pool([*inj, plain], path, sources_included=["inject"])
    back = {e.name: e for e in pool_mod.read_pool(path)}
    for e in inj:
        assert back[e.name] == e and back[e.name].kind in inject.KINDS
    assert back["p.step"] == plain and back["p.step"].kind is None
    rows = {r["name"]: r for r in json.loads(path.read_text())["files"]}
    assert "kind" not in rows["p.step"] and "kind_status" not in rows["p.step"]   # additive: not written when None


def test_a_pool_read_back_from_disk_still_refuses_a_quarantined_inject_entry(real_entries, tmp_path) -> None:
    """The real run_arm path: pool.json -> read_pool -> check_not_quarantined."""
    inject = rp.load_inject()
    entry = _clean_named(next(e for k, e in real_entries.items() if k in inject.QUARANTINED_KINDS))
    path = tmp_path / "pool.json"
    pool_mod.write_pool([entry], path, sources_included=["inject"])
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined(pool_mod.read_pool(path))


@pytest.mark.parametrize("source", ["abc_fail", "abc_pass", "occt", "nist", "f360"])
def test_a_non_inject_entry_with_no_kind_is_not_refused(source) -> None:
    rp.check_not_quarantined([pool_mod.PoolEntry("part.step", source, _sha(1), "u", None, "lic", None)])


def test_an_unknown_declared_kind_is_refused() -> None:
    entry = pool_mod.PoolEntry("part.step", "inject", _sha(1), "u", None, "lic", None, kind="not_a_kind",
                               kind_status="structural")
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([entry])


def test_an_injected_entry_with_no_declaration_is_refused() -> None:
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([pool_mod.PoolEntry("mystery.step", "inject", _sha(1), "u", None, "lic", None)])


# ------------------------------------------------------------------ the plan

def test_probe_pool_runs_the_full_verdict_on_every_file_and_never_the_cheap_tier(monkeypatch) -> None:
    """Amendment 2026-11-23 19:15: the cheap-first re-probe is dropped. The cheap tier passed 14 files of which 6
    failed the full panel, so a cheap pass cannot stand in for a verdict. Every file, pass or fail, no_solids or
    unbounded, gets the full verdict, once."""
    entries = [Entry(f"f{i}", _sha(i)) for i in range(1, 51)]
    calls: list[tuple[str, list[str]]] = []

    def fake_reprobe(ents, *, tier, excluded=None, **kw):  # noqa: ANN001, ANN202
        calls.append((tier, [e.sha256 for e in ents]))
        return [_row(i, None, "") for i, _ in enumerate(ents, start=1)]

    monkeypatch.setattr(rp, "reprobe", fake_reprobe)
    result = rp.probe_pool(entries, pool=None, resolve=str)
    assert calls == [("full", [e.sha256 for e in entries])]
    assert result.cheap == [] and len(result.full) == len(entries)
    assert result.final_rows() == result.full


def test_agreement_counts_the_cheap_tiers_misses() -> None:
    cheap = [_row(1, None, ""), _row(2, None, ""), _row(3, "panel", "other"), _row(4, "panel", "other")]
    full = [_row(1, None, ""), _row(2, "panel", "other"), _row(3, "panel", "other"), _row(4, None, "")]
    a = rp.agreement(cheap, full)
    assert (a["cheap_pass_full_pass"], a["cheap_pass_full_fail"], a["cheap_fail_full_fail"],
            a["cheap_fail_full_pass"]) == (1, 1, 1, 1)
    assert a["cheap_miss_rate_among_cheap_passes"] == 0.5 and a["agree"] == 0.5


# ------------------------------------------------------------------ splits

def _row(i, layer, family, source="abc_fail", name=None, group=None, tier="cheap", clean=False, extra=None,
         kind=None, kind_status=None):
    summary = {"tier": tier, "clean": clean, "counts_for_scoreboard": True, "hard": [family] if layer == "valid" else []}
    summary.update(extra or {})
    return rp.ProbeRow(name or f"f{i}.step", _sha(i), source, summary, layer, family, group, kind, kind_status)


def _population():
    rows, i = [], 0
    for layer, family, source, n in [
        ("panel", "plc_intersection", "abc_fail", 400), ("panel", "overlapping_facets", "abc_fail", 200),
        ("panel", "open_loop", "occt", 150), ("valid", "open_shell", "abc_fail", 100),
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
        rows.append(_row(i, None, "", "occt", tier="cheap"))  # a cheap pass: not certified clean
    for _ in range(40):
        i += 1
        rows.append(_row(i, "valid", "no_solids", "occt"))
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
    # (the population's panel rows carry no member list, so they sit at 0 failing members; key = layer/members/kind/source)
    assert splits.strata["dev"]["panel/0/geometry/abc_fail"] > 0
    assert splits.strata["heldout"]["panel/0/geometry/abc_fail"] > 0
    assert splits.strata["heldout"]["valid/-/open_shell/abc_fail"] > 0
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
    # 36 h, not 30: task 55 raised the per-file wall to 1800 s, which re-derived FULL_MIN_PER_FILE from 111 to
    # 132.4 s/file, and the whole 53,799-file pool on 96 shards now projects a 30.9 h wall. The test is about
    # the GATE (a sane wave passes, an unaffordable one is refused), so the sane wave's cap moves with the cost.
    real = evalspec.project(53_799, shards=96, max_run_hours=36)
    assert evalspec.check_budget(real) == []
    huge = evalspec.project(53_799, shards=96, max_run_hours=1000, full_min=40.0)
    reasons = evalspec.check_budget(huge)
    assert huge.vm_hours > evalspec.MAX_VM_HOURS and any("VM-hour ceiling" in r for r in reasons)
    with pytest.raises(evalspec.BudgetRefused):
        evalspec.enforce_budget(huge)
    assert evalspec.check_budget(huge, allow_over_budget=True) == []


def test_worst_case_bills_against_max_run_hours_not_average_wall() -> None:
    # expected VM-hours are small, but 12 VMs allowed to run 80 h each could bill 960
    p = evalspec.project(53_799, shards=96, max_run_hours=80)
    assert p.vm_hours < evalspec.MAX_VM_HOURS < p.worst_vm_hours
    assert any("worst-case" in r for r in evalspec.check_budget(p))


def test_skew_makes_the_slowest_shard_set_the_wall() -> None:
    flat = evalspec.project(53_799, shards=96, max_run_hours=99, skew=1.0)
    skewed = evalspec.project(53_799, shards=96, max_run_hours=99)
    assert skewed.wall_hours == pytest.approx(flat.wall_hours * evalspec.SKEW)
    tight = evalspec.project(53_799, shards=96, max_run_hours=flat.wall_hours * 1.2)
    assert any("deleted" in r for r in evalspec.check_budget(tight))  # fits balanced, not skewed


def test_override_does_not_waive_the_run_duration_or_vcpu_limits() -> None:
    short = evalspec.project(53_799, shards=96, max_run_hours=3)
    assert any("deleted" in r for r in evalspec.check_budget(short, allow_over_budget=True))
    wide = evalspec.project(53_799, shards=200, max_run_hours=30)
    assert any("vCPU" in r for r in evalspec.check_budget(wide, allow_over_budget=True))


def test_projection_formula() -> None:
    p = evalspec.project(600, shards=8, machine_type="c2d-standard-8", full_min=3.0, skew=2.0, max_run_hours=10)
    # full verdict on every file (no cheap tier): 600 * 3 / 60 = 30 vCPU-h
    assert (p.full_files, p.vms) == (600, 1)
    assert p.cpu_hours == pytest.approx(30.0) and p.wall_hours == pytest.approx(2 * 30.0 / 8)
    assert p.vm_hours == pytest.approx(2 * 30.0 / 8) and p.worst_vm_hours == 10


def test_the_default_minutes_are_lane_ds_measurement_at_the_current_file_wall() -> None:
    """132.4 s/file: lane D's measurement re-weighted to the pool's file sizes AT THE 1800 s per-file wall.
    The figure is a function of the wall (a higher wall lets the slow tail run longer), so it was re-derived
    from the same sample and the same size model when task 55 raised the wall; at the old 900 s wall it was
    111 s/file. If DEFAULT_FILE_TIMEOUT_S moves again, this has to be re-projected, not nudged."""
    assert evalspec.FULL_MIN_PER_FILE == pytest.approx(132.4 / 60)
    assert not hasattr(evalspec, "CHEAP_MIN_PER_FILE") and not hasattr(evalspec, "PASS_SAMPLE_FRACTION")


def test_launch_is_gated_and_never_reaches_the_creator_over_budget() -> None:
    created = []
    over = evalspec.project(53_799, shards=96, max_run_hours=3)
    with pytest.raises(evalspec.BudgetRefused):
        launch.launch(over, lambda: created.append(1))
    assert created == []
    ok = evalspec.project(53_799, shards=96, max_run_hours=36)
    launch.launch(ok, lambda: created.append(1))
    assert created == [1]


def test_create_flag_refuses_and_points_at_the_fleet_launcher(capsys) -> None:
    with pytest.raises(SystemExit, match="only preflights"):
        launch.main(["--files", "53799", "--shards", "96", "--max-run-hours", "36", "--create"])
    assert launch.main(["--files", "5000000", "--shards", "96", "--max-run-hours", "30", "--create"]) == 1


def test_launcher_preflight_prints_and_refuses(capsys) -> None:
    assert launch.main(["--files", "53799", "--shards", "96", "--max-run-hours", "36"]) == 0
    assert "PROJECTED" in capsys.readouterr().out
    assert launch.main(["--files", "5000000", "--shards", "96", "--max-run-hours", "30"]) == 1
    err = capsys.readouterr().err
    assert "REFUSE" in err and "ceiling" in err
    assert launch.main(["--files", "5000000", "--shards", "96", "--max-run-hours", "9999",
                        "--allow-over-budget"]) == 0


# ------------------------------------------------------------------ fail-closed defaults and new gates

def test_a_row_with_no_counts_for_scoreboard_key_is_not_counted() -> None:
    row = rp.ProbeRow("a", _sha(1), "abc_fail", {"tier": "full"}, "panel", "other")
    assert rp.is_counted(row) is False


@pytest.mark.parametrize("source", ["abc_fail", "abc_pass", "occt", "nist", "f360"])
def test_a_quarantined_kind_is_refused_under_any_source_label(source) -> None:
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([Entry("abc_0001.seam_variant.3.step", _sha(1), source)])
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([Entry("renamed.step", _sha(1), source, group="x-tolerance_inflate-1")])
    rp.check_not_quarantined([Entry("00500400_seamless.step", _sha(2), source)])  # substring-safe


def test_run_arm_checks_the_whole_pool_before_sharding(tmp_path) -> None:
    pool_json = tmp_path / "pool.json"
    pool_mod = run_arm.pool_mod
    entries = [pool_mod.PoolEntry("p.loose_reapprox.1.step", "occt", _sha(1), "u", None, "lic")]
    pool_mod.write_pool(entries, pool_json, sources_included=["occt"])
    with pytest.raises(rp.QuarantinedKind):
        run_arm.run_reprobe(
            run_arm.build_parser().parse_args(["--pool", str(pool_json), "--inputs", "/i"]), tmp_path, {})


def test_collect_refuses_short_splits_and_big_exclusions(tmp_path, capsys) -> None:
    src, out = tmp_path / "w", tmp_path / "out"
    _shard(src, 1, 1, [_row(i, "panel", "other") for i in range(1, 30)])
    args = ["--from", str(src), "--no-pool-check", "--out-dir", str(out), "--dev", "5", "--heldout", "5", "--clean", "10"]
    assert collect.main(args) == 2  # no clean files at all: the clean split is short
    assert "splits are short" in capsys.readouterr().err and not out.exists()
    assert collect.main([*args, "--allow-shortfall"]) == 0 and (out / "clean.json").is_file()
    assert collect.main(["--from", str(src), "--no-pool-check", "--dev", "500"]) == 2  # failing set smaller than asked
    # 3 of 32 = 9% excluded
    big = tmp_path / "x"
    ex = [rp.Excluded(f"e{i}", _sha(900 + i), "abc_fail", "file_timeout: 900s") for i in range(3)]
    _shard(big, 1, 1, [_row(i, "panel", "other") for i in range(1, 30)], excluded=ex)
    assert collect.main(["--from", str(big), "--no-pool-check", "--out-dir", str(tmp_path / "o2"), "--dev", "5", "--heldout", "5",
                         "--clean", "0"]) == 2
    assert "excluded" in capsys.readouterr().err
    assert collect.main(["--from", str(big), "--no-pool-check", "--out-dir", str(tmp_path / "o2"), "--dev", "5", "--heldout", "5",
                         "--clean", "0", "--allow-excluded"]) == 0


def test_clean_split_prefers_genuine_cheap_passes_and_reports_the_marginal_top_up() -> None:
    genuine = [_row(i, None, "", "abc_pass", tier="full", clean=True) for i in range(1, 11)]
    marginal = [_row(i, None, "", "abc_pass", tier="full", clean=True) for i in range(11, 21)]
    failing = [_row(i, "panel", "other") for i in range(100, 120)]
    cf = {r.sha256 for r in marginal}
    s = rp.build_splits(genuine + marginal + failing, dev=5, heldout=5, clean=8, cheap_failed=cf)
    assert {r.sha256 for r in s.clean} <= {r.sha256 for r in genuine} and s.strata["clean_from_cheap_fail"]["count"] == 0
    s = rp.build_splits(genuine + marginal + failing, dev=5, heldout=5, clean=15, cheap_failed=cf)
    assert len(s.clean) == 15 and s.strata["clean_from_cheap_fail"]["count"] == 5


# ------------------------------------------------------------------ source allow-list (Fusion 360 licence hold)

def test_f360_is_not_allowed_and_the_list_is_pluggable() -> None:
    assert "f360" not in rp.ALLOWED_SOURCES
    rp.check_sources([Entry("a", _sha(1), "f360")], allowed=rp.ALLOWED_SOURCES | {"f360"})  # opt-in later


def test_reprobe_refuses_an_f360_entry_before_any_mesh(layers) -> None:
    member = FakeMember("gmsh_default")
    with pytest.raises(rp.DisallowedSource):
        _probe([Entry("a", _sha(1), "abc_fail"), Entry("b", _sha(2), "f360")], members=[member])
    assert member.runs == 0


def test_build_splits_refuses_an_f360_row() -> None:
    rows = [_row(i, "panel", "other") for i in range(1, 12)] + [_row(99, "panel", "other", source="f360")]
    with pytest.raises(rp.DisallowedSource):
        rp.build_splits(rows, dev=3, heldout=2, clean=0)


def _run_arm_drop_f360(tmp_path, monkeypatch, extra_args=()):
    """Run run_reprobe over a pool of one ABC and one F360 entry, with the probe stubbed to record what it was
    handed and stop. The args come from the real parser, so every flag run_reprobe reads is present."""
    pool_mod = run_arm.pool_mod
    entries = [pool_mod.PoolEntry("a.step", "abc_fail", _sha(1), "u", None, "lic"),
               pool_mod.PoolEntry("f.step", "f360", _sha(2), "u", None, "lic")]
    pool_json = tmp_path / "pool.json"
    pool_mod.write_pool(entries, pool_json, sources_included=["abc_fail", "f360"])
    seen: dict = {}

    def fake_probe(entries, **kw):
        seen["entries"] = list(entries)
        raise RuntimeError("stop")

    monkeypatch.setattr(rp, "probe_pool", fake_probe)
    monkeypatch.setattr("meshpipeline.cad.repair_v4.worker_pool.WorkerPool", lambda n: type("P", (), {"close": lambda s: None})())
    prov: dict = {}
    args = run_arm.build_parser().parse_args(["--pool", str(pool_json), "--inputs", "/i", *extra_args])
    with pytest.raises(RuntimeError, match="stop"):
        run_arm.run_reprobe(args, tmp_path, prov)
    assert "entries" in seen, "the probe was never reached: the run raised before the drop path finished"
    return seen["entries"], prov


def test_run_arm_drops_f360_from_the_manifest_visibly(tmp_path, monkeypatch, capsys) -> None:
    entries, prov = _run_arm_drop_f360(tmp_path, monkeypatch)
    assert [e.source for e in entries] == ["abc_fail"]                      # the F360 entry never reaches the probe
    assert prov["pool_dropped_by_source"] == {"f360": 1} and prov["pool_listed"] == 2   # recorded in PROVENANCE
    assert "dropped {'f360': 1}" in capsys.readouterr().err                 # and printed, not silent


def test_run_arm_refuses_f360_even_when_the_operator_names_it(tmp_path, monkeypatch) -> None:
    # --sources f360 must not re-admit it: select_sources refuses the uncleared label before the probe runs
    probed: list = []
    monkeypatch.setattr(rp, "probe_pool", lambda entries, **kw: probed.append(list(entries)))
    pool_mod = run_arm.pool_mod
    pool_json = tmp_path / "pool.json"
    pool_mod.write_pool([pool_mod.PoolEntry("f.step", "f360", _sha(2), "u", None, "lic")], pool_json,
                        sources_included=["f360"])
    args = run_arm.build_parser().parse_args(["--pool", str(pool_json), "--sources", "f360"])
    with pytest.raises(ValueError, match="not cleared"):
        run_arm.run_reprobe(args, tmp_path, {})
    assert probed == []


def test_run_arm_parser_carries_every_flag_run_reprobe_reads() -> None:
    import re
    src = Path(run_arm.__file__).read_text()
    read = set(re.findall(r"\bargs\.([a-z_]+)", src))
    have = set(vars(run_arm.build_parser().parse_args([])))
    assert read <= have, f"run_arm reads args that the parser does not define: {sorted(read - have)}"


def test_launcher_counts_only_allowed_sources(tmp_path, capsys) -> None:
    pool_mod = run_arm.pool_mod
    entries = [pool_mod.PoolEntry(f"a{i}.step", "abc_fail", _sha(i), "u", None, "lic") for i in range(1, 4)]
    entries += [pool_mod.PoolEntry(f"f{i}.step", "f360", _sha(100 + i), "u", None, "lic") for i in range(1, 8)]
    pool_json = tmp_path / "pool.json"
    pool_mod.write_pool(entries, pool_json, sources_included=["abc_fail", "f360"])
    assert launch.main(["--pool", str(pool_json), "--shards", "8", "--max-run-hours", "5"]) == 0
    assert capsys.readouterr().out.startswith("3 files")


# ---- Task 39: the declined stratum is ADMITTED by the gate, as itself, and only as itself -----------------------------

def _inject_entry(name: str, kind: str | None, status: str | None):
    pool_mod = run_arm.pool_mod
    return pool_mod.PoolEntry(name, "inject", _sha(7), "u", None, "lic", "inject-abc", kind=kind, kind_status=status)


def test_the_declined_stratum_passes_the_gate_when_it_declares_itself() -> None:
    rp.check_not_quarantined([_inject_entry("p.face_delete_large.1.step", "face_delete_large", "declined_stratum")])
    # ... and the quarantine still bites, in the same call, so the stratum did not widen the gate to everything
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([_inject_entry("p.seam_variant.1.step", "seam_variant", "quarantined")])


@pytest.mark.parametrize("kind,status", [
    ("face_delete_large", "structural"),      # the stratum relabelled as a plain failing file
    ("face_delete", "declined_stratum"),     # a plain failing file relabelled as the stratum, to hide it from the rate
    ("face_delete_large", None), (None, "declined_stratum"),
])
def test_a_stratum_label_that_is_not_the_injectors_own_is_refused(kind, status) -> None:
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([_inject_entry("p.face_delete_large.1.step", kind, status)])


def test_a_status_the_injector_never_assigned_is_still_refused() -> None:
    with pytest.raises(rp.QuarantinedKind):
        rp.check_not_quarantined([_inject_entry("p.face_delete.1.step", "face_delete", "declined")])


def test_face_delete_large_is_not_a_default_draw_but_is_an_admitted_kind() -> None:
    inject = rp.load_inject()
    assert "face_delete_large" not in inject.DEFAULT_KINDS and "face_delete_large" in inject.ADMITTED_KINDS
    assert inject.KIND_STATUS["face_delete_large"] == "declined_stratum"
    assert inject.EXPECTED_OUTCOME == {"face_delete_large": "correctly_declined",
                                       "face_delete_inclined": "correctly_declined"}
    assert "face_delete_large" not in rp.not_default_kinds()
    assert rp.injected_kind("part.face_delete_large.3.step") == "face_delete_large"   # not mistaken for face_delete


@pytest.mark.parametrize("status", ["quarantined", "declined", "infeasible"])
def test_an_entry_of_any_source_carrying_a_status_the_gate_does_not_admit_is_refused(status) -> None:
    """The own-status branch, reached with NO kind to catch it first: a status that is neither `structural` nor
    `declined_stratum` is refused whatever the entry's source or name."""
    from types import SimpleNamespace

    item = SimpleNamespace(name="plain.step", source="abc_pass", group=None, label={"status": status})
    with pytest.raises(rp.QuarantinedKind, match="own kind status"):
        rp.check_not_quarantined([item])
    rp.check_not_quarantined([SimpleNamespace(name="plain.step", source="abc_pass", group=None,
                                              label={"status": "declined_stratum"})])


def test_unbounded_inputs_are_counted_but_never_a_failure_and_are_tallied_alone() -> None:
    rows = [_row(i, "panel", "other") for i in range(2, 12)]
    unbounded = [_row(100 + i, "valid", "unbounded_geometry", extra={"out_of_scope": True}) for i in range(7)]
    splits = rp.build_splits([*rows, *unbounded], dev=3, heldout=2, clean=0)
    ids = {r.sha256 for r in splits.dev + splits.heldout + splits.clean}
    assert not [r for r in unbounded if r.sha256 in ids]
    assert splits.excluded_unbounded == 7 and splits.excluded_uncounted == 0 and splits.excluded_no_solids == 0
    assert all(rp.is_unbounded(r) for r in unbounded) and not any(rp.is_unbounded(r) for r in rows)
    assert "unbounded_geometry" not in splits.strata["failing_population"]


def test_an_unbounded_input_is_a_counted_row_with_its_own_family_not_an_exclusion(layers) -> None:
    """Neither `excluded` (that is for what was NOT measured and would trip the excluded-fraction limit) nor a
    panel failure: a row whose family says what it is, and which no mesher ran for."""
    layers["valid"] = ValidReport(False, [Finding("unbounded_geometry")], [], "mm", 1, False)
    member = FakeMember("gmsh_default")
    rows, excluded = _probe([Entry("a", _sha(1))], members=[member])
    assert excluded == [] and member.runs == 0
    (row,) = rows
    assert (row.first_failing_layer, row.family) == ("valid", "unbounded_geometry")
    assert row.verdict_summary["counts_for_scoreboard"] is True and row.verdict_summary["out_of_scope"] is True
    assert row.verdict_summary["clean"] is False and rp.is_unbounded(row) and rp.is_counted(row)


# ------------------------------------------------------------------ panel family (Task 48)
# Task 43 measured 33 panel failures, 31 labelled `other`: gmsh_prod's "HXT 3D mesh failed" was unknown
# to `failure_family` and the label was the FIRST failing member's, almost always gmsh_prod.

def _m(name, status, kind, message=""):
    return SimpleNamespace(name=name, status=status, error_kind=kind, message=message)


def _panel_verdict(*members):
    return SimpleNamespace(valid=SimpleNamespace(ok=True, hard=[]), panel=list(members), faithful=None)


def test_hxt_failure_text_is_a_family_not_other():
    v = _panel_verdict(_m("gmsh_prod", "fail", "other", "HXT 3D mesh failed (code 2)"), _m("gmsh_default", "pass", None))
    assert rp.family_of(v, "panel") == "hxt_3d_failed"


def test_the_label_does_not_depend_on_which_member_failed_first():
    prod = _m("gmsh_prod", "fail", "low_quality", "minimum SICN 0.003 is below 0.01")
    dflt = _m("gmsh_default", "fail", "overlapping_facets", "Invalid boundary mesh (overlapping facets)")
    assert rp.family_of(_panel_verdict(prod, dflt), "panel") == "overlapping_facets"
    assert rp.family_of(_panel_verdict(dflt, prod), "panel") == "overlapping_facets"


def test_a_named_geometric_cause_outranks_a_quality_threshold_and_an_unexplained_failure():
    members = [("fail", "other", ""), ("fail", "low_quality", ""), ("fail", "edge_through_face", "")]
    assert rp.panel_family(members) == "edge_through_face"
    assert rp.panel_family(members[:2]) == "low_quality"


def test_other_is_the_label_only_when_no_failing_member_said_anything_more():
    assert rp.panel_family([("fail", "other", "something odd"), ("pass", None, "")]) == "other"
    assert rp.panel_family([("fail", "other", ""), ("fail", "low_quality", "")]) == "low_quality"


def test_a_timeout_or_crash_is_timeout_crash_unless_a_better_cause_is_named():
    assert rp.panel_family([("crashed", "netgen_crash", "")]) == "timeout_crash"
    assert rp.panel_family([("crashed", "netgen_crash", ""), ("fail", "periodic_surface", "")]) == "periodic_surface"


def test_task43_panel_failures_no_longer_collapse_into_one_stratum():
    """The 33 measured panel failures, as (member error_kinds): at most 1 stays `other`."""
    shapes = ([("low_quality",)] * 9 + [("low_quality", "other", "overlapping_facets")] * 5
              + [("edge_through_face", "netgen_meshing_failed", "no_volume", "other")] * 3
              + [("low_quality", "other")] * 3 + [("low_quality", "netgen_meshing_failed")] * 2
              + [("no_volume", "periodic_surface")] + [("other",)])
    families = [rp.panel_family([("fail", k, "") for k in kinds]) for kinds in shapes]
    assert families.count("other") == 1
    assert len(set(families)) >= 5


def _panel_row(i, family, statuses, source="abc_fail"):
    panel = [{"name": f"m{j}", "status": s, "error_kind": None, "min_sicn": None} for j, s in enumerate(statuses)]
    return rp.ProbeRow(f"p{i}.step", _sha(i), source, {"tier": "full", "clean": False, "counts_for_scoreboard": True,
                                                       "hard": [], "panel": panel}, "panel", family)


def test_stratum_separates_a_file_failing_one_member_from_one_failing_four():
    one = _panel_row(1, "low_quality", ["fail", "pass", "pass", "pass"])
    four = _panel_row(2, "low_quality", ["fail", "fail", "fail", "fail"])
    assert rp.failing_members(one) == 1 and rp.failing_members(four) == 4
    assert rp._stratum(one) != rp._stratum(four)
    assert rp._stratum(one) == ("panel", "1", "quality", "abc_fail")


def test_stratum_keeps_source_and_buckets_kinds_coarsely():
    assert rp._stratum(_panel_row(1, "overlapping_facets", ["fail"], "occt")) == ("panel", "1", "geometry", "occt")
    assert rp._stratum(_panel_row(2, "hxt_3d_failed", ["fail"]))[2] == "mesher"
    valid = _row(3, "valid", "round_trip_invalid")
    assert rp._stratum(valid) == ("valid", "-", "valid_other", "abc_fail")
    assert rp._stratum(_row(4, "valid", "body_overlap"))[2] == "body_overlap"



# ------------------------------------------------------------------ the form the checker judges

def test_the_reprobe_judges_the_file_as_the_engine_reads_it(layers) -> None:
    """Task 49's flip, root-caused by lane D (panel_repro.py, 11 near-threshold files x 2 forms): the panel is
    bit-identical run to run, but the ORIGINAL STEP and the STEP the engine re-exports from its BREP are different
    geometry to a mesher - gmsh_alt failed the original at min SICN 0.00417 and passed the re-export at 0.0494, and
    4 of 11 near-threshold files changed pass/fail between the two forms, in both directions. Every arm and the
    shipping engine judge the re-export; the re-probe that cuts the split labels must judge the same thing."""
    meshed: list[str] = []

    class Seen(FakeMember):
        def run(self, pool, step_path, *, timeout_s):
            meshed.append(step_path)
            return super().run(pool, step_path, timeout_s=timeout_s)

    rows, _ = _probe([Entry("a", _sha(1))], tier="full", members=[Seen("gmsh_alt")],
                     engine_form=lambda pool, step: step + ".engine.step")
    assert meshed == ["/x/a.engine.step"] and len(rows) == 1


def test_a_file_the_engine_cannot_read_is_excluded_not_judged(layers) -> None:
    def unreadable(pool, step):
        raise CheckerUnavailable("read_step_xcaf failed")

    rows, excluded = _probe([Entry("a", _sha(1))], tier="full", members=[FakeMember("gmsh_alt")],
                            engine_form=unreadable)
    assert rows == [] and excluded and excluded[0].reason.startswith("checker_unavailable")


def test_the_default_form_is_the_engines() -> None:
    import inspect

    from meshpipeline.cad.repair_v4 import search

    assert inspect.signature(rp.reprobe).parameters["engine_form"].default is None
    assert rp.default_engine_form() is search.engine_form


def test_a_reprobe_verdict_that_finished_past_its_file_wall_is_excluded_not_a_row(monkeypatch, tmp_path) -> None:
    import signal

    import meshpipeline.cad.repair_v4.verdict as verdict_pkg

    def slow_but_fine(pool, step, original, **kw):
        signal.setitimer(signal.ITIMER_REAL, 0)  # the alarm is lost
        time.sleep(0.5)
        return SimpleNamespace(counts_for_scoreboard=True, panel=[1], out_of_scope=False)

    entry = SimpleNamespace(name="a.step", sha256="a" * 64, source="abc_fail", group=None, kind=None, kind_status=None)
    monkeypatch.setattr(verdict_pkg, "verdict", slow_but_fine)
    excluded: list = []
    rows = rp.reprobe([entry], tier="full", pool=None, resolve=lambda e: str(tmp_path / "a.step"), excluded=excluded,
                      file_timeout_s=0.2, engine_form=lambda pool, step: step)
    assert rows == [] and excluded[0].reason.startswith("file_timeout: finished")
    assert rp.overran(0.0, 0.0, now=99.0) is None and rp.overran(0.0, 100.0, now=99.0) is None
    assert rp.overran(0.0, 10.0, now=99.0).startswith("file_timeout")
