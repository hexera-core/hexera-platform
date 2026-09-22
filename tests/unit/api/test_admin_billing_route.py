# Responsibility: Verify the product's ONLY cross-tenant surface refuses everyone it should.
#
# These routes deliberately do not scope on a tenant, which makes their credential the whole of
# their security. Every other route in this product would leak one customer's data if its guard
# broke; these would leak all of them.
from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import admin_billing
from meshpipeline.contracts import billing as billing_contract
from meshpipeline.contracts.billing import Invoice

ADMIN_KEY = "admin-secret-value"
HEADERS = {"X-Admin-Key": ADMIN_KEY}


class _Org:
    def __init__(self, **fields):
        self.stripe_customer_id = None
        self.current_period_end = None
        self.created_at = None
        self.plan = ""
        self.subscription_status = ""
        for key, value in fields.items():
            setattr(self, key, value)


class _OrgRepo:
    def __init__(self, rows=(), organization=None):
        self.rows = list(rows)
        self.organization = organization
        self.attached: list = []

    async def list_with_billing(self, db, *, limit=100):
        return self.rows[:limit]

    async def get_by_id(self, db, organization_id):
        return self.organization

    async def attach_customer(self, db, *, organization_id, stripe_customer_id):
        self.attached.append(stripe_customer_id)


class _LedgerRepo:
    async def usage_by_period(self, db, *, months=6):
        return [{"period": "2026-09-01T00:00:00", "granted": 10, "spent": 4, "entries": 2}]

    async def unmetered_total(self, db):
        return {"entries": 3, "credits": 42}

    async def list_for_org(self, db, *, organization_id, limit=50, before=None):
        return []


class _Gateway:
    def __init__(self):
        self.created: list = []

    def ensure_customer(self, *, organization_id, email, name):
        return "cus_new"

    def list_invoices(self, *, customer_id, limit=10):
        return [Invoice(id="in_1", number="A-1", status="open", amount_due=5000,
                        amount_paid=0, currency="usd", created_at=None,
                        hosted_url="https://pay.example/in_1")]

    def create_invoice(self, *, customer_id, amount, currency, description, days_until_due):
        self.created.append({"customer_id": customer_id, "amount": amount,
                             "currency": currency, "days_until_due": days_until_due})
        return Invoice(id="in_2", number="A-2", status="open", amount_due=amount,
                       amount_paid=0, currency=currency, created_at=None, hosted_url="")


@pytest.fixture
def orgs():
    return _OrgRepo()


@pytest.fixture
def client(monkeypatch, orgs):
    @contextlib.asynccontextmanager
    async def _no_db():
        yield object()

    monkeypatch.setattr(admin_billing, "get_db", _no_db)
    monkeypatch.setattr(admin_billing, "organization_repo", lambda: orgs)
    monkeypatch.setattr(admin_billing, "ledger_repo", _LedgerRepo)
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", ADMIN_KEY)
    app = FastAPI()
    app.include_router(admin_billing.router, prefix="/api/v1/admin/billing")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clear_gateway():
    yield
    billing_contract.set_billing_gateway(None)


# THE CREDENTIAL

def test_no_credential_is_refused(client):
    assert client.get("/api/v1/admin/billing/organizations").status_code == 403


def test_a_wrong_credential_is_refused(client):
    res = client.get("/api/v1/admin/billing/organizations",
                     headers={"X-Admin-Key": "not-the-key"})
    assert res.status_code == 403


def test_an_unconfigured_deployment_hides_the_routes_entirely(client, monkeypatch):
    # 404, NOT 503. A deployment that has not enabled cross-tenant access should not advertise that
    # the capability exists and is merely unconfigured - that is a map for whoever comes looking.
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", "")
    assert client.get("/api/v1/admin/billing/organizations").status_code == 404


