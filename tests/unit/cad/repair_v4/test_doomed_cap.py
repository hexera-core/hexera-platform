# Responsibility: lane C16 - the FULL panel's doomed cap: once a member does not pass the file cannot be clean, so the
#                 rest run cheapest-first at the cap with no retry; a capped timeout is a fail of its own kind, never
#                 inconclusive; an all-pass panel never sees the cap; the cheap tier's cache key is unchanged.
# Boundaries: hermetic; fake members return MemberResults and record the budget they were given.
from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4.verdict import panel
from meshpipeline.cad.repair_v4.verdict.cache import panel_config_hash


class _Member:
    def __init__(self, name: str, status: str, calls: list, error_kind: str | None = None) -> None:
        self.name, self.status, self.calls, self.error_kind = name, status, calls, error_kind
        self.config = {"n": name}

    def run(self, pool: Any, step: str, *, timeout_s: float) -> panel.MemberResult:
        self.calls.append((self.name, timeout_s))
        return panel.MemberResult(self.name, self.status, self.error_kind, "m", [], None, 0, 1.0)


def _members(calls: list, **status: str) -> list[_Member]:
    kinds = {"timeout": panel.INFRASTRUCTURE_KIND, "fail": "low_quality"}
    return [_Member(n, status.get(n, "pass"), calls, kinds.get(status.get(n, "pass")))
            for n in ("gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ")]


def test_an_all_pass_panel_never_sees_the_cap() -> None:
    calls: list = []
    got = panel.run_panel(None, "s.step", _members(calls), timeout_s=300.0, doomed_cap_s=60.0)
    assert [r.status for r in got] == ["pass"] * 4 and {t for _, t in calls} == {300.0}


def test_after_a_fail_the_rest_run_cheapest_first_at_the_cap_and_results_keep_the_callers_order() -> None:
    calls: list = []
    got = panel.run_panel(None, "s.step", _members(calls, gmsh_default="fail", netgen_occ="timeout"),
                          timeout_s=300.0, doomed_cap_s=60.0)
    assert calls == [("gmsh_default", 300.0), ("gmsh_prod", 60.0), ("gmsh_alt", 60.0), ("netgen_occ", 60.0)]
    assert [r.name for r in got] == ["gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ"]
    net = got[3]
    assert net.status == "fail" and net.error_kind == panel.CAPPED_KIND and not panel.is_inconclusive(net)
    assert got[0].status == "pass"   # a doomed member that finishes inside the cap keeps its own result


def test_without_a_cap_the_panel_is_unchanged() -> None:
    calls: list = []
    panel.run_panel(None, "s.step", _members(calls, gmsh_default="fail"), timeout_s=300.0)
    assert calls == [(n, 300.0) for n in ("gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ")]


def test_the_cheap_tier_key_is_byte_identical_and_the_full_tier_key_carries_the_cap() -> None:
    ms = _members([])
    base = panel_config_hash(ms, 300.0, {"gmsh": "4"})
    assert panel_config_hash(ms, 300.0, {"gmsh": "4"}, extra=None) == base
    assert panel_config_hash(ms, 300.0, {"gmsh": "4"}, extra={"doomed_cap_s": 60.0}) != base
