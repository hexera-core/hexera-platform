# Responsibility: Task 22b - the staged healer and the judge are wired to ONE verdict cache, and the cache hits.
# Boundaries: the kernel boundary (pool) and the meshers are fakes that COUNT invocations; everything above them
#             (cheap_verdict, verdict, VerdictCache, panel_config_hash, run_staged, search) is the real code.
"""A cache test that cannot tell a hit from a fast miss is worthless, so every assertion here is on how many
times a mesher was INVOKED, never on time."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import search, stages
from meshpipeline.cad.repair_v4.search import Budget, default_judge
from meshpipeline.cad.repair_v4.stages import staged_start
from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict import cache as cache_mod
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict, default_members, score
from meshpipeline.cad.repair_v4.verdict.cache import (
    DirStore,
    MemoryStore,
    VerdictCache,
    panel_config_hash,
)
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND, MemberResult
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Workspace

BIG = Budget(wall_s=1e9, ops=10**6, model_calls=0, model_usd=0.0)


@dataclass
class CountingMember:
    """A panel member that records every mesh it is asked for, per STEP path."""

    name: str
    status: str = "pass"
    error_kind: str | None = None
    runs: list[str] = field(default_factory=list)

    @property
    def config(self) -> dict:
        return {"fake": self.name}

    def run(self, pool: Any, step_path: str, *, timeout_s: float) -> MemberResult:
        self.runs.append(step_path)
        return MemberResult(self.name, self.status, error_kind=self.error_kind, min_sicn=0.3)  # type: ignore[arg-type]


class Pool:
    """The kernel boundary: STEP export (content follows the BREP's), indexing, versions. `versions_ok=False`
    is a dead pool."""

    def __init__(self, versions_ok: bool = True) -> None:
        self.versions_ok = versions_ok
        self.calls: list[str] = []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append(fn)
        if fn == "mesher_versions":
            if not self.versions_ok:
                return CallResult(False, None, "pool died", "crashed", 0.0)
            return CallResult(True, {"gmsh": "4.15.2", "netgen": "6.2", "ocp": "7.8"}, None, "ok", 0.0)
        if fn == "ws_index":
            ents = [{"type": "face", "index": k, "fp": f"f{k}"} for k in (1, 2)] + [{"type": "solid", "index": 1}]
            return CallResult(True, {"entities": ents, "quantum": 1e-6}, None, "ok", 0.0)
        if fn == "write_step_xcaf":
            Path(payload["path"]).write_bytes(b"STEP of " + Path(payload["brep"]).read_bytes())
            return CallResult(True, {}, None, "ok", 0.0)
        return CallResult(False, None, f"no such kernel fn {fn}", "error", 0.0)


class Checker:
    """Layers 1 and 3 replaced (they need a kernel); layer 2 is the real assembly over counting members."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tier_members: dict[str, list[CountingMember]]) -> None:
        self.members = tier_members
        monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: ValidReport(
            True, [], [], "mm", 1, True))
        monkeypatch.setattr(assemble, "check_faithful_steps", lambda pool, o, s, *, declared_added, declared_removed:
                            FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1))
        monkeypatch.setattr(assemble, "default_members", lambda tier: self.members[tier])
        monkeypatch.setattr(assemble, "_panel_module", lambda: SimpleNamespace(
            INFRASTRUCTURE_KIND=INFRASTRUCTURE_KIND, run_panel=self._run_panel))

    @staticmethod
    def _run_panel(pool: Any, step: str, members: Any, *, timeout_s: float) -> list:
        return [m.run(pool, step, timeout_s=timeout_s) for m in members]

    def meshes(self, tier: str | None = None) -> int:
        tiers = [tier] if tier else list(self.members)
        return sum(len(m.runs) for t in tiers for m in self.members[t])


def _members() -> dict[str, list[CountingMember]]:
    # the count is deliberately not the production one and differs per tier: nothing may depend on it
    return {"cheap": [CountingMember("cheap_a")],
            "full": [CountingMember("g1"), CountingMember("g2"), CountingMember("g3"), CountingMember("ng")]}


@pytest.fixture
def checker(monkeypatch: pytest.MonkeyPatch) -> Checker:
    return Checker(monkeypatch, _members())


def make_ws(tmp_path: Path, name: str = "w", body: bytes | None = None) -> Workspace:
    (tmp_path / "orig.brep").write_bytes(b"orig")
    brep = tmp_path / f"{name}.brep"
    brep.write_bytes(body if body is not None else name.encode())
    return Workspace(brep_path=str(brep), ids={"F1": "a", "F2": "b"}, labels={}, history=[],
                     original=str(tmp_path / "orig.brep"), indices={"F1": 1, "F2": 2})


# --- the cache hits --------------------------------------------------------------------------------------------


