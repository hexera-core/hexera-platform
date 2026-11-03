# Responsibility: Answer a data-subject's access and erasure requests for their own account.
# Owns: what an export contains, what erasure removes at once, what it removes later, and what it
#       deliberately keeps (the credit ledger and the billing identity - financial records).
# Boundaries: it never authenticates; the route hands it a proven owner and organisation. The
#             Identity Platform account itself is deleted by the console, which holds that session.
from __future__ import annotations

import enum
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.application import audit
from meshpipeline.application.maintenance.geometry_retention import SOURCE_REQUIRING_STATES
from meshpipeline.application.spend_gate import PAYING_STATUSES
from meshpipeline.persistence.models import (
    ApiKey,
    Artifact,
    ArtifactReconciliation,
    CadRepairJob,
    CaptureOperation,
    ChatSession,
    CreditLedgerEntry,
    GeometryInterpretationRow,
    GeometrySource,
    Membership,
    MembershipRole,
    Organization,
    SimulationJob,
    SourceObjectCleanup,
    User,
)
from meshpipeline.persistence.repositories import tenant_scope

logger = logging.getLogger(__name__)

#: EVERY TABLE THAT CARRIES `owner_id`. Erasure rewrites that column on each of them, and the
#: export reads each of them - a table added to the schema with an owner_id must be added here,
#: which tests/unit/application/test_privacy_service.py checks against the models.
OWNER_SCOPED: tuple = (
    GeometrySource, SimulationJob, ChatSession, CadRepairJob, GeometryInterpretationRow,
    CaptureOperation, ArtifactReconciliation, SourceObjectCleanup, ApiKey,
)

#: Columns an export never carries: a credential's hash is not the subject's data, it is ours.
_EXPORT_EXCLUDED_COLUMNS = frozenset({"key_hash"})

#: The most rows of one table one export carries. A larger account is told it was truncated and
#: support completes the request by hand (docs/compliance/dsar-runbook.md), rather than one
#: request holding a connection for minutes.
EXPORT_ROW_LIMIT = 5000

ERASED_ORGANIZATION_NAME = "Erased organisation"


class ErasureRefused(Exception):
    """The request cannot be honoured as asked; the message is safe to show the requester."""


def erased_owner(organization_id: uuid.UUID | str) -> str:
    # The pseudonym every owner_id becomes. Stable per organisation, so a row's lineage survives
    # (which job came from which upload) while nothing in it names a person any more.
    return f"erased:{organization_id}"


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        return None
    return value


def _row(row) -> dict:
    return {c.key: _jsonable(getattr(row, c.key)) for c in row.__mapper__.column_attrs
            if c.key not in _EXPORT_EXCLUDED_COLUMNS}


async def _rows(db: AsyncSession, stmt) -> tuple[list[dict], bool]:
    found = (await db.execute(stmt.limit(EXPORT_ROW_LIMIT + 1))).scalars().all()
    return [_row(r) for r in found[:EXPORT_ROW_LIMIT]], len(found) > EXPORT_ROW_LIMIT


async def export_account(db: AsyncSession, *, owner_id: str, organization_id: str,
                         now: datetime | None = None) -> dict:
    """Everything this service holds about the caller and their organisation, as JSON.

    Stored FILES are listed, not inlined: each is already downloadable by its owner through the
    routes that serve it, and inlining gigabytes of mesh into one response is not an export.
    """
    at = now or datetime.now(UTC)
    parsed = tenant_scope.parsed_organization_id(organization_id)
    user = (await db.execute(select(User).where(User.email == owner_id.strip().lower())
                             )).scalar_one_or_none()

    tables: dict[str, list[dict]] = {}
    truncated: list[str] = []
    for model in OWNER_SCOPED:
        rows, cut = await _rows(db, select(model).where(
            tenant_scope.scope(model, owner_id=owner_id, organization_id=organization_id)))
        tables[model.__tablename__] = rows
        if cut:
            truncated.append(model.__tablename__)

    organization = None
    if parsed is not None:
        org = await db.get(Organization, parsed)
        organization = _row(org) if org is not None else None
        rows, cut = await _rows(db, select(CreditLedgerEntry)
                                .where(CreditLedgerEntry.organization_id == parsed)
                                .order_by(CreditLedgerEntry.created_at))
        tables["credit_ledger"] = rows
        if cut:
            truncated.append("credit_ledger")
        job_ids = select(SimulationJob.id).where(SimulationJob.organization_id == parsed)
        rows, cut = await _rows(db, select(Artifact).where(Artifact.job_id.in_(job_ids)))
        tables["artifacts"] = rows
        if cut:
            truncated.append("artifacts")

    audit.record("privacy.export", actor=str(user.id) if user else "",
                 organization_id=organization_id, detail={"truncated": bool(truncated)})
    return {
        "format": "hexera-account-export/v1",
        "generated_at": at.isoformat(),
        "user": _row(user) if user is not None else None,
        "organization": organization,
        "tables": tables,
        "truncated_tables": truncated,
        "not_included": {
            "stored_files": "listed by key in geometry_sources and artifacts; download each "
                            "from the console while the account is active",
            "identity_platform": "your sign-in record is held by Google Identity Platform on "
                                 "our behalf and is deleted with your account",
            "payment_details": "card details are held by Stripe, never by Hexera",
        },
    }


