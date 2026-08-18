# Responsibility: Point Alembic at the right database and the right schema, for both offline and online runs.
# Owns: the sync URL, the validated schema, version-table placement and the search_path anchor.
# Boundaries: it configures.
# Collaborates with: persistence/migration_url.py and runtime/migrate.py.
import os
import re
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import meshpipeline.persistence.models  # noqa: E402,F401  (side-effect import: registers models on Base.metadata for autogenerate)
from meshpipeline.persistence.migration_url import sync_migration_url  # noqa: E402
from meshpipeline.persistence.session import Base  # noqa: E402

config = context.config
# Configure logging ONLY for a standalone `alembic` invocation. `fileConfig` reconfigures the
# ROOT logger for the whole process, so a caller that drives Alembic in-process - the test
# provisioning seam - would silently change every other logger's format and handlers. The
# release gate reads pytest node names out of captured output with `^ERROR (\S+)`, and the
# reformatted records started matching it.
if config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)
# The DDL connection is SYNC (psycopg2). ONE source of truth with runtime/migrate.py: parse the
# configured URL, swap the driver async->sync, and make TLS explicit - no fragile string surgery.
config.set_main_option("sqlalchemy.url", sync_migration_url())
target_metadata = Base.metadata

#: A schema name we are willing to put into DDL. Alembic's own configuration is a deployment
#: value rather than user input, but it still reaches SQL as an identifier, so it is validated
#: instead of trusted: plain PostgreSQL identifiers only, nothing that could carry a quote or a
#: statement separator.
_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def application_schema() -> str:
    schema = (config.get_main_option("version_table_schema") or "public").strip()
    if not _SAFE_SCHEMA.match(schema):
        raise ValueError(
            f"version_table_schema={schema!r} is not a plain PostgreSQL identifier; refusing to "
            "build DDL from it")
    return schema


def _quoted(schema: str) -> str:
    return '"' + schema.replace('"', '""') + '"'


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    # Alembic's own history table is not part of the application metadata, so autogenerate sees it
    # as an object the models no longer define and proposes dropping it. A generated revision that
    # carries `drop_table('alembic_version')` destroys the history it is being recorded in, so the
    # comparison never gets to consider it.
    if type_ == "table" and name == config.get_main_option("version_table", "alembic_version"):
        return False
    return True


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_object=_include_object,
        # WHERE the version table lives. Without this, Alembic resolves `alembic_version` through
        # search_path, so a hostile role could leave an existing installation's history behind and
        # quietly start a second one somewhere else.
        version_table_schema=application_schema(),
        # Autogenerate compares only the application schema. `include_schemas` stays off: turning
        # it on makes autogenerate consider every schema on the server and propose dropping
        # objects this project does not own.
        include_schemas=False,
        **kwargs,
    )


def run_migrations_offline():
    _configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    schema = application_schema()
    with connectable.connect() as connection:
        # ANCHOR EVERY UNQUALIFIED STATEMENT, for this migration and nothing beyond it.
        # The migrations issue unqualified DDL - create_table, create_index, CREATE TYPE,
        # CREATE FUNCTION, CREATE TRIGGER - which PostgreSQL resolves through `search_path`. With
        # a role configured `search_path = other, public` that put Hexera tables, enums,
        # functions and triggers into `other`, and made downgrade delete `other`'s objects while
        # leaving the application's own behind.
        # SET LOCAL, inside the migration transaction, on this connection only: it reverts when
        # the transaction ends and touches no other session. That is not a global or role-level
        # search_path change, and it is one place rather than a lookup repeated in every migration.
        # `pg_catalog` is listed explicitly because naming a search_path replaces the implicit
        # default, and built-in types and functions must still resolve.
        _configure(connection=connection)
        with context.begin_transaction():
            connection.execute(text(f"SET LOCAL search_path TO {_quoted(schema)}, pg_catalog"))
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
