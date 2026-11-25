# Responsibility: Verify Verdict assembly, `score` and the verdict cache without a kernel or a mesher.
#                 Layer 2 is a fake member that satisfies the PanelMember protocol; layers 1 and 3
#                 are replaced where the test is about how they are combined, not what they find.
# Boundaries: hermetic. What layer 3 measures is proven natively in
#             tests/native/test_native_repair_v4_faithful.py.
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict.assemble import (
    Declared,
    InconclusiveVerdict,
    Verdict,
    cheap_verdict,
    score,
    verdict,
)
from meshpipeline.cad.repair_v4.verdict.cache import (
    VERDICT_CODE_VERSION,
    DirStore,
    PanelConfigError,
    VerdictCache,
    file_sha256,
    mesher_versions,
    panel_config_hash,
)
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport, check_faithful
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult


@dataclass(frozen=True)
class FakeResult:
    name: str
    status: str
    min_sicn: float | None = None
    located: list = field(default_factory=list)
    error_kind: str | None = None


@dataclass
class FakeMember:
    name: str
    status: str = "pass"
    sicn: float | None = 0.3
    runs: int = 0
    error_kind: str | None = None
    config: dict = field(default_factory=dict)

    def run(self, pool, step_path, *, timeout_s):
        self.runs += 1
        return FakeResult(self.name, self.status, self.sicn, error_kind=self.error_kind)


def run_panel(pool, step, members, *, timeout_s):
    return [m.run(pool, step, timeout_s=timeout_s) for m in members]


def _valid(ok: bool = True) -> ValidReport:
    return ValidReport(ok, [], [], "mm", 1, True)


def _faith(ok: bool = True, dev: float = 0.0, added=(), removed=()) -> FaithfulReport:
    return FaithfulReport(ok, dev, 0.1, 1.0, 1.0, 1, 1, list(added), list(removed))


def _v(valid=True, results=(), faithful=None, tier="full") -> Verdict:
    return Verdict(_valid(valid), list(results), faithful, False, tier)


def _passes(n: int, sicn: float = 0.3) -> list[FakeResult]:
    return [FakeResult(f"m{i}", "pass", sicn) for i in range(n)]


def test_score_orders_valid_first() -> None:
    invalid_but_meshes = _v(False, _passes(3, 0.9), _faith(dev=0.0))
    valid_but_fails = _v(True, [FakeResult("m0", "fail", None)], _faith(dev=0.09))
    assert score(valid_but_fails) > score(invalid_but_meshes)


def test_score_counts_panel_passes() -> None:
    two = _v(True, [*_passes(2, 0.5), FakeResult("m2", "fail", None)], _faith())
    three = _v(True, _passes(3, 0.02), _faith())
    assert score(three) > score(two)
    assert score(two)[1] == 2 and score(three)[1] == 3


def test_score_breaks_ties_by_sicn_then_deviation_then_churn() -> None:
    base = _v(True, _passes(2, 0.30), _faith(dev=0.05, added=["F1"]))
    better_sicn = _v(True, _passes(2, 0.40), _faith(dev=0.09, added=["F1", "F2"]))
    closer = _v(True, _passes(2, 0.30), _faith(dev=0.01, added=["F1", "F2"]))
    quieter = _v(True, _passes(2, 0.30), _faith(dev=0.05))
    assert score(better_sicn) > score(base)  # SICN outranks deviation and churn
    assert score(closer) > score(base)  # deviation outranks churn
    assert score(quieter) > score(base)  # fewer edits wins a full tie


def test_score_uses_the_worst_member_sicn_and_ranks_unmeshed_lowest() -> None:
    mixed = _v(True, [FakeResult("a", "pass", 0.5), FakeResult("b", "pass", 0.05)], _faith())
    assert score(mixed)[2] == 0.05
    unmeshed = _v(True, [FakeResult("a", "fail", None)], _faith())
    assert score(unmeshed)[2] < 0.0


def test_score_tuple_shape() -> None:
    s = score(_v(True, _passes(1), _faith(dev=0.04, added=["F1"], removed=["F2"])))
    assert s == (1, 1, 0.3, -0.04, -2)


