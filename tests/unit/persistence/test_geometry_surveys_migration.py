# Responsibility: Verify the geometry_surveys migration builds exactly the table the ORM declares, keyed by the upload and the file's sha256.
# Boundaries: it reads the revision file and the ORM; building a real database is the integration tier's parity gate.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.models import GeometrySurvey

REPO = Path(__file__).parents[3]
REVISION = REPO / "alembic" / "versions" / "0004_geometry_surveys.py"
TABLE = "geometry_surveys"


def _calls(func_name: str, attr: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(ast.parse(REVISION.read_text(encoding="utf-8"))):
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


def test_the_revision_creates_every_column_the_orm_declares():
    assert _created_columns() == {c.name for c in GeometrySurvey.__table__.columns}


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
