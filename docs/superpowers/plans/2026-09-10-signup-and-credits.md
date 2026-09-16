# Sign up, sign in and signup credits — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the console's single-password env-var sign-in with real sign-up, sign-in and password reset backed by Google Cloud Identity Platform, give every new account an organisation and a starting credit balance, and make `organization_id` the tenant key the schema has been carrying an empty column for.

**Architecture:** Identity Platform holds the credential and sends the verification and reset emails; the product API holds `users`, `organizations`, `memberships` and an append-only `credit_ledger`. The console's Auth.js Credentials provider stops checking passwords and instead posts a Firebase ID token to a new, `MESH_API_KEY`-gated `POST /auth/session`, which verifies the token, provisions the account on first sight and returns the identity the existing session and proxy already expect.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async / Alembic / Postgres; Next.js 16 / React 19 / Auth.js 5 beta / Firebase Web SDK; `google-auth` and `httpx`, both already runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-09-10-signup-and-credits-design.md`

## Global Constraints

- **No new Python runtime dependency.** Token verification uses `google-auth==2.35.0` and `httpx==0.27.2`, both already in `requirements/runtime.txt`. `requests` is transitive-only (it appears in `requirements/constraints.txt`, not `runtime.txt`) and must not be imported.
- **Migrations never import the application.** `tests/unit/security/test_api_key_schema.py` asserts `"meshpipeline" not in src` for a migration file. Every new revision must hold that.
- **Every new setting is declared in both** `src/meshpipeline/settings/policy.py` **and** `src/meshpipeline/settings/inventory.py`. Gate A's configuration certification fails on a setting read but not declared.
- **`organization_id` is nullable everywhere this plan touches.** `NOT NULL` is a follow-up `0005` outside this plan (spec decision 10).
- **`owner_id` is the lowercased email and is never re-keyed** (spec decision 4).
- **Reads scope on `organization_id`; writes stamp both** `organization_id` and `owner_id` (spec §6).
- **The credit ledger is append-only.** No update or delete path, and the repository exposes none.
- **Nothing in this plan debits credits.** Only `grant` entries are ever written (spec §2, §7).
- Settings booleans use the established idiom: `optional_env("NAME", "true").lower() == "true"`.
- Run the unit tier with `make test-fast`; a single test with `python -m pytest tests/unit/... -v`. Console tests: `pnpm --filter @hexera/console test`.

---

### Task 1: Identity and credit tables

**Files:**
- Modify: `src/meshpipeline/persistence/models.py` (append after `ApiKey`, before `NativeSubmissionClaim`)
- Create: `alembic/versions/0003_identity_and_credits.py`
- Test: `tests/unit/security/test_identity_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: ORM classes `Organization`, `User`, `Membership`, `CreditLedgerEntry`; enums `MembershipRole` (`owner`|`member`) and `CreditEntryType` (`grant`|`debit`|`refund`); tables `organizations`, `users`, `memberships`, `credit_ledger`; alembic revision id `0003_identity_and_credits` with `down_revision = '0002_api_keys'`.

- [ ] **Step 1: Write the failing schema-agreement test**

Create `tests/unit/security/test_identity_schema.py`:

```python
# Responsibility: Verify the identity and credit tables the design specifies are what the ORM and the migration both declare.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.session import Base

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic" / "versions" / "0003_identity_and_credits.py"

_COLUMNS = {
    "organizations": {"id", "name", "slug", "created_at"},
    "users": {"id", "firebase_uid", "email", "name", "email_verified_at", "last_login_at",
              "created_at"},
    "memberships": {"id", "user_id", "organization_id", "role", "created_at"},
    "credit_ledger": {"id", "organization_id", "entry_type", "amount", "reason", "created_at"},
}


def test_the_orm_declares_the_four_tables_the_design_specifies():
    for table, columns in _COLUMNS.items():
        assert table in Base.metadata.tables, table
        assert {c.name for c in Base.metadata.tables[table].columns} == columns, table


def test_no_column_could_hold_a_password():
    for table in _COLUMNS:
        names = {c.name for c in Base.metadata.tables[table].columns}
        assert not {n for n in names if "password" in n or "secret" in n}, table


def test_the_lookup_columns_are_unique_and_indexed():
    users = Base.metadata.tables["users"]
    for column in ("firebase_uid", "email"):
        indexes = [i for i in users.indexes if [c.name for c in i.columns] == [column]]
        assert indexes, f"{column} carries no index - every sign-in would scan the table"
        assert all(i.unique for i in indexes), f"two users could share {column}"


def test_a_backfilled_user_may_have_no_firebase_uid_yet():
    cols = {c.name: c for c in Base.metadata.tables["users"].columns}
    assert cols["firebase_uid"].nullable, "a backfilled user has no uid until they first sign up"
    assert not cols["email"].nullable, "a user with no email cannot be linked to their owner_id"


def test_one_user_joins_an_organisation_at_most_once():
    memberships = Base.metadata.tables["memberships"]
    uniques = [c for c in memberships.constraints
               if getattr(c, "columns", None) is not None
               and {col.name for col in c.columns} == {"user_id", "organization_id"}]
    assert uniques, "a user could hold two memberships in one organisation"


def test_the_ledger_is_shaped_for_a_derived_balance():
    ledger = Base.metadata.tables["credit_ledger"]
    cols = {c.name: c for c in ledger.columns}
    assert not cols["amount"].nullable, "a null amount cannot be summed"
    assert not cols["organization_id"].nullable, "an entry belonging to nobody has no balance"
    assert any([c.name for c in i.columns] == ["organization_id", "created_at"]
               for i in ledger.indexes), "the balance query has no index to read"


def test_the_migration_creates_exactly_the_columns_the_orm_declares():
    tree = ast.parse(MIGRATION.read_text())
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and ast.unparse(call.func).endswith("create_table")):
            continue
        table = call.args[0].value
        if table not in _COLUMNS:
            continue
        declared = {a.args[0].value for a in call.args[1:]
                    if isinstance(a, ast.Call) and ast.unparse(a.func).endswith("Column")}
        assert declared == _COLUMNS[table], table


def test_the_migration_descends_from_the_api_keys_revision_and_reverses_itself():
    src = MIGRATION.read_text()
    tree = ast.parse(src)
    assigned = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert assigned["revision"] == "0003_identity_and_credits"
    assert assigned["down_revision"] == "0002_api_keys"
    for table in _COLUMNS:
        assert f"op.drop_table('{table}')" in src, table
    assert "meshpipeline" not in src, "the migration imports the application it is meant to outlive"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/unit/security/test_identity_schema.py -v`
Expected: FAIL — `KeyError: 'organizations'` and `FileNotFoundError` for the migration.

- [ ] **Step 3: Add the ORM models**

Append to `src/meshpipeline/persistence/models.py`, after the `ApiKey` class:

```python
class MembershipRole(str, PyEnum):
    owner  = "owner"
    member = "member"


class CreditEntryType(str, PyEnum):
    #: credits issued - the only kind this cycle writes
    grant  = "grant"
    #: credits consumed. Declared now so the ledger's shape is settled; nothing writes one yet.
    debit  = "debit"
    #: credits returned after a debit that should not have stood
    refund = "refund"


class Organization(Base):
    # THE TENANT. One per user today (there is no organisation UI), but the table and its
    # memberships exist from day one because widening a tenant boundary after data has
    # accumulated is the expensive migration - the same argument api_keys.organization_id
    # was already carrying an empty column for.

    __tablename__ = "organizations"

    id:   Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                            default=uuid.uuid4)
    name: Mapped[str]       = mapped_column(String(256), nullable=False)
    #: a URL-safe handle. Unique so it can address the organisation once anything needs to.
    slug: Mapped[str]       = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)


class User(Base):
    # THE PERSON. Identity Platform holds their credential; this row holds everything about them
    # that the columns in this schema scope on. There is deliberately NO password column - see
    # the design's decision 1. A password we never receive is one we cannot leak.

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    # THE IDENTITY PLATFORM SUBJECT. Nullable because the backfill creates a row per existing
    # owner_id before that person has ever signed up; it is filled in the first time they do,
    # which is what makes their existing jobs and geometry follow them in rather than being
    # stranded behind a second, empty account.
    firebase_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True,
                                                     index=True)
    # THE LINK TO owner_id, which is this address lowercased. Unique and indexed because both
    # the uid path and the backfill-linking path find a user by it.
    email: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    name:  Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    # WHEN the address was proven, not whether. A moment survives a provider that later stops
    # reporting the flag, and it is what a future "grant only on verified email" rule would read.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                               nullable=True)
    last_login_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                               nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    __table_args__: tuple = (
        # owner_id is the lowercased email everywhere else in this schema. A row that disagreed
        # would resolve to an organisation for one spelling of the address and not the other.
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercased"),
    )


class Membership(Base):
    # WHICH ORGANISATION a user acts within. One row per personal organisation today; the table
    # is what makes multi-user organisations a later feature rather than a later migration.

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    # RESTRICT, like every other lineage reference in this schema: a membership is how a user's
    # rows are reachable, so neither side may be deleted out from under it.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
        index=True)
    role: Mapped[MembershipRole] = mapped_column(Enum(MembershipRole, name="membershiprole"),
                                                 nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    __table_args__: tuple = (
        UniqueConstraint("user_id", "organization_id", name="uq_memberships_user_org"),
    )


class CreditLedgerEntry(Base):
    # ONE MOVEMENT of credits, APPEND-ONLY. The balance is SUM(amount) over an organisation and is
    # never stored: a counter decremented at submit leaks credits down every failure path, and
    # this pipeline has several (FailedReason, artifact_reconciliations). A row per movement also
    # answers "why is my balance this?", which a counter never can.
    #
    # Nothing in this cycle writes anything but a grant. debit and refund exist so that when
    # spending lands it is a new caller, not a new migration.

    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
        index=True)
    entry_type: Mapped[CreditEntryType] = mapped_column(
        Enum(CreditEntryType, name="creditentrytype"), nullable=False)
    # SIGNED, in whole credits. A grant is positive and a debit negative, so the balance is a
    # plain SUM with no per-type arithmetic that a new entry type could get wrong. BigInteger
    # because the unit is undecided and a small unit means large numbers.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: why this entry exists, for the person reading their own ledger. Display only.
    reason: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    __table_args__: tuple = (
        # THE BALANCE QUERY, and the ledger view's ordering. Both read this index.
        Index("ix_credit_ledger_org_created", "organization_id", "created_at"),
        # A zero-amount entry is a row that changes nothing and explains nothing.
        CheckConstraint("amount <> 0", name="ck_credit_ledger_amount_nonzero"),
    )
```

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0003_identity_and_credits.py`:

```python
# Responsibility: Create the identity and credit tables: organisations, users, memberships and the credit ledger.
# Boundaries: four new tables that reference nothing existing; adding organization_id to existing tables is 0004's work.

# Additive and reversible on its own terms, exactly as 0002 was: every table it creates is one
# nothing else references yet, so the downgrade drops precisely what the upgrade made. The
# organization_id columns on the EXISTING tables, and the backfill that fills them, are deliberately
# a separate revision - that is the half that touches live rows, and it must be reviewable,
# rehearsable and reversible without this half moving with it.
#
# Revision ID: 0003_identity_and_credits
# Revises: 0002_api_keys
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003_identity_and_credits'
down_revision = '0002_api_keys'
branch_labels = None
depends_on = None

#: Declared once each, because the downgrade must name exactly the types this revision created.
_MEMBERSHIP_ROLE = ("owner", "member")
_CREDIT_ENTRY_TYPE = ("grant", "debit", "refund")
_ENUM_TYPES = ("creditentrytype", "membershiprole")


def upgrade():
    op.create_table('organizations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_organizations_slug'), 'organizations', ['slug'], unique=True)

    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('firebase_uid', sa.String(length=128), nullable=True),
    sa.Column('email', sa.String(length=256), nullable=False),
    sa.Column('name', sa.String(length=256), server_default='', nullable=False),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('email = lower(email)', name='ck_users_email_lowercased'),
    sa.PrimaryKeyConstraint('id')
    )
    # UNIQUE on both: a duplicate uid would let one Identity Platform account resolve to two
    # users, and a duplicate email would make the backfill-linking lookup ambiguous.
    op.create_index(op.f('ix_users_firebase_uid'), 'users', ['firebase_uid'], unique=True)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    op.create_table('memberships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.Enum(*_MEMBERSHIP_ROLE, name='membershiprole'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'organization_id', name='uq_memberships_user_org')
    )
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_index(op.f('ix_memberships_organization_id'), 'memberships', ['organization_id'], unique=False)

    op.create_table('credit_ledger',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('entry_type', sa.Enum(*_CREDIT_ENTRY_TYPE, name='creditentrytype'), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('reason', sa.String(length=128), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount <> 0', name='ck_credit_ledger_amount_nonzero'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_credit_ledger_organization_id'), 'credit_ledger', ['organization_id'], unique=False)
    # The balance query and the ledger view read this one.
    op.create_index('ix_credit_ledger_org_created', 'credit_ledger', ['organization_id', 'created_at'], unique=False)


def downgrade():
    op.drop_index('ix_credit_ledger_org_created', table_name='credit_ledger')
    op.drop_index(op.f('ix_credit_ledger_organization_id'), table_name='credit_ledger')
    op.drop_table('credit_ledger')
    op.drop_index(op.f('ix_memberships_organization_id'), table_name='memberships')
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_index(op.f('ix_users_firebase_uid'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_organizations_slug'), table_name='organizations')
    op.drop_table('organizations')
    # The enum TYPES outlive their tables unless dropped by name - a re-run of the upgrade would
    # then fail on "type already exists". Same discipline as the baseline's _ENUM_TYPES.
    bind = op.get_bind()
    for name in _ENUM_TYPES:
        sa.Enum(name=name).drop(bind, checkfirst=True)
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/security/test_identity_schema.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 6: Run the full unit tier to check nothing else moved**

Run: `make test-fast`
Expected: PASS. If `tests/unit/architecture` or `tests/unit/hygiene` complains about the new models, read the failure — those tiers assert repository-map and import-direction rules and may need the new classes acknowledged.

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/persistence/models.py alembic/versions/0003_identity_and_credits.py tests/unit/security/test_identity_schema.py
git commit -m "feat(schema): organisations, users, memberships and the credit ledger

Four tables that reference nothing existing, so the revision is reversible
on its own terms. Users carry no password column: Identity Platform holds
the credential, and one we never receive is one we cannot leak.

firebase_uid is nullable because 0004's backfill creates a user per
existing owner_id before that person has ever signed up.

The ledger is append-only with a signed amount, so the balance is a plain
SUM and a failure path cannot leak credits the way a decremented counter
would."
```

---

### Task 2: Repositories for the new tables

**Files:**
- Create: `src/meshpipeline/persistence/repositories/organization_repository.py`
- Create: `src/meshpipeline/persistence/repositories/user_repository.py`
- Create: `src/meshpipeline/persistence/repositories/membership_repository.py`
- Create: `src/meshpipeline/persistence/repositories/credit_ledger_repository.py`
- Test: `tests/unit/persistence/test_identity_repositories.py`

**Interfaces:**
- Consumes: `Organization`, `User`, `Membership`, `CreditLedgerEntry`, `MembershipRole`, `CreditEntryType` from Task 1.
- Produces:
  - `OrganizationRepository.create(db, *, name: str, slug: str) -> Organization`
  - `UserRepository.get_by_firebase_uid(db, uid: str) -> User | None`
  - `UserRepository.get_by_email(db, email: str) -> User | None`
  - `UserRepository.create(db, *, email: str, name: str, firebase_uid: str | None) -> User`
  - `UserRepository.attach_firebase_uid(db, *, user_id: uuid.UUID, firebase_uid: str) -> bool`
  - `UserRepository.record_login(db, *, user_id: uuid.UUID, at: datetime, email_verified: bool) -> None`
  - `MembershipRepository.create(db, *, user_id, organization_id, role: MembershipRole) -> Membership`
  - `MembershipRepository.organization_id_for_email(db, email: str) -> uuid.UUID | None`
  - `CreditLedgerRepository.append(db, *, organization_id, entry_type: CreditEntryType, amount: int, reason: str) -> CreditLedgerEntry`
  - `CreditLedgerRepository.balance(db, *, organization_id) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/persistence/test_identity_repositories.py`. These use SQLite in-memory through the async engine so the tier stays hermetic:

