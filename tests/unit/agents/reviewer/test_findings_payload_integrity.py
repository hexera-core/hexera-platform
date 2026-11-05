# Responsibility: Verify a per-axis judgement is a literal JSON boolean, and no malformed shape can derive a verdict.
from __future__ import annotations

import itertools
import json
import subprocess
import types
from pathlib import Path

import pytest

from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    Eligibility,
    EligibilityDecision,
    evaluate_eligibility,
)
from meshpipeline.agents.reviewer.unified import FINDING_FIELDS, UNIFIED_TOOLS, parse_findings
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus
from meshpipeline.contracts.review_outcome import ReviewVerdict

REPO = Path(__file__).resolve().parents[4]
AXES = ("surface_capture", "wake_resolution")


def _plan(*names):
    return types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="engine:snappy", requires=())
                   for n in (names or AXES)),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset(), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())


def _entry(**over):
    base = {"axis_key": "surface_capture", "finding": "looks fine",
            "evidence_ids": ["v-001"], "passed": True}
    base.update(over)
    return base


def _parse(*entries, **extra):
    return parse_findings({"axis_findings": list(entries), **extra})


# the twelve documented input cases
def test_case_1_passed_true_is_accepted():
    findings, problems = _parse(_entry(passed=True))
    assert not problems and findings[0].passed is True


def test_case_2_passed_false_is_accepted():
    findings, problems = _parse(_entry(passed=False))
    assert not problems and findings[0].passed is False


def test_case_3_missing_passed_is_rejected():
    e = _entry()
    del e["passed"]
    findings, problems = _parse(e)
    assert findings == () and any("missing required field(s) passed" in p for p in problems)


def test_case_4_passed_null_is_rejected():
    findings, problems = _parse(_entry(passed=None))
    assert findings == () and any("must be the JSON boolean" in p for p in problems)


@pytest.mark.parametrize("value", ["true", "false", "True", "yes", ""])
def test_case_5_string_booleans_are_rejected(value):
    findings, problems = _parse(_entry(passed=value))
    assert findings == () and any("must be the JSON boolean" in p for p in problems)


@pytest.mark.parametrize("value", [1, 0, 1.0, -1])
def test_case_6_numeric_boolean_substitutes_are_rejected(value):
    findings, problems = _parse(_entry(passed=value))
    assert findings == () and any("must be the JSON boolean" in p for p in problems)


def test_case_7_retired_satisfied_true_without_passed_is_rejected():
    e = _entry()
    del e["passed"]
    e["satisfied"] = True
    findings, problems = _parse(e)
    assert findings == ()
    assert any("unknown field(s) satisfied" in p for p in problems)


def test_case_8_retired_satisfied_false_without_passed_is_rejected():
    e = _entry()
    del e["passed"]
    e["satisfied"] = False
    findings, problems = _parse(e)
    assert findings == (), "no axis may be judged from a retired field"
    assert any("unknown field(s) satisfied" in p for p in problems)


def test_case_9_supplying_both_passed_and_satisfied_is_rejected():
    findings, problems = _parse(_entry(satisfied=False))
    assert findings == () and any("unknown field(s) satisfied" in p for p in problems)


def test_case_10_an_unknown_additional_field_is_rejected():
    findings, problems = _parse(_entry(confidence=0.9))
    assert findings == () and any("unknown field(s) confidence" in p for p in problems)


def test_case_11_duplicate_axes_with_conflicting_judgements_are_rejected():
    L = EvidenceLedger()
    e = L.add_render_view("iso", "k", "open", image_ok=True)
    d = evaluate_eligibility(_plan(), L, (
        AxisFinding("surface_capture", "a", (e,), passed=True),
        AxisFinding("surface_capture", "b", (e,), passed=False),
        AxisFinding("wake_resolution", "c", (e,), passed=True)))
    assert d.outcome is Eligibility.REJECT and d.verdict is None
    assert any("exactly one required" in r for r in d.reasons)


