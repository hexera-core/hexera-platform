# Responsibility: Verify the migration history is one clean baseline whose downgrade reverses everything it creates.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence import models as m
from meshpipeline.persistence.session import Base

REPO_ROOT = Path(__file__).parents[3]
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

#: The one revision this project ships.
BASELINE = "0001_schema_baseline"

#: Identifiers that existed during development and were deleted by the history cut. None of them
#: may ever resolve again; a database stamped with one is recreated, not upgraded.
RETIRED_REVISIONS = (
    "0001_initial_schema", "0002_dispute_operation_key", "0003_source_object_cleanup",
)

# The CORE schema contract every release must keep (a floor, not an exact set: the ORM may add
# columns - a durable-concurrency field, a new index - without breaking this; dropping a core one
# fails). Exactness of the built schema vs the ORM is the integration parity gate's job.
_CHAT_SESSION_CORE = {
    "id", "owner_id", "messages", "request_txt", "review_brief_txt", "llm_metadata",
    "intake_submitted", "job_id", "geometry_source_id", "created_at", "updated_at",
    "intake_patches", "dimensionality", "mesh_engine", "engine_params", "domain",
    "purpose", "input_kind",
}
_SIMJOB_CORE = {
    "id", "owner_id", "status", "created_at", "updated_at", "started_at", "ended_at",
    "current_attempt", "workspace_purged", "geometry_source_id", "failed_reason",
}
_CORE_TABLES = {"simulation_jobs", "artifacts", "chat_sessions"}
_CORE_INDEXES = {
    "ix_simulation_jobs_owner_id", "ix_artifacts_job_id",
    "ix_chat_sessions_owner_id", "ix_chat_sessions_created_at", "ix_chat_sessions_job_id",
}


def _revision_of(path: Path) -> tuple[str | None, object]:
    rev, down = None, "MISSING"
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "revision":
                    rev = node.value.value
                elif isinstance(t, ast.Name) and t.id == "down_revision":
                    down = node.value.value
    return rev, down


def _root_revision_file() -> Path:
    roots = [p for p in VERSIONS_DIR.glob("*.py")
             if not p.name.startswith("__") and _revision_of(p)[1] is None]
    assert len(roots) == 1, f"exactly one baseline (root) revision expected, got {[p.name for p in roots]}"
    return roots[0]


# THE MIGRATION GRAPH. This project has ONE baseline. The development-era chain was deleted rather
# than superseded: it is a pre-release baseline, no deployment exists whose data has to survive, and
# there is deliberately no upgrade path from any former revision. Revisions AFTER the baseline are
# ordinary additive migrations and are expected; what must never reappear is a second root, which is
# what a stray file or a leftover autogenerate produces.
def test_the_migration_history_has_exactly_one_baseline_revision():
    files = [f for f in VERSIONS_DIR.glob("*.py") if not f.name.startswith("__")]
    assert files, "no revision files found - this scan read nothing"
    roots = [(f, *_revision_of(f)) for f in files if _revision_of(f)[1] is None]
    assert len(roots) == 1, (
        f"exactly one baseline (root) revision expected, found {sorted(f.name for f, _, _ in roots)}")
    _file, rev, _down = roots[0]
    assert rev == BASELINE, f"unexpected baseline identifier {rev!r}"


def test_the_migration_history_is_one_unbranched_chain():
    files = [f for f in VERSIONS_DIR.glob("*.py") if not f.name.startswith("__")]
    assert files, "no revision files found - this scan read nothing"

    revs = [_revision_of(f) for f in files]
    ids = [r for r, _ in revs]
    assert all(ids), f"a revision file declares no revision id: {sorted(f.name for f in files)}"
    assert len(set(ids)) == len(ids), f"duplicate revision identifiers: {sorted(ids)}"

    roots = [r for r, down in revs if down is None]
    assert roots == [BASELINE], (
        f"the baseline must be the one root, got {sorted(roots)}")

    # every non-root points at a revision that exists, so the chain cannot dangle
    known = set(ids)
    for rev, down in revs:
        assert down is None or down in known, f"{rev} descends from unknown revision {down!r}"


def test_the_migration_graph_has_one_head_and_no_branches():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    revisions = list(script.walk_revisions())
    assert len(script.get_heads()) == 1, script.get_heads()
    assert script.get_bases() == [BASELINE], script.get_bases()
    for rev in revisions:
        assert not rev.branch_labels, (rev.revision, rev.branch_labels)
        assert not rev.dependencies, (rev.revision, rev.dependencies)
        # a merge point would carry a tuple of parents; the chain must stay linear
        assert not isinstance(rev.down_revision, tuple), (rev.revision, rev.down_revision)