```python
# Responsibility: Verify the identity and credit repositories read and write the rows the services depend on.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meshpipeline.persistence.models import (
    CreditEntryType,
    MembershipRole,
    Organization,
    User,
)
from meshpipeline.persistence.repositories.credit_ledger_repository import (
    CreditLedgerRepository,
)
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)
from meshpipeline.persistence.repositories.user_repository import UserRepository
from meshpipeline.persistence.session import Base

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    # Only the four new tables: the rest of the schema uses Postgres-specific types that SQLite
    # cannot create, and nothing here references them.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [Base.metadata.tables[t] for t in
              ("organizations", "users", "memberships", "credit_ledger")]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _org_and_user(db, email="owner@example.com", uid="uid-1"):
    org = await OrganizationRepository().create(db, name=email, slug=f"org-{uuid.uuid4().hex[:8]}")
    user = await UserRepository().create(db, email=email, name="", firebase_uid=uid)
    await MembershipRepository().create(db, user_id=user.id, organization_id=org.id,
                                        role=MembershipRole.owner)
    await db.flush()
    return org, user


async def test_a_user_is_found_by_their_firebase_uid(db):
    _, user = await _org_and_user(db)
    found = await UserRepository().get_by_firebase_uid(db, "uid-1")
    assert found is not None and found.id == user.id


async def test_an_unknown_firebase_uid_is_absent(db):
    await _org_and_user(db)
    assert await UserRepository().get_by_firebase_uid(db, "uid-absent") is None


async def test_a_user_is_found_by_email_case_insensitively(db):
    _, user = await _org_and_user(db, email="mixed@example.com")
    found = await UserRepository().get_by_email(db, "MIXED@Example.COM")
    assert found is not None and found.id == user.id


async def test_a_backfilled_user_can_have_a_uid_attached_once(db):
    user = await UserRepository().create(db, email="later@example.com", name="",
                                         firebase_uid=None)
    await db.flush()
    assert await UserRepository().attach_firebase_uid(db, user_id=user.id,
                                                      firebase_uid="uid-later") is True
    # A second attach finds no row with a null uid and reports so, rather than overwriting one.
    assert await UserRepository().attach_firebase_uid(db, user_id=user.id,
                                                      firebase_uid="uid-other") is False
    await db.refresh(user)
    assert user.firebase_uid == "uid-later"


async def test_recording_a_login_stamps_the_moment_and_the_verification(db):
    _, user = await _org_and_user(db)
    at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    await UserRepository().record_login(db, user_id=user.id, at=at, email_verified=True)
    await db.refresh(user)
    assert user.last_login_at is not None
    assert user.email_verified_at is not None


async def test_verification_is_recorded_once_and_not_moved_by_later_logins(db):
    _, user = await _org_and_user(db)
    first = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    later = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    await UserRepository().record_login(db, user_id=user.id, at=first, email_verified=True)
    await db.refresh(user)
    stamped = user.email_verified_at
    await UserRepository().record_login(db, user_id=user.id, at=later, email_verified=True)
    await db.refresh(user)
    assert user.email_verified_at == stamped
    assert user.last_login_at != stamped


async def test_an_unverified_login_leaves_the_verification_unstamped(db):
    _, user = await _org_and_user(db)
    at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    await UserRepository().record_login(db, user_id=user.id, at=at, email_verified=False)
    await db.refresh(user)
    assert user.email_verified_at is None
    assert user.last_login_at is not None


async def test_an_email_resolves_to_the_organisation_its_user_belongs_to(db):
    org, _ = await _org_and_user(db, email="member@example.com", uid="uid-m")
    found = await MembershipRepository().organization_id_for_email(db, "MEMBER@example.com")
    assert found == org.id


async def test_an_email_with_no_user_resolves_to_no_organisation(db):
    await _org_and_user(db)
    assert await MembershipRepository().organization_id_for_email(db, "nobody@example.com") is None


async def test_the_balance_of_an_organisation_with_no_entries_is_zero(db):
    org, _ = await _org_and_user(db)
    assert await CreditLedgerRepository().balance(db, organization_id=org.id) == 0


async def test_the_balance_is_the_sum_of_the_entries(db):
    org, _ = await _org_and_user(db)
    repo = CreditLedgerRepository()
    await repo.append(db, organization_id=org.id, entry_type=CreditEntryType.grant,
                      amount=100, reason="signup")
    await repo.append(db, organization_id=org.id, entry_type=CreditEntryType.debit,
                      amount=-30, reason="job")
    await db.flush()
    assert await repo.balance(db, organization_id=org.id) == 70


async def test_one_organisations_entries_do_not_reach_another(db):
    org_a, _ = await _org_and_user(db, email="a@example.com", uid="uid-a")
    org_b = await OrganizationRepository().create(db, name="b", slug="org-b")
    repo = CreditLedgerRepository()
    await repo.append(db, organization_id=org_a.id, entry_type=CreditEntryType.grant,
                      amount=100, reason="signup")
    await db.flush()
    assert await repo.balance(db, organization_id=org_b.id) == 0
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/unit/persistence/test_identity_repositories.py -v`
Expected: FAIL — `ModuleNotFoundError: meshpipeline.persistence.repositories.organization_repository`.

If it instead fails on `sqlite+aiosqlite`, check whether `aiosqlite` is available: `python -c "import aiosqlite"`. If it is absent, do not add it — instead move this file to `tests/integration/test_identity_repositories_postgres.py`, drop the `db` fixture in favour of the tier's existing disposable-database fixture (see `tests/disposable_database.py` and how `tests/integration/test_terminal_outbox_postgres.py` uses it), and keep every test body identical.

- [ ] **Step 3: Write the four repositories**

`src/meshpipeline/persistence/repositories/organization_repository.py`:

```python
# Responsibility: Read and write organizations rows.
# Boundaries: rows only - who may belong to one is the membership repository's question.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Organization


class OrganizationRepository:

    async def create(self, db: AsyncSession, *, name: str, slug: str) -> Organization:
        row = Organization(name=name[:256], slug=slug[:64])
        db.add(row)
        await db.flush()
        return row

    async def get(self, db: AsyncSession, organization_id: uuid.UUID) -> Organization | None:
        res = await db.execute(select(Organization).where(Organization.id == organization_id))
        return res.scalar_one_or_none()
```

`src/meshpipeline/persistence/repositories/user_repository.py`:

```python
# Responsibility: Read and write users rows: find one by uid or email, create one, link a uid, record a login.
# Boundaries: rows only - whether a caller may become this user is the application's judgement.
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import User


class UserRepository:

    async def get_by_firebase_uid(self, db: AsyncSession, firebase_uid: str) -> User | None:
        res = await db.execute(select(User).where(User.firebase_uid == firebase_uid))
        return res.scalar_one_or_none()

    async def get_by_email(self, db: AsyncSession, email: str) -> User | None:
        # Lowercased in the predicate rather than trusting the caller: the column carries a
        # CHECK that it is already lowercase, so lower() here matches the stored form exactly
        # and a mixed-case address from a token still finds its row.
        res = await db.execute(select(User).where(User.email == func.lower(email.strip())))
        return res.scalar_one_or_none()

    async def create(self, db: AsyncSession, *, email: str, name: str,
                     firebase_uid: str | None) -> User:
        row = User(email=email.strip().lower()[:256], name=name[:256],
                   firebase_uid=(firebase_uid or None))
        db.add(row)
        await db.flush()
        return row

    async def attach_firebase_uid(self, db: AsyncSession, *, user_id: uuid.UUID,
                                  firebase_uid: str) -> bool:
        # `firebase_uid is null` is the whole safety property: this links a BACKFILLED row to the
        # account that just signed up, and must never re-point a row that already names one.
        # A second call reports False rather than silently moving somebody's account.
        res = await db.execute(
            update(User)
            .where(User.id == user_id, User.firebase_uid.is_(None))
            .values(firebase_uid=firebase_uid))
        return bool(res.rowcount)

    async def record_login(self, db: AsyncSession, *, user_id: uuid.UUID, at: datetime,
                           email_verified: bool) -> None:
        values: dict = {"last_login_at": at}
        if email_verified:
            # WHEN it was FIRST proven. `is null` keeps the original moment: re-stamping it on
            # every sign-in would turn "verified since" into "last seen", which is what
            # last_login_at already is.
            await db.execute(
                update(User)
                .where(User.id == user_id, User.email_verified_at.is_(None))
                .values(email_verified_at=at))
        await db.execute(update(User).where(User.id == user_id).values(**values))
```

`src/meshpipeline/persistence/repositories/membership_repository.py`:

```python
# Responsibility: Read and write memberships, and answer which organisation an owner acts within.
# Boundaries: rows only; what that organisation then scopes is the repositories' and routes' question.
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Membership, MembershipRole, User


class MembershipRepository:

    async def create(self, db: AsyncSession, *, user_id: uuid.UUID,
                     organization_id: uuid.UUID,
                     role: MembershipRole = MembershipRole.member) -> Membership:
        row = Membership(user_id=user_id, organization_id=organization_id, role=role)
        db.add(row)
        await db.flush()
        return row

    async def organization_id_for_email(self, db: AsyncSession,
                                        email: str) -> uuid.UUID | None:
        # THE SEAM the header credential resolves through. owner_id is the lowercased email, so
        # this is the whole journey from "who is calling" to "which tenant". One indexed join;
        # it is deliberately a single function so a cache can go here later without touching a
        # single call site.
        res = await db.execute(
            select(Membership.organization_id)
            .join(User, User.id == Membership.user_id)
            .where(User.email == func.lower(email.strip()))
            .order_by(Membership.created_at.asc())
            .limit(1))
        return res.scalar_one_or_none()
```

`src/meshpipeline/persistence/repositories/credit_ledger_repository.py`:

```python
# Responsibility: Append credit ledger entries and derive an organisation's balance from them.
# Boundaries: rows and their sum - what a credit is worth, and when one may be spent, is not decided here.
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import CreditEntryType, CreditLedgerEntry


class CreditLedgerRepository:
    # APPEND AND SUM, and deliberately nothing else. There is no update and no delete method,
    # because an entry that can be rewritten is not a ledger - a correction is another entry.

    async def append(self, db: AsyncSession, *, organization_id: uuid.UUID,
                     entry_type: CreditEntryType, amount: int,
                     reason: str = "") -> CreditLedgerEntry:
        row = CreditLedgerEntry(organization_id=organization_id, entry_type=entry_type,
                                amount=int(amount), reason=reason[:128])
        db.add(row)
        await db.flush()
        return row

    async def balance(self, db: AsyncSession, *, organization_id: uuid.UUID) -> int:
        # coalesce, because SUM over no rows is NULL and a new organisation's balance is 0.
        res = await db.execute(
            select(func.coalesce(func.sum(CreditLedgerEntry.amount), 0))
            .where(CreditLedgerEntry.organization_id == organization_id))
        return int(res.scalar_one())
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/persistence/test_identity_repositories.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/persistence/repositories/ tests/unit/persistence/test_identity_repositories.py
git commit -m "feat(persistence): repositories for organisations, users, memberships and credits

attach_firebase_uid predicates on firebase_uid IS NULL so linking a
backfilled row can never re-point an account that already names a uid.

record_login stamps email_verified_at only while it is null: re-stamping
on every sign-in would turn 'verified since' into 'last seen', which
last_login_at already is.

The ledger repository exposes append and balance and nothing else. An
entry that can be rewritten is not a ledger; a correction is another entry."
```

---

### Task 3: The credit service and its settings

**Files:**
- Create: `src/meshpipeline/application/credit_service.py`
- Modify: `src/meshpipeline/settings/policy.py` (after the `MAX_CONCURRENT_JOBS` block, around line 57)
- Modify: `src/meshpipeline/settings/inventory.py` (the `Group("Quotas", ...)` block, around line 326)
- Test: `tests/unit/application/test_credit_service.py`

**Interfaces:**
- Consumes: `CreditLedgerRepository`, `CreditEntryType` from Tasks 1-2.
- Produces:
  - `credit_service.grant(db, *, organization_id: uuid.UUID, amount: int, reason: str) -> None`
  - `credit_service.balance(db, *, organization_id: uuid.UUID) -> int`
  - `credit_service.grant_signup_credits(db, *, organization_id: uuid.UUID) -> int` (returns the amount granted, `0` when disabled)
  - settings `polcfg.SIGNUP_GRANT_CREDITS: int` (default `100`) and `polcfg.CONSOLE_SIGNUP_ENABLED: bool` (default `True`)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/application/test_credit_service.py`:

```python
# Responsibility: Verify credits are issued exactly as configured, and that nothing here spends them.
from __future__ import annotations

import inspect
import uuid

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.persistence.models import CreditEntryType

pytestmark = pytest.mark.asyncio


class _LedgerDouble:

    def __init__(self):
        self.entries: list[dict] = []

    async def append(self, db, *, organization_id, entry_type, amount, reason=""):
        self.entries.append({"organization_id": organization_id, "entry_type": entry_type,
                             "amount": amount, "reason": reason})
        return None

    async def balance(self, db, *, organization_id):
        return sum(e["amount"] for e in self.entries
                   if e["organization_id"] == organization_id)


@pytest.fixture
def ledger(monkeypatch):
    double = _LedgerDouble()
    monkeypatch.setattr(credit_service, "credit_ledger_repo", double)
    return double


async def test_a_grant_is_a_positive_entry(ledger):
    org = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="signup")
    assert ledger.entries == [{"organization_id": org, "entry_type": CreditEntryType.grant,
                               "amount": 100, "reason": "signup"}]


async def test_a_grant_of_zero_writes_nothing(ledger):
    # The ledger's CHECK refuses a zero amount, so the service must not offer it one.
    await credit_service.grant(None, organization_id=uuid.uuid4(), amount=0, reason="signup")
    assert ledger.entries == []


async def test_a_negative_grant_is_refused(ledger):
    # Spending is out of scope for this cycle. A grant that could be negative is a debit with
    # the wrong name on it, and would be the one way this cycle could remove credits.
    with pytest.raises(ValueError):
        await credit_service.grant(None, organization_id=uuid.uuid4(), amount=-1, reason="x")
    assert ledger.entries == []


async def test_the_balance_is_read_through_the_ledger(ledger):
    org = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="signup")
    assert await credit_service.balance(None, organization_id=org) == 100


async def test_the_signup_grant_honours_the_configured_amount(ledger, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 250)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(None, organization_id=org) == 250
    assert ledger.entries[0]["amount"] == 250


async def test_a_configured_zero_disables_the_signup_grant(ledger, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 0)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(None, organization_id=org) == 0
    assert ledger.entries == []


def test_the_service_offers_no_way_to_spend():
    # The guard on the design's central promise: this cycle issues credits and nothing else.
    names = {n for n, _ in inspect.getmembers(credit_service, inspect.isfunction)
             if not n.startswith("_")}
    assert not (names & {"debit", "spend", "charge", "hold", "settle", "refund"}), names


def test_the_settings_are_declared_in_the_inventory():
    from meshpipeline.settings import inventory
    declared = {v.name for g in inventory.GROUPS for v in g.vars}
    assert "SIGNUP_GRANT_CREDITS" in declared
    assert "CONSOLE_SIGNUP_ENABLED" in declared
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/unit/application/test_credit_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'credit_service'`.

If `test_the_settings_are_declared_in_the_inventory` fails on `inventory.GROUPS`, open `src/meshpipeline/settings/inventory.py` and use whatever the module actually names its top-level list of `Group` objects; fix the test to match the real name before proceeding.

- [ ] **Step 3: Declare the two settings**

In `src/meshpipeline/settings/policy.py`, directly after the `MAX_CONCURRENT_JOBS` line:

```python
# WHAT A NEW ACCOUNT IS GIVEN, in whole credits, and the only place the number lives. 0 disables
# the grant without a code change. What a credit is WORTH is deliberately not decided here or
# anywhere else yet - see the design's decision 8.
SIGNUP_GRANT_CREDITS: int = int(optional_env("SIGNUP_GRANT_CREDITS", "100"))
# WHETHER an unrecognised Identity Platform account may provision itself one. This is the REAL
# gate and it lives on the API, not the console: anyone can create an Identity Platform account
# directly against the project's public web API key, so a console that merely hides the sign-up
# form is not a gate at all.
CONSOLE_SIGNUP_ENABLED: bool = optional_env("CONSOLE_SIGNUP_ENABLED", "true").lower() == "true"
```

In `src/meshpipeline/settings/inventory.py`, inside `Group("Quotas", vars=[...])`, after the `MAX_CONCURRENT_JOBS` entry:

```python
        EnvVar("SIGNUP_GRANT_CREDITS", "100", kind="int",
               help="credits a newly provisioned organisation is granted once; 0 disables it"),
        EnvVar("CONSOLE_SIGNUP_ENABLED", "true", kind="bool",
               help="whether an unknown Identity Platform account may provision itself an "
                    "organisation on first sign-in"),
