# Responsibility: Create the entire application schema in one revision: tables, indexes, enum types, trigger.
# Owns: the enum label sets, declared once because two tables each reference artifacttype and reconciliationstate.
# Boundaries: the downgrade drops only what this revision created; alembic_version belongs to Alembic.

# This is a PRE-RELEASE baseline and the only revision this project ships. It supports a fresh
# database and nothing else. There is deliberately no upgrade path from any former development
# revision: those were deleted rather than superseded, and no deployment exists whose data has to
# survive. A database carrying an unknown stamp, or application objects with no history table at
# all, is refused by runtime/migrate.py before any DDL runs, and the operator recreates it empty.
#
# Revision ID: 0001_schema_baseline
# Revises: nothing; this is the root.
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001_schema_baseline'
down_revision = None
branch_labels = None
depends_on = None

#: The enum types this revision creates, named once. `artifacttype` and `reconciliationstate` are
#: each referenced from two tables, and a disagreement between those declarations would create the
#: type from whichever table happens to be built first.
_JOB_STATUS = ("pending", "running", "succeeded", "failed", "queued", "pending_review")
_FAILED_REASON = ("mesh_generation", "reviewer_rejected", "api_failure", "unhandled")
_ARTIFACT_TYPE = ("mesh", "mesh_bundle", "viewer_data")
_RECONCILIATION_STATE = ("pending", "resolved_deleted", "resolved_adopted", "blocked_conflict",
                         "abandoned")

#: Every enum the downgrade removes. Named exactly; there is deliberately no "drop every enum in
#: this schema" helper, because a migration owns the types it created and nothing more.
_ENUM_TYPES = ("reconciliationstate", "artifacttype", "failedreason", "jobstatus")