async def request_erasure(db: AsyncSession, *, owner_id: str, organization_id: str,
                          now: datetime | None = None) -> dict:
    """Erase the caller's account. Everything that names them goes in THIS transaction; stored
    bytes go in the maintenance sweep (`sweep_erasures`), which cannot be done atomically with it.

    What stays, and why: the organisations row (renamed), its credit ledger and its Stripe
    customer id are accounting records with a statutory retention period; the signup-grant claim
    digest is fraud-prevention data. Both are named in docs/compliance/retention-schedule.md.
    """
    at = now or datetime.now(UTC)
    parsed = tenant_scope.parsed_organization_id(organization_id)
    if parsed is None:
        raise ErasureRefused("This account has no organisation to erase. Contact support.")

    owner = owner_id.strip().lower()
    member = (await db.execute(
        select(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == parsed, User.email == owner))).first()
    if member is None or member[1] != MembershipRole.owner:
        raise ErasureRefused("Only the organisation's owner can delete it.")
    user = member[0]

    org = (await db.execute(select(Organization).where(Organization.id == parsed)
                            .with_for_update())).scalar_one()
    if org.erasure_requested_at is not None:
        return {"status": "already_requested", "requested_at": org.erasure_requested_at.isoformat()}
    if (org.stripe_subscription_id and org.subscription_status
            and org.subscription_status in PAYING_STATUSES - {""}):
        # Erasing a tenant that is still being billed would leave a subscription charging an
        # account nobody can sign in to. The person cancels first, under Billing.
        raise ErasureRefused("Cancel your subscription under Billing before deleting the account.")

    # THE SCOPE OF AN ERASURE IS ONE ORGANISATION. The owner-matched half catches only rows whose
    # organization_id is NULL (written before 0004's backfill, or during a degraded lookup); a row
    # stamped with ANOTHER organisation belongs to that tenant, and this person being a member
    # there must not wipe or re-own their teammates' data.
    def _belongs(model):
        return or_(model.organization_id == parsed,
                   and_(model.organization_id.is_(None), model.owner_id == owner))

    revoked = (await db.execute(update(ApiKey).where(_belongs(ApiKey),
                                                     ApiKey.revoked_at.is_(None))
                                .values(revoked_at=at))).rowcount

    # CONVERSATIONS ARE DELETED OUTRIGHT: they are free text the person typed, the most likely
    # place in the schema for personal data we did not ask for.
    chats = (await db.execute(delete(ChatSession).where(_belongs(ChatSession)))).rowcount

    pseudonym = erased_owner(parsed)
    for model in OWNER_SCOPED:
        if model is ChatSession:
            continue
        await db.execute(update(model).where(_belongs(model)).values(owner_id=pseudonym))
    # The name the person gave their upload is their text, not lineage; the row keeps its ids.
    await db.execute(update(GeometrySource).where(_belongs(GeometrySource))
                     .values(original_filename=""))

    member_ids = select(Membership.user_id).where(Membership.organization_id == parsed)
    users = list((await db.execute(select(User).where(User.id.in_(member_ids)))).scalars())
    await db.execute(delete(Membership).where(Membership.organization_id == parsed))
    for row in users:
        # A person who also belongs to another organisation keeps their account there.
        elsewhere = (await db.execute(select(Membership.id)
                                      .where(Membership.user_id == row.id).limit(1))).first()
        if elsewhere is None:
            await db.execute(delete(User).where(User.id == row.id))

    # THE SLUG IS REWRITTEN TOO. `_slug_for` is a pure function of the Identity Platform uid, so a
    # surviving row keeping its slug would make the SAME person's next sign-up collide on the
    # unique index and 500 - "delete my account, then change my mind" must stay possible.
    await db.execute(update(Organization).where(Organization.id == parsed)
                     .values(name=ERASED_ORGANIZATION_NAME, slug=f"erased-{parsed}",
                             erasure_requested_at=at))

    logger.info("erasure requested for organisation %s: %s keys revoked, %s conversations "
                "deleted", parsed, revoked, chats)
    # The audit line is the CALLER's to write, after the transaction commits: a line describing a
    # rolled-back erasure would be a record of something that never happened.
    return {"status": "requested", "requested_at": at.isoformat(),
            "audit_detail": {"actor": str(user.id), "api_keys_revoked": revoked,
                             "conversations_deleted": chats, "users_removed": len(users)}}