```

- [ ] **Step 4: Write the service**

Create `src/meshpipeline/application/credit_service.py`:

```python
# Responsibility: Issue credits to an organisation and answer what its balance is.
# Owns: the fact that a balance is derived from entries, never stored.
# Boundaries: issuance and reading only - this cycle deliberately has no way to spend (design section 2).
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.persistence.models import CreditEntryType
from meshpipeline.persistence.repositories.credit_ledger_repository import (
    CreditLedgerRepository,
)

credit_ledger_repo = CreditLedgerRepository()

#: What a signup grant is called in the ledger, so a person reading their own history sees why
#: the credits are there.
SIGNUP_REASON = "signup grant"


async def grant(db: AsyncSession, *, organization_id: uuid.UUID, amount: int,
                reason: str = "") -> None:
    # A grant is POSITIVE, always. Allowing a negative one would make this the single function in
    # the cycle capable of removing credits - a debit wearing the wrong name - and the design says
    # nothing here spends.
    if amount < 0:
        raise ValueError("a grant cannot be negative")
    # Zero is not an error, it is the configured way to disable the grant. The ledger's CHECK
    # refuses a zero-amount row, so the entry is simply not written.
    if amount == 0:
        return
    await credit_ledger_repo.append(db, organization_id=organization_id,
                                    entry_type=CreditEntryType.grant, amount=amount,
                                    reason=reason)


async def balance(db: AsyncSession, *, organization_id: uuid.UUID) -> int:
    return await credit_ledger_repo.balance(db, organization_id=organization_id)


async def grant_signup_credits(db: AsyncSession, *, organization_id: uuid.UUID) -> int:
    # Read through the module, not bound at import, so an operator's value and a test's
    # monkeypatch both reach it - the same discipline settings/plans.py uses.
    amount = polcfg.SIGNUP_GRANT_CREDITS
    await grant(db, organization_id=organization_id, amount=amount, reason=SIGNUP_REASON)
    return amount
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/application/test_credit_service.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 6: Regenerate the env template and run configuration certification**

Run: `make check-fast`
Expected: PASS. If it reports `.env.example` is stale, regenerate it with the target the Makefile names for the generated env (search `Makefile` for `inventory` or `env.example`) and include the regenerated file in the commit.

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/application/credit_service.py src/meshpipeline/settings/policy.py src/meshpipeline/settings/inventory.py tests/unit/application/test_credit_service.py .env.example
git commit -m "feat(credits): issue a configurable signup grant, derive the balance

grant() refuses a negative amount. Spending is out of scope this cycle,
and a grant that could go negative would be the one function capable of
removing credits.

SIGNUP_GRANT_CREDITS=0 disables the grant without a code change; the
ledger's CHECK refuses a zero-amount row, so nothing is written.

CONSOLE_SIGNUP_ENABLED is an API setting, not a console one. Anyone can
create an Identity Platform account against the project's public web API
key, so a console that only hides the form is not a gate."
```

---

### Task 4: Firebase ID token verification

**Files:**
- Create: `src/meshpipeline/contracts/firebase_token.py`
- Test: `tests/unit/security/test_firebase_token.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `VerifiedToken` frozen dataclass: `uid: str`, `email: str`, `email_verified: bool`, `name: str`
  - `InvalidToken(Exception)`
  - `verify(raw_token: str, *, project_id: str, certs_provider: Callable[[], dict[str, str]] | None = None, now: datetime | None = None) -> VerifiedToken`
  - `google_certs() -> dict[str, str]` — the TTL-cached default provider
  - `reset_cert_cache() -> None` — test seam

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/security/test_firebase_token.py`:

```python
# Responsibility: Verify that only a genuine, current, correctly-addressed Identity Platform token is accepted.
from __future__ import annotations

import json
import time

import pytest

from meshpipeline.contracts import firebase_token

_PROJECT = "hexera-dev"


class _StubJWT:
    """Stands in for google.auth.jwt.decode, which is the one thing here that needs a key."""

    def __init__(self, claims=None, error=None):
        self.claims = claims
        self.error = error
        self.calls: list = []

    def __call__(self, token, certs=None, audience=None):
        self.calls.append({"token": token, "certs": certs, "audience": audience})
        if self.error is not None:
            raise self.error
        return dict(self.claims)


def _claims(**overrides):
    now = int(time.time())
    base = {
        "iss": f"https://securetoken.google.com/{_PROJECT}",
        "aud": _PROJECT,
        "sub": "firebase-uid-1",
        "user_id": "firebase-uid-1",
        "email": "Person@Example.com",
        "email_verified": True,
        "name": "A Person",
        "iat": now - 10,
        "exp": now + 3600,
    }
    base.update(overrides)
    return base


def _verify(monkeypatch, claims=None, error=None):
    stub = _StubJWT(claims=claims if claims is not None else _claims(), error=error)
    monkeypatch.setattr(firebase_token, "_decode", stub)
    return stub


def test_a_genuine_token_yields_the_identity_it_asserts(monkeypatch):
    _verify(monkeypatch)
    result = firebase_token.verify("raw", project_id=_PROJECT,
                                   certs_provider=lambda: {"kid": "cert"})
    assert result.uid == "firebase-uid-1"
    assert result.email_verified is True
    assert result.name == "A Person"


def test_the_email_is_normalised_the_way_owner_id_is(monkeypatch):
    # owner_id is the lowercased email everywhere in this schema. A token asserting mixed case
    # must not produce a second, differently-spelled tenant.
    _verify(monkeypatch)
    result = firebase_token.verify("raw", project_id=_PROJECT,
                                   certs_provider=lambda: {"kid": "cert"})
    assert result.email == "person@example.com"


def test_a_token_addressed_to_another_project_is_refused(monkeypatch):
    _verify(monkeypatch, claims=_claims(iss="https://securetoken.google.com/someone-else"))
    with pytest.raises(firebase_token.InvalidToken):
        firebase_token.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_the_audience_is_checked_by_the_decoder_not_by_us(monkeypatch):
    # google.auth.jwt.decode enforces `aud`; passing it is how that happens. If this argument
    # ever stopped being passed, any project's token would verify here.
    stub = _verify(monkeypatch)
    firebase_token.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})
    assert stub.calls[0]["audience"] == _PROJECT


def test_a_token_with_no_subject_is_refused(monkeypatch):
    _verify(monkeypatch, claims=_claims(sub="", user_id=""))
    with pytest.raises(firebase_token.InvalidToken):
        firebase_token.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_a_token_with_no_email_is_refused(monkeypatch):
    # owner_id IS the email. A token without one cannot name a tenant.
    _verify(monkeypatch, claims=_claims(email=""))
    with pytest.raises(firebase_token.InvalidToken):
        firebase_token.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_a_forged_or_expired_token_is_refused_as_one_kind_of_refusal(monkeypatch):
    _verify(monkeypatch, error=ValueError("Token expired"))
    with pytest.raises(firebase_token.InvalidToken):
        firebase_token.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_an_empty_token_never_reaches_the_decoder(monkeypatch):
    stub = _verify(monkeypatch)
    with pytest.raises(firebase_token.InvalidToken):
        firebase_token.verify("", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})
    assert stub.calls == []


def test_no_configured_project_refuses_rather_than_accepting_anything(monkeypatch):
    stub = _verify(monkeypatch)
    with pytest.raises(firebase_token.InvalidToken):
        firebase_token.verify("raw", project_id="", certs_provider=lambda: {"kid": "cert"})
    assert stub.calls == []


def test_the_certificates_are_fetched_once_and_reused_within_the_ttl(monkeypatch):
    fetches: list = []

    def fake_get(url, timeout=None):
        fetches.append(url)

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"kid-1": "-----BEGIN CERTIFICATE-----"}

        return _Response()

    firebase_token.reset_cert_cache()
    monkeypatch.setattr(firebase_token.httpx, "get", fake_get)
    monkeypatch.setattr(firebase_token, "_now_monotonic", lambda: 1000.0)
    assert firebase_token.google_certs() == {"kid-1": "-----BEGIN CERTIFICATE-----"}
    assert firebase_token.google_certs() == {"kid-1": "-----BEGIN CERTIFICATE-----"}
    assert len(fetches) == 1, "the certificates were re-fetched inside their own TTL"


def test_the_certificates_are_refetched_once_the_ttl_lapses(monkeypatch):
    fetches: list = []
    clock = {"t": 1000.0}

    def fake_get(url, timeout=None):
        fetches.append(url)

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"kid-1": "cert"}

        return _Response()

    firebase_token.reset_cert_cache()
    monkeypatch.setattr(firebase_token.httpx, "get", fake_get)
    monkeypatch.setattr(firebase_token, "_now_monotonic", lambda: clock["t"])
    firebase_token.google_certs()
    clock["t"] = 1000.0 + firebase_token.CERT_TTL_SECONDS + 1
    firebase_token.google_certs()
    assert len(fetches) == 2


def test_the_module_does_not_import_requests():
    # requests is transitive-only in this project (constraints.txt, not runtime.txt). Importing
    # it directly makes the runtime depend on whatever google-cloud-storage happens to pull.
    import pathlib
    src = pathlib.Path(firebase_token.__file__).read_text()
    assert "import requests" not in src
    assert json is not None  # keeps the import used; the assertion above is the point
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/unit/security/test_firebase_token.py -v`
Expected: FAIL — `ModuleNotFoundError: meshpipeline.contracts.firebase_token`.

- [ ] **Step 3: Write the verifier**

Create `src/meshpipeline/contracts/firebase_token.py`:

```python
# Responsibility: Decide whether a presented Identity Platform ID token is genuine, current and addressed to this deployment.
# Owns: the claims a token must carry to name an identity here, and the cached copy of Google's signing certificates.
# Boundaries: the token only - what the identity it names may then do belongs to the application.
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from google.auth import jwt as google_jwt

#: Where Google publishes the certificates Identity Platform signs ID tokens with. This is the
#: securetoken issuer's key set specifically - NOT the general OAuth2 certificate URL, which
#: signs a different family of tokens and would verify nothing we care about here.
CERTS_URL = ("https://www.googleapis.com/robot/v1/metadata/x509/"
             "securetoken@system.gserviceaccount.com")

#: How long a fetched key set is reused. Google rotates these on the order of days, so an hour is
#: comfortably fresh while making a sign-in cost no outbound request in the ordinary case.
CERT_TTL_SECONDS = 3600

#: The issuer a token from THIS project must name. Checked separately from `aud` because the two
#: are different assertions: `aud` says who the token is for, `iss` says who minted it, and only
#: checking one of them accepts a token from the wrong side of that pair.
_ISSUER_TEMPLATE = "https://securetoken.google.com/{project_id}"

#: Indirected so tests can stand in for the one operation that needs a real key.
_decode = google_jwt.decode

_certs_lock = threading.Lock()
_certs_cache: dict | None = None
_certs_fetched_at: float = 0.0


class InvalidToken(Exception):
    """One exception for every way a token fails. The caller answers one refusal either way:
    which part was wrong is only useful to somebody probing which of their guesses is close."""


@dataclass(frozen=True)
class VerifiedToken:
    #: the Identity Platform subject - stable across an email change, which is why it, and not
    #: the address, is what `users.firebase_uid` stores
    uid: str
    #: lowercased, because owner_id is the lowercased email everywhere in this schema
    email: str
    email_verified: bool
    name: str


def _now_monotonic() -> float:
    # Monotonic, not wall clock: a TTL measured against a clock that can step backwards over NTP
    # would pin a stale key set for as long as the step.
    return time.monotonic()


def reset_cert_cache() -> None:
    """Test seam: forget the cached key set."""
    global _certs_cache, _certs_fetched_at
    with _certs_lock:
        _certs_cache = None
        _certs_fetched_at = 0.0


def google_certs() -> dict:
    global _certs_cache, _certs_fetched_at
    with _certs_lock:
        fresh = (_certs_cache is not None
                 and _now_monotonic() - _certs_fetched_at < CERT_TTL_SECONDS)
        if fresh:
            return _certs_cache
    # httpx, not requests: requests reaches this project only as a transitive dependency of
    # google-cloud-storage, and a runtime that imports it directly depends on that accident.
    response = httpx.get(CERTS_URL, timeout=10.0)
    response.raise_for_status()
    certs = response.json()
    with _certs_lock:
        _certs_cache = certs
        _certs_fetched_at = _now_monotonic()
    return certs


def verify(raw_token: str, *, project_id: str,
           certs_provider: Callable[[], dict] | None = None) -> VerifiedToken:
    token = (raw_token or "").strip()
    # Shape and configuration first, and refuse on either alone - neither costs a network call
    # or a signature check. An unset project_id is the dangerous case: without this, `aud` would
    # be compared against "" and the decoder would be asked to accept anything.
    if not token:
        raise InvalidToken("no token presented")
    if not project_id:
        raise InvalidToken("no Identity Platform project is configured")

    provider = certs_provider or google_certs
    try:
        claims = _decode(token, certs=provider(), audience=project_id)
    except Exception as exc:
        # Signature, expiry and audience all land here, and all become the same refusal.
        raise InvalidToken(f"token did not verify: {exc}") from exc

    if claims.get("iss") != _ISSUER_TEMPLATE.format(project_id=project_id):
        raise InvalidToken("token was not minted for this project")

    uid = str(claims.get("user_id") or claims.get("sub") or "").strip()
    if not uid:
        raise InvalidToken("token names no subject")

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        # owner_id IS the email. A token without one names no tenant here, however genuine it is.
        raise InvalidToken("token carries no email address")

    return VerifiedToken(uid=uid, email=email,
                         email_verified=bool(claims.get("email_verified")),
                         name=str(claims.get("name") or "").strip())
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/security/test_firebase_token.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Confirm the unit tier stayed hermetic**

Run: `python -m pytest tests/unit/api/test_unit_tier_is_hermetic.py -v`
Expected: PASS. This tier forbids outbound calls; `google_certs()` must never run under it, which is why every test injects `certs_provider`.

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/contracts/firebase_token.py tests/unit/security/test_firebase_token.py
git commit -m "feat(auth): verify Identity Platform ID tokens with google-auth

No new dependency: google-auth and httpx are already runtime deps.
requests is transitive-only here, so it is deliberately not imported.

iss is checked separately from aud. They are different assertions - aud
says who the token is for, iss says who minted it - and checking only one
accepts a token from the wrong side of that pair.

An unset project_id refuses rather than comparing aud against the empty
string, which would ask the decoder to accept anything.

Google's key set is cached for an hour against a monotonic clock, so an
NTP step backwards cannot pin a stale set."
```

---

### Task 5: Account provisioning

**Files:**
- Create: `src/meshpipeline/application/account_service.py`
- Test: `tests/unit/application/test_account_service.py`

**Interfaces:**
- Consumes: `VerifiedToken` (Task 4); the four repositories (Task 2); `credit_service.grant_signup_credits` (Task 3); `polcfg.CONSOLE_SIGNUP_ENABLED` (Task 3).
- Produces:
  - `Account` frozen dataclass: `user_id: str`, `owner_id: str`, `organization_id: str`, `email_verified: bool`, `name: str`, `provisioned: bool`
  - `SignupDisabled(Exception)`
  - `account_service.resolve_or_provision(db, token: VerifiedToken, *, now: datetime | None = None) -> Account`
  - `account_service.organization_id_for_owner(db, owner_id: str) -> str` — returns `""` when the owner has no membership

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/application/test_account_service.py`:

```python
# Responsibility: Verify a token becomes exactly one account, once, with exactly one grant.
from __future__ import annotations

import uuid

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import account_service
from meshpipeline.contracts.firebase_token import VerifiedToken
from meshpipeline.persistence.models import MembershipRole

pytestmark = pytest.mark.asyncio


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Fakes:
    """Stands in for the four repositories and the credit service, sharing one store."""

    def __init__(self):
        self.users: list = []
        self.orgs: list = []
        self.memberships: list = []
        self.grants: list = []

    # users
    async def get_by_firebase_uid(self, db, firebase_uid):
        return next((u for u in self.users if u.firebase_uid == firebase_uid), None)

    async def get_by_email(self, db, email):
        return next((u for u in self.users if u.email == email.strip().lower()), None)

    async def create_user(self, db, *, email, name, firebase_uid):
        row = _Row(id=uuid.uuid4(), email=email.strip().lower(), name=name,
                   firebase_uid=firebase_uid, email_verified_at=None, last_login_at=None)
        self.users.append(row)
        return row

    async def attach_firebase_uid(self, db, *, user_id, firebase_uid):
        user = next((u for u in self.users if u.id == user_id), None)
        if user is None or user.firebase_uid is not None:
            return False
        user.firebase_uid = firebase_uid
        return True

    async def record_login(self, db, *, user_id, at, email_verified):
        user = next(u for u in self.users if u.id == user_id)
        user.last_login_at = at
        if email_verified and user.email_verified_at is None:
            user.email_verified_at = at

    # organisations
    async def create_org(self, db, *, name, slug):
        row = _Row(id=uuid.uuid4(), name=name, slug=slug)
        self.orgs.append(row)
        return row

    # memberships
    async def create_membership(self, db, *, user_id, organization_id, role):
        row = _Row(id=uuid.uuid4(), user_id=user_id, organization_id=organization_id, role=role)
        self.memberships.append(row)
        return row

    async def organization_id_for_email(self, db, email):
        user = await self.get_by_email(db, email)
        if user is None:
            return None
        m = next((m for m in self.memberships if m.user_id == user.id), None)
        return m.organization_id if m else None


@pytest.fixture
def fakes(monkeypatch):
    f = _Fakes()

    class _UserRepo:
        get_by_firebase_uid = staticmethod(f.get_by_firebase_uid)
        get_by_email = staticmethod(f.get_by_email)
        create = staticmethod(f.create_user)
        attach_firebase_uid = staticmethod(f.attach_firebase_uid)
        record_login = staticmethod(f.record_login)

    class _OrgRepo:
        create = staticmethod(f.create_org)

    class _MembershipRepo:
        create = staticmethod(f.create_membership)
        organization_id_for_email = staticmethod(f.organization_id_for_email)

    async def fake_grant(db, *, organization_id):
        f.grants.append(organization_id)
        return polcfg.SIGNUP_GRANT_CREDITS

    monkeypatch.setattr(account_service, "user_repo", _UserRepo())
    monkeypatch.setattr(account_service, "organization_repo", _OrgRepo())
    monkeypatch.setattr(account_service, "membership_repo", _MembershipRepo())
    monkeypatch.setattr(account_service.credit_service, "grant_signup_credits", fake_grant)
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", True)
    return f


def _token(uid="uid-1", email="person@example.com", verified=True, name="A Person"):
    return VerifiedToken(uid=uid, email=email, email_verified=verified, name=name)


async def test_a_new_account_gets_a_user_an_organisation_a_membership_and_one_grant(fakes):
    account = await account_service.resolve_or_provision(None, _token())
    assert len(fakes.users) == 1
    assert len(fakes.orgs) == 1
    assert len(fakes.memberships) == 1
    assert fakes.grants == [fakes.orgs[0].id]
    assert account.provisioned is True
    assert account.owner_id == "person@example.com"
    assert account.organization_id == str(fakes.orgs[0].id)


async def test_the_new_member_owns_their_own_organisation(fakes):
    await account_service.resolve_or_provision(None, _token())
    assert fakes.memberships[0].role is MembershipRole.owner


async def test_a_second_sign_in_provisions_nothing_further(fakes):
    first = await account_service.resolve_or_provision(None, _token())
    second = await account_service.resolve_or_provision(None, _token())
    assert (len(fakes.users), len(fakes.orgs), len(fakes.memberships)) == (1, 1, 1)
    assert fakes.grants == [fakes.orgs[0].id], "the signup grant was issued twice"
    assert second.organization_id == first.organization_id
    assert second.provisioned is False


async def test_a_backfilled_user_is_linked_rather_than_duplicated(fakes):
    # The state 0004's backfill leaves behind: a user and an organisation exist for this email,
    # and no uid has ever been attached.
    existing = await fakes.create_user(None, email="veteran@example.com", name="",
                                       firebase_uid=None)
    org = await fakes.create_org(None, name="veteran@example.com", slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)

    account = await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com"))

    assert len(fakes.users) == 1, "signing up created a second user for an existing owner"
    assert len(fakes.orgs) == 1, "signing up stranded their existing rows in a new organisation"
    assert fakes.users[0].firebase_uid == "uid-veteran"
    assert account.organization_id == str(org.id)
    assert account.provisioned is False


async def test_a_linked_backfilled_user_is_not_granted_signup_credits(fakes):
    # A grant is for a NEW account. Issuing one here hands a balance to somebody who predates
    # the feature, every time the backfill runs against a fresh environment.
    existing = await fakes.create_user(None, email="veteran@example.com", name="",
                                       firebase_uid=None)
    org = await fakes.create_org(None, name="veteran@example.com", slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)
    await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com"))
    assert fakes.grants == []


async def test_signing_in_records_the_login(fakes):
    await account_service.resolve_or_provision(None, _token())
    assert fakes.users[0].last_login_at is not None


async def test_an_unverified_email_still_signs_in_and_is_reported_as_unverified(fakes):
    account = await account_service.resolve_or_provision(None, _token(verified=False))
    assert account.email_verified is False
    assert fakes.users[0].email_verified_at is None


async def test_signup_can_be_closed_without_locking_out_existing_accounts(fakes, monkeypatch):
    await account_service.resolve_or_provision(None, _token())
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)

    # The known account still signs in.
    account = await account_service.resolve_or_provision(None, _token())
    assert account.owner_id == "person@example.com"

    # A stranger does not get provisioned.
    with pytest.raises(account_service.SignupDisabled):
        await account_service.resolve_or_provision(None, _token(uid="uid-2",
                                                                email="new@example.com"))
    assert len(fakes.users) == 1


async def test_a_closed_signup_still_links_a_backfilled_user(fakes, monkeypatch):
    # Linking is not signing up: the account already exists, it simply has no uid yet. Refusing
    # here would lock every pre-existing owner out of their own data.
    existing = await fakes.create_user(None, email="veteran@example.com", name="",
                                       firebase_uid=None)
    org = await fakes.create_org(None, name="veteran@example.com", slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)

    account = await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com"))
    assert account.organization_id == str(org.id)


async def test_an_owner_resolves_to_their_organisation(fakes):
    await account_service.resolve_or_provision(None, _token())
    found = await account_service.organization_id_for_owner(None, "person@example.com")
    assert found == str(fakes.orgs[0].id)


async def test_an_owner_with_no_membership_resolves_to_no_organisation(fakes):
    assert await account_service.organization_id_for_owner(None, "nobody@example.com") == ""
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/unit/application/test_account_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'account_service'`.

- [ ] **Step 3: Write the service**

Create `src/meshpipeline/application/account_service.py`:

```python
# Responsibility: Turn a verified Identity Platform token into the account it names, provisioning one the first time.
# Owns: the rule that a new account is a user, an organisation, a membership and one grant - all or none of them.
# Boundaries: it decides identity and tenancy, never authorisation.
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.contracts.firebase_token import VerifiedToken
from meshpipeline.persistence.models import MembershipRole
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)
from meshpipeline.persistence.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

user_repo = UserRepository()
organization_repo = OrganizationRepository()
membership_repo = MembershipRepository()


class SignupDisabled(Exception):
    """This deployment does not provision unrecognised accounts."""


@dataclass(frozen=True)
class Account:
    user_id: str
    # THE TENANT STRING every existing column scopes on: the lowercased email. Deliberately not
    # the Identity Platform uid - re-keying owner_id would rewrite live rows and detach every
    # existing job, session and geometry from the person who made them.
    owner_id: str
    organization_id: str
    email_verified: bool
    name: str
    #: whether THIS call created the account. The caller uses it for nothing but logging; it is
    #: here because a test cannot otherwise tell "provisioned" from "found" without counting rows.
    provisioned: bool


async def resolve_or_provision(db: AsyncSession, token: VerifiedToken, *,
                               now: datetime | None = None) -> Account:
    at = now or datetime.now(UTC)

    # 1. THE UID PATH: the ordinary case, every sign-in after the first.
    user = await user_repo.get_by_firebase_uid(db, token.uid)
    provisioned = False

    if user is None:
        # 2. THE LINKING PATH. A row already carries this address but no uid - which is exactly
        # what 0004's backfill leaves for every owner who predates Identity Platform. Attaching
        # the uid is what makes their existing jobs and geometry follow them in. It is NOT a
        # signup, so it is neither gated by CONSOLE_SIGNUP_ENABLED nor granted credits.
        existing = await user_repo.get_by_email(db, token.email)
        if existing is not None:
            if existing.firebase_uid is None:
                await user_repo.attach_firebase_uid(db, user_id=existing.id,
                                                    firebase_uid=token.uid)
            user = existing
        else:
            # 3. THE SIGNUP PATH.
            if not polcfg.CONSOLE_SIGNUP_ENABLED:
                raise SignupDisabled("this deployment does not provision new accounts")
            user = await _provision(db, token)
            provisioned = True

    await user_repo.record_login(db, user_id=user.id, at=at,
                                 email_verified=token.email_verified)

    organization_id = await membership_repo.organization_id_for_email(db, token.email)
    return Account(
        user_id=str(user.id),
        owner_id=token.email,
        organization_id=str(organization_id) if organization_id else "",
        email_verified=token.email_verified,
        name=token.name or user.name or token.email,
        provisioned=provisioned,
    )


async def _provision(db: AsyncSession, token: VerifiedToken):
    # ONE TRANSACTION, four writes. The session this runs in is committed by the caller's
    # `get_db()` context, so a failure anywhere here leaves no user without an organisation and
    # no organisation without its grant. That atomicity is the whole reason the grant is issued
    # here rather than by a later, separately-failing step.
    user = await user_repo.create(db, email=token.email, name=token.name,
                                  firebase_uid=token.uid)
    organization = await organization_repo.create(db, name=token.email,
                                                  slug=_slug_for(token.uid))
    await membership_repo.create(db, user_id=user.id, organization_id=organization.id,
                                 role=MembershipRole.owner)
    granted = await credit_service.grant_signup_credits(db, organization_id=organization.id)
    logger.info("provisioned organisation %s for a new account, granted %s credits",
                organization.id, granted)
    return user


def _slug_for(firebase_uid: str) -> str:
    # Derived from the uid rather than the email: an address contains characters a slug should
    # not, and two people at the same domain must not collide. The uid is already unique and
    # already URL-safe.
    return f"org-{firebase_uid.lower()[:48]}"


async def organization_id_for_owner(db: AsyncSession, owner_id: str) -> str:
    # THE SEAM the signed-header credential resolves its tenant through, and the only place that
    # journey is written. A cache belongs here and nowhere else.
    if not owner_id:
        return ""
    organization_id = await membership_repo.organization_id_for_email(db, owner_id)
    return str(organization_id) if organization_id else ""
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/application/test_account_service.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Handle the concurrent-first-sign-in race**

Two simultaneous first sign-ins for one uid both find no user and both try to provision. `users.firebase_uid` is unique, so one insert wins and the other raises `IntegrityError`. Add the retry to `resolve_or_provision`, replacing the `user = await _provision(db, token)` / `provisioned = True` pair:

```python
            try:
                user = await _provision(db, token)
                provisioned = True
            except IntegrityError:
                # A concurrent first sign-in won the unique index on firebase_uid. Roll back to
                # the savepoint the failed insert poisoned and read the row the winner wrote -
                # the loser must not answer with a refusal for an account that now exists.
                await db.rollback()
                user = await user_repo.get_by_firebase_uid(db, token.uid)
                if user is None:
                    raise
```

Add the covering test to `tests/unit/application/test_account_service.py`:

```python
async def test_a_concurrent_first_sign_in_yields_one_organisation(fakes, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    real_create = fakes.create_user
    state = {"raised": False}

    async def create_then_lose_the_race(db, *, email, name, firebase_uid):
        if not state["raised"]:
            state["raised"] = True
            # Stand in for the winner having already inserted this uid.
            await real_create(db, email=email, name=name, firebase_uid=firebase_uid)
            await fakes.create_org(db, name=email, slug="org-winner")
            await fakes.create_membership(db, user_id=fakes.users[-1].id,
                                          organization_id=fakes.orgs[-1].id,
                                          role=MembershipRole.owner)
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return await real_create(db, email=email, name=name, firebase_uid=firebase_uid)

    class _UserRepo:
        get_by_firebase_uid = staticmethod(fakes.get_by_firebase_uid)
        get_by_email = staticmethod(fakes.get_by_email)
        create = staticmethod(create_then_lose_the_race)
        attach_firebase_uid = staticmethod(fakes.attach_firebase_uid)
        record_login = staticmethod(fakes.record_login)

    class _Session:
        async def rollback(self):
            return None

    monkeypatch.setattr(account_service, "user_repo", _UserRepo())

    account = await account_service.resolve_or_provision(_Session(), _token())
    assert len(fakes.users) == 1, "the race produced two users"
    assert len(fakes.orgs) == 1, "the race produced two organisations"
    assert account.organization_id == str(fakes.orgs[0].id)
```

- [ ] **Step 6: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/application/test_account_service.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/application/account_service.py tests/unit/application/test_account_service.py
git commit -m "feat(auth): resolve a verified token to an account, provisioning one once

Three paths, in order: a known uid, an existing email with no uid yet, and
a genuine signup. The middle one is what 0004's backfill leaves behind for
every owner who predates Identity Platform, and linking it is what makes
their existing jobs and geometry follow them in rather than being stranded
behind a second, empty account.

Linking is deliberately neither gated by CONSOLE_SIGNUP_ENABLED nor
granted credits: the account already exists, it simply has no uid. Gating
it would lock every pre-existing owner out of their own data, and granting
would hand a balance to somebody who predates the feature.

A new account is a user, an organisation, a membership and one grant in a
single transaction - all of them or none. A concurrent first sign-in loses
the unique index on firebase_uid and re-reads the winner's row instead of
refusing an account that now exists."
```

---

### Task 6: The `POST /auth/session` endpoint

**Files:**
- Create: `src/meshpipeline/api/auth.py`
- Modify: `src/meshpipeline/api/app.py` (beside `app.include_router(v1_router)`, around line 163)
- Test: `tests/unit/security/test_auth_session_route.py`

**Interfaces:**
- Consumes: `firebase_token.verify` (Task 4), `account_service.resolve_or_provision` (Task 5).
- Produces: `POST /auth/session`, request body `{"id_token": "<jwt>"}`, response `{"user_id", "owner_id", "organization_id", "email_verified", "name"}`; router object `meshpipeline.api.auth.router`; setting `polcfg.FIREBASE_PROJECT_ID: str`.

- [ ] **Step 1: Declare the project setting**

In `src/meshpipeline/settings/policy.py`, directly after `CONSOLE_SIGNUP_ENABLED`:

```python
# THE IDENTITY PLATFORM PROJECT this deployment accepts tokens from, and the audience every
# presented token is checked against. Empty means no token verifies - which is the correct
# posture for a deployment that has not been given a project, and is why the verifier refuses
# rather than comparing `aud` against the empty string.
FIREBASE_PROJECT_ID: str = optional_env("FIREBASE_PROJECT_ID", "")
```

In `src/meshpipeline/settings/inventory.py`, in the same `Group("Quotas", ...)` block edited in Task 3 — or, if the file has an authentication-shaped group, there instead:

```python
        EnvVar("FIREBASE_PROJECT_ID", "",
               help="the Identity Platform project whose ID tokens the console signs in with"),
```

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/security/test_auth_session_route.py`:

```python
# Responsibility: Verify the console's sign-in endpoint refuses everything it should and discloses nothing.
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import auth as auth_route
from meshpipeline.application.account_service import Account, SignupDisabled
from meshpipeline.contracts.firebase_token import InvalidToken, VerifiedToken

pytestmark = pytest.mark.asyncio

_ACCOUNT = Account(user_id="user-1", owner_id="person@example.com",
                   organization_id="org-1", email_verified=True, name="A Person",
                   provisioned=False)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "console-key")
    monkeypatch.setattr(polcfg, "FIREBASE_PROJECT_ID", "hexera-dev")

    def fake_verify(raw, *, project_id):
        if raw == "good":
            return VerifiedToken(uid="uid-1", email="person@example.com",
                                 email_verified=True, name="A Person")
        raise InvalidToken("no")

    async def fake_resolve(db, token, now=None):
        return _ACCOUNT

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(auth_route.firebase_token, "verify", fake_verify)
    monkeypatch.setattr(auth_route.account_service, "resolve_or_provision", fake_resolve)
    monkeypatch.setattr(auth_route, "get_db", lambda: _NullSession())

    application = FastAPI()
    application.include_router(auth_route.router)
    return application


async def _post(app, body, headers=None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/auth/session", json=body,
                                 headers=headers or {"x-api-key": "console-key"})


