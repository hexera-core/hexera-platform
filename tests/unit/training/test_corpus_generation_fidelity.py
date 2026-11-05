# Responsibility: Prove a corpus sample speaks for exactly one execution generation.
# Boundaries: the export projection and the generation it selects; capture's own write rules are elsewhere.
from __future__ import annotations

import json

import pytest
from tests.capture_authority import InMemoryAuthority, export_projection

OWNER = "corpus-owner"
JOB = "job-gen"


def _authority():
    a = InMemoryAuthority()
    # a superseded worker's records are KEPT, stamped with the generation that produced them
    a.record_operation(owner_id=OWNER, job_id=JOB, execution_generation=1, op_key="op-stale",
                       name="builder.turn", payload={"marker": "STALE-GEN1"})
    a.record_operation(owner_id=OWNER, job_id=JOB, execution_generation=2, op_key="op-live",
                       name="builder.turn", payload={"marker": "LIVE-GEN2"})
    return a


def test_the_projection_carries_the_generation_that_produced_each_record():
    rows = export_projection(_authority(), OWNER, JOB, execution_generation=2)
    assert rows, "the projection rendered nothing"
    assert all("execution_generation" in r for r in rows), (
        "an exported capture record does not say which epoch produced it, so a consumer cannot "
        "tell a superseded worker's output from the live run")


def test_only_the_selected_generation_is_exported():
    rows = export_projection(_authority(), OWNER, JOB, execution_generation=2)
    assert {r["execution_generation"] for r in rows} == {2}
    blob = json.dumps(rows)
    assert "STALE-GEN1" in json.dumps(export_projection(_authority(), OWNER, JOB,
                                                        execution_generation=1))
    assert "STALE-GEN1" not in blob, "a superseded generation's payload entered the live sample"
    assert "LIVE-GEN2" in blob


def test_another_owner_and_another_job_are_excluded():
    a = _authority()
    a.record_operation(owner_id="stranger", job_id=JOB, execution_generation=2, op_key="op-x",
                       name="builder.turn", payload={"marker": "STRANGER"})
    a.record_operation(owner_id=OWNER, job_id="other-job", execution_generation=2, op_key="op-y",
                       name="builder.turn", payload={"marker": "OTHERJOB"})
    blob = json.dumps(export_projection(a, OWNER, JOB, execution_generation=2))
    assert "STRANGER" not in blob and "OTHERJOB" not in blob


def test_a_repeated_operation_identity_stays_one_record():
    a = _authority()
    a.record_operation(owner_id=OWNER, job_id=JOB, execution_generation=2, op_key="op-live",
                       name="builder.turn", payload={"marker": "LIVE-GEN2"})
    rows = export_projection(a, OWNER, JOB, execution_generation=2)
    assert len(rows) == 1, f"a replayed operation identity produced {len(rows)} records"


def test_the_projection_is_deterministic_from_unchanged_state():
    a = _authority()
    assert export_projection(a, OWNER, JOB, execution_generation=2) == \
           export_projection(a, OWNER, JOB, execution_generation=2)


def test_hashes_and_ordering_survive_the_projection():
    a = _authority()
    rows = export_projection(a, OWNER, JOB, execution_generation=2)
    raw = a.trusted_operations(owner_id=OWNER, job_id=JOB, execution_generation=2)
    assert [r["payload_sha256"] for r in rows] == [r["payload_sha256"] for r in raw]
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in raw)


def test_the_production_projection_never_queries_every_generation():
    # The defect was a call with the generation filter omitted, which merged epochs silently.
    import inspect

    from meshpipeline.application.maintenance import export as X

    src = inspect.getsource(X._render_corpus_projection)
    assert "authoritative_generation" in src, (
        "the export no longer selects one authoritative generation before reading capture rows")
    assert "execution_generation=generation" in src, (
        "the export reads capture rows without pinning the generation it selected")


@pytest.mark.parametrize("gen", [1, 2])
def test_each_generation_can_be_exported_on_its_own(gen):
    rows = export_projection(_authority(), OWNER, JOB, execution_generation=gen)
    assert {r["execution_generation"] for r in rows} == {gen}
