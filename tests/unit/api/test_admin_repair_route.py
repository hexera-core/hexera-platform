# Responsibility: Verify the operator repair queue is guarded, cross-tenant, and records every decision it applies.
from __future__ import annotations

import ast
import contextlib
import inspect
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import admin_repair
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import RepairJobStatus as S

ADMIN_KEY = "admin-secret-value"
HEADERS = {"X-Admin-Key": ADMIN_KEY}


class _Job:
    def __init__(self, **f):
        now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        self.id = f.get("id", uuid.uuid4())
        self.owner_id = f.get("owner_id", "owner-a")
        self.organization_id = f.get("organization_id")
        self.status = f.get("status", S.received)
        self.target_engine = f.get("target_engine", "snappy")
        self.service_priority = f.get("service_priority", 100)
        self.repair_status = f.get("repair_status", "")
        self.current_strategy = f.get("current_strategy", "")
        self.assigned_operator = f.get("assigned_operator")
        self.blocked_reason = f.get("blocked_reason")
        self.created_at = now
        self.updated_at = now
        self.assigned_at = None


class _Repo:
    """A double that honours the real repository's contract: the transition is decided by the
    legal-source table, not by whatever the caller asked for."""

    def __init__(self, jobs=None):
        self.jobs = {j.id: j for j in (jobs or [])}
        self.decisions: list[dict] = []
        self.queue_args: list[dict] = []
        self.assigned: list[tuple] = []

    async def operator_queue(self, db, *, statuses=(), assigned_operator=None,
                             unassigned_only=False, limit=50):
        self.queue_args.append({"statuses": statuses, "operator": assigned_operator,
                                "unassigned": unassigned_only, "limit": limit})
        rows = list(self.jobs.values())
        if statuses:
            rows = [j for j in rows if j.status in statuses]
        if unassigned_only:
            rows = [j for j in rows if j.assigned_operator is None]
        elif assigned_operator:
            rows = [j for j in rows if j.assigned_operator == assigned_operator]
        rows.sort(key=lambda j: (j.service_priority, j.created_at))
        return rows[:limit]

    async def operator_job(self, db, job_id):
        # the repository's CROSS-TENANT read, named as such - `get_internal` is the worker's seam
        # and the architecture suite forbids a request handler from touching it
        return self.jobs.get(job_id)

    async def attempts_for_job(self, db, job_id):
        return []

    async def decisions_for_job(self, db, job_id):
        return [_Decision(**d) for d in self.decisions if d["repair_job_id"] == job_id]

    async def assign(self, db, job_id, *, operator, claim_only_if_unassigned=False):
        job = self.jobs[job_id]
        self.assigned.append((job_id, operator, claim_only_if_unassigned))
        if claim_only_if_unassigned and job.assigned_operator is not None:
            return False
        job.assigned_operator = operator or None
        job.assigned_at = datetime(2026, 10, 1, 10, 0, tzinfo=UTC) if operator else None
        return True

    async def transition(self, db, job_id, target, *, blocked_reason=None,
                         current_strategy=None, repair_status=None, simulation_job_id=None):
        from meshpipeline.persistence.repair_job_state import legal_sources
        job = self.jobs.get(job_id)
        if job is None:
            return TransitionResult.not_found
        if job.status in legal_sources(target):
            job.status = target
            if blocked_reason is not None:
                job.blocked_reason = blocked_reason or None
            if current_strategy is not None:
                job.current_strategy = current_strategy
            return TransitionResult.applied
        if job.status == target:
            return TransitionResult.already_at_target
        return TransitionResult.rejected_current_state

    async def record_decision(self, db, *, repair_job_id, decision, actor, from_status="",
                              reason="", notes=""):
        row = {"repair_job_id": repair_job_id, "decision": decision, "actor": actor,
               "from_status": from_status, "reason": reason or None, "notes": notes or None}
        self.decisions.append(row)
        return _Decision(**row)


class _Decision:
    def __init__(self, **f):
        self.decision = f["decision"]
        self.from_status = f.get("from_status", "")
        self.actor = f["actor"]
        self.reason = f.get("reason")
        self.notes = f.get("notes")
        self.created_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)