async def test_a_good_token_returns_the_identity_the_console_needs(app):
    response = await _post(app, {"id_token": "good"})
    assert response.status_code == 200
    assert response.json() == {"user_id": "user-1", "owner_id": "person@example.com",
                               "organization_id": "org-1", "email_verified": True,
                               "name": "A Person"}


async def test_a_caller_without_the_api_key_is_refused(app):
    response = await _post(app, {"id_token": "good"}, headers={})
    assert response.status_code == 401


async def test_a_caller_with_the_wrong_api_key_is_refused(app):
    response = await _post(app, {"id_token": "good"}, headers={"x-api-key": "not-it"})
    assert response.status_code == 401


async def test_a_bad_token_is_refused_without_saying_why(app):
    response = await _post(app, {"id_token": "forged"})
    assert response.status_code == 401
    # One refusal for every cause, exactly as api_key_service.authenticate does.
    assert response.json()["detail"] == auth_route.REFUSAL


async def test_a_missing_token_is_refused_the_same_way(app):
    response = await _post(app, {})
    assert response.status_code == 401
    assert response.json()["detail"] == auth_route.REFUSAL


async def test_a_closed_signup_answers_403_so_the_console_can_explain_it(app, monkeypatch):
    # Distinct from 401 on purpose: this is the ONE refusal a user can do something about, and
    # "this deployment is not open" is not a secret. It leaks no account existence - it is the
    # same answer for every unknown token.
    async def refuse(db, token, now=None):
        raise SignupDisabled("closed")

    monkeypatch.setattr(auth_route.account_service, "resolve_or_provision", refuse)
    response = await _post(app, {"id_token": "good"})
    assert response.status_code == 403


