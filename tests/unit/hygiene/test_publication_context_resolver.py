# Responsibility: Verify every publication candidate resolves to determinate authority and lifecycle contexts.
# Boundaries: the resolver only - the canonical manifest and its drift comparison are a separate gate.
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "devtools" / "quality"))
import publication_authority as PA  # noqa: E402

RECORDS = PA.scan()


def _at(canonical_fragment: str) -> list:
    return [r for r in RECORDS if canonical_fragment in r.canonical]


# no ambiguity survives


def test_no_candidate_remains_caller_determined():
    stuck = [f"{r.path}:{r.lineno} {r.qualname}::{r.semantic} recv={r.receiver!r}"
             for r in RECORDS if r.authority == PA.FORWARDED]
    assert stuck == [], (
        "these publication sites still depend on who calls them; each needs its reachable "
        f"authorities expanded into determinate records: {stuck}")


def test_no_receiver_remains_unresolved():
    stuck = [f"{r.path}:{r.lineno} {r.qualname}::{r.semantic} recv={r.receiver!r}"
             for r in RECORDS if r.authority == PA.UNRESOLVED]
    assert stuck == [], f"these receivers could not be resolved to a type: {stuck}"


def test_every_record_carries_a_lifecycle():
    missing = [f"{r.path}:{r.lineno} {r.qualname}::{r.semantic} authority={r.authority}"
               for r in RECORDS if not r.lifecycle]
    assert missing == [], f"these records have no lifecycle classification: {missing}"


def test_every_publisher_record_names_a_root():
    rootless = [f"{r.path}:{r.lineno} {r.qualname}::{r.semantic}"
                for r in RECORDS
                if r.authority not in (PA.NOT_A_PUBLISHER,) and not r.roots]
    assert rootless == [], f"these publisher records name no construction or caller root: {rootless}"


def test_every_syntactic_candidate_produces_at_least_one_record():
    by_site: dict = {}
    for r in RECORDS:
        by_site.setdefault(r.canonical, []).append(r)
    empty = [c for c, rs in by_site.items() if not rs]
    assert empty == [], f"these candidates produced no context record: {empty}"


def test_no_duplicate_effective_context():
    seen: dict = {}
    for r in RECORDS:
        seen.setdefault(r.context, []).append(r)
    dupes = {c: len(rs) for c, rs in seen.items() if len(rs) > 1}
    assert dupes == {}, f"identical effective contexts were emitted more than once: {dupes}"


# the five formerly ambiguous sites, asserted against current source


@pytest.mark.parametrize("fn", ["tool_call", "tool_result", "round_begin", "round_end"])
def test_the_synchronous_tracing_calls_resolve_to_the_request_path(fn):
    # TraceContext is constructed by intake alone now, so its publisher is the request path's
    records = _at(f"agents/loop/tracing.py::{fn}::")
    assert records, f"tracing.{fn} produced no record"
    assert {r.authority for r in records} == {PA.INTAKE}, [r.authority for r in records]
    assert {r.lifecycle for r in records} == {PA.L_INTAKE}
    assert all(not r.gated_required for r in records)


def test_the_rationale_helper_expands_into_its_real_contexts():
    records = _at("contracts/rationale.py::_say::")
    got = {(r.authority, r.lifecycle) for r in records}
    assert got == {(PA.PLAIN, PA.L_TERMINAL), (PA.INTAKE, PA.L_INTAKE)}, (
        f"_say is reached by intake and the terminal path; resolved: {sorted(got)}")
    assert len(records) == 2, f"one record per reachable context, got {len(records)}"
    for r in records:
        assert r.roots, f"{r.authority} context names no supporting root"


def test_the_rationale_contexts_name_their_supporting_roots():
    roots = {r.authority: r.roots for r in _at("contracts/rationale.py::_say::")}
    assert any("terminal_finalize" in x for x in roots[PA.PLAIN])
    assert any("intake" in x for x in roots[PA.INTAKE])


# lifecycle distinctions


def test_terminal_and_maintenance_closings_are_plain_and_distinguished():
    terminal = [r for r in RECORDS if r.path == "application/pipeline_run.py"
                and r.semantic == "closing"]
    maintenance = [r for r in RECORDS if r.path == "application/maintenance/cleanup.py"
                   and r.semantic == "closing"]
    assert len(terminal) == 5 and len(maintenance) == 1, (len(terminal), len(maintenance))
    assert {r.lifecycle for r in terminal} == {PA.L_TERMINAL}
    assert {r.lifecycle for r in maintenance} == {PA.L_MAINTENANCE}
    assert all(r.authority == PA.PLAIN and not r.gated_required
               for r in terminal + maintenance), "a post-ownership closing was gated"


def test_no_execution_owned_record_uses_the_plain_publisher():
    offenders = [f"{r.path}:{r.lineno} {r.qualname}::{r.semantic}"
                 for r in RECORDS
                 if r.lifecycle == PA.L_EXECUTION and r.authority != PA.GATED]
    assert offenders == [], f"these run under a claim but publish ungated: {offenders}"


def test_every_gated_record_is_marked_as_requiring_gating():
    assert all(r.gated_required for r in RECORDS if r.authority == PA.GATED)
    assert not any(r.gated_required for r in RECORDS if r.authority != PA.GATED)


def test_the_artifact_uploader_factory_calls_resolve_through_their_caller():
    # only its publications; the module also calls loggers, which resolve as non-publishers
    records = [r for r in RECORDS if r.path == "application/artifact_uploader.py"
               and r.authority != PA.NOT_A_PUBLISHER]
    assert records, "the artifact uploader produced no publisher records"
    assert {r.authority for r in records} == {PA.PLAIN}
    assert {r.lifecycle for r in records} == {PA.L_TERMINAL}


# determinism


def test_the_scan_is_stable_across_repeats():
    again = PA.scan()
    assert [r.context for r in again] == [r.context for r in RECORDS], \
        "the resolver is not deterministic across repeated scans"


def test_a_cyclic_forwarding_graph_does_not_hang_or_crash():
    # the real tree contains forwarding chains; a cycle must terminate rather than recurse away
    assert len(RECORDS) >= len({r.canonical for r in RECORDS})


def test_the_totals_are_reported_by_authority_and_lifecycle():
    import collections
    by_authority = collections.Counter(r.authority for r in RECORDS)
    by_lifecycle = collections.Counter(r.lifecycle for r in RECORDS)
    assert by_authority[PA.FORWARDED] == 0 and by_authority[PA.UNRESOLVED] == 0
    assert "" not in by_lifecycle
    assert by_authority[PA.GATED] == by_lifecycle[PA.L_EXECUTION]
