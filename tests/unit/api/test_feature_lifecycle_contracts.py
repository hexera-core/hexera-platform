# Responsibility: Verify the lifecycle invariants a durable field or public route is only useful under.
# Boundaries: the typed request boundary, the reconstruction refusal, the dispatch record and the served route set.
from __future__ import annotations

import pathlib

import pytest

_FLAG = {"x": 0.9, "y": 0.1, "z": 0.0, "span": 0.05, "patch": "wing", "note": "collapsed"}


# accept never rebuilds, so it cannot carry rebuild flags

def test_accept_with_no_flags_is_accepted():
    from meshpipeline.api.schemas.job import DisputeIn

    body = DisputeIn(flags=[], comment="good enough for my study", mode="accept")
    assert body.mode == "accept" and body.flags == []


def test_accept_preserves_its_comment():
    from meshpipeline.api.schemas.job import DisputeIn

    body = DisputeIn(flags=[], comment="the wake is coarse but fine for this study", mode="accept")
    assert body.comment == "the wake is coarse but fine for this study"


@pytest.mark.parametrize("n", [1, 2])
def test_accept_with_flags_is_refused(n):
    from pydantic import ValidationError

    from meshpipeline.api.schemas.job import DisputeFlag, DisputeIn

    with pytest.raises(ValidationError) as exc:
        DisputeIn(flags=[DisputeFlag(**_FLAG) for _ in range(n)], comment="", mode="accept")
    text = str(exc.value)
    assert "accept" in text and "rebuild" in text
    # the refusal explains the product rule; it leaks no identifier, key or payload detail
    for leak in ("of_job_id", "owner", "job_id", "0.9", "wing", "collapsed"):
        assert leak not in text, f"the public refusal leaked {leak!r}"


def test_rebuild_with_flags_is_still_accepted():
    from meshpipeline.api.schemas.job import DisputeFlag, DisputeIn

    body = DisputeIn(flags=[DisputeFlag(**_FLAG)], comment="fix the root", mode="rebuild")
    assert len(body.flags) == 1 and body.flags[0].patch == "wing"


def test_the_shipped_ui_accept_action_sends_no_flags():
    import pathlib
    import re

    src = pathlib.Path("ui/js/viewer/dispute.js").read_text()
    accept = src[src.index("export function acceptMesh"):]
    call = re.search(r"disputeReview\([^)]*\)", accept)
    assert call, "acceptMesh no longer calls disputeReview"
    assert "flags: []" in call.group(0), f"the accept action sends flags: {call.group(0)}"


def test_a_hand_built_accept_payload_cannot_enter_the_review_graph():
    # DEFENCE IN DEPTH: a payload that never passed the schema still reaches the graph through
    # dispatch_payload. Entering with both would compose the human-flag axis and promise a rebuild
    # accept mode never performs.
    from meshpipeline.application.job_service import resolve_dispute_seed
    from meshpipeline.errors import SystemFailure

    class _Log:
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass

    bad = {"of_job_id": "p-1", "mode": "accept", "comment": "", "flags": [_FLAG]}
    with pytest.raises(SystemFailure) as exc:
        resolve_dispute_seed(bad, jlog=_Log())
    assert "accept" in str(exc.value)


def test_the_conditional_axis_is_never_composed_for_an_accepted_mesh():
    # The invariant's point: no accepted mesh can reach a rubric that promises a rebuild.
    from meshpipeline.contracts import human_flags as HF
    from meshpipeline.engines.quality_criteria import (
        HUMAN_FEEDBACK_AXIS_NAME,
        compose_review_rubric,
    )

    accept_no_flags = {"of_job_id": "p", "mode": "accept", "comment": "fine", "flags": []}
    axes = compose_review_rubric("gmsh", "external_cfd", accept_no_flags, HF.PHASE_PARENT)
    assert HUMAN_FEEDBACK_AXIS_NAME not in {a.name for a in axes}


# the served route set is the supported route set