async def test_the_token_never_reaches_the_logs(app, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    await _post(app, {"id_token": "good"})
    await _post(app, {"id_token": "forged"})
    assert "good" not in caplog.text
    assert "forged" not in caplog.text


async def test_no_project_configured_refuses_every_token(app, monkeypatch):
    monkeypatch.setattr(polcfg, "FIREBASE_PROJECT_ID", "")
    response = await _post(app, {"id_token": "good"})
    assert response.status_code == 401
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `python -m pytest tests/unit/security/test_auth_session_route.py -v`
Expected: FAIL — `ImportError: cannot import name 'auth' from 'meshpipeline.api'`.

- [ ] **Step 4: Write the route**

Create `src/meshpipeline/api/auth.py`:

```python
# Responsibility: Turn the console's Identity Platform token into the identity the rest of this API already understands.
# Owns: the only endpoint that accepts a credential the product did not mint, and the refusal it answers with.
# Boundaries: identity only - it never decides what the caller may then do.
from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import account_service
from meshpipeline.contracts import firebase_token
from meshpipeline.persistence.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

#: ONE refusal for every cause: an absent token, a malformed one, a forged one, an expired one,
#: one addressed to another project. The difference is only useful to somebody probing which of
#: their guesses is close - the same discipline api_key_service.authenticate applies.
REFUSAL = "Invalid or expired sign-in token"


@router.post("/auth/session")
async def create_session(
    id_token: Annotated[str, Body(embed=True)] = "",
    x_api_key: Annotated[str | None, Header()] = None,
) -> dict:
    # THE INTERNAL GATE, checked before anything else. This endpoint accepts a credential minted
    # outside the product, so it must not be a surface the public can reach even to have refused.
    # An unset MESH_API_KEY is local dev, where nothing is gated - identical to verify_identity.
    if polcfg.MESH_API_KEY:
        if not x_api_key or not hmac.compare_digest(x_api_key, polcfg.MESH_API_KEY):
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    try:
        verified = firebase_token.verify(id_token,
                                         project_id=polcfg.FIREBASE_PROJECT_ID)
    except firebase_token.InvalidToken:
        # Deliberately NOT logged with the token, the exception message or the email. The first
        # is a live credential, and the third would put an address into the log of every failed
        # attempt - including attempts by people who do not have an account here.
        logger.info("sign-in refused: token did not verify")
        raise HTTPException(status_code=401, detail=REFUSAL) from None

    try:
        async with get_db() as db:
            account = await account_service.resolve_or_provision(db, verified)
    except account_service.SignupDisabled:
        # 403, not 401, and the one refusal that says something. A user can act on "this
        # deployment is not open"; it discloses no account existence, because it is the answer
        # for every unrecognised token alike.
        raise HTTPException(
            status_code=403,
            detail="This deployment is not accepting new accounts") from None

    return {
        "user_id": account.user_id,
        "owner_id": account.owner_id,
        "organization_id": account.organization_id,
        "email_verified": account.email_verified,
        "name": account.name,
    }
```

- [ ] **Step 5: Mount it**

In `src/meshpipeline/api/app.py`, beside the existing `app.include_router(v1_router)` (around line 163):

```python
from meshpipeline.api.auth import router as auth_router

# Mounted OUTSIDE /api/v1 deliberately. It is not part of the versioned product surface a caller
# with an API key uses; it is the console's own sign-in seam, gated on MESH_API_KEY, and keeping
# it off /api/v1 means an edge rule can exclude it by path without carving a hole in the version.
app.include_router(auth_router, tags=["auth"])
```

Place the import beside the other `meshpipeline.api` imports at the top of the file rather than inline.

- [ ] **Step 6: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/security/test_auth_session_route.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 7: Run the whole-app HTTP smoke**

Run: `python -m pytest tests/unit/api/test_http_smoke.py tests/unit/api/test_readiness.py -v`
Expected: PASS. These assemble the real app; a mounting mistake shows up here.

- [ ] **Step 8: Commit**

```bash
git add src/meshpipeline/api/auth.py src/meshpipeline/api/app.py src/meshpipeline/settings/policy.py src/meshpipeline/settings/inventory.py tests/unit/security/test_auth_session_route.py
git commit -m "feat(api): POST /auth/session, the console's sign-in seam

Gated on MESH_API_KEY before anything else: this accepts a credential the
product did not mint, so it must not be a surface the public can reach
even to have refused.

One 401 for every cause - absent, malformed, forged, expired, wrong
project - because the difference is only useful to someone probing which
guess is close. The token, the exception message and the email are all
kept out of the log; logging the email would record an address on every
failed attempt, including from people with no account here.

Closed signup answers 403, not 401. It is the one refusal a user can act
on, and it discloses nothing: it is the same answer for every unrecognised
token."
```

---

### Task 7: Tenant columns and the backfill

**Files:**
- Modify: `src/meshpipeline/persistence/models.py` (add `organization_id` to seven classes; add the FK to `ApiKey.organization_id`)
- Create: `alembic/versions/0004_tenant_columns.py`
- Test: `tests/unit/security/test_tenant_columns_schema.py`
- Test: `tests/integration/test_tenant_backfill_postgres.py`

**Interfaces:**
- Consumes: `organizations` from Task 1.
- Produces: `organization_id: Mapped[uuid.UUID | None]` on `GeometrySource`, `SimulationJob`, `ChatSession`, `GeometryInterpretationRow`, `CaptureOperation`, `ArtifactReconciliation`, `SourceObjectCleanup`; alembic revision `0004_tenant_columns` with `down_revision = '0003_identity_and_credits'`.

- [ ] **Step 1: Write the failing schema test**

Create `tests/unit/security/test_tenant_columns_schema.py`:

```python
# Responsibility: Verify every owner-scoped table carries the tenant column, nullable and indexed.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.session import Base

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic" / "versions" / "0004_tenant_columns.py"

#: Every table that scopes on owner_id today, and therefore every table that must scope on an
#: organisation tomorrow. `artifacts` is absent deliberately: it carries no owner_id, reaching
#: its tenant through the job it belongs to.
_TENANT_TABLES = (
    "geometry_sources", "simulation_jobs", "chat_sessions", "geometry_interpretations",
    "capture_operations", "artifact_reconciliations", "source_object_cleanups",
)


def test_every_owner_scoped_table_carries_the_tenant_column():
    for table in _TENANT_TABLES:
        columns = {c.name for c in Base.metadata.tables[table].columns}
        assert "organization_id" in columns, table


def test_the_tenant_column_is_nullable_everywhere():
    # 0004 runs BEFORE the new image (deploy.sh stage 220 precedes 245), so the old revision
    # briefly inserts rows that name no organisation. NOT NULL here would fail those inserts.
    for table in _TENANT_TABLES:
        column = Base.metadata.tables[table].columns["organization_id"]
        assert column.nullable, table


def test_the_tenant_column_is_indexed_everywhere():
    for table in _TENANT_TABLES:
        indexes = Base.metadata.tables[table].indexes
        assert any("organization_id" in [c.name for c in i.columns] for i in indexes), table


def test_every_owner_scoped_table_is_covered_by_this_test():
    # The guard that stops a NEW owner-scoped table quietly escaping the tenant boundary.
    owner_scoped = {name for name, table in Base.metadata.tables.items()
                    if "owner_id" in {c.name for c in table.columns}}
    assert owner_scoped - {"api_keys"} == set(_TENANT_TABLES), owner_scoped


def test_the_api_keys_tenant_column_finally_has_its_foreign_key():
    # 0002 shipped it unconstrained with a comment promising the FK "arrives with the
    # organisations migration". This is that migration.
    column = Base.metadata.tables["api_keys"].columns["organization_id"]
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert targets == {"organizations.id"}


def test_the_migration_descends_from_the_identity_revision_and_reverses_itself():
    src = MIGRATION.read_text()
    tree = ast.parse(src)
    assigned = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert assigned["revision"] == "0004_tenant_columns"
    assert assigned["down_revision"] == "0003_identity_and_credits"
    for table in _TENANT_TABLES:
        assert f"op.drop_column('{table}', 'organization_id')" in src, table
    assert "meshpipeline" not in src, "the migration imports the application it is meant to outlive"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/unit/security/test_tenant_columns_schema.py -v`
Expected: FAIL — `organization_id` absent from `geometry_sources`.

- [ ] **Step 3: Add the column to each model**

For each of the seven classes in `src/meshpipeline/persistence/models.py`, add the column immediately after its `owner_id` declaration. Use this text every time, so the seven read identically:

```python
    # WHICH ORGANISATION this row belongs to - the tenant key reads scope on. Nullable through
    # this cycle: 0004 migrates before the new image ships, so the old revision briefly inserts
    # rows that name no organisation, and NOT NULL would fail those inserts. 0005 closes it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True,
        index=True)
```

Then change `ApiKey.organization_id` to carry the same `ForeignKey("organizations.id", ondelete="RESTRICT")`, and update its comment — the sentence promising the FK "arrives with the organisations migration" is now stale. Replace that comment with:

```python
    # WHICH ORGANISATION that owner belongs to. The foreign key 0002 promised arrives in 0004,
    # with the table it points at. Still nullable: a key issued before the backfill names none.
```

Update `tests/unit/security/test_api_key_schema.py::test_the_tenant_columns_are_scoped_the_way_the_rest_of_the_schema_is` — its comment claims organisations do not exist. Replace that comment with `# Nullable still: a key issued before the backfill names no organisation.` and leave the assertions alone.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0004_tenant_columns.py`:

```python
# Responsibility: Give every owner-scoped table an organisation, and fill it in for the rows that already exist.
# Boundaries: the tenant column and its backfill; the tables it points at were created by 0003.

# THIS IS THE HALF THAT TOUCHES LIVE ROWS, which is why it is not 0003. It must be idempotent -
# re-running it changes nothing - and it must be rehearsed against a restored copy of the dev
# database before it is allowed near prod.
#
# The column is NULLABLE on purpose. deploy.sh migrates at stage 220 and deploys the API at stage
# 245, so between them the OLD revision serves against the NEW schema and inserts rows naming no
# organisation. NOT NULL would fail those inserts. 0005 closes the column once no writer can
# produce one.
#
# Revision ID: 0004_tenant_columns
# Revises: 0003_identity_and_credits
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004_tenant_columns'
down_revision = '0003_identity_and_credits'
branch_labels = None
depends_on = None

#: Every table that scopes on owner_id. `artifacts` is deliberately absent: it has no owner_id and
#: reaches its tenant through the job it belongs to.
_TENANT_TABLES = (
    'geometry_sources', 'simulation_jobs', 'chat_sessions', 'geometry_interpretations',
    'capture_operations', 'artifact_reconciliations', 'source_object_cleanups',
)


def upgrade():
    for table in _TENANT_TABLES:
        op.add_column(table, sa.Column('organization_id', sa.UUID(), nullable=True))
        op.create_index(op.f(f'ix_{table}_organization_id'), table, ['organization_id'],
                        unique=False)
        op.create_foreign_key(f'fk_{table}_organization_id', table, 'organizations',
                              ['organization_id'], ['id'], ondelete='RESTRICT')

    # The key 0002 declared without one, now that the table it points at exists.
    op.create_foreign_key('fk_api_keys_organization_id', 'api_keys', 'organizations',
                          ['organization_id'], ['id'], ondelete='RESTRICT')

    _backfill()


def _backfill():
    bind = op.get_bind()

    # 1. ONE ORGANISATION AND ONE USER PER DISTINCT owner_id, across every tenant table.
    #    owner_id is an email everywhere it is a real identity. A value that is not one still
    #    gets a row: it owns data, and leaving it unstamped would make that data unreachable
    #    once reads scope on the organisation.
    #
    #    `WHERE NOT EXISTS` is what makes the whole backfill idempotent: a second run inserts
    #    nothing, which matters because this migration will be rehearsed more than once.
    owners_union = " UNION ".join(
        f"SELECT DISTINCT owner_id FROM {table} WHERE owner_id IS NOT NULL AND owner_id <> ''"
        for table in _TENANT_TABLES)

    bind.execute(sa.text(f"""
        INSERT INTO users (id, firebase_uid, email, name, created_at)
        SELECT gen_random_uuid(), NULL, lower(o.owner_id), '', now()
        FROM ({owners_union}) AS o
        WHERE o.owner_id LIKE '%@%'
          AND NOT EXISTS (SELECT 1 FROM users u WHERE u.email = lower(o.owner_id))
    """))

    bind.execute(sa.text(f"""
        INSERT INTO organizations (id, name, slug, created_at)
        SELECT gen_random_uuid(), o.owner_id, 'backfill-' || md5(lower(o.owner_id)), now()
        FROM ({owners_union}) AS o
        WHERE NOT EXISTS (
            SELECT 1 FROM organizations g WHERE g.slug = 'backfill-' || md5(lower(o.owner_id)))
    """))

    #    A membership only where the owner_id was an address and so produced a user. An owner_id
    #    that is not an address still has an organisation - its data has a tenant - but nobody
    #    can sign in as it, which is correct.
    bind.execute(sa.text(f"""
        INSERT INTO memberships (id, user_id, organization_id, role, created_at)
        SELECT gen_random_uuid(), u.id, g.id, 'owner', now()
        FROM ({owners_union}) AS o
        JOIN users u ON u.email = lower(o.owner_id)
        JOIN organizations g ON g.slug = 'backfill-' || md5(lower(o.owner_id))
        WHERE NOT EXISTS (
            SELECT 1 FROM memberships m
            WHERE m.user_id = u.id AND m.organization_id = g.id)
    """))

    # 2. STAMP EVERY EXISTING ROW from its own owner_id. `IS NULL` keeps it idempotent and means
    #    a row written between the stamp and the new image is left for the application to fill.
    for table in _TENANT_TABLES:
        bind.execute(sa.text(f"""
            UPDATE {table} SET organization_id = g.id
            FROM organizations g
            WHERE g.slug = 'backfill-' || md5(lower({table}.owner_id))
              AND {table}.organization_id IS NULL
              AND {table}.owner_id IS NOT NULL AND {table}.owner_id <> ''
        """))

    bind.execute(sa.text("""
        UPDATE api_keys SET organization_id = g.id
        FROM organizations g
        WHERE g.slug = 'backfill-' || md5(lower(api_keys.owner_id))
          AND api_keys.organization_id IS NULL
          AND api_keys.owner_id IS NOT NULL AND api_keys.owner_id <> ''
    """))


def downgrade():
    # The FK first: a column cannot be dropped while a constraint names it. The backfilled
    # organisations, users and memberships are 0003's tables and are NOT removed here - dropping
    # them is that revision's downgrade, and doing it from this one would delete accounts.
    op.drop_constraint('fk_api_keys_organization_id', 'api_keys', type_='foreignkey')
    op.execute("UPDATE api_keys SET organization_id = NULL")
    for table in _TENANT_TABLES:
        op.drop_constraint(f'fk_{table}_organization_id', table, type_='foreignkey')
        op.drop_index(op.f(f'ix_{table}_organization_id'), table_name=table)
        op.drop_column(table, 'organization_id')
```

- [ ] **Step 5: Confirm `gen_random_uuid()` is available**

Run: `psql "$DATABASE_URL" -c "SELECT gen_random_uuid();"` against any development database, or check the baseline migration for a `pgcrypto` extension.
Expected: a uuid. `gen_random_uuid()` is built in from PostgreSQL 13. If the target server is older, add `op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')` as the first statement of `_backfill()` and note it in the migration's header comment.

- [ ] **Step 6: Run the schema test**

Run: `python -m pytest tests/unit/security/test_tenant_columns_schema.py tests/unit/security/test_api_key_schema.py -v`
Expected: PASS.

- [ ] **Step 7: Write the backfill integration test**

Create `tests/integration/test_tenant_backfill_postgres.py`. Follow the tier's existing conventions — open `tests/integration/test_migration_wrapper_postgres.py` and `tests/disposable_database.py` first and mirror how they provision a disposable database and run alembic:

```python
# Responsibility: Verify 0004 stamps every existing row with an organisation, exactly once, whatever it is re-run against.
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)


def _seed(conn):
    """Two owners with rows across three tenant tables, as they existed before 0004."""
    for owner in ("alpha@example.com", "bravo@example.com"):
        conn.execute(text("""
            INSERT INTO geometry_sources
                (id, owner_id, original_filename, object_key, sha256, size_bytes)
            VALUES (:id, :owner, 'part.step', :key, :sha, 10)
        """), {"id": uuid.uuid4(), "owner": owner, "key": f"k/{owner}/{uuid.uuid4()}",
               "sha": "0" * 64})


def test_the_backfill_gives_every_owner_exactly_one_organisation(migrated_to_0003, run_migration):
    # Seed at 0003 - the identity tables exist, the tenant columns do not - then run 0004.
    _seed(migrated_to_0003)
    run_migration("0004_tenant_columns")

    count = migrated_to_0003.execute(text("SELECT count(*) FROM organizations")).scalar()
    assert count == 2

    unstamped = migrated_to_0003.execute(
        text("SELECT count(*) FROM geometry_sources WHERE organization_id IS NULL")).scalar()
    assert unstamped == 0


def test_each_owners_rows_land_in_their_own_organisation(migrated_to_0003, run_migration):
    _seed(migrated_to_0003)
    run_migration("0004_tenant_columns")

    distinct = migrated_to_0003.execute(text("""
        SELECT count(DISTINCT organization_id) FROM geometry_sources
    """)).scalar()
    assert distinct == 2, "two owners' rows share one organisation"


def test_every_backfilled_owner_gets_a_user_and_a_membership(migrated_to_0003, run_migration):
    _seed(migrated_to_0003)
    run_migration("0004_tenant_columns")

    users = migrated_to_0003.execute(text("SELECT count(*) FROM users")).scalar()
    memberships = migrated_to_0003.execute(text("SELECT count(*) FROM memberships")).scalar()
    assert (users, memberships) == (2, 2)


def test_a_backfilled_user_has_no_uid_and_no_credits(migrated_to_0003, run_migration):
    # They have not signed up yet. The uid arrives when they do; the grant never does, because a
    # grant is for a NEW account and these predate the feature.
    _seed(migrated_to_0003)
    run_migration("0004_tenant_columns")

    uids = migrated_to_0003.execute(
        text("SELECT count(*) FROM users WHERE firebase_uid IS NOT NULL")).scalar()
    entries = migrated_to_0003.execute(text("SELECT count(*) FROM credit_ledger")).scalar()
    assert (uids, entries) == (0, 0)


def test_running_the_backfill_twice_changes_nothing(migrated_to_0003, run_migration):
    # It WILL be run more than once: rehearsal against a restored copy is a requirement, and a
    # re-run that duplicated organisations would split one tenant's data across two.
    _seed(migrated_to_0003)
    run_migration("0004_tenant_columns")
    before = migrated_to_0003.execute(text("""
        SELECT (SELECT count(*) FROM organizations), (SELECT count(*) FROM users),
               (SELECT count(*) FROM memberships)
    """)).one()

    migrated_to_0003.execute(text("SELECT 1"))
    run_migration("0004_tenant_columns", rerun=True)

    after = migrated_to_0003.execute(text("""
        SELECT (SELECT count(*) FROM organizations), (SELECT count(*) FROM users),
               (SELECT count(*) FROM memberships)
    """)).one()
    assert before == after


def test_the_downgrade_removes_the_columns_without_deleting_accounts(migrated_to_0003,
                                                                     run_migration):
    _seed(migrated_to_0003)
    run_migration("0004_tenant_columns")
    run_migration("0003_identity_and_credits", downgrade=True)

    columns = migrated_to_0003.execute(text("""
        SELECT count(*) FROM information_schema.columns
        WHERE table_name = 'geometry_sources' AND column_name = 'organization_id'
    """)).scalar()
    assert columns == 0
    # 0003's tables are 0003's to drop. This downgrade must not take accounts with it.
    users = migrated_to_0003.execute(text("SELECT count(*) FROM users")).scalar()
    assert users == 2
```

Write the `migrated_to_0003` and `run_migration` fixtures in this file or in `tests/integration/conftest.py`, following whatever `tests/integration/test_migration_wrapper_postgres.py` already does to stand up a disposable database and drive alembic. Do not invent a second mechanism.

- [ ] **Step 8: Run the integration test**

Run: `make test-integration`
Expected: PASS. This tier needs real Postgres; if it is not provisioned locally, `make test-container-integration` provisions one.

- [ ] **Step 9: Commit**

```bash
git add src/meshpipeline/persistence/models.py alembic/versions/0004_tenant_columns.py tests/unit/security/test_tenant_columns_schema.py tests/unit/security/test_api_key_schema.py tests/integration/test_tenant_backfill_postgres.py
git commit -m "feat(schema): organization_id on every owner-scoped table, backfilled

Separate from 0003 because this is the half that touches live rows. It has
to be reviewable, rehearsable and reversible without the new tables moving
with it.

Every insert predicates on NOT EXISTS and every update on IS NULL, so
re-running changes nothing - which matters, because rehearsing against a
restored copy before prod means it runs more than once, and a re-run that
duplicated organisations would split one tenant's data across two.

The column is nullable: deploy.sh migrates at stage 220 and deploys the API
at stage 245, so the old revision briefly inserts rows naming no
organisation. 0005 closes it once no writer can produce one.

The downgrade drops the columns but not the accounts. Those are 0003's
tables and 0003's downgrade."
```

---

### Task 8: Populate `Principal.organization_id`

**Files:**
- Modify: `src/meshpipeline/api/security.py`
- Test: `tests/unit/security/test_principal_organization.py`

**Interfaces:**
- Consumes: `account_service.organization_id_for_owner` (Task 5).
- Produces: `resolve_principal` returning a populated `organization_id`; new dependency `org_dep(principal) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/security/test_principal_organization.py`:

```python
# Responsibility: Verify a proven caller resolves to the organisation they act within, whichever credential proved them.
from __future__ import annotations

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import security
from meshpipeline.contracts.identity import Credential

pytestmark = pytest.mark.asyncio


@pytest.fixture
def memberships(monkeypatch):
    store = {"person@example.com": "org-1"}

    async def fake_lookup(db, owner_id):
        return store.get(owner_id, "")

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(security.account_service, "organization_id_for_owner", fake_lookup)
    monkeypatch.setattr(security, "get_db", lambda: _NullSession())
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "")
    return store


async def test_a_header_caller_resolves_to_their_organisation(memberships):
    principal = await security.resolve_principal(None, None, "person@example.com", None)
    assert principal.owner_id == "person@example.com"
    assert principal.organization_id == "org-1"
    assert principal.credential is Credential.self_asserted


async def test_an_owner_with_no_membership_resolves_to_no_organisation(memberships):
    principal = await security.resolve_principal(None, None, "stranger@example.com", None)
    assert principal.organization_id == ""


async def test_a_failed_lookup_does_not_refuse_the_caller(memberships, monkeypatch):
    # The organisation is a SCOPE, not a credential. A database hiccup while resolving it must
    # degrade to today's owner-only behaviour, not turn a proven caller into a 500.
    async def explode(db, owner_id):
        raise RuntimeError("the database is having a moment")

    monkeypatch.setattr(security.account_service, "organization_id_for_owner", explode)
    principal = await security.resolve_principal(None, None, "person@example.com", None)
    assert principal.owner_id == "person@example.com"
    assert principal.organization_id == ""


async def test_a_key_caller_keeps_the_organisation_its_row_names(memberships, monkeypatch):
    # A key already carries its organisation. Re-resolving it from the owner would override the
    # row - and a key may later be scoped to one organisation while its owner belongs to several.
    from meshpipeline.contracts.identity import Principal

    async def fake_key_principal(presented):
        return Principal(owner_id="person@example.com", organization_id="org-from-the-key",
                         credential=Credential.api_key)

    monkeypatch.setattr(security, "_principal_from_key", fake_key_principal)
    principal = await security.resolve_principal("Bearer hx_live_abc_def", None, None, None)
    assert principal.organization_id == "org-from-the-key"


async def test_org_dep_reports_what_the_principal_carries():
    from meshpipeline.contracts.identity import Principal
    assert await security.org_dep(Principal(owner_id="x", organization_id="org-9")) == "org-9"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/unit/security/test_principal_organization.py -v`
Expected: FAIL — `AttributeError: module 'meshpipeline.api.security' has no attribute 'account_service'`.

- [ ] **Step 3: Populate the organisation at the seam**

In `src/meshpipeline/api/security.py`, add to the imports:

```python
from meshpipeline.application import account_service
```

Replace the tail of `resolve_principal` — the `owner_id = verify_identity(...)` block — with:

```python
    owner_id = verify_identity(x_api_key, x_user_id, x_user_sig)
    return Principal(owner_id=owner_id,
                     organization_id=await _organization_for(owner_id),
                     credential=Credential.signed_header if polcfg.USER_TOKEN_SECRET
                     else Credential.self_asserted)


async def _organization_for(owner_id: str) -> str:
    # THE ONE PLACE a header credential's tenant is resolved. One indexed read per request; a
    # cache belongs here and nowhere else, which is why the journey is a single call rather than
    # a join written at each call site.
    #
    # A key credential does NOT come through here: its row already names an organisation, and a
    # key may be scoped to one while its owner belongs to several.
    if not owner_id:
        return ""
    try:
        async with get_db() as db:
            return await account_service.organization_id_for_owner(db, owner_id)
    except Exception as exc:
        # FAIL OPEN TO OWNER SCOPE, never to a refusal. The organisation is a scope, not a
        # credential: the caller has already proven who they are, and a lookup that cannot run
        # must degrade to today's owner-only behaviour rather than 500 a proven request. Reads
        # fall back to owner_id when the principal names no organisation (see the repositories).
        logger.warning("could not resolve an organisation for %s - scoping on owner alone: %s",
                       owner_id, exc)
        return ""
```

Add `org_dep` beside `plan_dep` at the end of the module:

```python
async def org_dep(principal: Annotated[Principal, Depends(principal_dep)]) -> str:
    # Beside owner_dep, resolved from the SAME cached principal - so a route taking both gets one
    # credential check and one organisation lookup, not two.
    return principal.organization_id
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/security/test_principal_organization.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Run the whole security suite**

Run: `python -m pytest tests/unit/security -v`
Expected: PASS. `test_auth.py`, `test_auth_fail_closed.py` and `test_api_key_authentication.py` all drive `resolve_principal`; if any now needs a database that the unit tier does not have, the `_organization_for` fallback is not catching something — fix that rather than the test.

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/api/security.py tests/unit/security/test_principal_organization.py
git commit -m "feat(auth): resolve the organisation a header credential acts within

The seam's own comment promised this: it said resolve_principal would
start filling organization_id when organisations arrived. They have.

A key credential is untouched - its row already names an organisation, and
a key may be scoped to one while its owner belongs to several.

The lookup fails open to owner scope, never to a refusal. An organisation
is a scope, not a credential: the caller has already proven who they are,
and a lookup that cannot run must degrade to today's behaviour rather than
500 a proven request."
```

---

### Task 9: Scope the repositories on the organisation

**Files:**
- Modify: every file under `src/meshpipeline/persistence/repositories/` that filters on `owner_id` (50 sites)
- Modify: the 13 `owner_dep` sites under `src/meshpipeline/api/`
- Test: `tests/unit/persistence/test_org_scoped_reads.py`
- Test: extend `tests/integration/test_owner_isolation_matrix.py`

**Interfaces:**
- Consumes: `Principal.organization_id` (Task 8), the `organization_id` columns (Task 7).
- Produces: repository read methods taking `organization_id: str = ""` as a keyword-only argument beside `owner_id`; write methods stamping both.

- [ ] **Step 1: Read the ground truth before changing anything**

Run: `grep -rn "owner_id" src/meshpipeline/persistence/repositories/ | wc -l` (expect 50) and `grep -rn "owner_dep" src/meshpipeline/api/ | wc -l` (expect 13).

Read `tests/integration/test_owner_isolation_matrix.py` and `tests/tenant_repositories.py` in full. The scoping rule you are implementing must satisfy `scoped_repo_contract`, which already asserts that a foreign row and an unknown row are indistinguishable.

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/persistence/test_org_scoped_reads.py`:

```python
# Responsibility: Verify a read scopes on the organisation, and degrades to the owner when there is none.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _predicate_columns(statement) -> set:
    """Every column named anywhere in a statement's WHERE clause."""
    return {c.name for c in statement.whereclause.get_children(column_collections=False)
            if hasattr(c, "name")} if statement.whereclause is not None else set()


async def test_a_read_with_an_organisation_scopes_on_it():
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    statement = GeometrySourceRepository().scope_predicate(
        owner_id="person@example.com", organization_id="11111111-1111-1111-1111-111111111111")
    assert "organization_id" in statement


async def test_a_read_without_an_organisation_falls_back_to_the_owner():
    # A principal whose organisation could not be resolved - a database hiccup, or a deployment
    # mid-migration - must still see their own rows. Returning nothing would look like data loss.
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    statement = GeometrySourceRepository().scope_predicate(
        owner_id="person@example.com", organization_id="")
    assert "owner_id" in statement
    assert "organization_id" not in statement


async def test_a_write_stamps_both():
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    values = GeometrySourceRepository().stamp(
        owner_id="person@example.com", organization_id="11111111-1111-1111-1111-111111111111")
    assert set(values) == {"owner_id", "organization_id"}
```

Adjust the import and method names in these three tests to whatever Step 3 actually produces — write the tests first, watch them fail, then make them pass.

- [ ] **Step 3: Add the shared scoping helper**

Create `src/meshpipeline/persistence/repositories/tenant_scope.py`:

```python
# Responsibility: Express the one tenant predicate every scoped read uses, and the one stamp every scoped write applies.
# Owns: the rule that reads scope on the organisation and fall back to the owner when there is none.
# Boundaries: the predicate and the stamp; which table they are applied to is the repository's question.
from __future__ import annotations

import uuid


def scope(model, *, owner_id: str, organization_id: str = ""):
    """The WHERE clause a tenant-scoped read filters on.

    THE RULE: reads scope on organization_id. A principal that names no organisation - one whose
    lookup failed, or a deployment between 0004 and the image that fills the column - falls back
    to owner_id rather than matching nothing. Matching nothing would read as data loss to the
    person whose rows they are, and this fallback is exactly today's behaviour, so the degraded
    path is one we already ship.
    """
    if organization_id:
        return model.organization_id == uuid.UUID(organization_id)
    return model.owner_id == owner_id


def stamp(*, owner_id: str, organization_id: str = "") -> dict:
    """The columns a tenant-scoped write sets.

    BOTH, always. owner_id stays the actor - who did this - and organization_id becomes the
    tenant. Dropping owner_id would lose per-seat attribution the moment organisations hold more
    than one person, which memberships already allows.
    """
    values: dict = {"owner_id": owner_id}
    if organization_id:
        values["organization_id"] = uuid.UUID(organization_id)
    return values
```

- [ ] **Step 4: Apply it, one repository at a time**

Work through `src/meshpipeline/persistence/repositories/` alphabetically. For each read method that today filters `.where(Model.owner_id == owner_id)`:

- add a keyword-only `organization_id: str = ""` parameter beside the existing `owner_id`
- replace the predicate with `.where(tenant_scope.scope(Model, owner_id=owner_id, organization_id=organization_id))`

For each write method that sets `owner_id=owner_id`:

- add the same parameter
- replace with `**tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id)`

The default `""` means every existing caller keeps compiling and keeps behaving exactly as it does today, so this task can be committed per-repository and each commit is green.

Commit after each repository:

```bash
git add src/meshpipeline/persistence/repositories/<name>_repository.py
git commit -m "refactor(persistence): scope <name> reads on the organisation"
```

- [ ] **Step 5: Thread the organisation through the routes**

For each of the 13 `owner_dep` sites under `src/meshpipeline/api/`, add the organisation beside the owner:

```python
    owner_id: Annotated[str, Depends(owner_dep)],
    organization_id: Annotated[str, Depends(org_dep)],