def test_a_blank_credential_does_not_match_a_blank_configuration(client, monkeypatch):
    # THE EMPTY-EQUALS-EMPTY TRAP. If the unconfigured check were removed, `compare_digest("", "")`
    # is True - so every unconfigured deployment would silently grant cross-tenant access to a
    # caller who simply sent no header at all.
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", "")
    res = client.get("/api/v1/admin/billing/organizations", headers={"X-Admin-Key": ""})
    assert res.status_code == 404


def test_every_route_carries_the_guard():
    # READ FROM THE SOURCE, not from FastAPI's resolved dependency tree. The tree's shape is a
    # private detail that has already changed once between versions, and a guard test that breaks on
    # an upgrade gets "fixed" by loosening it - which is the one outcome this must not have.
    #
    # An ALLOWLIST of the guarded set, not a spot check: a route added here without the dependency
    # is unscoped AND unauthenticated, and nothing else in the suite would notice.
    import ast
    import pathlib

    source = pathlib.Path(admin_billing.__file__).read_text()
    unguarded = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"):
                continue
            if "Depends(admin_dep)" not in ast.unparse(dec):
                unguarded.append(node.name)
    assert unguarded == [], unguarded


# THE READS

def test_organizations_are_listed_with_their_balance(client, orgs):
    org_id = uuid.uuid4()
    orgs.rows = [(_Org(id=org_id, name="Acme", slug="acme", plan="team",
                       subscription_status="active", stripe_customer_id="cus_1"), 2_500)]
    body = client.get("/api/v1/admin/billing/organizations", headers=HEADERS).json()
    row = body["organizations"][0]
    assert row["balance"] == 2_500
    assert row["plan"] == "team"
    # The included allowance is resolved from the catalogue, not stored on the row - so a pricing
    # change is reflected here without a backfill.
    assert row["included_credits"] == 10_000


def test_an_organisation_with_no_ledger_still_appears(client, orgs):
    # The tenants with nothing are the interesting ones on an operator's list; an INNER join would
    # hide exactly them.
    orgs.rows = [(_Org(id=uuid.uuid4(), name="New", slug="new"), 0)]
    body = client.get("/api/v1/admin/billing/organizations", headers=HEADERS).json()
    assert body["organizations"][0]["balance"] == 0
    assert body["organizations"][0]["has_billing_account"] is False


def test_usage_reports_the_meter_backlog_alongside_the_periods(client):
    # The backlog is the caveat on the spend column: a large pending figure means the consumption is
    # real but has not reached an invoice yet.
    body = client.get("/api/v1/admin/billing/usage", headers=HEADERS).json()
    assert body["periods"][0]["spent"] == 4
    assert body["pending_meter"] == {"entries": 3, "credits": 42}


def test_a_malformed_organisation_id_is_a_404(client):
    res = client.get("/api/v1/admin/billing/organizations/not-a-uuid/ledger", headers=HEADERS)
    assert res.status_code == 404