async def sweep_erasures(db: AsyncSession, store, *, limit: int = 10,
                         now: datetime | None = None) -> dict:
    """Delete the stored bytes of every organisation whose erasure is pending, then mark it erased.

    An organisation with a job that still needs its bytes waits for the next run: the reaper
    (STALLED_JOB_TIMEOUT_HOURS) bounds how long that can be. Idempotent - a source already purged
    or an artifact already gone is simply skipped - so a failed run is re-run, not unwound.
    """
    at = now or datetime.now(UTC)
    # Organisations still waiting on a job are EXCLUDED FROM THE PAGE, not skipped inside it:
    # `limit` oldest-first with in-loop skips would hand the same waiting rows back every run and
    # starve every erasure requested after them.
    active_job = exists().where(and_(
        SimulationJob.organization_id == Organization.id,
        SimulationJob.status.in_(sorted(s.value for s in SOURCE_REQUIRING_STATES))))
    base = (Organization.erasure_requested_at.is_not(None), Organization.erased_at.is_(None))
    pending = list((await db.execute(
        select(Organization.id).where(*base, ~active_job)
        .order_by(Organization.erasure_requested_at).limit(limit))).scalars())
    waiting = (await db.execute(select(func.count()).select_from(Organization)
                                .where(*base, active_job))).scalar_one()

    erased, failed = 0, 0
    for org_id in pending:
        try:
            await _erase_bytes(db, store, org_id, at)
            # MARKED ERASED ONLY WHEN NOTHING REMAINS, re-read after the deletes: a job that was
            # finishing while this ran may have delivered another artifact, and `erased_at` closes
            # the organisation to every future sweep. Not clean yet means the next run retries.
            clean = await _nothing_remains(db, org_id)
        except Exception as exc:                     # noqa: BLE001 - provider detail stays internal
            failed += 1
            logger.warning("erasure sweep: organisation %s not finished (%s)", org_id,
                           type(exc).__name__)
            await db.rollback()
            continue
        if not clean:
            await db.commit()
            waiting += 1
            continue
        await db.execute(update(Organization).where(Organization.id == org_id)
                         .values(erased_at=at))
        await db.commit()
        erased += 1
        audit.record("privacy.erasure_completed", actor="system", organization_id=str(org_id))
    return {"pending": len(pending) + waiting, "erased": erased, "waiting_on_jobs": waiting,
            "failed": failed}


async def _nothing_remains(db: AsyncSession, org_id: uuid.UUID) -> bool:
    job_ids = select(SimulationJob.id).where(SimulationJob.organization_id == org_id)
    unpurged = (await db.execute(select(GeometrySource.id).where(
        GeometrySource.organization_id == org_id,
        GeometrySource.purged_at.is_(None)).limit(1))).first()
    artifact = (await db.execute(select(Artifact.id)
                                 .where(Artifact.job_id.in_(job_ids)).limit(1))).first()
    active = (await db.execute(select(SimulationJob.id).where(and_(
        SimulationJob.organization_id == org_id,
        SimulationJob.status.in_(sorted(s.value for s in SOURCE_REQUIRING_STATES)),
    )).limit(1))).first()
    return unpurged is None and artifact is None and active is None


async def _erase_bytes(db: AsyncSession, store, org_id: uuid.UUID, at: datetime) -> None:
    sources = list((await db.execute(select(GeometrySource).where(
        GeometrySource.organization_id == org_id, GeometrySource.purged_at.is_(None)))).scalars())
    for source in sources:
        _delete_if_present(store, source.object_key)
        source.purged_at = at
    job_ids = select(SimulationJob.id).where(SimulationJob.organization_id == org_id)
    artifacts = list((await db.execute(select(Artifact).where(Artifact.job_id.in_(job_ids))
                                       )).scalars())
    for artifact in artifacts:
        _delete_if_present(store, artifact.storage_key)
        await db.delete(artifact)
    # Upload intents that never became a source still name a stored object each.
    keys = (await db.execute(select(ArtifactReconciliation.object_key).distinct().where(
        ArtifactReconciliation.organization_id == org_id))).scalars()
    for key in keys:
        _delete_if_present(store, key)
    await db.flush()


def _delete_if_present(store, key: str) -> None:
    if not key:
        return
    if hasattr(store, "exists") and not store.exists(object_key=key):
        return
    store.delete_object(object_key=key)