```

and pass `organization_id=organization_id` into the repository call. Both dependencies resolve from the same cached principal, so this adds no second credential check.

- [ ] **Step 6: Run the tests**

Run: `make test-fast`
Expected: PASS.

- [ ] **Step 7: Extend the isolation matrix**

In `tests/integration/test_owner_isolation_matrix.py`, add a case proving a foreign *organisation* is refused, mirroring exactly how the file already proves a foreign owner is. Read the existing cases and follow their shape; do not introduce a second mechanism for standing up the API.

- [ ] **Step 8: Run the integration tier**

Run: `make test-integration`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/meshpipeline/persistence/repositories/tenant_scope.py src/meshpipeline/api tests/unit/persistence/test_org_scoped_reads.py tests/integration/test_owner_isolation_matrix.py
git commit -m "feat(tenancy): reads scope on the organisation, writes stamp both

One predicate and one stamp, written once in tenant_scope and applied at
every site, so the rule cannot drift between fifty of them.

A principal naming no organisation falls back to owner_id rather than
matching nothing. Matching nothing would read as data loss to the person
whose rows they are, and the fallback is exactly today's behaviour.

Writes keep stamping owner_id. It stays the actor - who did this - and
dropping it would lose per-seat attribution the moment an organisation
holds more than one person, which memberships already allows."
```

---

### Task 10: `GET /api/v1/credits`

**Files:**
- Create: `src/meshpipeline/api/v1/credits.py`
- Modify: `src/meshpipeline/api/v1/router.py`
- Modify: `src/meshpipeline/api/v1/client_config.py`
- Test: `tests/unit/api/test_credits_route.py`

**Interfaces:**
- Consumes: `credit_service.balance` (Task 3), `org_dep` (Task 8).
- Produces: `GET /api/v1/credits` returning `{"balance": int, "unit": "credits"}`; `client_config()` gaining `"auth": {"signup_enabled": bool}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/api/test_credits_route.py`:

```python
# Responsibility: Verify the balance is read for the caller's own organisation and for nobody else's.
from __future__ import annotations

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import client_config, credits

pytestmark = pytest.mark.asyncio


@pytest.fixture
def balances(monkeypatch):
    store = {"org-1": 100}

    async def fake_balance(db, *, organization_id):
        return store.get(organization_id, 0)

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(credits.credit_service, "balance", fake_balance)
    monkeypatch.setattr(credits, "get_db", lambda: _NullSession())
    return store


async def test_the_balance_is_the_callers_own(balances):
    assert await credits.read_balance(organization_id="org-1") == {"balance": 100,
                                                                   "unit": "credits"}


async def test_another_organisations_balance_is_not_reachable(balances):
    assert await credits.read_balance(organization_id="org-2") == {"balance": 0,
                                                                   "unit": "credits"}


async def test_a_caller_with_no_organisation_reads_zero_rather_than_failing(balances):
    # Mid-migration, or a lookup that could not run. A missing balance is 0, not a 500.
    assert await credits.read_balance(organization_id="") == {"balance": 0, "unit": "credits"}


async def test_the_client_config_tells_the_console_whether_signup_is_open(monkeypatch):
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)
    payload = await client_config.client_config()
    assert payload["auth"] == {"signup_enabled": False}
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/unit/api/test_credits_route.py -v`
Expected: FAIL — `ImportError: cannot import name 'credits'`.

- [ ] **Step 3: Write the route**

Create `src/meshpipeline/api/v1/credits.py`:

```python
# Responsibility: Report what an organisation's credit balance is.
# Boundaries: reading only - nothing in this cycle spends, and this route offers no way to.
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from meshpipeline.api.security import org_dep
from meshpipeline.application import credit_service
from meshpipeline.persistence.session import get_db

router = APIRouter()


@router.get("")
async def read_balance(organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    # An organisation-less caller reads 0 rather than failing. That is a deployment between the
    # migration and the image that fills the column, or a lookup that could not run - neither is
    # a reason to answer a proven caller with an error.
    if not organization_id:
        return {"balance": 0, "unit": "credits"}
    async with get_db() as db:
        balance = await credit_service.balance(db, organization_id=organization_id)
    # The unit is named but deliberately undefined: what a credit BUYS is not decided, and a
    # client that reads this must not infer a currency from a bare number.
    return {"balance": balance, "unit": "credits"}
```

In `src/meshpipeline/api/v1/router.py`:

```python
from meshpipeline.api.v1 import chat, client_config, credits, simulation, upload, ws
...
router.include_router(credits.router,    prefix="/credits",    tags=["credits"])
```

In `src/meshpipeline/api/v1/client_config.py`, add to the returned dict:

```python
        # WHETHER THE CONSOLE SHOULD OFFER SIGN-UP. Advisory, exactly like `intake` above: the
        # real gate is on POST /auth/session, because anyone can create an Identity Platform
        # account against the project's public web API key without asking this endpoint first.
        "auth": {
            "signup_enabled": polcfg.CONSOLE_SIGNUP_ENABLED,
        },
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `python -m pytest tests/unit/api/test_credits_route.py tests/unit/api/test_client_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/api/v1/credits.py src/meshpipeline/api/v1/router.py src/meshpipeline/api/v1/client_config.py tests/unit/api/test_credits_route.py
git commit -m "feat(api): GET /api/v1/credits, and publish whether signup is open

Reading only. Nothing in this cycle spends and this route offers no way to.

The unit is named and deliberately undefined - what a credit buys is not
decided, and a client must not infer a currency from a bare number.

signup_enabled in client-config is advisory, like intake: the real gate is
on POST /auth/session, because anyone can create an Identity Platform
account against the project's public web API key without asking first."
```

---

### Task 11: The console signs in through Identity Platform

**Files:**
- Modify: `apps/console/package.json` (add `firebase`)
- Create: `apps/console/src/lib/firebase/client.ts`
- Create: `apps/console/src/lib/auth/firebase-session.ts`
- Create: `apps/console/src/lib/auth/firebase-session.test.ts`
- Modify: `apps/console/src/auth.ts`
- Modify: `apps/console/src/types/next-auth.d.ts`
- Modify: `apps/console/src/lib/auth/session.ts`

**Interfaces:**
- Consumes: `POST /auth/session` (Task 6).
- Produces:
  - `authorizeFirebaseSession(credentials, env?) -> Promise<ConsoleUser | null>` where `ConsoleUser = { id, email, name, organizationId, emailVerified }`
  - `firebaseAuth()` returning the initialised client-side `Auth` instance
  - session shape `{ user, subject, organizationId, emailVerified }`

- [ ] **Step 1: Add the dependency**

Run: `pnpm --filter @hexera/console add firebase`
Expected: `firebase` appears in `apps/console/package.json` dependencies and the lockfile updates.

- [ ] **Step 2: Write the failing test**

Create `apps/console/src/lib/auth/firebase-session.test.ts`, following the conventions in the existing `credentials.test.ts` (node:test, run by `node --import tsx --test`):

```ts
import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { authorizeFirebaseSession } from "./firebase-session.js";

const ENV = {
  HEXERA_API_BASE_URL: "http://api.test",
  MESH_API_KEY: "console-key",
};

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function stubFetch(status: number, body: unknown) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  globalThis.fetch = (async (url: string | URL, init: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return calls;
}

describe("authorizeFirebaseSession", () => {
  it("maps a verified token to the session the proxy already expects", async () => {
    stubFetch(200, {
      user_id: "user-1",
      owner_id: "person@example.com",
      organization_id: "org-1",
      email_verified: true,
      name: "A Person",
    });

    const user = await authorizeFirebaseSession({ idToken: "good" }, ENV);

    assert.deepEqual(user, {
      id: "person@example.com",
      email: "person@example.com",
      name: "A Person",
      organizationId: "org-1",
      emailVerified: true,
    });
  });

  it("identifies the console to the API with the mesh key", async () => {
    const calls = stubFetch(200, {
      user_id: "user-1",
      owner_id: "person@example.com",
      organization_id: "org-1",
      email_verified: true,
      name: "A Person",
    });

    await authorizeFirebaseSession({ idToken: "good" }, ENV);

    const headers = new Headers(calls[0].init.headers);
    assert.equal(headers.get("x-api-key"), "console-key");
    assert.equal(calls[0].url, "http://api.test/auth/session");
  });

  it("refuses a rejected token without throwing", async () => {
    stubFetch(401, { detail: "Invalid or expired sign-in token" });
    assert.equal(await authorizeFirebaseSession({ idToken: "forged" }, ENV), null);
  });

  it("refuses an absent token without calling the API", async () => {
    const calls = stubFetch(200, {});
    assert.equal(await authorizeFirebaseSession({}, ENV), null);
    assert.equal(calls.length, 0);
  });

  it("refuses rather than guessing when the API base URL is unset", async () => {
    const calls = stubFetch(200, {});
    assert.equal(
      await authorizeFirebaseSession({ idToken: "good" }, { MESH_API_KEY: "k" }),
      null,
    );
    assert.equal(calls.length, 0);
  });

  it("surfaces a closed signup distinctly so the page can explain it", async () => {
    stubFetch(403, { detail: "This deployment is not accepting new accounts" });
    await assert.rejects(
      () => authorizeFirebaseSession({ idToken: "good" }, ENV),
      /SignupDisabled/,
    );
  });
});
```

- [ ] **Step 3: Run it to make sure it fails**

Run: `pnpm --filter @hexera/console test`
Expected: FAIL — cannot resolve `./firebase-session.js`.

- [ ] **Step 4: Write the session bridge**

Create `apps/console/src/lib/auth/firebase-session.ts`:

```ts
type ConsoleAuthEnv = Record<string, string | undefined>;

export type ConsoleUser = {
  id: string;
  email: string;
  name: string;
  organizationId: string;
  emailVerified: boolean;
};

/** Thrown, not returned, because it is the one refusal the sign-up page must explain rather
 *  than reporting as bad credentials. Every other failure is an indistinguishable null. */
export class SignupDisabled extends Error {
  constructor() {
    super("SignupDisabled");
    this.name = "SignupDisabled";
  }
}

export async function authorizeFirebaseSession(
  credentials: Partial<Record<"idToken", unknown>>,
  env: ConsoleAuthEnv = process.env,
): Promise<ConsoleUser | null> {
  const idToken = credentials.idToken;
  if (typeof idToken !== "string" || !idToken) {
    return null;
  }

  const baseUrl = env.HEXERA_API_BASE_URL;
  if (!baseUrl) {
    // Refuse rather than guessing an origin. A console pointed at the wrong API would sign
    // people in against a database that is not this deployment's.
    return null;
  }

  const headers = new Headers({ "content-type": "application/json" });
  if (env.MESH_API_KEY) {
    headers.set("x-api-key", env.MESH_API_KEY);
  }

  const response = await fetch(new URL("/auth/session", normalizedBaseUrl(baseUrl)), {
    method: "POST",
    headers,
    body: JSON.stringify({ id_token: idToken }),
  });

  if (response.status === 403) {
    throw new SignupDisabled();
  }

  if (!response.ok) {
    // One null for every other cause, matching the API's own single refusal.
    return null;
  }

  const payload = (await response.json()) as Record<string, unknown>;
  const ownerId = typeof payload.owner_id === "string" ? payload.owner_id : "";
  if (!ownerId) {
    return null;
  }

  return {
    // `id` IS the owner_id the proxy signs into every API call. Keeping them the same value is
    // what lets ownerIdFromSession and proxy.ts stay exactly as they are.
    id: ownerId,
    email: ownerId,
    name: typeof payload.name === "string" && payload.name ? payload.name : ownerId,
    organizationId:
      typeof payload.organization_id === "string" ? payload.organization_id : "",
    emailVerified: payload.email_verified === true,
  };
}

function normalizedBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
}
```

- [ ] **Step 5: Write the Firebase client**

Create `apps/console/src/lib/firebase/client.ts`:

```ts
"use client";

import { getApp, getApps, initializeApp, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";

// PUBLIC BY DESIGN. The Firebase web API key identifies the project; it authorises nothing on
// its own, which is why it ships as plain environment and not as a Secret Manager reference.
// The gate that matters is CONSOLE_SIGNUP_ENABLED on the product API.
const config = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
};

function app(): FirebaseApp {
  // Next re-executes modules across client navigations; initializeApp twice throws.
  return getApps().length ? getApp() : initializeApp(config);
}

export function firebaseAuth(): Auth {
  return getAuth(app());
}

export function firebaseIsConfigured(): boolean {
  return Boolean(config.apiKey && config.authDomain && config.projectId);
}
```

- [ ] **Step 6: Rewire Auth.js**

Replace the provider block in `apps/console/src/auth.ts`:

```ts
import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { authorizeFirebaseSession } from "@/lib/auth/firebase-session";

export const { auth, handlers, signIn, signOut } = NextAuth({
  pages: {
    signIn: "/sign-in",
  },
  providers: [
    Credentials({
      // The console never sees a password. Identity Platform verified it and minted this token;
      // the product API verifies the token. This provider only carries it between the two.
      credentials: {
        idToken: { label: "ID token", type: "text" },
      },
      authorize: (credentials) => authorizeFirebaseSession(credentials),
    }),
  ],
  session: {
    strategy: "jwt",
  },
  trustHost: true,
  callbacks: {
    jwt({ token, user }) {
      if (user) {
        token.organizationId = (user as { organizationId?: string }).organizationId ?? "";
        token.emailVerified = (user as { emailVerified?: boolean }).emailVerified ?? false;
      }
      return token;
    },
    session({ session, token }) {
      return {
        ...session,
        subject: token.sub ?? null,
        organizationId: (token.organizationId as string | undefined) ?? "",
        emailVerified: token.emailVerified === true,
      };
    },
  },
});
```

Extend `apps/console/src/types/next-auth.d.ts` so `Session` carries `organizationId: string` and `emailVerified: boolean`, and `JWT` carries the same two. Read the existing file and follow its declaration style.

Leave `apps/console/src/lib/auth/session.ts` alone: `ownerIdFromSession` already returns the lowercased email, and `id`/`email` are now both the `owner_id`. Confirm this by re-running its test.

- [ ] **Step 7: Run the console tests**

Run: `pnpm --filter @hexera/console test && pnpm --filter @hexera/console typecheck && pnpm --filter @hexera/console lint`
Expected: PASS. `src/lib/auth/credentials.test.ts` will now fail — it tests the env-JSON user list that Task 13 deletes. Leave it failing only if Task 13 is the very next task; otherwise delete `credentials.ts`, `credentials.test.ts` and `hash-password.cli.ts` here and fold Task 13's console half into this commit.

- [ ] **Step 8: Commit**

```bash
git add apps/console/package.json apps/console/src/lib/firebase apps/console/src/lib/auth/firebase-session.ts apps/console/src/lib/auth/firebase-session.test.ts apps/console/src/auth.ts apps/console/src/types/next-auth.d.ts pnpm-lock.yaml
git commit -m "feat(console): sign in with an Identity Platform token

The Credentials provider stops checking passwords and starts carrying a
token between Identity Platform, which minted it, and the product API,
which verifies it. The console never sees a password.

session.id is the owner_id, so ownerIdFromSession and the whole proxy path
are unchanged.

The Firebase web API key is public by design - it identifies the project
and authorises nothing - so it ships as plain environment rather than a
Secret Manager reference.

A closed signup throws rather than returning null: it is the one refusal
the sign-up page must explain instead of reporting as bad credentials."
```

---

### Task 12: Sign-up, sign-in and password-reset pages

**Files:**
- Create: `apps/console/src/app/sign-up/page.tsx`
- Create: `apps/console/src/app/forgot-password/page.tsx`
- Create: `apps/console/src/app/_components/auth-forms.tsx`
- Modify: `apps/console/src/app/sign-in/page.tsx`
- Modify: `apps/console/src/app/_components/auth-buttons.tsx`
- Modify: `apps/console/src/app/(console)/page.tsx`

**Interfaces:**
- Consumes: `firebaseAuth`, `firebaseIsConfigured` (Task 11); `signIn` from `@/auth`; `GET /api/v1/client-config` for `auth.signup_enabled` (Task 10).
- Produces: routes `/sign-up` and `/forgot-password`; client components `SignUpForm`, `SignInForm`, `ForgotPasswordForm`, `VerifyEmailBanner`.

- [ ] **Step 1: Build the shared client form module**

Create `apps/console/src/app/_components/auth-forms.tsx` as a `"use client"` module. Each form calls Firebase, then hands the resulting ID token to `signIn("credentials", { idToken, redirectTo: "/" })`.

`SignUpForm`:

```tsx
"use client";