def test_invoices_for_an_organisation_without_an_account_are_empty_not_an_error(client, orgs):
    orgs.organization = _Org(id=uuid.uuid4(), name="Acme", slug="acme")
    res = client.get(f"/api/v1/admin/billing/organizations/{uuid.uuid4()}/invoices",
                     headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["invoices"] == []


def test_invoices_are_flattened_out_of_the_provider_shape(client, orgs):
    orgs.organization = _Org(id=uuid.uuid4(), name="Acme", slug="acme",
                             stripe_customer_id="cus_1")
    billing_contract.set_billing_gateway(_Gateway())
    body = client.get(f"/api/v1/admin/billing/organizations/{uuid.uuid4()}/invoices",
                      headers=HEADERS).json()
    assert body["invoices"][0]["amount_due"] == 5000
    assert body["invoices"][0]["hosted_url"] == "https://pay.example/in_1"


# RAISING AN INVOICE

def test_an_invoice_can_be_raised_for_an_enterprise_account(client, orgs):
    orgs.organization = _Org(id=uuid.uuid4(), name="Acme", slug="acme",
                             stripe_customer_id="cus_1")
    gateway = _Gateway()
    billing_contract.set_billing_gateway(gateway)
    res = client.post(f"/api/v1/admin/billing/organizations/{uuid.uuid4()}/invoices",
                      headers=HEADERS,
                      json={"amount": 250_000, "currency": "USD", "description": "Q4 seats"})
    assert res.status_code == 200
    # THE CURRENCY IS LOWERCASED on the way out: the provider rejects "USD", and an operator typing
    # it in capitals is the ordinary case rather than a mistake worth refusing.
    assert gateway.created == [{"customer_id": "cus_1", "amount": 250_000,
                                "currency": "usd", "days_until_due": 30}]


def test_an_account_without_a_customer_gets_one_before_being_invoiced(client, orgs):
    # An enterprise account may never have touched self-serve checkout. Refusing here would mean the
    # only way to bill them is to first make them buy something they do not want.
    orgs.organization = _Org(id=uuid.uuid4(), name="Acme", slug="acme")
    billing_contract.set_billing_gateway(_Gateway())
    res = client.post(f"/api/v1/admin/billing/organizations/{uuid.uuid4()}/invoices",
                      headers=HEADERS,
                      json={"amount": 1000, "description": "Setup"})
    assert res.status_code == 200
    assert orgs.attached == ["cus_new"]


@pytest.mark.parametrize("amount", [0, -1])
def test_a_non_positive_invoice_is_refused(client, orgs, amount):
    # A zero or negative invoice is a credit note, which is a different object with different rules.
    # Accepting one here would produce a provider error long after the operator has moved on.
    orgs.organization = _Org(id=uuid.uuid4(), name="Acme", slug="acme",
                             stripe_customer_id="cus_1")
    billing_contract.set_billing_gateway(_Gateway())
    res = client.post(f"/api/v1/admin/billing/organizations/{uuid.uuid4()}/invoices",
                      headers=HEADERS, json={"amount": amount, "description": "x"})
    assert res.status_code == 422


def test_raising_an_invoice_without_a_configured_provider_is_a_503(client, orgs):
    orgs.organization = _Org(id=uuid.uuid4(), name="Acme", slug="acme",
                             stripe_customer_id="cus_1")
    billing_contract.set_billing_gateway(None)
    res = client.post(f"/api/v1/admin/billing/organizations/{uuid.uuid4()}/invoices",
                      headers=HEADERS, json={"amount": 100, "description": "x"})
    assert res.status_code == 503


# THE METER SWEEP, reachable over HTTP because the hosted deployment runs no Beat

def test_the_sweep_requires_the_admin_credential(client):
    assert client.post("/api/v1/admin/billing/meter/sweep").status_code == 403


def test_the_sweep_runs_and_reports_what_it_did(client, monkeypatch):
    from meshpipeline.application import metering_service

    calls: list = []

    async def _sweep(db, *, limit):
        calls.append(limit)
        return {"reported": 2, "skipped": 0, "billing": "configured"}

    monkeypatch.setattr(metering_service, "report_pending_usage", _sweep)
    res = client.post("/api/v1/admin/billing/meter/sweep", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["reported"] == 2
    # NO LIMIT GIVEN falls back to the service's own batch size rather than to an unbounded scan.
    assert calls == [metering_service.SWEEP_BATCH]


def test_the_sweep_limit_is_bounded(client, monkeypatch):
    # A scheduler - or a finger slip - must not be able to ask for an unbounded transaction that
    # holds a connection for minutes.
    from meshpipeline.application import metering_service

    calls: list = []

    async def _sweep(db, *, limit):
        calls.append(limit)
        return {"reported": 0, "skipped": 0, "billing": "configured"}

    monkeypatch.setattr(metering_service, "report_pending_usage", _sweep)
    client.post("/api/v1/admin/billing/meter/sweep?limit=999999", headers=HEADERS)
    assert calls == [1000]
