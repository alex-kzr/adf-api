"""Initial immutable control-plane migration. Future changes require a new revision."""

import sqlalchemy as sa
from alembic import op

revision = "0001_control_plane"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for schema in ("control", "runtime", "audit", "checkpoints"):
            op.execute(sa.schema.CreateSchema(schema, if_not_exists=True))
    op.create_table(
        "records",
        sa.Column("actor", sa.String(128), nullable=False, primary_key=False),
        sa.Column("action", sa.String(128), nullable=False, primary_key=False),
        sa.Column("resource_id", sa.String(128), nullable=False, primary_key=False),
        sa.Column("payload", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="audit",
    )
    op.create_table(
        "definitions",
        sa.Column("kind", sa.String(32), nullable=False, primary_key=False),
        sa.Column("name", sa.String(200), nullable=False, primary_key=False),
        sa.Column(
            "current_version_id", sa.String(36), nullable=False, primary_key=False
        ),
        sa.Column("data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="control",
    )
    op.create_index(
        "ix_control_definitions_kind",
        "definitions",
        ["kind"],
        schema="control",
        unique=False,
    )
    op.create_table(
        "idempotency",
        sa.Column("key", sa.String(512), nullable=False, primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False, primary_key=False),
        sa.Column("response", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="control",
    )
    op.create_table(
        "projects",
        sa.Column("name", sa.String(200), nullable=False, primary_key=False),
        sa.Column("repo_path", sa.String(1024), nullable=False, primary_key=False),
        sa.Column("data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("repo_path"),
        schema="control",
    )
    op.create_table(
        "workers",
        sa.Column("capabilities", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("allowed_roots", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("project_ids", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("last_heartbeat", sa.String(40), nullable=False, primary_key=False),
        sa.Column("running_executions", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="runtime",
    )
    op.create_table(
        "config_versions",
        sa.Column(
            "definition_id",
            sa.String(36),
            sa.ForeignKey("control.definitions.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("config_hash", sa.String(64), nullable=False, primary_key=False),
        sa.Column("data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("definition_id", "version"),
        schema="control",
    )
    op.create_table(
        "tasks",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("control.projects.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("title", sa.String(500), nullable=False, primary_key=False),
        sa.Column("status", sa.String(32), nullable=False, primary_key=False),
        sa.Column("data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="control",
    )
    op.create_index(
        "ix_control_tasks_project_id",
        "tasks",
        ["project_id"],
        schema="control",
        unique=False,
    )
    op.create_table(
        "workflow_versions",
        sa.Column(
            "workflow_id",
            sa.String(36),
            sa.ForeignKey("control.definitions.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("graph_hash", sa.String(64), nullable=False, primary_key=False),
        sa.Column("graph_json", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("workflow_id", "version"),
        schema="control",
    )
    op.create_table(
        "runs",
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("control.tasks.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("control.projects.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column(
            "workflow_version_id",
            sa.String(36),
            sa.ForeignKey("control.workflow_versions.id"),
            nullable=True,
            primary_key=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, primary_key=False),
        sa.Column("pause_requested", sa.Boolean(), nullable=False, primary_key=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, primary_key=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False, primary_key=False),
        sa.Column(
            "integration_status", sa.String(32), nullable=False, primary_key=False
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("result", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="runtime",
    )
    op.create_index(
        "ix_runtime_runs_status", "runs", ["status"], schema="runtime", unique=False
    )
    op.create_index(
        "ix_runtime_runs_project_id",
        "runs",
        ["project_id"],
        schema="runtime",
        unique=False,
    )
    op.create_table(
        "approvals",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runtime.runs.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("action", sa.String(128), nullable=False, primary_key=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False, primary_key=False),
        sa.Column("snapshot", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("expires_at", sa.String(40), nullable=False, primary_key=False),
        sa.Column("decision", sa.String(16), nullable=True, primary_key=False),
        sa.Column("decided_at", sa.String(40), nullable=True, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("run_id", "action", "snapshot_hash"),
        schema="control",
    )
    op.create_index(
        "ix_control_approvals_run_id",
        "approvals",
        ["run_id"],
        schema="control",
        unique=False,
    )
    op.create_table(
        "commands",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runtime.runs.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False, primary_key=False),
        sa.Column("status", sa.String(32), nullable=False, primary_key=False),
        sa.Column("data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        schema="control",
    )
    op.create_table(
        "executions",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runtime.runs.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("node_id", sa.String(128), nullable=False, primary_key=False),
        sa.Column("attempt", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("operation", sa.String(32), nullable=False, primary_key=False),
        sa.Column("status", sa.String(32), nullable=False, primary_key=False),
        sa.Column("worker_id", sa.String(128), nullable=True, primary_key=False),
        sa.Column("epoch", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("lease_expires_at", sa.String(40), nullable=True, primary_key=False),
        sa.Column("request_hash", sa.String(64), nullable=False, primary_key=False),
        sa.Column("request", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("process_identity", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("result", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("output_bytes", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("run_id", "node_id", "attempt", "operation"),
        schema="runtime",
    )
    op.create_index(
        "ix_runtime_executions_run_id",
        "executions",
        ["run_id"],
        schema="runtime",
        unique=False,
    )
    op.create_index(
        "ix_runtime_executions_status",
        "executions",
        ["status"],
        schema="runtime",
        unique=False,
    )
    op.create_table(
        "nodes",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runtime.runs.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("node_id", sa.String(128), nullable=False, primary_key=False),
        sa.Column("attempt", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("status", sa.String(32), nullable=False, primary_key=False),
        sa.Column("result", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("run_id", "node_id", "attempt"),
        schema="runtime",
    )
    op.create_index(
        "ix_runtime_nodes_run_id", "nodes", ["run_id"], schema="runtime", unique=False
    )
    op.create_table(
        "artifacts",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runtime.runs.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column(
            "execution_id",
            sa.String(36),
            sa.ForeignKey("runtime.executions.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("kind", sa.String(128), nullable=False, primary_key=False),
        sa.Column("media_type", sa.String(128), nullable=False, primary_key=False),
        sa.Column("size", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("sha256", sa.String(64), nullable=False, primary_key=False),
        sa.Column("storage_key", sa.String(128), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("storage_key"),
        schema="runtime",
    )
    op.create_table(
        "events",
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runtime.runs.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("run_sequence", sa.Integer(), nullable=False, primary_key=False),
        sa.Column(
            "execution_id",
            sa.String(36),
            sa.ForeignKey("runtime.executions.id"),
            nullable=True,
            primary_key=False,
        ),
        sa.Column("epoch", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("sequence", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("type", sa.String(128), nullable=False, primary_key=False),
        sa.Column("payload", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("run_id", "run_sequence"),
        sa.UniqueConstraint("execution_id", "epoch", "sequence"),
        schema="runtime",
    )
    op.create_index(
        "ix_runtime_events_run_id", "events", ["run_id"], schema="runtime", unique=False
    )
    op.create_table(
        "outbox",
        sa.Column(
            "execution_id",
            sa.String(36),
            sa.ForeignKey("runtime.executions.id"),
            nullable=False,
            primary_key=False,
        ),
        sa.Column("delivered", sa.Boolean(), nullable=False, primary_key=False),
        sa.Column("data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("created_at", sa.String(40), nullable=False, primary_key=False),
        sa.UniqueConstraint("execution_id"),
        schema="runtime",
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adf_api') THEN GRANT SELECT ON TABLE public.alembic_version TO adf_api; END IF; END $$"
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adf_api') THEN GRANT USAGE ON SCHEMA control TO adf_api; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA control TO adf_api; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA control TO adf_api; END IF; END $$"
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adf_api') THEN GRANT USAGE ON SCHEMA runtime TO adf_api; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA runtime TO adf_api; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA runtime TO adf_api; END IF; END $$"
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adf_api') THEN GRANT USAGE ON SCHEMA audit TO adf_api; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA audit TO adf_api; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA audit TO adf_api; END IF; END $$"
        )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrade is disabled; restore a coordinated backup instead."
    )
