# Responsibility: Verify the geometry_surveys migration builds exactly the table the ORM declares, keyed by the upload and the file's sha256.
# Boundaries: it reads the revision file and the ORM; building a real database is the integration tier's parity gate.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.models import GeometrySurvey

REPO = Path(__file__).parents[3]
REVISION = REPO / "alembic" / "versions" / "0004_geometry_surveys.py"
#: Every later revision that adds a column to this table. The ORM's columns are what the create built
#: plus what these added, so a column declared on the model and migrated by nobody is still caught.
LATER = (REPO / "alembic" / "versions" / "0005_geometry_step.py",
         REPO / "alembic" / "versions" / "0006_geometry_survey_asking.py")
TABLE = "geometry_surveys"


def _calls(func_name: str, attr: str, revision: Path = REVISION) -> list[ast.Call]:
    out = []
    for node in ast.walk(ast.parse(revision.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        out += [inner for inner in ast.walk(node) if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute) and inner.func.attr == attr]
    return out


def _created_columns() -> set[str]:
    for call in _calls("upgrade", "create_table"):
        if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == TABLE:
            return {a.args[0].value for a in call.args[1:]
                    if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and a.func.attr == "Column" and a.args and isinstance(a.args[0], ast.Constant)}
    raise AssertionError(f"the revision does not create {TABLE}")


def _added_columns(*only: Path) -> set[str]:
    """Columns a later revision adds to this table, and only to this table. All of them, or the ones named."""
    out: set[str] = set()
    for revision in (only or LATER):
        for call in _calls("upgrade", "add_column", revision):
            if not (call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == TABLE):
                continue
            column = call.args[1]
            if isinstance(column, ast.Call) and column.args and isinstance(column.args[0], ast.Constant):
                out.add(column.args[0].value)
    return out


def test_the_revisions_create_every_column_the_orm_declares():
    """A column on the model that no revision builds is a column that is not there on a real database,
    and the code that writes it fails only in production."""
    assert _created_columns() | _added_columns() == {c.name for c in GeometrySurvey.__table__.columns}


def test_the_geometry_agents_step_is_two_nullable_columns_added_by_a_later_revision():
    """Added, not baked into 0004: a deployment already carrying survey rows upgrades in place, and both
    columns are null on every row it has, which is what the step being off means."""
    assert _added_columns(LATER[0]) == {"geometry_step", "late"}
    columns = {c.name: c for c in GeometrySurvey.__table__.columns}
    assert columns["geometry_step"].nullable is True and columns["geometry_step"].default is None
    assert columns["late"].nullable is True and columns["late"].default is None


def test_the_three_keys_the_row_had_no_place_for_are_added_with_a_default_each():
    """0006. `asking` carries the question finder's decisions, `look_queued` the look queue's answer and
    `state_schema` the state's own name; all three were written into the state and dropped by `record`.

    EVERY ONE HAS A SERVER DEFAULT, because a deployment already holding survey rows upgrades in place and
    a NOT NULL column with no default cannot be added to a table with rows in it. The defaults are also the
    readings the application already has for "nothing here": `asking_of` reads `{}` as a row composed before
    the finder was wired, and `look_state` reads an empty queue answer as a look nobody queued.
    """
    assert _added_columns(LATER[1]) == {"asking", "look_queued", "state_schema"}
    columns = {c.name: c for c in GeometrySurvey.__table__.columns}
    for name in ("asking", "look_queued", "state_schema"):
        assert columns[name].nullable is False, name
        assert columns[name].server_default is not None, f"{name} cannot be added to a table with rows"


def test_every_later_revision_drops_exactly_the_columns_it_added():
    for revision in LATER:
        dropped = {c.args[1].value for c in _calls("downgrade", "drop_column", revision)
                   if len(c.args) > 1 and isinstance(c.args[1], ast.Constant)}
        assert dropped == _added_columns(revision), revision.name


def test_one_survey_per_upload_carrying_the_digest_an_answer_is_bound_to():
    columns = {c.name: c for c in GeometrySurvey.__table__.columns}
    assert columns["geometry_source_id"].nullable is False
    assert columns["sha256"].nullable is False
    uniques = {tuple(sorted(c.name for c in con.columns)) for con in GeometrySurvey.__table__.constraints
               if con.__class__.__name__ == "UniqueConstraint"}
    assert ("geometry_source_id",) in uniques
    indexes = {tuple(c.name for c in ix.columns) for ix in GeometrySurvey.__table__.indexes}
    assert ("owner_id", "sha256") in indexes


def test_deleting_a_source_cannot_quietly_destroy_its_survey():
    fk = next(iter(GeometrySurvey.__table__.columns["geometry_source_id"].foreign_keys))
    assert fk.ondelete == "RESTRICT" and fk.column.table.name == "geometry_sources"


def test_the_downgrade_drops_exactly_what_the_upgrade_created():
    dropped = {c.args[0].value for c in _calls("downgrade", "drop_table")
               if c.args and isinstance(c.args[0], ast.Constant)}
    assert dropped == {TABLE}


def test_the_revision_follows_the_measurement_it_sits_beside():
    text = REVISION.read_text(encoding="utf-8")
    assert "revision = '0004_geometry_surveys'" in text
    assert "down_revision = '0003_geometry_measurements'" in text
