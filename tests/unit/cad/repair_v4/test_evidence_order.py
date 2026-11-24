"""Lane C22: later panels run in the INPUT's evidence order - input failures first (fastest first), then input passes
(DOOM_ORDER), then input hangs/caps last - so the doomed cap lands on the hang, not on the member that would fail in
two seconds. The input verdict itself keeps DOOM_ORDER. The order changes who is capped, so it is in the cache key."""
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict.panel import CAPPED_KIND, MemberResult, evidence_order, run_panel


def _v(*members: MemberResult) -> SimpleNamespace:
    return SimpleNamespace(panel=list(members))


#: 32ca3e9a6e9a's shape: gmsh_default HUNG at the input (840 s) and is first in DOOM_ORDER; the others fail fast
INPUT_32CA3E = _v(MemberResult("gmsh_prod", "fail", "other", "", seconds=2.0),
                  MemberResult("gmsh_default", "timeout", "timeout", "", seconds=840.0),
                  MemberResult("gmsh_alt", "fail", "no_volume", "", seconds=1.5),
                  MemberResult("netgen_occ", "pass", None, "", min_sicn=0.3, seconds=20.0))


def test_input_failures_first_fastest_first_then_passes_then_hangs_last() -> None:
    assert evidence_order(INPUT_32CA3E) == ("gmsh_alt", "gmsh_prod", "netgen_occ", "gmsh_default")
    capped = _v(MemberResult("gmsh_default", "fail", "overlapping_facets", "", seconds=0.6),
                MemberResult("netgen_occ", "fail", CAPPED_KIND, "", seconds=60.0))
    assert evidence_order(capped)[0] == "gmsh_default" and evidence_order(capped)[-1] == "netgen_occ"


def test_where_doom_order_already_suits_the_file_nothing_changes() -> None:
    usual = _v(MemberResult("gmsh_default", "fail", "overlapping_facets", "", seconds=0.6),
               MemberResult("gmsh_prod", "fail", "other", "", seconds=1.0),
               MemberResult("gmsh_alt", "pass", None, "", seconds=3.0),
               MemberResult("netgen_occ", "timeout", "timeout", "", seconds=300.0))
    assert evidence_order(usual) == ("gmsh_default", "gmsh_prod", "gmsh_alt", "netgen_occ")


class Member:
    log: list = []

    def __init__(self, name: str, status: str, kind: str | None = None) -> None:
        self.name, self.status, self.kind = name, status, kind

    def run(self, pool, step_path: str, *, timeout_s: float) -> MemberResult:
        Member.log.append((self.name, timeout_s))
        return MemberResult(self.name, self.status, self.kind, "")


def test_run_panel_takes_the_callers_order_and_the_cap_lands_on_the_hang() -> None:
    Member.log = []
    members = [Member("gmsh_prod", "fail", "other"), Member("gmsh_default", "timeout", "timeout"),
               Member("gmsh_alt", "fail", "no_volume"), Member("netgen_occ", "pass")]
    got = run_panel(None, "x.step", members, timeout_s=300, doomed_cap_s=60,  # type: ignore[arg-type]
                    order=evidence_order(INPUT_32CA3E))
    assert [n for n, _ in Member.log] == ["gmsh_alt", "gmsh_prod", "netgen_occ", "gmsh_default"]
    assert dict(Member.log)["gmsh_default"] == 60                      # the hang gets the cap, not 300 s + a retry
    assert [r.name for r in got] == ["gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ"]   # the caller's order


def test_the_order_is_set_after_the_input_and_enters_the_key(monkeypatch, tmp_path) -> None:
    seen: list = []

    def runner(pool, step, members, *, timeout_s, doomed_cap_s=None, order=None):
        seen.append(order)
        return [MemberResult(m.name, "pass", None, "", min_sicn=0.3) for m in members]

    keys: list = []
    real = assemble.panel_config_hash
    monkeypatch.setattr(assemble, "panel_config_hash", lambda *a, **k: keys.append(k.get("extra")) or real(*a, **k))
    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: SimpleNamespace(
        ok=True, hard=[], soft=[], unbounded=False))
    monkeypatch.setattr(assemble, "check_faithful_steps", lambda *a, **k: None)
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "x"})
    step = tmp_path / "s.step"
    step.write_text("x")
    from meshpipeline.cad.repair_v4.verdict.cache import MemoryStore, VerdictCache
    cache = VerdictCache(MemoryStore())
    members = [SimpleNamespace(name="gmsh_default", config={})]
    try:
        assemble.set_evidence_order(None)
        assemble.verdict(None, str(step), "o", members=members, run_panel=runner, cache=cache)  # type: ignore[arg-type]
        assemble.set_evidence_order(INPUT_32CA3E)
        assemble.verdict(None, str(step), "o", members=members, run_panel=runner, cache=cache)  # type: ignore[arg-type]
    finally:
        assemble.set_evidence_order(None)
    assert seen[0] is None and seen[1] == list(evidence_order(INPUT_32CA3E))
    assert keys[0] != keys[1]                         # a different order is a different panel config: no stale hit