import { useState } from "react";
import { signIn } from "next-auth/react";
import {
  createUserWithEmailAndPassword,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
} from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";

export function SignUpForm() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setError(null);
    const email = String(formData.get("email") ?? "");
    const password = String(formData.get("password") ?? "");
    try {
      const credential = await createUserWithEmailAndPassword(firebaseAuth(), email, password);
      // Sent before the session exists, so a signup that fails at the API still leaves the
      // person with a verifiable address rather than an account they cannot prove is theirs.
      await sendEmailVerification(credential.user);
      const idToken = await credential.user.getIdToken();
      const result = await signIn("credentials", { idToken, redirect: false });
      if (result?.error) {
        setError("This deployment is not accepting new accounts.");
        return;
      }
      window.location.href = "/";
    } catch (cause) {
      // Firebase's own codes are the only place these distinctions are safe to make: it applies
      // its own enumeration protection, so echoing its message does not tell an attacker
      // anything it would not tell them directly.
      setError(messageFor(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} style={formStyle}>
      {/* email + password inputs, matching the existing EmailPasswordSignInForm markup */}
    </form>
  );
}

function messageFor(cause: unknown): string {
  const code = (cause as { code?: string })?.code ?? "";
  if (code === "auth/email-already-in-use") return "That email already has an account.";
  if (code === "auth/weak-password") return "Choose a longer password.";
  if (code === "auth/invalid-email") return "That does not look like an email address.";
  return "Could not create the account. Try again.";
}
```

Write `SignInForm` and `ForgotPasswordForm` in the same file and the same shape — `signInWithEmailAndPassword` and `sendPasswordResetEmail` respectively. `ForgotPasswordForm` must report the same confirmation whether or not the address exists ("If that address has an account, a reset link is on its way"), because Firebase's `sendPasswordResetEmail` does not distinguish and the page must not either.

Reuse the exact `signInFormStyle`, `labelStyle`, `inputStyle` and `errorStyle` constants from `auth-buttons.tsx` — export them from there and import them here rather than copying, so the four forms cannot drift apart.

- [ ] **Step 2: Build the two pages**

`apps/console/src/app/sign-up/page.tsx` mirrors the existing `sign-in/page.tsx` exactly: same `LegacyStyles`, same header, same `#app` / `#stage` / `chat-col` structure, with the chip reading `create an account` and the body rendering `<SignUpForm />` plus a link to `/sign-in`.

It must additionally redirect to `/sign-in` when signup is closed. Fetch `GET /api/v1/client-config` server-side through `getHexeraApiClient()` and read `auth.signup_enabled`; if it is false, `redirect("/sign-in?signup=closed")`.

`apps/console/src/app/forgot-password/page.tsx` follows the same shell, chip reading `reset your password`, rendering `<ForgotPasswordForm />` and a link back to `/sign-in`.

- [ ] **Step 3: Update the sign-in page**

In `apps/console/src/app/sign-in/page.tsx`:

- swap `EmailPasswordSignInForm` for the new `SignInForm`
- add links to `/sign-up` and `/forgot-password` beneath it
- when `searchParams.signup === "closed"`, render "This deployment is not accepting new accounts."
- when `firebaseIsConfigured()` is false, render "Sign-in is not configured for this deployment." instead of the form — a console whose Identity Platform project was never set must say so rather than presenting a form that fails on submit

- [ ] **Step 4: Add the verification banner**

Add `VerifyEmailBanner` to `auth-forms.tsx` and render it in `apps/console/src/app/(console)/page.tsx` when `session.emailVerified` is false. It shows one line and a resend control calling `sendEmailVerification(firebaseAuth().currentUser)`. It does not block anything — an unverified user signs in and works, per the design's §4.

- [ ] **Step 5: Run the console checks**

Run: `pnpm --filter @hexera/console typecheck && pnpm --filter @hexera/console lint && pnpm --filter @hexera/console build`
Expected: PASS. The build catches a client component imported into a server one without `"use client"`, which is the most likely mistake here.

- [ ] **Step 6: Drive it by hand**

Run: `pnpm --filter @hexera/console dev` with `NEXT_PUBLIC_FIREBASE_*` pointing at a real Identity Platform project and the API running locally.
Check: `/sign-up` creates an account and lands on `/`; the verification email arrives; `/sign-in` works with those credentials; `/forgot-password` sends a reset; an unverified account sees the banner and is not blocked.

- [ ] **Step 7: Commit**

```bash
git add apps/console/src/app
git commit -m "feat(console): sign-up, sign-in and password-reset pages

Password reset reports the same confirmation whether or not the address
exists. Firebase does not distinguish, and the page must not either.

Error messages come from Firebase's own codes. It applies its own
enumeration protection, so echoing them tells an attacker nothing it would
not tell them directly.

An unconfigured Identity Platform project renders an explanation instead of
a form that fails on submit.

The verification banner blocks nothing. Nothing costly is gated on a
verified address yet, so blocking would be support burden with no security
benefit; email_verified_at is recorded so that choice stays available."
```

---

### Task 13: Delete `CONSOLE_AUTH_USERS`

**Files:**
- Delete: `apps/console/src/lib/auth/credentials.ts`, `apps/console/src/lib/auth/credentials.test.ts`, `apps/console/src/lib/auth/hash-password.cli.ts`
- Modify: `apps/console/package.json` (drop the `auth:hash` script)
- Modify: `apps/console/.env.example`
- Modify: `deploy/gcp/scripts/create-secrets.sh`
- Modify: `deploy/gcp/scripts/create-console-service.sh`

**Interfaces:**
- Consumes: Task 11's replacement, which must be merged first.
- Produces: no `CONSOLE_AUTH_USERS` reference anywhere in the repository.

- [ ] **Step 1: Confirm nothing still reads it**

Run: `grep -rn "CONSOLE_AUTH_USERS\|verifyConsoleCredentials\|hashConsolePassword" --exclude-dir=node_modules --exclude-dir=.next .`
Expected: hits only in the files this task deletes or edits, plus documentation. If `src/auth.ts` still imports `verifyConsoleCredentials`, Task 11 is not merged — stop and merge it first.

- [ ] **Step 2: Delete the console-side files**

```bash
git rm apps/console/src/lib/auth/credentials.ts apps/console/src/lib/auth/credentials.test.ts apps/console/src/lib/auth/hash-password.cli.ts
```

Remove the `"auth:hash"` line from `apps/console/package.json` scripts.

- [ ] **Step 3: Update the console env template**

In `apps/console/.env.example`, delete the `CONSOLE_AUTH_USERS` block entirely and add:

```
# The Identity Platform project the console signs in against. All three are PUBLIC by design:
# the web API key identifies the project and authorises nothing on its own. The gate that matters
# is CONSOLE_SIGNUP_ENABLED on the product API.
NEXT_PUBLIC_FIREBASE_API_KEY=
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=
NEXT_PUBLIC_FIREBASE_PROJECT_ID=
```

- [ ] **Step 4: Remove the secret container**

In `deploy/gcp/scripts/create-secrets.sh`: delete the `CONSOLE_AUTH_USERS_NAME` assignment (line ~50) and its entry in the `SECRETS` array.

In `deploy/gcp/scripts/create-console-service.sh`: delete `"CONSOLE_AUTH_USERS:CONSOLE_AUTH_USERS_SECRET"` from the secret pair loop (line ~77), and add the three `NEXT_PUBLIC_FIREBASE_*` values to `CONSOLE_ENV_PAIRS` (line ~167) as plain environment, following exactly how `NEXT_PUBLIC_HEXERA_API_BASE_URL` is already passed there.

Note: this leaves the `console-auth-users` secret container itself in the live projects. Deleting it is an operator action, not a script one — record it in Task 14's runbook rather than adding a destructive step here.

- [ ] **Step 5: Verify**

Run: `grep -rn "CONSOLE_AUTH_USERS" --exclude-dir=node_modules --exclude-dir=.next .`
Expected: no hits outside `docs/` (design documents that describe the history are correct to keep it).

Run: `pnpm --filter @hexera/console test && pnpm --filter @hexera/console build && bash -n deploy/gcp/scripts/create-secrets.sh && bash -n deploy/gcp/scripts/create-console-service.sh`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A apps/console deploy/gcp/scripts
git commit -m "chore(console): delete CONSOLE_AUTH_USERS and the scrypt path

Identity Platform holds the credential now. A JSON array of password
hashes in an environment variable was never going to be how strangers get
accounts.

The secret CONTAINER is left in the live projects deliberately. Deleting it
is an operator action with a rollback consequence, not something a
provisioning script should do on the next run; it is in the runbook."
```

---

### Task 14: Deploy wiring and documentation

**Files:**
- Modify: `deploy/gcp/scripts/enable-apis.sh`
- Modify: `deploy/gcp/scripts/deploy-preflight.sh`
- Modify: `deploy/gcp/scripts/create-api-service.sh`
- Create: `docs/deployment/identity-platform.md`
- Modify: `docs/reference/configuration.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: every earlier task.
- Produces: `identitytoolkit.googleapis.com` enabled; a preflight check reporting an uninitialised Identity Platform; `FIREBASE_PROJECT_ID`, `CONSOLE_SIGNUP_ENABLED` and `SIGNUP_GRANT_CREDITS` reaching the API service.

- [ ] **Step 1: Enable the API**

In `deploy/gcp/scripts/enable-apis.sh`, add to the list (around line 33):

```
  identitytoolkit.googleapis.com         # Identity Platform, the console's sign-in provider
```

- [ ] **Step 2: Add the preflight check**

In `deploy/gcp/scripts/deploy-preflight.sh`, add a read-only check following the file's existing check style. Identity Platform initialisation is not something `gcloud` creates, so the check reports rather than fixes:

```bash
# IDENTITY PLATFORM, which the console signs in through. Enabling the API is not the same as
# INITIALISING the product - that is a one-time console action - and the difference is invisible
# until somebody tries to sign up and the token verification finds no project. Read-only: this
# reports, it does not provision.
if gc services list --enabled --filter identitytoolkit.googleapis.com --format 'value(NAME)' \
     | grep -q identitytoolkit; then
  if gc identity-platform config describe >/dev/null 2>&1; then
    log "Identity Platform initialised in ${GCP_PROJECT_ID}"
  else
    warn "identitytoolkit.googleapis.com is enabled but Identity Platform is NOT initialised in
         ${GCP_PROJECT_ID}. The console will render sign-up and fail on submit. Initialise it once at
         https://console.cloud.google.com/customer-identity/providers?project=${GCP_PROJECT_ID}
         and enable the Email/Password provider."
  fi
else
  warn "identitytoolkit.googleapis.com is not enabled - console sign-in will not work"
fi
```

Verify `gcloud identity-platform config describe` exists in the installed CLI before relying on it: `gcloud identity-platform config describe --help`. If the command is absent in this gcloud version, substitute a `gcloud services list` check alone and say so in the warning text.

- [ ] **Step 3: Pass the settings to the API service**

In `deploy/gcp/scripts/create-api-service.sh`, add `FIREBASE_PROJECT_ID`, `CONSOLE_SIGNUP_ENABLED` and `SIGNUP_GRANT_CREDITS` to whatever array that script uses to build the API's environment. Read the file first and follow its existing pattern exactly. `FIREBASE_PROJECT_ID` defaults to `${GCP_PROJECT_ID}` — Identity Platform lives in the same project.

- [ ] **Step 4: Write the operator document**

Create `docs/deployment/identity-platform.md` covering, in this order:

1. **What it is and why**: Identity Platform holds the credential and sends the verification and reset emails; the product API holds the account. Link the design doc.
2. **One-time initialisation per project**, with the exact console URL and the Email/Password provider toggle. State plainly that `enable-apis.sh` enabling the API is not the same thing.
3. **Where the web config comes from** and that all three `NEXT_PUBLIC_FIREBASE_*` values are public by design.
4. **The sender domain**: emails come from `<project>.firebaseapp.com` until a custom domain is configured; configuring one is out of scope for this cycle.
5. **Opening and closing signup** with `CONSOLE_SIGNUP_ENABLED`, and why it is an API setting rather than a console one.
6. **`SIGNUP_GRANT_CREDITS`**, that credits cannot currently be spent, and that `0` disables the grant.
7. **The backfill runbook**: rehearse `0004` against a restored copy of the dev database, confirm the counts, then run it against dev, then prod. It is idempotent.
8. **Retiring the old secret**: `gcloud secrets delete console-auth-users --project <id>` once the new console revision is serving, and not before.

- [ ] **Step 5: Update the configuration reference**

Add `FIREBASE_PROJECT_ID`, `CONSOLE_SIGNUP_ENABLED` and `SIGNUP_GRANT_CREDITS` to `docs/reference/configuration.md`, in whichever section that file uses for auth and quota settings. Follow its existing table shape. Add `docs/deployment/identity-platform.md` to the index table in `docs/README.md`.

- [ ] **Step 6: Verify**

Run: `bash -n deploy/gcp/scripts/enable-apis.sh && bash -n deploy/gcp/scripts/deploy-preflight.sh && bash -n deploy/gcp/scripts/create-api-service.sh`
Expected: PASS.

Run: `make check`
Expected: PASS — the full blocking gate, including the docs and configuration certification lanes.

- [ ] **Step 7: Commit**

```bash
git add deploy/gcp/scripts docs
git commit -m "feat(deploy): enable Identity Platform, and document initialising it

Enabling identitytoolkit.googleapis.com is not the same as initialising
Identity Platform - that is a one-time console action, and the difference
is invisible until somebody tries to sign up and token verification finds
no project. The preflight reports it rather than provisioning it, because
gcloud cannot.

The runbook keeps deleting console-auth-users as an operator step after
the new revision is serving. A provisioning script that deletes a secret on
its next run removes the rollback."
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §4 what Identity Platform holds | 4 (verification), 12 (the pages that use it) |
| §4 console pages and the ID token | 11, 12 |
| §4 `POST /auth/session`, its four steps and idempotency | 5, 6 |
| §4 `CONSOLE_SIGNUP_ENABLED` as an API setting, published through client-config | 3, 6, 10 |
| §5 `0003` four tables | 1 |
| §5 `0004` tenant columns, `api_keys` FK, backfill, email-linking | 7 (schema + backfill), 5 (linking) |
| §6 `Principal.organization_id`, `org_dep`, the 50 + 13 sites, the fallback rule | 8, 9 |
| §7 `credit_service`, `SIGNUP_GRANT_CREDITS`, `GET /api/v1/credits` | 3, 10 |
| §8 `identitytoolkit`, initialisation, env, `CONSOLE_AUTH_USERS` removal, sender domain | 13, 14 |
| §9 testing table | 1, 2, 3, 4, 5, 6, 7, 9, 11 |
| §10 risks: backfill rehearsal, manual initialisation | 7 (idempotency tests), 14 (runbook, preflight) |

Gaps found and closed while reviewing: the console header credit chip named in §7 is **not** implemented by any task — `GET /api/v1/credits` exists (Task 10) but nothing renders it. That is a one-component addition to `apps/console/src/app/(console)/page.tsx`; fold it into Task 12 Step 4 alongside the verification banner, reading the balance server-side through the existing proxy.

**Placeholder scan:** no "TBD", no "handle edge cases", no "similar to Task N". Three places deliberately instruct the implementer to read an existing file and follow its pattern rather than showing code — Task 7's integration fixtures, Task 9's 50-site sweep, and Task 14's `create-api-service.sh` env array. Each names the exact file to read and why inventing a second mechanism would be wrong; these are not placeholders but they are the least prescriptive steps in the plan, and a reviewer should check them hardest.

**Type consistency:** `Account.owner_id`, `ConsoleUser.id`, `Principal.owner_id` and the `owner_id` column are all the lowercased email. `organization_id` is `uuid.UUID` in the persistence layer, `str` on `Principal`, `Account` and every API boundary, converted only in `tenant_scope.scope`/`stamp` and in `account_service`. `credit_service.grant_signup_credits` returns the granted amount in both its definition (Task 3) and its fake (Task 5).
