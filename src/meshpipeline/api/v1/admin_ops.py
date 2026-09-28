# Responsibility: Answer launch-operator questions across tenants, and expose append-only controls.
# Boundaries: HTTP shape, the admin credential, and orchestration over repositories/services.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import meshpipeline.settings.billing as billcfg
import meshpipeline.settings.plans as plancfg
from meshpipeline.api.v1.admin_billing import admin_dep
from meshpipeline.application import credit_service
from meshpipeline.persistence.models import CreditLedgerEntry, Organization, SimulationJob
from meshpipeline.persistence.session import get_db

router = APIRouter()


class CreditGrantRequest(BaseModel):
    amount: int = Field(gt=0, le=10_000_000)
    operation_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=128)


@router.get("/activity", dependencies=[Depends(admin_dep)])
async def read_activity(limit: int = 50) -> dict:
    async with get_db() as db:
        rows = await job_repo().list_recent_admin(db, limit=limit)
    return {"runs": [_run(row[0], row[1], row[2]) for row in rows]}


@router.get("/organizations", dependencies=[Depends(admin_dep)])
async def list_organizations(limit: int = 250) -> dict:
    bounded = None if limit <= 0 else max(1, min(limit, 500))
    async with get_db() as db:
        rows = await organization_repo().list_with_billing(db, limit=bounded)
        stats = await _stats(db)
    return {
        "billing_enabled": billcfg.enabled(),
        "organizations": [_organization(org, balance, stats.get(org.id, {})) for org, balance in rows],
    }


@router.get("/organizations/{organization_id}", dependencies=[Depends(admin_dep)])
async def read_organization(organization_id: str) -> dict:
    organization = _parse(organization_id)
    async with get_db() as db:
        org = await organization_repo().get_by_id(db, organization)
        if org is None:
            raise HTTPException(status_code=404, detail="organisation not found")
        balance = await credit_service.balance(db, organization_id=organization)
        ledger = await ledger_repo().list_for_org(db, organization_id=organization, limit=50)
        members = await membership_repo().list_members(db, organization_id=organization)
        runs = await job_repo().list_recent_admin(db, organization_id=organization, limit=25)
        stats = (await _stats(db, organization_id=organization)).get(org.id, {})
    return {
        "billing_enabled": billcfg.enabled(),
        "organization": _organization(org, balance, stats),
        "ledger": [_ledger(row) for row in ledger],
        "members": [
            {"email": user.email, "name": user.name or user.email,
             "role": getattr(role, "value", role)}
            for user, role in members
        ],
        "recent_runs": [_run(row[0], row[1], row[2]) for row in runs],
    }


@router.post("/organizations/{organization_id}/credit-grants", dependencies=[Depends(admin_dep)])
async def grant_credits(organization_id: str, body: CreditGrantRequest) -> dict:
    organization = _parse(organization_id)
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    async with get_db() as db:
        org = await organization_repo().get_by_id(db, organization)
        if org is None:
            raise HTTPException(status_code=404, detail="organisation not found")
        try:
            await credit_service.grant(
                db,
                organization_id=organization,
                amount=body.amount,
                reason=reason,
                entry_id=body.operation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        balance = await credit_service.balance(db, organization_id=organization)
    return {"balance": balance}


async def _stats(db, *, organization_id: uuid.UUID | None = None) -> dict:
    stats_reader = getattr(job_repo(), "stats_by_organization", None)
    if stats_reader is None:
        return {}
    return await stats_reader(db, organization_id=organization_id)


def _organization(org: Organization, balance: int, stats: dict) -> dict:
    plan = org.plan or ""
    return {
        "active_runs": int(stats.get("active_runs") or 0),
        "balance": int(balance),
        "created_at": org.created_at.isoformat() if org.created_at else None,
        "current_period_end": org.current_period_end.isoformat() if org.current_period_end else None,
        "has_billing_account": bool(org.stripe_customer_id),
        "id": str(org.id),
        "included_credits": plancfg.limits_for(plan).included_credits,
        "last_run_at": stats.get("last_run_at"),
        "name": org.name,
        "plan": plan,
        "run_count": int(stats.get("run_count") or 0),
        "slug": org.slug,
        "status": org.subscription_status,
        "stripe_customer_id": org.stripe_customer_id or "",
    }


def _run(job: SimulationJob, task_label: str | None, organization_name: str | None) -> dict:
    return {
        "attempts": job.current_attempt,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        "failed_reason": getattr(job.failed_reason, "value", job.failed_reason),
        "id": str(job.id),
        "lease_heartbeat_at": job.lease_heartbeat_at.isoformat() if job.lease_heartbeat_at else None,
        "organization_id": str(job.organization_id) if job.organization_id else "",
        "organization_name": organization_name or "",
        "owner_id": job.owner_id,
        "pipeline_backend": job.pipeline_backend or "",
        "pipeline_dispatch_state": job.pipeline_dispatch_state or "",
        "pipeline_execution_id": job.pipeline_execution_id or "",
        "pipeline_launch_error": job.pipeline_launch_error or "",
        "pipeline_submitted_at": (
            job.pipeline_submitted_at.isoformat() if job.pipeline_submitted_at else None
        ),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "status": getattr(job.status, "value", job.status),
        "task_label": task_label or "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _ledger(row: CreditLedgerEntry) -> dict:
    return {
        "amount": row.amount,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "entry_type": getattr(row.entry_type, "value", row.entry_type),
        "id": str(row.id),
        "metered_at": row.metered_at.isoformat() if row.metered_at else None,
        "overage": int(getattr(row, "overage", 0) or 0),
        "reason": row.reason,
    }


def _parse(organization_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(organization_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="organisation not found")


def organization_repo():
    from meshpipeline.persistence.repositories.organization_repository import (
        OrganizationRepository,
    )
    return OrganizationRepository()


def job_repo():
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    return JobRepository()


def ledger_repo():
    from meshpipeline.persistence.repositories.credit_ledger_repository import (
        CreditLedgerRepository,
    )
    return CreditLedgerRepository()


def membership_repo():
    from meshpipeline.persistence.repositories.membership_repository import (
        MembershipRepository,
    )
    return MembershipRepository()