def test_no_superseded_revision_identifier_is_selectable():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    known = {s.revision for s in ScriptDirectory.from_config(cfg).walk_revisions()}
    # Every development-era identifier was retired with the history cut and must never resolve
    # again. A database stamped with one is refused by runtime/migrate.py, never adopted.
    assert BASELINE in known, sorted(known)
    for retired in RETIRED_REVISIONS:
        assert retired not in known, f"{retired} is selectable again"


# the final SCHEMA contract, from the ORM model (the authority)
def test_orm_declares_the_core_tables():
    for tbl in _CORE_TABLES:
        assert tbl in Base.metadata.tables, f"the ORM no longer declares {tbl!r}"


def test_chat_sessions_has_the_core_columns():
    cols = {c.name for c in Base.metadata.tables["chat_sessions"].columns}
    missing = _CHAT_SESSION_CORE - cols
    assert not missing, f"chat_sessions is missing core columns: {sorted(missing)}"


def test_simulation_jobs_has_the_core_columns():
    cols = {c.name for c in Base.metadata.tables["simulation_jobs"].columns}
    missing = _SIMJOB_CORE - cols
    assert not missing, f"simulation_jobs is missing core columns: {sorted(missing)}"


def test_enum_types_have_final_values():
    art = {e.value for e in m.ArtifactType}
    assert "mesh_bundle" in art and "openfoam_case" not in art, art
    assert {e.value for e in m.JobStatus} >= {"pending", "running", "succeeded", "failed"}
    assert hasattr(m, "FailedReason")


def test_core_indexes_are_declared_on_the_models():
    declared = {i.name for t in Base.metadata.tables.values() for i in t.indexes}
    missing = _CORE_INDEXES - declared
    assert not missing, f"core indexes no longer declared on the ORM models: {sorted(missing)}"


# from-scratch baseline DDL hygiene (on the discovered root revision)
def test_the_baseline_is_a_clean_from_scratch_migration():
    src = _root_revision_file().read_text()
    assert "NOT VALID" not in src.upper(), (
        "the baseline uses NOT VALID - that is only for zero-downtime live alterations, never a "
        "from-scratch baseline")
    assert "drop_table" in src.lower() or "DROP TABLE" in src.upper(), \
        "the baseline downgrade does not drop its tables"


def test_the_baseline_describes_the_schema_rather_than_building_it_from_the_orm():
    src = _root_revision_file().read_text()
    for banned in ("create_all(", "metadata.create_all", "Base.metadata", "drop_all("):
        assert banned not in src, f"the baseline calls {banned!r} instead of describing the schema"
    tree = ast.parse(src)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    app = sorted(m for m in imported if m and m.startswith("meshpipeline"))
    assert app == [], f"the baseline imports application modules: {app}"


def test_the_baseline_replays_no_superseded_history():
    tree = ast.parse(_root_revision_file().read_text())

    # DECLARED COLUMNS, from the AST - not a substring search over the file, which would also
    # match the docstring where the baseline explains which columns it deliberately omits.
    declared = {n.args[0].value for n in ast.walk(tree)
                if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("Column") and n.args
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}
    assert declared, "no columns found - this scan read nothing"
    assert "step_file_path" not in declared, (
        "the baseline recreates a column the superseded chain created and then dropped")

    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for replayed in ("op.add_column", "op.drop_column", "op.alter_column", "op.rename_table"):
        assert replayed not in calls, (
            f"the baseline uses {replayed} - a from-scratch schema has nothing to alter")

    # Enum labels belong in the CREATE, never in a later ALTER. Checked against executed SQL
    # strings, which is where an `ALTER TYPE ... ADD VALUE` would actually live.
    executed = " ".join(
        a.value for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "op.execute"
        for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    assert "ADD VALUE" not in executed.upper(), (
        "the baseline extends an enum instead of creating it with its final labels")


def test_the_baseline_downgrade_reverses_every_table_it_creates():
    tree = ast.parse(_root_revision_file().read_text())
    created, dropped = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = ast.unparse(node.func)
        if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            if fn == "op.create_table":
                created.add(node.args[0].value)
            elif fn == "op.drop_table":
                dropped.add(node.args[0].value)
    assert created, "no tables created - this scan found nothing to check"
    assert created == dropped, (
        f"created but never dropped: {sorted(created - dropped)}; "
        f"dropped but never created: {sorted(dropped - created)}")


def test_the_baseline_defines_the_updated_at_trigger():
    src = _root_revision_file().read_text()
    assert "_chat_sessions_set_updated_at" in src and "trg_chat_sessions_updated_at" in src


def test_the_baseline_has_no_legacy_confirmed_column():
    src = _root_revision_file().read_text()
    assert '"confirmed"' not in src and "'confirmed'" not in src, (
        "the retired legacy 'confirmed' column reappeared in the baseline")
