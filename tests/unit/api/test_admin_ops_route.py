# Responsibility: Verify the launch admin surface is cross-tenant, guarded, and append-only where
# it mutates billing facts.
from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import admin_ops

ADMIN_KEY = "admin-secret-value"
HEADERS = {"X-Admin-Key": ADMIN_KEY}


class _Row:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _OrgRepo:
    def __init__(self):
        self.seen_limits = []
        self.organization = _Row(
            id=uuid.uuid4(),
            name="Acme",
            slug="acme",
            plan="team",
            subscription_status="active",
            stripe_customer_id="cus_1",
            current_period_end=None,
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        self.rows = [(self.organization, 2500)]

    async def list_with_billing(self, db, *, limit=100):
        self.seen_limits.append(limit)
        return self.rows[:limit]

    async def get_by_id(self, db, organization_id):
        return self.organization if organization_id == self.organization.id else None


class _JobRepo:
    def __init__(self, organization_id: uuid.UUID):
        self.job = _Row(
            id=uuid.uuid4(),
            owner_id="owner@example.com",
            organization_id=organization_id,
            status="running",
            created_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 26, 10, 5, tzinfo=UTC),
            started_at=datetime(2026, 9, 26, 10, 1, tzinfo=UTC),
            ended_at=None,
            current_attempt=2,
            failed_reason=None,
            pipeline_backend="deferred",
            pipeline_dispatch_state="submitted",
            pipeline_execution_id="exec-1",
            pipeline_submitted_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
            pipeline_launch_error=None,
            lease_heartbeat_at=datetime(2026, 9, 26, 10, 4, tzinfo=UTC),
        )

    async def list_recent_admin(self, db, *, limit=50, organization_id=None):
        if organization_id and organization_id != self.job.organization_id:
            return []
        return [(self.job, "duct flow", "Acme")]

    async def stats_by_organization(self, db, *, organization_id=None):
        if organization_id is not None and organization_id != self.job.organization_id:
            return {}
        return {
            self.job.organization_id: {
                "active_runs": 1,
                "last_run_at": self.job.created_at.isoformat(),
                "run_count": 1,
            }
        }


class _LedgerRepo:
    def __init__(self):
        self.entries = []

    async def list_for_org(self, db, *, organization_id, limit=25, before=None):
        return self.entries


class _Members:
    async def list_members(self, db, *, organization_id):
        return [(_Row(email="owner@example.com", name="Owner"), "owner")]


def _client(monkeypatch, orgs: _OrgRepo, jobs: _JobRepo, ledger: _LedgerRepo):
    @contextlib.asynccontextmanager
    async def _no_db():
        yield object()

    monkeypatch.setattr(admin_ops, "get_db", _no_db)
    monkeypatch.setattr(admin_ops, "organization_repo", lambda: orgs)
    monkeypatch.setattr(admin_ops, "job_repo", lambda: jobs)
    monkeypatch.setattr(admin_ops, "ledger_repo", lambda: ledger)
    monkeypatch.setattr(admin_ops, "membership_repo", lambda: _Members())
    monkeypatch.setattr(admin_ops.credit_service, "balance", _balance)
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", ADMIN_KEY)
    app = FastAPI()
    app.include_router(admin_ops.router, prefix="/api/v1/admin/ops")
    return TestClient(app, raise_server_exceptions=False)


async def _balance(db, *, organization_id):
    return 2500


def test_activity_lists_recent_runs_across_tenants(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    body = client.get("/api/v1/admin/ops/activity", headers=HEADERS).json()

    assert body["runs"][0]["organization_name"] == "Acme"
    assert body["runs"][0]["task_label"] == "duct flow"
    assert body["runs"][0]["pipeline_dispatch_state"] == "submitted"


def test_customers_list_contains_operator_summary(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    body = client.get("/api/v1/admin/ops/organizations", headers=HEADERS).json()

    row = body["organizations"][0]
    assert row["balance"] == 2500
    assert row["run_count"] == 1
    assert row["active_runs"] == 1


def test_customers_list_can_request_every_organization(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    res = client.get("/api/v1/admin/ops/organizations?limit=0", headers=HEADERS)

    assert res.status_code == 200
    assert orgs.seen_limits == [None]


def test_org_detail_includes_members_ledger_and_recent_runs(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    body = client.get(f"/api/v1/admin/ops/organizations/{orgs.organization.id}", headers=HEADERS).json()

    assert body["organization"]["name"] == "Acme"
    assert body["members"][0]["email"] == "owner@example.com"
    assert body["recent_runs"][0]["status"] == "running"


def test_credit_grant_appends_a_positive_ledger_entry(monkeypatch):
    orgs = _OrgRepo()
    grants = []

    operation_id = uuid.uuid4()

    async def _grant(db, *, organization_id, amount, reason="", entry_id=None):
        grants.append({
            "amount": amount,
            "entry_id": entry_id,
            "organization_id": organization_id,
            "reason": reason,
        })

    monkeypatch.setattr(admin_ops.credit_service, "grant", _grant)
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    res = client.post(
        f"/api/v1/admin/ops/organizations/{orgs.organization.id}/credit-grants",
        headers=HEADERS,
        json={"amount": 1500, "operation_id": str(operation_id), "reason": "launch goodwill"},
    )

    assert res.status_code == 200
    assert grants == [{
        "amount": 1500,
        "entry_id": operation_id,
        "organization_id": orgs.organization.id,
        "reason": "launch goodwill",
    }]


def test_credit_grant_requires_an_operation_id(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    res = client.post(
        f"/api/v1/admin/ops/organizations/{orgs.organization.id}/credit-grants",
        headers=HEADERS,
        json={"amount": 1500, "reason": "launch goodwill"},
    )

    assert res.status_code == 422


def test_credit_grant_rejects_negative_amount(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    res = client.post(
        f"/api/v1/admin/ops/organizations/{orgs.organization.id}/credit-grants",
        headers=HEADERS,
        json={"amount": -1, "operation_id": str(uuid.uuid4()), "reason": "not a grant"},
    )

    assert res.status_code == 422


def test_credit_grant_rejects_blank_reason(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    res = client.post(
        f"/api/v1/admin/ops/organizations/{orgs.organization.id}/credit-grants",
        headers=HEADERS,
        json={"amount": 1, "operation_id": str(uuid.uuid4()), "reason": "   "},
    )

    assert res.status_code == 422


def test_every_route_carries_the_guard(monkeypatch):
    orgs = _OrgRepo()
    client = _client(monkeypatch, orgs, _JobRepo(orgs.organization.id), _LedgerRepo())

    probes = [
        ("GET", "/api/v1/admin/ops/activity", None),
        ("GET", "/api/v1/admin/ops/organizations", None),
        ("GET", f"/api/v1/admin/ops/organizations/{orgs.organization.id}", None),
        (
            "POST",
            f"/api/v1/admin/ops/organizations/{orgs.organization.id}/credit-grants",
            {"amount": 1, "operation_id": str(uuid.uuid4()), "reason": "guard probe"},
        ),
    ]

    for method, path, body in probes:
        res = client.request(method, path, json=body)
        assert res.status_code == 403, f"{method} {path} did not require the admin credential"