@pytest.fixture
def layers(monkeypatch):
    """Replace layers 1 and 3; hand back the knobs and a record of how layer 3 was called."""
    state = {"valid": True, "faithful": _faith(), "faithful_calls": []}

    def fake_valid(pool, step, *, original_units):
        if isinstance(state["valid"], Exception):
            raise state["valid"]
        return _valid(bool(state["valid"]))

    def fake_faithful(pool, original, step, *, declared_added, declared_removed):
        state["faithful_calls"].append((original, step, declared_added, declared_removed))
        return state["faithful"]

    monkeypatch.setattr(assemble, "check_valid", fake_valid)
    monkeypatch.setattr(assemble, "check_faithful_steps", fake_faithful)
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "4.15.2"})
    return state


def _run(layers_state, members, **kwargs) -> Verdict:
    return verdict(None, "s.step", "o.step", members=members, run_panel=run_panel, **kwargs)  # type: ignore[arg-type]


def test_clean_requires_valid_and_the_whole_panel(layers) -> None:
    """Owner decision 2026-11-24: the two layers that gate are 1 and 2."""
    members = [FakeMember("a"), FakeMember("b")]
    assert _run(layers, members).clean is True

    layers["valid"] = False
    assert _run(layers, members).clean is False
    layers["valid"] = True

    assert _run(layers, [FakeMember("a"), FakeMember("b", status="fail", sicn=None)]).clean is False

    # layer 3 is measured and reported, and does not gate: an unfaithful output is still clean
    layers["faithful"] = _faith(ok=False, dev=0.5)
    v = _run(layers, members)
    assert v.clean is True and v.faithful is not None and v.faithful.ok is False


def test_an_invalid_file_is_still_measured_against_the_original(layers) -> None:
    """Reported on EVERY row: the fidelity numbers of an invalid output still say what the repair did."""
    layers["valid"] = False
    v = _run(layers, [FakeMember("a")])
    assert layers["faithful_calls"] == [("o.step", "s.step", [], [])]
    assert v.faithful is not None and v.clean is False


def test_declared_edits_reach_layer_3(layers) -> None:
    _run(layers, [FakeMember("a")], declared=Declared(added=["F3"], removed=["F9"]))
    assert layers["faithful_calls"] == [("o.step", "s.step", ["F3"], ["F9"])]


def test_cheap_verdict_runs_one_member_and_is_never_clean(layers, monkeypatch) -> None:
    class Cheap(FakeMember):
        pass

    monkeypatch.setattr(assemble, "default_members", lambda tier: [Cheap("gmsh_default")] if tier == "cheap" else [])
    v = cheap_verdict(None, "s.step", "o.step", run_panel=run_panel)  # type: ignore[arg-type]
    assert v.tier == "cheap" and [r.name for r in v.panel] == ["gmsh_default"]
    assert v.valid.ok and v.faithful is not None and v.faithful.ok and v.panel[0].status == "pass"
    assert v.clean is False


def test_checker_unavailable_is_not_an_unclean_verdict(layers) -> None:
    layers["valid"] = CheckerUnavailable("read_step_xcaf: timeout")
    with pytest.raises(CheckerUnavailable):
        _run(layers, [FakeMember("a")])


def test_layer_3_infrastructure_failure_is_not_a_finding() -> None:
    class Pool:
        def call(self, name, payload, *, timeout_s):
            return CallResult(False, None, "worker died", "crashed", 0.1)

    with pytest.raises(CheckerUnavailable):
        check_faithful(Pool(), "a.brep", "b.brep", declared_added=[], declared_removed=[])  # type: ignore[arg-type]


# ---- the cache ------------------------------------------------------------------------------


def _fake_factory(data: dict) -> FakeResult:
    return FakeResult(data["name"], data["status"], data["min_sicn"], data["located"], data["error_kind"])


@pytest.fixture
def step(tmp_path: Path) -> str:
    path = tmp_path / "part.step"
    path.write_text("ISO-10303-21; not really a step file")
    return str(path)


def _cache(tmp_path: Path) -> VerdictCache:
    return VerdictCache(DirStore(tmp_path / "bucket"), result_factory=_fake_factory)


