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
from meshpipeline.cad.repair_v4.verdict.assemble import Declared, Verdict, cheap_verdict, score, verdict
from meshpipeline.cad.repair_v4.verdict.cache import (
    VERDICT_CODE_VERSION,
    DirStore,
    VerdictCache,
    file_sha256,
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


@dataclass
class FakeMember:
    name: str
    status: str = "pass"
    sicn: float | None = 0.3
    runs: int = 0

    def run(self, pool, step_path, *, timeout_s):
        self.runs += 1
        return FakeResult(self.name, self.status, self.sicn)


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
    return state


def _run(layers_state, members, **kwargs) -> Verdict:
    return verdict(None, "s.step", "o.step", members=members, run_panel=run_panel, **kwargs)  # type: ignore[arg-type]


def test_clean_requires_all_layers(layers) -> None:
    members = [FakeMember("a"), FakeMember("b")]
    assert _run(layers, members).clean is True

    layers["valid"] = False
    assert _run(layers, members).clean is False
    layers["valid"] = True

    assert _run(layers, [FakeMember("a"), FakeMember("b", status="fail", sicn=None)]).clean is False

    layers["faithful"] = _faith(ok=False, dev=0.5)
    assert _run(layers, members).clean is False


def test_an_invalid_file_has_no_faithful_layer_and_is_not_compared(layers) -> None:
    layers["valid"] = False
    v = _run(layers, [FakeMember("a")])
    assert v.faithful is None and layers["faithful_calls"] == []


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
    return FakeResult(data["name"], data["status"], data["min_sicn"], data["located"])


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


def test_panel_config_hash_tracks_members_and_timeout() -> None:
    a, b = FakeMember("a"), FakeMember("b")
    assert panel_config_hash([a, b], 300) == panel_config_hash([b, a], 300)
    assert panel_config_hash([a], 300) != panel_config_hash([a, b], 300)
    assert panel_config_hash([a], 300) != panel_config_hash([a], 120)
