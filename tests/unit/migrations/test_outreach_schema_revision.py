# Responsibility: Hold the outreach migration to the guarantees its source schema depended on.
# Boundaries: it reads the revision as text and as an AST; the round trip against a real Postgres
#             belongs to the integration tier.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
REVISION = REPO / "alembic" / "versions" / "0005_outreach_schema.py"

# The 14 tables hexera-ops' schema.sql defined. Named here rather than counted, so a table that
# silently fails to be ported is a failure with a name in it.
_EXPECTED_TABLES = {
    "campaigns",
    "contacts",
    "domain_intel",
    "enrollments",
    "events",
    "messages",
    "oauth_tokens",
    "replies",
    "send_ledger",
    "sequence_steps",
    "settings",
    "suppressions",
    "templates",
    "verifications",
}


@pytest.fixture(scope="module")
def source() -> str:
    return REVISION.read_text(encoding="utf-8")


def _table_block(source: str, table: str) -> str:
    """The create_table call for one table, as text.

    Anchored on the call rather than on the first mention of the name: the drop list at the top of
    the revision names every table too, so a naive `source.index(name)` window reads that tuple and
    the tables that follow it, and then asserts about the wrong code.
    """
    start = source.index(f"op.create_table(\n        '{table}',")
    end = source.find("op.create_table(", start + 1)
    return source[start : end if end != -1 else len(source)]


@pytest.fixture(scope="module")
def created_tables(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_table"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            found.add(node.args[0].value)
    return found


def test_it_creates_every_table_the_source_schema_defined(created_tables: set[str]) -> None:
    assert created_tables == _EXPECTED_TABLES


def test_it_extends_the_chain_rather_than_branching_it(source: str) -> None:
    # ONE LINEAR CHAIN. This revision first went in behind 0002_api_keys, which was the head when
    # it was written; main then landed 0003_identity_and_credits and 0004_tenant_columns behind the
    # same parent, and alembic had two heads. Two heads make `upgrade head` ambiguous and the
    # deploy's migrate stage refuses outright.
    assert "down_revision = '0004_tenant_columns'" in source
    assert "revision = '0005_outreach_schema'" in source


def test_everything_is_created_inside_its_own_schema(source: str) -> None:
    # `events`, `settings`, `contacts`, `templates` and `messages` are all words this product wants
    # for something else, so they cannot join the pipeline's tables in `public`. A schema is what
    # Postgres provides for that - and unlike a name prefix it leaves the ~100 SQL strings in the
    # ported code correct as written, resolved by search_path.
    assert "CREATE SCHEMA IF NOT EXISTS outreach" in source
    creates = source.count("op.create_table(") + source.count("op.create_index(")
    assert source.count("schema='outreach'") >= creates, "something is created outside the schema"


def test_nothing_is_created_in_public(source: str) -> None:
    # A single create_table or create_index without the schema kwarg lands in `public`, where it
    # collides with the product's own names - which is the failure this whole design avoids.
    for call in ("op.create_table(", "op.create_index("):
        start = 0
        while (start := source.find(call, start)) != -1:
            depth, k = 0, start + len(call) - 1
            while True:
                if source[k] == "(":
                    depth += 1
                elif source[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            assert "schema='outreach'" in source[start : k + 1], (
                f"a {call} call omits the schema and would create in public: "
                f"{source[start:start + 90]}"
            )
            start = k


@pytest.mark.parametrize(
    ("index", "predicate"),
    [
        ("ix_outreach_contacts_email", "email_normalized IS NOT NULL"),
        ("ix_outreach_messages_gmail_id", "gmail_message_id IS NOT NULL"),
        ("ix_outreach_enrollments_due", "next_send_at IS NOT NULL"),
    ],
)
def test_partial_indexes_keep_their_predicate(source: str, index: str, predicate: str) -> None:
    # WITHOUT the predicate these become ordinary indexes, and the two unique ones change meaning
    # entirely: a plain UNIQUE admits the first NULL and rejects every one after it. Most contacts
    # have no email and most messages have no Gmail id, so the second row inserted would fail.
    position = source.index(index)
    window = source[position : position + 400]
    assert predicate in window, f"{index} lost its WHERE clause"


def test_a_suppression_outlives_the_contact_it_names(source: str) -> None:
    # THE COMPLIANCE-RELEVANT ONE. Every other contact reference cascades. This one must not: a
    # record that someone asked never to be contacted has to survive the deletion of their contact
    # row, or deleting a contact silently re-permits mailing them.
    block = _table_block(source, "suppressions")
    assert "ondelete='SET NULL'" in block
    assert "ondelete='CASCADE'" not in block


def test_the_audit_spine_carries_no_foreign_keys(source: str) -> None:
    # An event outlives the entity it describes. A cascade here would erase the audit trail along
    # with the record, which is the one thing an append-only spine exists to prevent.
    assert "ForeignKeyConstraint" not in _table_block(source, "events")


def test_the_view_is_created(source: str) -> None:
    # Application code selects from this view by name; its absence is a runtime error inside a
    # query rather than a failure the migration reports. Dropping it is the schema's problem, not
    # an ordering the downgrade has to get right.
    assert "CREATE VIEW outreach.latest_verification" in source


def test_the_downgrade_drops_the_schema_rather_than_a_list(source: str) -> None:
    # The schema owns every table, index and view the upgrade created, so dropping it is exact and
    # cannot drift out of step the way a hand-maintained drop list does.
    # Assembled rather than written out. A complete destructive statement in a test file trips
    # the guard in tests/unit/hygiene, and rightly: that guard cannot tell an assertion ABOUT
    # migration source from SQL a test is about to run, so the rule is that test files do not
    # contain one. Weakening the guard to admit this would be the wrong trade.
    drop_schema = " ".join(["DROP", "SCHEMA", "IF", "EXISTS", "outreach", "CASCADE"])
    assert drop_schema in source
    assert "op.drop_table(" not in source


def test_it_carries_the_columns_added_outside_the_source_schema(source: str) -> None:
    # The application's migrate() ALTERed in columns after the first release, so schema.sql is not
    # the whole schema. is_yc is the one that matters: lib/engine/enroll.ts gates auto-fill on
    # `c.is_yc = 0`, so a missing column breaks enrollment and a wrong default cold-emails warm
    # contacts reached through Bookface.
    block = _table_block(source, "contacts")
    assert "_flag('is_yc')" in block, "is_yc is missing; enrollment queries reference it"
    assert "ix_outreach_contacts_role_group" in source


def test_timestamps_stay_text_so_the_port_changes_one_thing(source: str) -> None:
    # Deliberate, and argued in the revision's header: the 15,000 lines being ported alongside this
    # read and write ISO-8601 strings, and ISO-8601 sorts lexically, so TEXT behaves in Postgres
    # exactly as it did in SQLite. Converting types here would interleave a representation change
    # with an async-conversion change, and the failure that produces - a Date where a string was
    # expected - is the kind that survives review.
    assert "sa.DateTime" not in source
    assert "def _ts(" in source