def test_cache_hit_never_runs_a_mesher(layers, step, tmp_path) -> None:
    cache = _cache(tmp_path)
    members = [FakeMember("a"), FakeMember("b", status="fail", sicn=None)]
    first = verdict(None, step, "o.step", members=members, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    assert [m.runs for m in members] == [1, 1]
    second = verdict(None, step, "o.step", members=members, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    assert [m.runs for m in members] == [1, 1]  # served from the cache
    assert [r.status for r in second.panel] == [r.status for r in first.panel] == ["pass", "fail"]


def test_cache_misses_on_a_code_version_change(step, tmp_path) -> None:
    cache = _cache(tmp_path)
    sha, cfg = file_sha256(step), "cfg"
    assert cache.put(sha, cfg, _passes(2), version="crs-v4.verdict.old") is True
    assert cache.get(sha, cfg, version="crs-v4.verdict.old") is not None
    assert cache.get(sha, cfg) is None  # current VERDICT_CODE_VERSION differs: a miss
    assert VERDICT_CODE_VERSION != "crs-v4.verdict.old"


def test_cache_rejects_a_record_that_does_not_carry_the_version_even_under_the_right_key(step, tmp_path) -> None:
    cache = _cache(tmp_path)
    sha, cfg = file_sha256(step), "cfg"
    cache.put(sha, cfg, _passes(1))
    key = cache.key(sha, cfg)
    text = cache.store.get(key)
    assert text is not None
    cache.store.put(key, text.replace(VERDICT_CODE_VERSION, "something-else"))
    assert cache.get(sha, cfg) is None


def test_cache_misses_on_changed_file_or_panel(layers, step, tmp_path) -> None:
    cache = _cache(tmp_path)
    members = [FakeMember("a")]
    verdict(None, step, "o.step", members=members, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    other = [FakeMember("a"), FakeMember("b")]  # a different panel
    verdict(None, step, "o.step", members=other, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    assert [m.runs for m in other] == [1, 1]
    Path(step).write_text("changed bytes")
    again = [FakeMember("a")]
    verdict(None, step, "o.step", members=again, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    assert again[0].runs == 1


def test_timeout_and_crash_are_never_cached(layers, step, tmp_path) -> None:
    cache = _cache(tmp_path)
    flaky = [FakeMember("a", status="timeout", sicn=None)]
    verdict(None, step, "o.step", members=flaky, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    verdict(None, step, "o.step", members=flaky, run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    assert flaky[0].runs == 2


def test_panel_config_hash_tracks_members_config_versions_and_timeout() -> None:
    v = {"gmsh": "4.15.2"}
    a, b = FakeMember("a"), FakeMember("b")
    assert panel_config_hash([a, b], 300, v) == panel_config_hash([b, a], 300, v)
    assert panel_config_hash([a], 300, v) != panel_config_hash([a, b], 300, v)
    assert panel_config_hash([a], 300, v) != panel_config_hash([a], 120, v)
    assert panel_config_hash([a], 300, v) != panel_config_hash([FakeMember("a", config={"Mesh.Algorithm": 6})], 300, v)
    assert panel_config_hash([a], 300, v) != panel_config_hash([a], 300, {"gmsh": "4.16.0"})  # a mesher upgrade


def test_a_member_without_config_fails_closed() -> None:
    class NoConfig:
        name = "x"

    with pytest.raises(PanelConfigError):
        panel_config_hash([NoConfig()], 300, {})


def test_cache_miss_is_logged_with_its_reason(step, tmp_path, caplog) -> None:
    def broken(data: dict):
        raise AttributeError("module has no attribute 'Located'")

    cache = _cache(tmp_path)
    cache.put(file_sha256(step), "cfg", _passes(1))
    bad = VerdictCache(cache.store, result_factory=broken)
    with caplog.at_level("WARNING"):
        assert bad.get(file_sha256(step), "cfg") is None
    assert "AttributeError" in caplog.text and "Located" in caplog.text


def test_pool_failure_is_inconclusive_but_a_mesher_crash_is_a_verdict(layers) -> None:
    pool_died = _run(
        layers, [FakeMember("a"), FakeMember("b", status="crashed", sicn=None, error_kind="infrastructure")]
    )
    assert pool_died.inconclusive and not pool_died.clean and not pool_died.counts_for_scoreboard
    with pytest.raises(InconclusiveVerdict):
        score(pool_died)

    for status in ("crashed", "timeout"):  # the mesher itself: the file failed the definition
        mesher = _run(layers, [FakeMember("a"), FakeMember("b", status=status, sicn=None, error_kind="segfault")])
        assert not mesher.inconclusive and mesher.counts_for_scoreboard and not mesher.clean
        assert score(mesher)[1] == 1


def test_an_inconclusive_panel_is_never_cached(layers, step, tmp_path) -> None:
    cache = _cache(tmp_path)
    member = FakeMember("a", status="crashed", sicn=None, error_kind="infrastructure")
    verdict(None, step, "o.step", members=[member], run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    verdict(None, step, "o.step", members=[member], run_panel=run_panel, cache=cache)  # type: ignore[arg-type]
    assert member.runs == 2


def test_a_state_that_meshes_in_more_members_outranks_however_far_it_moved_the_part() -> None:
    """Owner decision 2026-11-24 (B12): the goal is VALID + MESHABLE. A valid state that meshes in 3 members beats one
    that meshes in 1, even if it moved the surface by 25 mm and layer 3 calls it unfaithful. (This test used to pin the
    opposite: an unfaithful state never outranked a faithful one.)"""
    faithful = _v(True, _passes(1, 0.05), _faith(ok=True, dev=0.09))
    moved = _v(True, _passes(3, 0.9), _faith(ok=False, dev=25.0))      # e.g. area_changed or fill_too_large
    assert score(moved) > score(faithful)
    assert score(moved)[0] == score(faithful)[0] == 1                 # key 0 is validity alone


def test_deviation_only_breaks_ties_on_the_goal_keys() -> None:
    small = _v(True, _passes(2, 0.3), _faith(ok=True, dev=0.01))
    large = _v(True, _passes(2, 0.3), _faith(ok=False, dev=5.0))
    assert score(small) > score(large)                                # equal goal keys: the smaller move wins
    better_mesh = _v(True, _passes(2, 0.31), _faith(ok=False, dev=5.0))
    assert score(better_mesh) > score(small)                          # a better min SICN beats any deviation
    assert score(_v(False, _passes(4, 0.9), _faith(ok=True, dev=0.0))) < score(large)   # invalid still loses


def test_declared_default_is_not_shared_between_calls(layers) -> None:
    _run(layers, [FakeMember("a")])
    assert Declared().added == [] and Declared().removed == []
    assert Declared() is not Declared()


def test_a_member_with_config_none_fails_closed() -> None:
    with pytest.raises(PanelConfigError):
        panel_config_hash([FakeMember("a", config=None)], 300, {})  # type: ignore[arg-type]


def test_mesher_versions_pool_failure_is_checker_unavailable() -> None:
    class Pool:
        def call(self, name, payload, *, timeout_s):
            return CallResult(False, None, "worker died", "crashed", 0.1)

    with pytest.raises(CheckerUnavailable):
        mesher_versions(Pool())


def test_a_zero_baseline_still_flags_any_growth_and_is_json_safe() -> None:
    """The ratio check must not go silent when the original has no area or volume: a zero-volume
    SOLID original (no solid-less exemption applies) with a real volume afterwards, and a zero-area
    original with area afterwards, are changes. Zero to zero is not."""
    from meshpipeline.cad.repair_v4.verdict.faithful_kernel import _changed, _ratio

    assert _changed(100.0, 0.0) is True
    assert _changed(1e-9, 0.0) is True
    assert _changed(0.0, 0.0) is False
    assert _changed(102.5, 100.0) is True and _changed(101.9, 100.0) is False
    assert _ratio(100.0, 0.0) is None and _ratio(0.0, 0.0) is None  # None, not 1.0 and not inf
    assert _ratio(100.0, 100.0) == 1.0  # a genuine no-change stays distinguishable


def test_an_unbounded_input_is_measured_out_of_scope_and_never_meshed(monkeypatch) -> None:
    """Not clean, not inconclusive, not a panel failure: a verdict about the file that no mesher was asked for."""
    from meshpipeline.cad.repair_v4.verdict.valid import Finding

    unbounded = ValidReport(False, [Finding("unbounded_geometry", [], {"diagonal_mm": 3.46e100})], [], "mm", 1, False)
    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: unbounded)
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "4.15.2"})
    member = FakeMember("a")
    for tier in (verdict, cheap_verdict):
        v = tier(None, "s.step", "o.step", members=[member], run_panel=run_panel)  # type: ignore[arg-type]
        assert v.out_of_scope and not v.clean and not v.inconclusive
        assert v.counts_for_scoreboard  # measured: the unmeasured (inconclusive) rate does not move
        assert v.panel == [] and v.faithful is None
    assert member.runs == 0  # no mesher saw it


def test_only_an_unbounded_report_is_out_of_scope() -> None:
    assert not _v(True, _passes(1), _faith()).out_of_scope
    assert not _v(False).out_of_scope


def test_a_cached_located_face_keeps_its_box(step, tmp_path) -> None:
    """D17: `Located.box` is what the workspace names a mesher's face by; a cache round trip must not drop it."""
    from meshpipeline.cad.repair_v4.verdict.cache import MemoryStore, VerdictCache, _default_factory
    from meshpipeline.cad.repair_v4.verdict.panel import Located, MemberResult

    cache = VerdictCache(MemoryStore(), result_factory=_default_factory)
    r = MemberResult("gmsh_prod", "fail", "other", "HXT 3D mesh failed",
                     [Located("face:ab", None, "surface 3", (1.0, 2.0, 3.0, 4.0, 5.0, 6.0))])
    cache.put("sha", "cfg", [r])
    [back] = cache.get("sha", "cfg")
    assert back.located[0].box == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0) and back == r


@pytest.mark.parametrize("tier", ["cheap", "full"])
def test_both_tiers_hand_the_runner_the_doomed_cap(layers, monkeypatch, tier: str) -> None:
    """C16: the staged healer and the search judge on the cheap tier with every member the start fails; without the
    cap a hanging member cost 300 s + its 600 s retry on every stage of an already doomed panel."""
    seen: list = []

    def capped_runner(pool, step, members, *, timeout_s, doomed_cap_s=None):
        seen.append(doomed_cap_s)
        return run_panel(pool, step, members, timeout_s=timeout_s)

    monkeypatch.setattr(assemble, "default_members", lambda t: [FakeMember("gmsh_default")])
    judge = cheap_verdict if tier == "cheap" else verdict
    judge(None, "s.step", "o.step", run_panel=capped_runner)  # type: ignore[operator]
    assert seen == [assemble.DOOMED_CAP_S]


def test_a_cheap_verdict_meshes_in_parallel_only_when_asked_and_the_key_does_not_change(layers, step, tmp_path,
                                                                                         monkeypatch) -> None:
    """D25: the staged healer's cheap panels take P (panel_threads); the search's do not ask, so they stay serial.
    P changes only WHEN members run, so it is not in the cache key: the parallel panel is a hit for the serial one."""
    from meshpipeline.cad.repair_v4.verdict import assemble as A

    seen: list[dict] = []

    def runner(pool, step_path, members, *, timeout_s, doomed_cap_s=None, panel_threads=1):
        seen.append({"panel_threads": panel_threads})
        return [m.run(pool, step_path, timeout_s=timeout_s) for m in members]

    members = [FakeMember("a"), FakeMember("b")]
    cache = _cache(tmp_path)
    A.cheap_verdict(None, step, "o.step", members=members, run_panel=runner, panel_threads=4)   # type: ignore[arg-type]
    A.cheap_verdict(None, step, "o.step", members=members, run_panel=runner)                    # type: ignore[arg-type]
    assert seen == [{"panel_threads": 4}, {"panel_threads": 1}]
    A.cheap_verdict(None, step, "o.step", members=members, run_panel=runner, panel_threads=4,  # type: ignore[arg-type]
                    cache=cache)
    A.cheap_verdict(None, step, "o.step", members=members, run_panel=runner, cache=cache)  # type: ignore[arg-type]
    assert len(seen) == 3                     # the serial call was a cache HIT for the parallel one: same key