def test_a_second_judgement_of_the_same_state_invokes_no_mesher(checker: Checker, tmp_path: Path) -> None:
    j = default_judge(h_mm=2.5, cache=VerdictCache(MemoryStore()))
    pool, ws = Pool(), make_ws(tmp_path)
    first = j.full(pool, ws, "o.step")
    n_full = len(checker.members["full"])
    assert checker.meshes("full") == n_full  # one mesh per member the tier defines, whatever that count is
    second = j.full(pool, ws, "o.step")
    assert checker.meshes("full") == n_full  # HIT: not one mesher invoked
    assert [r.status for r in second.panel] == [r.status for r in first.panel]
    assert second.clean is first.clean is True


def test_a_different_state_is_a_miss_so_the_counter_can_tell_a_hit_from_a_fast_miss(checker: Checker,
                                                                                   tmp_path: Path) -> None:
    j = default_judge(h_mm=2.5, cache=VerdictCache(MemoryStore()))
    pool = Pool()
    j.cheap(pool, make_ws(tmp_path, "a"), "o.step")
    assert checker.meshes("cheap") == 1
    j.cheap(pool, make_ws(tmp_path, "b"), "o.step")  # different STEP bytes
    assert checker.meshes("cheap") == 2


def test_cheap_and_full_tiers_do_not_serve_each_other(checker: Checker, tmp_path: Path) -> None:
    j = default_judge(h_mm=2.5, cache=VerdictCache(MemoryStore()))
    pool, ws = Pool(), make_ws(tmp_path)
    j.cheap(pool, ws, "o.step")
    j.full(pool, ws, "o.step")
    assert checker.meshes("cheap") == 1 and checker.meshes("full") == len(checker.members["full"])
    j.cheap(pool, ws, "o.step")
    j.full(pool, ws, "o.step")
    assert checker.meshes("cheap") == 1 and checker.meshes("full") == len(checker.members["full"])


