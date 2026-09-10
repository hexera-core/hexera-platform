# Responsibility: Verify 0004 stamps every existing row with an organisation, exactly once, whatever it is re-run against.
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

# No `pytestmark = pytest.mark.asyncio`: every test here is synchronous (it drives alembic and a
# sync SQLAlchemy connection directly, like test_migration_wrapper_postgres.py does). The tier's
# asyncio_mode is "auto", which already handles the `async def` tests elsewhere in this suite;
# marking a sync function asyncio produces a PytestWarning and runs it as an ordinary sync test
# either way.

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

import meshpipeline.settings.providers as provcfg  # noqa: E402
from meshpipeline.persistence.migration_url import sync_migration_url  # noqa: E402
from meshpipeline.runtime import migrate  # noqa: E402

_PRE_TENANT_REVISION = "0003_identity_and_credits"


def _sync_url() -> str:
    # Same normalisation test_migration_wrapper_postgres.py uses: the local throwaway Postgres
    # has no TLS, so the test's own connections force ssl_required=False.
    return sync_migration_url(provcfg.POSTGRES_DSN, ssl_required=False)


def _connect_autocommit():
    eng = create_engine(_sync_url(), pool_pre_ping=True)
    return eng, eng.connect().execution_options(isolation_level="AUTOCOMMIT")


def _wipe_and_upgrade_to(rev: str) -> None:
    try:
        eng, conn = _connect_autocommit()
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL is not reachable for the tenant-backfill integration test - it "
                    f"must be PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    try:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    finally:
        conn.close()
        eng.dispose()

    from alembic import command
    command.upgrade(migrate._alembic_config(), rev)


@pytest.fixture()
def migrated_to_0003():
    # Wipe to bare metal, then drive alembic to 0003 directly - mirroring
    # test_migration_wrapper_postgres.py's `empty_db`, not a second mechanism.
    _wipe_and_upgrade_to(_PRE_TENANT_REVISION)

    # An AUTOCOMMIT connection: the test seeds rows and then hands control to alembic's own,
    # separate connection to run 0004. A transactional connection would hold the seed uncommitted
    # and invisible to that second connection.
    eng2 = create_engine(_sync_url(), isolation_level="AUTOCOMMIT")
    conn2 = eng2.connect()
    try:
        yield conn2
    finally:
        conn2.close()
        eng2.dispose()

        # This database is SHARED with every other file in the integration session (the tier's
        # autouse conftest fixtures bring it to head before each OTHER test, but never touch its
        # ROWS). A test above seeds real geometry_sources rows that end up referencing real
        # organizations - exactly the FK this migration adds - and those rows survive past this
        # test unless something removes them. Left behind, they make an unrelated suite's
        # child-first DELETE (test_identity_repositories_postgres.py's _clean_identity_tables)
        # fail on the very foreign key this migration introduces. Wiping back to a clean, empty
        # head - not a truncate, not a downgrade - is what returns the database to the state
        # every other suite in the session assumes it starts from.
        _wipe_and_upgrade_to("head")


@pytest.fixture()
def run_migration():
    from alembic import command

    def _run(rev: str, *, downgrade: bool = False, rerun: bool = False):
        cfg = migrate._alembic_config()
        if downgrade:
            command.downgrade(cfg, rev)
            return
        if rerun:
            # `alembic upgrade <rev>` when already AT <rev> is a no-op - it never calls
            # upgrade() again. Forcing a real second run of the backfill (the thing this test
            # exists to prove is idempotent) means genuinely leaving the revision and coming
            # back, not re-asking for a revision already reached.
            command.downgrade(cfg, _PRE_TENANT_REVISION)
        command.upgrade(cfg, rev)

    return _run


def _seed(conn):
    """Two owners with rows across three tenant tables, as they existed before 0004."""
    for owner in ("alpha@example.com", "bravo@example.com"):
        conn.execute(text("""
            INSERT INTO geometry_sources
                (id, owner_id, original_filename, object_key, sha256, size_bytes)
            VALUES (:id, :owner, 'part.step', :key, :sha, 10)
        """), {"id": uuid.uuid4(), "owner": owner, "key": f"k/{owner}/{uuid.uuid4()}",
               "sha": "0" * 64})


def _seed_api_key_only_owner(conn, owner: str):
    """An owner known ONLY through an api_keys row - no geometry_sources/simulation_jobs/etc.

    This is the case Finding 1 of the code review caught: the original _backfill() built its
    owners_union from _TENANT_TABLES alone, so an owner who never created anything but held an
    API key was invisible to the organisation/membership inserts. Their api_keys.organization_id
    then had no slug to match and stayed NULL forever - not just on the first run, but on every
    re-run, since nothing in the union would ever produce that owner's organisation.
    """
    conn.execute(text("""
        INSERT INTO api_keys (id, owner_id, name, key_prefix, key_hash)
        VALUES (:id, :owner, '', :prefix, :hash)
    """), {"id": uuid.uuid4(), "owner": owner, "prefix": f"hx_live_{uuid.uuid4().hex[:12]}",
           "hash": "0" * 64})


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


def test_an_owner_known_only_through_an_api_key_still_gets_an_organisation(migrated_to_0003,
                                                                           run_migration):
    # Finding 1: the owners_union that mints organisations/users/memberships originally came from
    # _TENANT_TABLES alone, so an owner with an api_keys row but no geometry source, job, chat,
    # interpretation, capture operation, reconciliation or cleanup was never a candidate for one.
    _seed(migrated_to_0003)
    _seed_api_key_only_owner(migrated_to_0003, "keyholder@example.com")
    run_migration("0004_tenant_columns")

    org_id = migrated_to_0003.execute(text("""
        SELECT organization_id FROM api_keys WHERE owner_id = 'keyholder@example.com'
    """)).scalar()
    assert org_id is not None, "an API-key-only owner was left with no organisation"

    # Three owners now: two from _seed (geometry_sources) and this one (api_keys only).
    count = migrated_to_0003.execute(text("SELECT count(*) FROM organizations")).scalar()
    assert count == 3

    # And it is genuinely their OWN organisation, not one of the other two owners' by accident.
    other_orgs = migrated_to_0003.execute(text("""
        SELECT organization_id FROM geometry_sources
    """)).scalars().all()
    assert org_id not in other_orgs