def test_the_removed_quality_route_is_absent_from_the_served_surface():
    from meshpipeline.api.app import app

    paths = set(app.openapi()["paths"])
    assert "/api/v1/simulation/{job_id}/quality" not in paths
    # the surface route, which carries the same quality projection, is still served
    assert "/api/v1/simulation/{job_id}/surface" in paths


def test_the_served_routes_are_exactly_the_supported_set():
    from meshpipeline.api.app import app

    served = {(m.upper(), p)
              for p, ops in app.openapi()["paths"].items() for m in ops}
    expected = {
        ("GET", "/api/v1/chat/history/{session_id}"),
        ("POST", "/api/v1/chat/message"),
        ("GET", "/api/v1/client-config"),
        ("GET", "/api/v1/simulation/{job_id}"),
        ("POST", "/api/v1/simulation/{job_id}/dispute"),
        ("GET", "/api/v1/simulation/{job_id}/surface"),
        ("GET", "/api/v1/simulation/{job_id}/surface.vtk"),     # the viewer's ParaView export
        ("POST", "/api/v1/upload/step-file"),
        ("POST", "/api/v1/ws/ticket"),
    }
    v1 = {r for r in served if r[1].startswith("/api/v1/")}
    assert v1 == expected, (
        "the versioned API surface changed. Every route here is a supported capability with a "
        f"caller; adding one without a consumer is what made /quality dead code.\n"
        f"  added:   {sorted(v1 - expected)}\n  removed: {sorted(expected - v1)}")


def test_no_tracked_source_references_the_removed_endpoint():
    import subprocess

    # THIS FILE IS EXCLUDED, because it necessarily names the symbol it is asserting about - and a
    # search that matches itself never fails. A git failure is also a failure here: an empty result
    # from a command that did not run would be a vacuous pass, which is the shape of the very
    # problem this suite exists for.
    proc = subprocess.run(
        ["git", "grep", "-n", "get_quality", "--", "src", "tests", "ui", "docs",
         f":(exclude){pathlib.Path(__file__).name}", ":(exclude)*test_feature_lifecycle_contracts.py"],
        capture_output=True, text=True)
    assert proc.returncode in (0, 1), (
        f"the search itself failed ({proc.returncode}); an empty result would prove nothing: "
        f"{proc.stderr.strip()[:200]}")
    hits = proc.stdout.strip()
    assert hits == "", f"a caller of the removed endpoint survives:\n{hits}"


# durable lifecycle fields have a writer and a reader

def test_the_dispatch_bookkeeping_columns_have_a_writer():
    import inspect

    from meshpipeline.persistence.repositories.job_repository import JobRepository

    src = inspect.getsource(JobRepository.mark_launched)
    for field in ("pipeline_backend", "pipeline_submitted_at", "pipeline_dispatch_state"):
        assert field in src, f"{field} has no writer at the dispatch authority"


def test_the_acceptance_record_is_written_only_once():
    import inspect

    from meshpipeline.persistence.repositories.job_repository import JobRepository

    src = inspect.getsource(JobRepository.mark_launched)
    # the idempotency guard is what stops a redelivery moving the original acceptance moment
    assert "pipeline_submitted_at.is_(None)" in src


def test_the_reconciliation_claim_is_due_time_aware():
    import inspect

    from meshpipeline.persistence.repositories.reconciliation_repository import (
        ReconciliationRepository,
    )

    claim = inspect.getsource(ReconciliationRepository.claim_pending)
    assert "next_attempt_at" in claim, "the claim ignores the schedule the column exists for"
    assert "func.now()" in claim, "the due comparison must be made by the database, not the worker"
    assert "skip_locked=True" in claim, "concurrent claimers must still be mutually exclusive"


def test_the_retry_delay_is_a_catalogue_setting():
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.settings.inventory import all_vars

    assert isinstance(polcfg.RECONCILE_RETRY_DELAY_SECONDS, int)
    assert polcfg.RECONCILE_RETRY_DELAY_SECONDS > 0
    names = {v.name for v in all_vars()}
    assert "RECONCILE_RETRY_DELAY_SECONDS" in names, "the setting is not in the one catalogue"