def test_a_hit_still_recomputes_layers_1_and_3(checker: Checker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache holds only the panel: a hit must never skip a check that depends on more than the STEP."""
    seen = {"valid": 0, "faithful": 0}

    def valid(pool: Any, step: str, *, original_units: Any) -> ValidReport:
        seen["valid"] += 1
        return ValidReport(True, [], [], "mm", 1, True)

    def faithful(pool: Any, o: str, s: str, *, declared_added: Any, declared_removed: Any) -> FaithfulReport:
        seen["faithful"] += 1
        return FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1)

    monkeypatch.setattr(assemble, "check_valid", valid)
    monkeypatch.setattr(assemble, "check_faithful_steps", faithful)
    j = default_judge(h_mm=2.5, cache=VerdictCache(MemoryStore()))
    pool, ws = Pool(), make_ws(tmp_path)
    j.full(pool, ws, "o.step")
    j.full(pool, ws, "o.step")
    assert checker.meshes("full") == len(checker.members["full"])
    assert seen == {"valid": 2, "faithful": 2}


def test_the_engine_default_cache_is_shared_by_the_staged_healer_and_the_search(checker: Checker,
                                                                               tmp_path: Path) -> None:
    """run_staged judges the start state (cheap); `search` then judges the same state cheap again. Together: ONE
    cheap mesh. Neither was handed a cache by the test: both took the process-wide one."""
    pool, ws = Pool(), make_ws(tmp_path)
    start = staged_start(pool, ws)
    assert start.measured and start.result is not None and "baseline" in start.result.kept
    assert checker.meshes("cheap") == 1
    r = search.search(pool, start.ws, "o.step", [], BIG, workers=1, h_mm=2.5)  # no judge: the real default one
    assert r.clean is True
    assert checker.meshes("cheap") == 1, "the search re-meshed a state the staged healer had already measured"
    assert checker.meshes("full") == len(checker.members["full"])


def test_both_judges_take_the_same_default_cache_object(checker: Checker, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = assemble.cheap_verdict

    def spy(pool: Any, step: str, original: str, **kw: Any) -> Any:
        seen.append(kw.get("cache"))
        return real(pool, step, original, **kw)

    monkeypatch.setattr(stages, "cheap_verdict", spy)
    monkeypatch.setattr(search, "cheap_verdict", spy)
    pool, ws = Pool(), make_ws(tmp_path)
    stages._judge(pool, ws)
    default_judge(h_mm=1.0).cheap(pool, ws, "o.step")
    assert len(seen) == 2 and seen[0] is seen[1] is cache_mod.default_verdict_cache()


def test_a_durable_cache_survives_a_new_process_view_of_the_store(checker: Checker, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cache_mod.CACHE_URI_ENV, str(tmp_path / "bucket"))
    pool, ws = Pool(), make_ws(tmp_path)
    default_judge(h_mm=1.0).cheap(pool, ws, "o.step")
    cache_mod.reset_default_verdict_cache()  # a fresh process: only the directory remains
    default_judge(h_mm=1.0).cheap(pool, ws, "o.step")
    assert checker.meshes("cheap") == 1
    assert isinstance(cache_mod.default_verdict_cache().store, DirStore)


# --- the key ---------------------------------------------------------------------------------------------------


def test_every_default_member_of_both_tiers_is_keyable_and_the_tiers_key_differently() -> None:
    """`panel_config_hash` fails closed on a member without `config`. The real panel must never hit that. No
    member count is asserted: adding a member (Netgen) must change the key, not break this test."""
    versions = {"gmsh": "4.15.2", "netgen": "6.2"}
    hashes = {t: panel_config_hash(default_members(t), assemble.MEMBER_TIMEOUT_S, versions) for t in ("cheap", "full")}
    assert hashes["cheap"] != hashes["full"]


def test_adding_a_panel_member_changes_the_key_so_old_entries_are_misses_not_stale_hits() -> None:
    base = default_members("full")
    extra = CountingMember("one_more_mesher")
    before = panel_config_hash(base, assemble.MEMBER_TIMEOUT_S, {})
    after = panel_config_hash([*base, extra], assemble.MEMBER_TIMEOUT_S, {})
    assert before != after
    store = VerdictCache(MemoryStore())
    store.put("sha", before, [MemberResult("m", "pass")])
    assert store.get("sha", before) is not None and store.get("sha", after) is None


def test_a_member_without_config_fails_the_judge_loudly_rather_than_running_uncached(checker: Checker,
                                                                                    tmp_path: Path) -> None:
    @dataclass
    class NoConfig:
        name: str = "nc"

        def run(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("must not mesh")

    checker.members["cheap"] = [NoConfig()]  # type: ignore[list-item]
    j = default_judge(h_mm=1.0, cache=VerdictCache(MemoryStore()))
    with pytest.raises(cache_mod.PanelConfigError):
        j.cheap(Pool(), make_ws(tmp_path), "o.step")


# --- infrastructure is not a verdict ---------------------------------------------------------------------------


def test_a_dead_pool_is_checker_unavailable_not_a_verdict_and_nothing_meshes(checker: Checker, tmp_path: Path) -> None:
    j = default_judge(h_mm=1.0, cache=VerdictCache(MemoryStore()))
    with pytest.raises(CheckerUnavailable):
        j.full(Pool(versions_ok=False), make_ws(tmp_path), "o.step")
    assert checker.meshes() == 0


def test_a_missing_step_is_checker_unavailable_not_a_verdict(checker: Checker, tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with pytest.raises(CheckerUnavailable):
        assemble.cheap_verdict(Pool(), str(tmp_path / "never_written.step"), "o.step",
                               cache=VerdictCache(MemoryStore()))
    assert checker.meshes() == 0
    del ws


def test_the_staged_healer_on_a_dead_checker_is_unmeasured_never_a_failure(checker: Checker, tmp_path: Path) -> None:
    out = staged_start(Pool(versions_ok=False), make_ws(tmp_path))
    assert out.measured is False and out.result is None and out.reason
    assert out.ws.brep_path.endswith("w.brep")  # the input, untouched
    assert checker.meshes() == 0


def test_a_pool_caused_timeout_is_inconclusive_unranked_uncached_and_not_retried_by_the_judge(
        checker: Checker, tmp_path: Path) -> None:
    flaky = checker.members["cheap"][0]
    flaky.status, flaky.error_kind = "timeout", INFRASTRUCTURE_KIND
    j = default_judge(h_mm=1.0, cache=VerdictCache(MemoryStore()))
    pool, ws = Pool(), make_ws(tmp_path)
    v = j.cheap(pool, ws, "o.step")
    assert v.inconclusive and not v.counts_for_scoreboard and not v.clean
    with pytest.raises(InconclusiveVerdict):
        score(v)
    assert len(flaky.runs) == 1  # the judge added no retry of its own (the panel owns the one retry budget)
    j.cheap(pool, ws, "o.step")
    assert len(flaky.runs) == 2  # never cached: infrastructure must not become permanent


def test_a_genuine_mesher_failure_is_a_verdict_and_is_cached(checker: Checker, tmp_path: Path) -> None:
    bad = checker.members["cheap"][0]
    bad.status, bad.error_kind = "fail", "inverted_elements"
    j = default_judge(h_mm=1.0, cache=VerdictCache(MemoryStore()))
    pool, ws = Pool(), make_ws(tmp_path)
    v = j.cheap(pool, ws, "o.step")
    assert not v.inconclusive and not v.clean and v.panel[0].status == "fail"
    j.cheap(pool, ws, "o.step")
    assert len(bad.runs) == 1


def test_a_cache_store_outage_is_a_miss_not_a_crash_and_not_a_verdict(checker: Checker, tmp_path: Path) -> None:
    class Broken(MemoryStore):
        def get(self, key: str) -> str | None:
            raise PermissionError("403")

        def put(self, key: str, text: str) -> None:
            raise ConnectionError("down")

    j = default_judge(h_mm=1.0, cache=VerdictCache(Broken()))
    v = j.cheap(Pool(), make_ws(tmp_path), "o.step")
    assert v.panel[0].status == "pass" and not v.inconclusive
    assert checker.meshes("cheap") == 1