class _Session:
    """The least session the routes use: they write through the repository and commit once."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _client(monkeypatch, repo: _Repo):
    @contextlib.asynccontextmanager
    async def _no_db():
        yield _Session()

    monkeypatch.setattr(admin_repair, "get_db", _no_db)
    # `repo` is a factory on the module, so the route resolves persistence per call rather than
    # importing it at module scope; the double is substituted the same way
    monkeypatch.setattr(admin_repair, "repo", lambda: repo)
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", ADMIN_KEY)
    app = FastAPI()
    app.include_router(admin_repair.router, prefix="/api/v1/admin/repair")
    return TestClient(app, raise_server_exceptions=False)


# THE GUARD


def test_every_route_is_behind_the_admin_credential():
    source = inspect.getsource(admin_repair)
    tree = ast.parse(source)
    routes = [n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
              and any("router." in ast.unparse(d) for d in n.decorator_list)]
    assert routes, "no routes found - the guard would pass vacuously"
    for fn in routes:
        decorators = " ".join(ast.unparse(d) for d in fn.decorator_list)
        assert "Depends(admin_dep)" in decorators, f"{fn.name} is not guarded"


def test_a_wrong_or_absent_credential_is_refused(monkeypatch):
    client = _client(monkeypatch, _Repo([_Job()]))
    assert client.get("/api/v1/admin/repair/queue").status_code == 403
    assert client.get("/api/v1/admin/repair/queue",
                      headers={"X-Admin-Key": "nope"}).status_code == 403


def test_an_unconfigured_deployment_does_not_advertise_the_surface(monkeypatch):
    client = _client(monkeypatch, _Repo([_Job()]))
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", "")
    # 404, not 503: a deployment with no operator surface should not say the capability exists
    assert client.get("/api/v1/admin/repair/queue", headers=HEADERS).status_code == 404


# THE QUEUE


def test_the_queue_is_cross_tenant_and_most_urgent_first(monkeypatch):
    low = _Job(owner_id="owner-a", service_priority=200)
    urgent = _Job(owner_id="owner-b", service_priority=5)
    client = _client(monkeypatch, _Repo([low, urgent]))

    body = client.get("/api/v1/admin/repair/queue", headers=HEADERS).json()

    # two different customers, one list - which is the point of this surface
    assert [j["id"] for j in body["jobs"]] == [str(urgent.id), str(low.id)]
    assert {j["owner_id"] for j in body["jobs"]} == {"owner-a", "owner-b"}


def test_the_queue_narrows_by_status_and_echoes_the_filter(monkeypatch):
    waiting = _Job(status=S.waiting_customer)
    client = _client(monkeypatch, _Repo([waiting, _Job(status=S.received)]))

    body = client.get("/api/v1/admin/repair/queue?status=waiting_customer",
                      headers=HEADERS).json()

    assert [j["id"] for j in body["jobs"]] == [str(waiting.id)]
    # echoed so a client can tell an empty queue from a filter that matched nothing
    assert body["filter"]["status"] == ["waiting_customer"]


def test_a_misspelled_status_filter_is_refused_rather_than_ignored(monkeypatch):
    client = _client(monkeypatch, _Repo([_Job()]))
    res = client.get("/api/v1/admin/repair/queue?status=waitng_customer", headers=HEADERS)
    # answering a typo with every row is how an operator works the wrong list
    assert res.status_code == 422 and "waitng_customer" in res.json()["detail"]


def test_the_queue_can_show_only_unclaimed_work(monkeypatch):
    mine = _Job(assigned_operator="ana")
    free = _Job()
    client = _client(monkeypatch, _Repo([mine, free]))

    body = client.get("/api/v1/admin/repair/queue?unassigned=true", headers=HEADERS).json()
    assert [j["id"] for j in body["jobs"]] == [str(free.id)]


def test_each_row_says_whether_it_is_waiting_blocked_or_settled(monkeypatch):
    client = _client(monkeypatch, _Repo([_Job(status=S.mesh_review)]))
    row = client.get("/api/v1/admin/repair/queue", headers=HEADERS).json()["jobs"][0]
    # answered once, here, so a screen and the metrics cannot disagree about what a state means
    assert (row["awaiting_human"], row["blocked"], row["settled"]) == (True, False, False)


# READING ONE JOB


def test_a_job_lists_only_the_decisions_this_state_allows(monkeypatch):
    job = _Job(status=S.received)
    client = _client(monkeypatch, _Repo([job]))

    body = client.get(f"/api/v1/admin/repair/jobs/{job.id}", headers=HEADERS).json()

    # from `received` an operator may inspect, ask the customer, or stop - it may not deliver
    assert "inspect" in body["available_decisions"]
    assert "ask_customer" in body["available_decisions"]
    assert "deliver" not in body["available_decisions"]
    assert "review_mesh" not in body["available_decisions"]


def test_an_unknown_job_is_a_404_and_so_is_a_malformed_id(monkeypatch):
    client = _client(monkeypatch, _Repo([]))
    assert client.get(f"/api/v1/admin/repair/jobs/{uuid.uuid4()}",
                      headers=HEADERS).status_code == 404
    assert client.get("/api/v1/admin/repair/jobs/not-a-uuid",
                      headers=HEADERS).status_code == 404


# ASSIGNMENT


def test_claiming_an_unheld_job_takes_it(monkeypatch):
    job = _Job()
    client = _client(monkeypatch, _Repo([job]))

    body = client.post(f"/api/v1/admin/repair/jobs/{job.id}/assign",
                       json={"operator": "ana"}, headers=HEADERS).json()

    assert body["job"]["assigned_operator"] == "ana"
    assert body["job"]["assigned_at"] is not None


def test_two_operators_cannot_both_believe_they_claimed_it(monkeypatch):
    job = _Job(assigned_operator="ana")
    client = _client(monkeypatch, _Repo([job]))

    res = client.post(f"/api/v1/admin/repair/jobs/{job.id}/assign",
                      json={"operator": "ben"}, headers=HEADERS)

    # 409, not 403: the caller is entitled to the queue, it just lost the race for this item
    assert res.status_code == 409 and "ana" in res.json()["detail"]
    assert job.assigned_operator == "ana"


def test_a_lead_may_reassign_held_work(monkeypatch):
    job = _Job(assigned_operator="ana")
    client = _client(monkeypatch, _Repo([job]))

    res = client.post(f"/api/v1/admin/repair/jobs/{job.id}/assign",
                      json={"operator": "ben", "claim": False}, headers=HEADERS)

    assert res.status_code == 200
    assert job.assigned_operator == "ben"


def test_releasing_a_job_returns_it_to_the_unassigned_queue(monkeypatch):
    job = _Job(assigned_operator="ana")
    client = _client(monkeypatch, _Repo([job]))

    body = client.post(f"/api/v1/admin/repair/jobs/{job.id}/assign",
                       json={"operator": ""}, headers=HEADERS).json()

    assert body["job"]["assigned_operator"] is None
    assert body["job"]["assigned_at"] is None


# DECISIONS


def test_a_decision_moves_the_job_and_is_recorded_with_its_actor(monkeypatch):
    job = _Job(status=S.received)
    repo = _Repo([job])
    client = _client(monkeypatch, repo)

    body = client.post(f"/api/v1/admin/repair/jobs/{job.id}/decide",
                       json={"decision": "inspect", "actor": "ana"}, headers=HEADERS).json()

    assert body["job"]["status"] == "inspecting"
    assert body["applied"] == "applied"
    recorded = repo.decisions[-1]
    # WHO decided, and WHERE FROM: a status column can say neither
    assert (recorded["actor"], recorded["decision"], recorded["from_status"]) == (
        "ana", "inspect", "received")


def test_a_decision_the_state_does_not_allow_is_refused_without_moving_anything(monkeypatch):
    job = _Job(status=S.received)
    repo = _Repo([job])
    client = _client(monkeypatch, repo)

    res = client.post(f"/api/v1/admin/repair/jobs/{job.id}/decide",
                      json={"decision": "deliver", "actor": "ana"}, headers=HEADERS)

    assert res.status_code == 409 and "received" in res.json()["detail"]
    assert job.status is S.received
    # and nothing was written to the audit for a decision that never took effect
    assert repo.decisions == []


def test_stopping_a_customers_job_must_state_why(monkeypatch):
    job = _Job(status=S.received)
    repo = _Repo([job])
    client = _client(monkeypatch, repo)

    res = client.post(f"/api/v1/admin/repair/jobs/{job.id}/decide",
                      json={"decision": "ask_customer", "actor": "ana"}, headers=HEADERS)

    # a refusal nobody can answer a complaint about is not a decision
    assert res.status_code == 422 and "reason" in res.json()["detail"]
    assert job.status is S.received
    assert repo.decisions == []


def test_the_stated_reason_becomes_the_reason_the_customer_is_told(monkeypatch):
    job = _Job(status=S.received)
    client = _client(monkeypatch, _Repo([job]))

    body = client.post(
        f"/api/v1/admin/repair/jobs/{job.id}/decide",
        json={"decision": "ask_customer", "actor": "ana",
              "reason": "the STEP file has no units"}, headers=HEADERS).json()

    # ONE text, not two that can drift apart
    assert body["job"]["blocked_reason"] == "the STEP file has no units"


def test_a_strategy_choice_is_saved_with_the_decision_that_made_it(monkeypatch):
    job = _Job(status=S.inspecting)
    repo = _Repo([job])
    client = _client(monkeypatch, repo)

    body = client.post(
        f"/api/v1/admin/repair/jobs/{job.id}/decide",
        json={"decision": "choose_strategy", "actor": "ana", "strategy": "conservative"},
        headers=HEADERS).json()

    # one request, so the screen cannot half-save: a recorded decision with no strategy behind it
    assert body["job"]["current_strategy"] == "conservative"
    assert repo.decisions[-1]["decision"] == "choose_strategy"


def test_an_unknown_decision_is_refused(monkeypatch):
    job = _Job()
    client = _client(monkeypatch, _Repo([job]))
    res = client.post(f"/api/v1/admin/repair/jobs/{job.id}/decide",
                      json={"decision": "obliterate", "actor": "ana"}, headers=HEADERS)
    assert res.status_code == 422 and "obliterate" in res.json()["detail"]


def test_a_decision_must_name_an_actor(monkeypatch):
    job = _Job()
    client = _client(monkeypatch, _Repo([job]))
    res = client.post(f"/api/v1/admin/repair/jobs/{job.id}/decide",
                      json={"decision": "inspect"}, headers=HEADERS)
    assert res.status_code == 422


def test_every_named_decision_targets_a_real_state():
    # the route's menu and the state machine must speak about the same things
    for name, target in admin_repair._DECISIONS.items():
        assert isinstance(target, S), name
    assert set(admin_repair._NEEDS_REASON) <= set(admin_repair._DECISIONS)


# SHADOW TRIAGE


class _AttemptRow:
    def __init__(self, report):
        self.attempt_no = 1
        self.mode = "inspect"
        self.profile = ""
        self.tool_version = ""
        self.input_sha256 = "a" * 64
        self.output_sha256 = None
        self.status = "repairable"
        self.report = report
        self.caps = None
        self.measurements = None
        self.created_at = datetime(2026, 10, 4, tzinfo=UTC)


def test_a_job_shows_advice_beside_the_evidence(monkeypatch):
    job = _Job(status=S.inspecting, repair_status="repairable")
    repo = _Repo([job])

    async def _attempts(db, job_id):
        return [_AttemptRow({"report": {"defects": [{"code": "wire_gap", "severity": "error"}]}})]

    repo.attempts_for_job = _attempts
    client = _client(monkeypatch, repo)

    body = client.get(f"/api/v1/admin/repair/jobs/{job.id}", headers=HEADERS).json()

    advice = body["recommendation"]
    assert advice["route"] == "conservative_repair"
    # SHADOW: the payload says so itself, and the job did not move
    assert advice["shadow"] is True
    assert body["job"]["status"] == "inspecting"
    assert job.status is S.inspecting


def test_advice_cannot_move_a_job_however_often_it_is_read(monkeypatch):
    job = _Job(status=S.inspecting, repair_status="repairable")
    repo = _Repo([job])
    client = _client(monkeypatch, repo)

    for _ in range(3):
        client.get(f"/api/v1/admin/repair/jobs/{job.id}", headers=HEADERS)

    # reading advice is a read: no transition, no decision row, nothing recorded as done
    assert job.status is S.inspecting
    assert repo.decisions == []


def test_a_job_with_no_inspection_gets_an_honest_abstention(monkeypatch):
    job = _Job(status=S.received)
    client = _client(monkeypatch, _Repo([job]))

    advice = client.get(f"/api/v1/admin/repair/jobs/{job.id}",
                        headers=HEADERS).json()["recommendation"]

    assert advice["route"] == "abstain"
    assert advice["abstain_reason"]
    assert advice["confidence"] == 0.0


# THROUGHPUT


def test_throughput_is_served_from_the_repository_without_reshaping_it(monkeypatch):
    repo = _Repo([])
    counted = {"jobs_total": 3, "delivery_rate": 0.6667, "by_state": {"delivered": 2}}

    async def _throughput(db):
        return counted

    repo.service_throughput = _throughput
    client = _client(monkeypatch, repo)

    body = client.get("/api/v1/admin/repair/throughput", headers=HEADERS).json()

    # the route is transport: a second place that rounds or relabels these is a second authority
    assert body["throughput"] == counted


def test_throughput_needs_the_admin_credential(monkeypatch):
    assert _client(monkeypatch, _Repo([])).get(
        "/api/v1/admin/repair/throughput").status_code == 403