def _application_schema() -> str:
    # The anchor is where THIS migration's own history table lives, not `current_schema()`, which
    # follows `search_path` and under `other, public` names a schema the migration never touched.
    ctx = op.get_context()
    schema = ctx.version_table_schema or op.get_bind().exec_driver_sql(
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE c.relname = '{ctx.version_table}' AND c.relkind = 'r' LIMIT 1").scalar()
    return schema or op.get_bind().exec_driver_sql("SELECT current_schema()").scalar()


def upgrade():
    op.create_table('geometry_sources',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('original_filename', sa.String(length=512), nullable=False),
    sa.Column('suffix_hint', sa.String(length=16), server_default='', nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('purge_claim_id', sa.String(length=64), nullable=True),
    sa.Column('purge_claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('object_key')
    )
    op.create_index('ix_geometry_sources_owner_created', 'geometry_sources', ['owner_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_geometry_sources_owner_id'), 'geometry_sources', ['owner_id'], unique=False)
    op.create_index(op.f('ix_geometry_sources_sha256'), 'geometry_sources', ['sha256'], unique=False)
    op.create_index('ix_geometry_sources_unpurged_created', 'geometry_sources', ['created_at'], unique=False, postgresql_where=sa.text('purged_at IS NULL'))
    op.create_table('source_object_cleanups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('state', sa.Enum(*_RECONCILIATION_STATE, name='reconciliationstate'), nullable=False),
    sa.Column('retry_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('object_key')
    )
    op.create_index(op.f('ix_source_object_cleanups_owner_id'), 'source_object_cleanups', ['owner_id'], unique=False)
    op.create_index('ix_source_object_cleanups_pending', 'source_object_cleanups', ['created_at'], unique=False, postgresql_where=sa.text("state = 'pending'"))
    op.create_table('geometry_interpretations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('geometry_source_id', sa.UUID(), nullable=False),
    sa.Column('unit', sa.String(length=8), nullable=False),
    sa.Column('scale_to_metres', sa.Float(), nullable=False),
    sa.Column('basis', sa.String(length=16), nullable=False),
    sa.Column('evidence', sa.String(length=256), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['geometry_source_id'], ['geometry_sources.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'geometry_source_id', 'unit', 'basis', name='uq_geometry_interpretation')
    )
    op.create_index('ix_geometry_interpretations_source', 'geometry_interpretations', ['owner_id', 'geometry_source_id'], unique=False)
    op.create_table('simulation_jobs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('status', sa.Enum(*_JOB_STATUS, name='jobstatus'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_attempt', sa.Integer(), nullable=False),
    sa.Column('workspace_purged', sa.Boolean(), nullable=False),
    sa.Column('failed_reason', sa.Enum(*_FAILED_REASON, name='failedreason'), nullable=True),
    sa.Column('geometry_source_id', sa.UUID(), nullable=True),
    sa.Column('geometry_interpretation_id', sa.UUID(), nullable=True),
    sa.Column('dispatch_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('final_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('pipeline_backend', sa.String(length=32), nullable=True),
    sa.Column('pipeline_dispatch_state', sa.String(length=24), nullable=True),
    sa.Column('pipeline_execution_id', sa.String(length=512), nullable=True),
    sa.Column('pipeline_submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pipeline_launch_error', sa.Text(), nullable=True),
    sa.Column('execution_generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('execution_claim_epoch', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('active_worker_token', sa.UUID(), nullable=True),
    sa.Column('lease_acquired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('owning_backend', sa.String(length=32), nullable=True),
    sa.Column('pipeline_deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dispute_operation_key', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['geometry_interpretation_id'], ['geometry_interpretations.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['geometry_source_id'], ['geometry_sources.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_jobs_cleanup', 'simulation_jobs', ['status', 'ended_at', 'workspace_purged'], unique=False)
    op.create_index('ix_jobs_owner_status', 'simulation_jobs', ['owner_id', 'status'], unique=False)
    op.create_index('ix_jobs_stalled', 'simulation_jobs', ['status'], unique=False)
    op.create_index(op.f('ix_simulation_jobs_geometry_interpretation_id'), 'simulation_jobs', ['geometry_interpretation_id'], unique=False)
    op.create_index(op.f('ix_simulation_jobs_geometry_source_id'), 'simulation_jobs', ['geometry_source_id'], unique=False)
    op.create_index(op.f('ix_simulation_jobs_owner_id'), 'simulation_jobs', ['owner_id'], unique=False)
    op.create_index('uq_simulation_jobs_dispute_operation', 'simulation_jobs', ['dispute_operation_key'], unique=True, postgresql_where=sa.text('dispute_operation_key is not null'))
    op.create_table('artifact_reconciliations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('delivery_attempt', sa.Integer(), nullable=False),
    sa.Column('logical_key', sa.String(length=128), nullable=False),
    sa.Column('artifact_type', sa.Enum(*_ARTIFACT_TYPE, name='artifacttype'), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('object_checksum', sa.String(length=128), nullable=True),
    sa.Column('object_size', sa.Integer(), nullable=False),
    sa.Column('failure_category', sa.String(length=64), server_default='row_write_failed', nullable=False),
    sa.Column('retry_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state', sa.Enum(*_RECONCILIATION_STATE, name='reconciliationstate'), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['simulation_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'delivery_attempt', 'object_key', name='uq_reconcile_job_attempt_object')
    )
    op.create_index(op.f('ix_artifact_reconciliations_job_id'), 'artifact_reconciliations', ['job_id'], unique=False)
    op.create_index(op.f('ix_artifact_reconciliations_owner_id'), 'artifact_reconciliations', ['owner_id'], unique=False)
    op.create_index('ix_artifact_reconciliations_state', 'artifact_reconciliations', ['state'], unique=False)
    op.create_table('artifacts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('artifact_type', sa.Enum(*_ARTIFACT_TYPE, name='artifacttype'), nullable=False),
    sa.Column('logical_key', sa.String(length=128), server_default='', nullable=False),
    sa.Column('delivery_attempt', sa.Integer(), server_default='0', nullable=False),
    sa.Column('execution_generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('storage_key', sa.String(length=1024), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('checksum', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['simulation_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'logical_key', name='uq_artifacts_job_logical')
    )
    op.create_index(op.f('ix_artifacts_job_id'), 'artifacts', ['job_id'], unique=False)
    op.create_table('capture_operations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('execution_generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('op_key', sa.String(length=128), nullable=False),
    sa.Column('record_type', sa.String(length=32), server_default='span_event', nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=32), server_default='event', nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), server_default='', nullable=False),
    sa.Column('conflicted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('conflict_evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['simulation_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'job_id', 'execution_generation', 'op_key', name='uq_capture_operations_identity'),
    sa.UniqueConstraint('seq')
    )
    op.create_index('ix_capture_operations_scope', 'capture_operations', ['owner_id', 'job_id', 'execution_generation'], unique=False)
    op.create_table('chat_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('messages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('request_txt', sa.Text(), nullable=True),
    sa.Column('review_brief_txt', sa.Text(), nullable=True),
    sa.Column('intake_patches', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('dimensionality', sa.String(length=16), nullable=True),
    sa.Column('purpose', sa.String(length=32), nullable=True),
    sa.Column('input_kind', sa.String(length=32), nullable=True),
    sa.Column('requested_mesh_fidelity', sa.String(length=16), nullable=True),
    sa.Column('mesh_engine', sa.String(length=32), nullable=True),
    sa.Column('domain', sa.Text(), nullable=True),
    sa.Column('engine_params', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('intake_gate', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('llm_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('intake_submitted', sa.Boolean(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=True),
    sa.Column('geometry_source_id', sa.UUID(), nullable=True),
    sa.Column('geometry_interpretation_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['geometry_interpretation_id'], ['geometry_interpretations.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['geometry_source_id'], ['geometry_sources.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id'], ['simulation_jobs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chat_sessions_created_at', 'chat_sessions', ['created_at'], unique=False)
    op.execute("""
        CREATE FUNCTION _chat_sessions_set_updated_at()
            RETURNS trigger LANGUAGE plpgsql
        AS $function$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $function$;
        CREATE TRIGGER trg_chat_sessions_updated_at
            BEFORE UPDATE ON chat_sessions
            FOR EACH ROW EXECUTE FUNCTION _chat_sessions_set_updated_at();
    """)
    op.create_index(op.f('ix_chat_sessions_geometry_interpretation_id'), 'chat_sessions', ['geometry_interpretation_id'], unique=False)
    op.create_index(op.f('ix_chat_sessions_geometry_source_id'), 'chat_sessions', ['geometry_source_id'], unique=False)
    op.create_index('ix_chat_sessions_job_id', 'chat_sessions', ['job_id'], unique=False)
    op.create_index(op.f('ix_chat_sessions_owner_id'), 'chat_sessions', ['owner_id'], unique=False)
    op.create_table('native_submission_claims',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('execution_generation', sa.Integer(), nullable=False),
    sa.Column('semantic_operation', sa.String(length=64), server_default='native_mesh_submission', nullable=False),
    sa.Column('operation_key', sa.String(length=64), nullable=False),
    sa.Column('engine', sa.String(length=64), nullable=False),
    sa.Column('payload_digest', sa.String(length=64), nullable=False),
    sa.Column('disposition', sa.String(length=16), server_default='claimed', nullable=False),
    sa.Column('claimant_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('provider_reference', sa.String(length=512), nullable=True),
    sa.Column('failure_class', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(disposition <> 'accepted') or (provider_reference is not null and length(provider_reference) > 0)", name='ck_native_submission_claims_accepted_has_reference'),
    sa.CheckConstraint("disposition in ('claimed', 'accepted', 'indeterminate', 'failed')", name='ck_native_submission_claims_disposition'),
    sa.CheckConstraint("operation_key ~ '^[0-9a-f]{64}$'", name='ck_native_submission_claims_operation_key_shape'),
    sa.CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name='ck_native_submission_claims_payload_digest'),
    sa.CheckConstraint('execution_generation >= 0', name='ck_native_submission_claims_generation'),
    sa.ForeignKeyConstraint(['job_id'], ['simulation_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'execution_generation', 'semantic_operation', name='uq_native_submission_claims_identity'),
    sa.UniqueConstraint('operation_key', name='uq_native_submission_claims_operation_key')
    )
    op.create_index('ix_native_submission_claims_job', 'native_submission_claims', ['job_id', 'execution_generation'], unique=False)
    op.create_index('uq_native_submission_claims_provider_reference', 'native_submission_claims', ['provider_reference'], unique=True, postgresql_where=sa.text('provider_reference is not null'))
    op.create_table('terminal_outbox',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('execution_generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('terminal_status', sa.String(length=16), nullable=False),
    sa.Column('final_result_schema_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('event_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('dedup_key', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('publish_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.String(length=256), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['simulation_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key', name='uq_terminal_outbox_dedup')
    )
    op.create_index(op.f('ix_terminal_outbox_job_id'), 'terminal_outbox', ['job_id'], unique=False)
    op.create_index('ix_terminal_outbox_pending', 'terminal_outbox', ['published_at'], unique=False)
def downgrade():
    op.execute("""
        DROP TRIGGER IF EXISTS trg_chat_sessions_updated_at ON chat_sessions;
        DROP FUNCTION IF EXISTS _chat_sessions_set_updated_at();
    """)
    op.drop_index('ix_terminal_outbox_pending', table_name='terminal_outbox')
    op.drop_index(op.f('ix_terminal_outbox_job_id'), table_name='terminal_outbox')
    op.drop_table('terminal_outbox')
    op.drop_index('uq_native_submission_claims_provider_reference', table_name='native_submission_claims', postgresql_where=sa.text('provider_reference is not null'))
    op.drop_index('ix_native_submission_claims_job', table_name='native_submission_claims')
    op.drop_table('native_submission_claims')
    op.drop_index(op.f('ix_chat_sessions_owner_id'), table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_job_id', table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_geometry_source_id'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_geometry_interpretation_id'), table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_created_at', table_name='chat_sessions')
    op.drop_table('chat_sessions')
    op.drop_index('ix_capture_operations_scope', table_name='capture_operations')
    op.drop_table('capture_operations')
    op.drop_index(op.f('ix_artifacts_job_id'), table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_index('ix_artifact_reconciliations_state', table_name='artifact_reconciliations')
    op.drop_index(op.f('ix_artifact_reconciliations_owner_id'), table_name='artifact_reconciliations')
    op.drop_index(op.f('ix_artifact_reconciliations_job_id'), table_name='artifact_reconciliations')
    op.drop_table('artifact_reconciliations')
    op.drop_index('uq_simulation_jobs_dispute_operation', table_name='simulation_jobs', postgresql_where=sa.text('dispute_operation_key is not null'))
    op.drop_index(op.f('ix_simulation_jobs_owner_id'), table_name='simulation_jobs')
    op.drop_index(op.f('ix_simulation_jobs_geometry_source_id'), table_name='simulation_jobs')
    op.drop_index(op.f('ix_simulation_jobs_geometry_interpretation_id'), table_name='simulation_jobs')
    op.drop_index('ix_jobs_stalled', table_name='simulation_jobs')
    op.drop_index('ix_jobs_owner_status', table_name='simulation_jobs')
    op.drop_index('ix_jobs_cleanup', table_name='simulation_jobs')
    op.drop_table('simulation_jobs')
    op.drop_index('ix_geometry_interpretations_source', table_name='geometry_interpretations')
    op.drop_table('geometry_interpretations')
    op.drop_index('ix_source_object_cleanups_pending', table_name='source_object_cleanups', postgresql_where=sa.text("state = 'pending'"))
    op.drop_index(op.f('ix_source_object_cleanups_owner_id'), table_name='source_object_cleanups')
    op.drop_table('source_object_cleanups')
    op.drop_index('ix_geometry_sources_unpurged_created', table_name='geometry_sources', postgresql_where=sa.text('purged_at IS NULL'))
    op.drop_index(op.f('ix_geometry_sources_sha256'), table_name='geometry_sources')
    op.drop_index(op.f('ix_geometry_sources_owner_id'), table_name='geometry_sources')
    op.drop_index('ix_geometry_sources_owner_created', table_name='geometry_sources')
    op.drop_table('geometry_sources')

    # The types the tables above depended on, once nothing references them. IF EXISTS keeps a
    # re-run idempotent; no CASCADE, because a surviving dependency is a real problem that must
    # surface rather than be dragged away.
    schema = _application_schema()
    for enum_name in _ENUM_TYPES:
        op.execute(f'DROP TYPE IF EXISTS "{schema}"."{enum_name}"')
