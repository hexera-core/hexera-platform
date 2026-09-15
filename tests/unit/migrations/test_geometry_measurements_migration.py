# Responsibility: Verify the geometry_measurements migration builds exactly the table the ORM declares.
# Boundaries: it reads the revision file and the ORM; building a real database is the integration tier's parity gate.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.models import GeometryMeasurement

REPO = Path(__file__).parents[3]
REVISION = REPO / "alembic" / "versions" / "0003_geometry_measurements.py"
TABLE = "geometry_measurements"


def _tree() -> ast.Module:
    return ast.parse(REVISION.read_text(encoding="utf-8"))


def _calls(func_name: str, attr: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == attr):
                out.append(inner)
    return out


def _created_columns() -> set[str]:
    for call in _calls("upgrade", "create_table"):
        if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == TABLE:
            return {a.args[0].value for a in call.args[1:]
                    if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and a.func.attr == "Column" and a.args
                    and isinstance(a.args[0], ast.Constant)}
    raise AssertionError(f"the revision does not create {TABLE}")


def test_the_revision_creates_every_column_the_orm_declares():
    declared = {c.name for c in GeometryMeasurement.__table__.columns}
    assert _created_columns() == declared, (
        "the migration and the ORM disagree about this table's columns")


def test_the_table_is_keyed_to_the_source_and_carries_the_digest():
    columns = {c.name: c for c in GeometryMeasurement.__table__.columns}
    assert columns["geometry_source_id"].nullable is False
    assert columns["sha256"].nullable is False
    assert columns["status"].nullable is False
    # One upload, one measurement: the measurement is a pure function of the bytes.
    uniques = {tuple(sorted(c.name for c in con.columns))
               for con in GeometryMeasurement.__table__.constraints
               if con.__class__.__name__ == "UniqueConstraint"}
    assert ("geometry_source_id",) in uniques


def test_deleting_a_source_cannot_quietly_destroy_its_measurement():
    fk = next(iter(GeometryMeasurement.__table__.columns["geometry_source_id"].foreign_keys))
    assert fk.ondelete == "RESTRICT"
    assert fk.column.table.name == "geometry_sources"


def test_the_downgrade_drops_exactly_what_the_upgrade_created():
    dropped = {c.args[0].value for c in _calls("downgrade", "drop_table")
               if c.args and isinstance(c.args[0], ast.Constant)}
    assert dropped == {TABLE}


def test_the_revision_follows_the_head_it_names():
    text = REVISION.read_text(encoding="utf-8")
    assert "revision = '0003_geometry_measurements'" in text
    assert "down_revision = '0002_api_keys'" in text