def test_case_12_malformed_json_never_reaches_the_findings_parser():
    import asyncio

    from meshpipeline.agents.loop.accounting import ToolInvocation
    from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
    from meshpipeline.contracts.agent_loop import LoopLimits
    p = ReviewLoopPolicy(plan=_plan(), ledger=EvidenceLedger(), runtime=None,
                         limits_=LoopLimits(max_rounds=30))
    out = asyncio.run(p.execute(ToolInvocation(
        round_index=1, call_index=1, tool="submit_findings",
        raw_arguments="{not json", parsed=None)))
    assert out.accepted is False and "could not be parsed" in out.content
    assert p.submissions == 0 and p.accepted is None


def test_there_is_exactly_one_axis_finding_construction_site():
    out = [h for h in _scan_sources(r"AxisFinding\(") if h.startswith("src/")]
    assert len(out) == 1, f"more than one construction site:\n{out}"
    assert "unified.py" in out[0]


def test_the_field_roster_is_closed():
    assert FINDING_FIELDS == {"axis_key", "finding", "evidence_ids", "passed"}


def _scan_sources(pattern: str, *, exclude: set[str] | None = None) -> list[str]:
    import re as _re
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                          "--", "src/", "tests/"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    rx = _re.compile(pattern)
    skip = exclude or set()
    hits: list[str] = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        if not rel or rel in skip or not (REPO / rel).is_file():
            continue
        for n, line in enumerate((REPO / rel).read_text(encoding="utf-8",
                                                        errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{n}:{line.strip()}")
    return hits


def _working_tree_sources(*prefixes: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    seen: list[Path] = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        if not rel or not rel.endswith(".py"):
            continue
        if prefixes and not rel.startswith(prefixes):
            continue
        path = REPO / rel
        if path.is_file():                      # skip paths deleted in the working tree
            seen.append(path)
    return seen


# the schema itself forbids extra properties
def test_the_tool_schema_requires_the_boolean_and_forbids_unknown_fields():
    sf = next(t for t in UNIFIED_TOOLS if t["function"]["name"] == "submit_findings")
    params = sf["function"]["parameters"]
    assert params["additionalProperties"] is False
    item = params["properties"]["axis_findings"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["passed"]["type"] == "boolean"
    assert set(item["required"]) == FINDING_FIELDS
    assert "satisfied" not in json.dumps(sf)


# exhaustive: no malformed combination wins
def _usable(L):
    return L.add_render_view("iso", "k", "open", image_ok=True)


@pytest.mark.parametrize(
    "present,boolean,valid_evidence,judgement,complete",
    list(itertools.product([True, False], repeat=5)))
def test_no_malformed_combination_can_derive_a_verdict(
        present, boolean, valid_evidence, judgement, complete):
    L = EvidenceLedger()
    good = _usable(L)
    gate = L.add_gate("mesh_quality", EvidenceStatus.FAIL, "skew", "executor")
    eid = good if valid_evidence else "t-999"

    def entry(axis):
        e = {"axis_key": axis, "finding": f"{axis} assessed",
             "evidence_ids": [gate if (valid_evidence and not judgement) else eid]}
        if present:
            e["passed"] = judgement if boolean else "yes"
        return e

    axes = AXES if complete else AXES[:1]
    findings, problems = _parse(*(entry(a) for a in axes))

    if problems:
        assert findings == (), "a malformed payload must construct nothing"
        return                                   # never reaches eligibility, so never a verdict

    d = evaluate_eligibility(_plan(), L, findings)
    if not d.accepted:
        assert d.verdict is None
        return
    # An accepted submission is only possible in the fully-valid corner, and always carries
    # exactly one of the two verdicts.
    assert present and boolean and valid_evidence and complete
    assert d.verdict is (ReviewVerdict.passed if judgement else ReviewVerdict.failed)


def test_the_all_valid_corner_actually_reaches_a_verdict():
    L = EvidenceLedger()
    e = _usable(L)
    findings, problems = _parse(*({"axis_key": a, "finding": "ok", "evidence_ids": [e],
                                   "passed": True} for a in AXES))
    assert not problems
    d = evaluate_eligibility(_plan(), L, findings)
    assert d.accepted and d.verdict is ReviewVerdict.passed


# the decision cannot be half-formed
def test_eligibility_has_no_non_verdict_member():
    assert {m.name for m in Eligibility} == {"PASS", "FAIL", "REJECT"}
    assert not hasattr(Eligibility, "NON_VERDICT")
    assert "classification" not in EligibilityDecision.__dataclass_fields__


# normalize_verdict is a boundary parser, and only that
def test_normalize_verdict_has_only_serialized_boundary_call_sites():
    out = subprocess.run(["git", "grep", "-n", "normalize_verdict(", "--", "src/"],
                         cwd=REPO, capture_output=True, text=True).stdout.strip().splitlines()
    call_sites = [ln for ln in out if "def normalize_verdict" not in ln]
    assert {ln.split(":")[0] for ln in call_sites} == {
        "src/meshpipeline/pipeline/outcome.py",
        "src/meshpipeline/capture/source.py",
        "src/meshpipeline/application/maintenance/export.py",
    }, call_sites
    # the Reviewer never touches it - its verdict comes from eligibility, typed
    reviewer = subprocess.run(["git", "grep", "-l", "normalize_verdict", "--",
                               "src/meshpipeline/agents/"],
                              cwd=REPO, capture_output=True, text=True).stdout.strip()
    assert not reviewer, f"the Reviewer must not parse verdict strings: {reviewer}"


def test_normalize_verdict_accepts_only_the_exact_current_wire_forms():
    from meshpipeline.contracts.review_outcome import normalize_verdict
    assert normalize_verdict("PASS") == "PASS" and normalize_verdict("FAIL") == "FAIL"
    assert normalize_verdict("  PASS  ") == "PASS"          # surrounding whitespace only
    for rejected in ("pass", "Pass", "passed", "<<PASS>>", "<<FAIL>>", "", "nonsense", None, 1):
        assert normalize_verdict(rejected) == "", rejected


def test_there_is_exactly_one_boundary_parser():
    hits = [p for p in _working_tree_sources("src/")
            if "def normalize_verdict" in p.read_text(encoding="utf-8", errors="replace")]
    assert len(hits) == 1, f"more than one boundary parser: {[str(h) for h in hits]}"
    assert hits[0].as_posix().endswith("contracts/review_outcome.py")


# full coverage for BOTH polarities is policy
def test_full_coverage_is_required_for_fail_as_well_as_pass():
    L = EvidenceLedger()
    gate = L.add_gate("mesh_quality", EvidenceStatus.FAIL, "skew", "executor")
    # one grounded defect, but the other required axis is unanswered
    d = evaluate_eligibility(_plan(), L, (
        AxisFinding("surface_capture", "skew too high", (gate,), passed=False),))
    assert d.outcome is Eligibility.REJECT and d.verdict is None
    assert any("wake_resolution" in r and "no finding" in r for r in d.reasons)


def test_prior_attempt_verdicts_cannot_leak_into_the_current_one():
    from meshpipeline.application.final_result import (
        ReviewExecution,
        TerminalStatus,
        build_final_result,
    )
    for prior in ("PASS", "FAIL"):
        fr = build_final_result(
            job_id="j", owner_id="o", status=TerminalStatus.failed, engine="snappy",
            purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
            executor_success=True, reviewer_verdict=prior, failed_gate="",
            api_failure="reviewer_evidence_missing", attempts=2, attempts_max=5,
            required_ready=False, delivered_types=[], optional_warnings=[])
        assert fr.reviewer_verdict is None
        assert fr.review_execution is ReviewExecution.failed_to_complete


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
