# Responsibility: B23's verdict-level savings - the phase timing exists, and layer 3 is skipped exactly where
#                 nothing reads it (cheap tier, layer 1 failing) and nowhere else.
# Boundaries: no OCCT. Layer 1, the panel and layer 3 are injected; what is tested is which ones RAN.
from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4.verdict import assemble as A
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport


class _Member:
    name = "gmsh_default"

    def run(self, pool: Any, step_path: str, *, timeout_s: float) -> Any:
        class R:
            name, status, min_sicn, error_kind = "gmsh_default", "pass", 0.5, None
        return R()


def _valid(ok: bool) -> ValidReport:
    return ValidReport(ok, [], [], "mm", 1, False)


def _report(monkeypatch: Any, *, ok: bool, tier: str) -> tuple[Any, list[str]]:
    ran: list[str] = []

    def valid(pool: Any, step: str, *, original_units: str | None = None) -> ValidReport:
        ran.append("valid")
        return _valid(ok)

    def faithful(pool: Any, original: str, step: str, **kw: Any) -> FaithfulReport:
        ran.append("faithful")
        return FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1)

    monkeypatch.setattr(A, "check_valid", valid)
    monkeypatch.setattr(A, "check_faithful_steps", faithful)
    A.reset_verdict_phases()
    fn = A.cheap_verdict if tier == "cheap" else A.verdict
    v = fn(None, "/fake/step.step", "/fake/orig.step", members=[_Member()],
           run_panel=lambda pool, step, members, *, timeout_s: [m.run(pool, step, timeout_s=timeout_s)
                                                                for m in members])
    return v, ran


def test_layer_3_is_SKIPPED_on_a_cheap_invalid_state_because_nothing_reads_it(monkeypatch: Any) -> None:
    """`score` zeroes keys 3 and 4 while layer 1 fails (lane A's frontier fix), `clean` is already False, and no
    row carries a cheap verdict's fidelity numbers. The measurement was computed and discarded."""
    v, ran = _report(monkeypatch, ok=False, tier="cheap")
    assert ran == ["valid"]
    assert v.faithful is None and v.clean is False
    assert A.verdict_phases()["faithful_skipped_n"] == 1
    # the decision the search makes is unchanged: keys 3 and 4 are zero either way
    assert A.score(v)[3:] == (0.0, 0)


def test_layer_3_still_RUNS_on_a_cheap_VALID_state_where_it_breaks_ties(monkeypatch: Any) -> None:
    v, ran = _report(monkeypatch, ok=True, tier="cheap")
    assert ran == ["valid", "faithful"]
    assert v.faithful is not None


def test_layer_3_still_RUNS_on_a_FULL_invalid_state_because_the_row_reports_it(monkeypatch: Any) -> None:
    """A delivered-but-invalid output's fidelity numbers still say what the repair did to the part."""
    v, ran = _report(monkeypatch, ok=False, tier="full")
    assert ran == ["valid", "faithful"]
    assert v.faithful is not None


def test_the_phases_are_reported_per_layer_with_counts(monkeypatch: Any) -> None:
    _report(monkeypatch, ok=True, tier="full")
    phases = A.verdict_phases()
    assert phases["valid_n"] == 1 and phases["panel_n"] == 1 and phases["faithful_n"] == 1
    assert phases["valid_s"] >= 0.0 and phases["panel_s"] >= 0.0
    A.reset_verdict_phases()
    assert A.verdict_phases() == {}
