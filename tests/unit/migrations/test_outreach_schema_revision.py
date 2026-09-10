# Responsibility: Hold the outreach migration to the guarantees its source schema depended on.
# Boundaries: it reads the revision as text and as an AST; the round trip against a real Postgres
#             belongs to the integration tier.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
REVISION = REPO / "alembic" / "versions" / "0003_outreach_schema.py"

# The 14 tables hexera-ops' schema.sql defined. Named here rather than counted, so a table that
# silently fails to be ported is a failure with a name in it.
_EXPECTED_TABLES = {
    "outreach_campaigns",
    "outreach_contacts",
    "outreach_domain_intel",
    "outreach_enrollments",
    "outreach_events",
    "outreach_messages",
    "outreach_oauth_tokens",
    "outreach_replies",
    "outreach_send_ledger",
    "outreach_sequence_steps",
    "outreach_settings",
    "outreach_suppressions",
    "outreach_templates",
    "outreach_verifications",
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
    start = source.index(f"op.create_table(\n        '{table}'")
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


def test_it_follows_the_api_keys_revision(source: str) -> None:
    # One linear chain. A branch here would make `upgrade head` ambiguous.
    assert "down_revision = '0002_api_keys'" in source
    assert "revision = '0003_outreach_schema'" in source


def test_every_table_is_prefixed(created_tables: set[str]) -> None:
    # `events`, `settings`, `contacts`, `templates` and `messages` are all words this product wants
    # for something else. Unprefixed, the first of them becomes a migration conflict.
    assert all(name.startswith("outreach_") for name in created_tables)


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
    block = _table_block(source, "outreach_suppressions")
    assert "ondelete='SET NULL'" in block
    assert "ondelete='CASCADE'" not in block


def test_the_audit_spine_carries_no_foreign_keys(source: str) -> None:
    # An event outlives the entity it describes. A cascade here would erase the audit trail along
    # with the record, which is the one thing an append-only spine exists to prevent.
    assert "ForeignKeyConstraint" not in _table_block(source, "outreach_events")


def test_the_view_is_created_and_dropped_before_its_table(source: str) -> None:
    # Application code selects from this view by name; its absence is a runtime error inside a
    # query rather than a failure the migration reports. On the way down it must go first, because
    # it depends on a table the downgrade is about to drop.
    assert "CREATE VIEW outreach_latest_verification" in source
    drop_view = source.index("DROP VIEW IF EXISTS outreach_latest_verification")
    drop_tables = source.index("op.drop_table(table)")
    assert drop_view < drop_tables


def test_the_downgrade_drops_everything_it_created(source: str, created_tables: set[str]) -> None:
    tree = ast.parse(source)
    listed = {
        element.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", "") == "_TABLES" for target in node.targets)
        for element in getattr(node.value, "elts", [])
        if isinstance(element, ast.Constant)
    }
    assert listed == created_tables, "the drop list and the create calls disagree"


def test_timestamps_stay_text_so_the_port_changes_one_thing(source: str) -> None:
    # Deliberate, and argued in the revision's header: the 15,000 lines being ported alongside this
    # read and write ISO-8601 strings, and ISO-8601 sorts lexically, so TEXT behaves in Postgres
    # exactly as it did in SQLite. Converting types here would interleave a representation change
    # with an async-conversion change, and the failure that produces - a Date where a string was
    # expected - is the kind that survives review.
    assert "sa.DateTime" not in source
    assert "def _ts(" in source
