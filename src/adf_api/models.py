"""Versioned control definitions and fenced execution ledger."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    if TYPE_CHECKING:
        id: Mapped[str]


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=now, nullable=False)


class Project(Record, Base):
    __tablename__ = "projects"
    __table_args__: Any = {"schema": "control"}
    name: Mapped[str] = mapped_column(String(200))
    repo_path: Mapped[str] = mapped_column(String(1024), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class Task(Record, Base):
    __tablename__ = "tasks"
    __table_args__: Any = {"schema": "control"}
    project_id: Mapped[str] = mapped_column(
        ForeignKey("control.projects.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class Definition(Record, Base):
    __tablename__ = "definitions"
    __table_args__: Any = {"schema": "control"}
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    current_version_id: Mapped[str] = mapped_column(String(36))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class ConfigVersion(Record, Base):
    __tablename__ = "config_versions"
    __table_args__: Any = (
        UniqueConstraint("definition_id", "version"),
        {"schema": "control"},
    )
    definition_id: Mapped[str] = mapped_column(ForeignKey("control.definitions.id"))
    version: Mapped[int] = mapped_column(Integer)
    config_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class WorkflowVersion(Record, Base):
    __tablename__ = "workflow_versions"
    __table_args__: Any = (
        UniqueConstraint("workflow_id", "version"),
        {"schema": "control"},
    )
    workflow_id: Mapped[str] = mapped_column(ForeignKey("control.definitions.id"))
    version: Mapped[int] = mapped_column(Integer)
    graph_hash: Mapped[str] = mapped_column(String(64))
    graph_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class Run(Record, Base):
    __tablename__ = "runs"
    __table_args__: Any = {"schema": "runtime"}
    task_id: Mapped[str] = mapped_column(ForeignKey("control.tasks.id"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("control.projects.id"), index=True
    )
    workflow_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("control.workflow_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    pause_requested: Mapped[bool] = mapped_column(default=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    integration_status: Mapped[str] = mapped_column(String(32), default="pending")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Node(Record, Base):
    __tablename__ = "nodes"
    __table_args__: Any = (
        UniqueConstraint("run_id", "node_id", "attempt"),
        {"schema": "runtime"},
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime.runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Execution(Record, Base):
    __tablename__ = "executions"
    __table_args__: Any = (
        UniqueConstraint("run_id", "node_id", "attempt", "operation"),
        {"schema": "runtime"},
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime.runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    process_identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_bytes: Mapped[int] = mapped_column(Integer, default=0)


class Approval(Record, Base):
    __tablename__ = "approvals"
    __table_args__: Any = (
        UniqueConstraint("run_id", "action", "snapshot_hash"),
        {"schema": "control"},
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime.runs.id"), index=True)
    action: Mapped[str] = mapped_column(String(128))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Worker(Record, Base):
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    __tablename__ = "workers"
    __table_args__: Any = {"schema": "runtime"}
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON)
    allowed_roots: Mapped[list[str]] = mapped_column(JSON)
    project_ids: Mapped[list[str]] = mapped_column(JSON)
    last_heartbeat: Mapped[str] = mapped_column(String(40))
    running_executions: Mapped[list[str]] = mapped_column(JSON, default=list)


class Event(Record, Base):
    __tablename__ = "events"
    __table_args__: Any = (
        UniqueConstraint("run_id", "run_sequence"),
        UniqueConstraint("execution_id", "epoch", "sequence"),
        {"schema": "runtime"},
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime.runs.id"), index=True)
    run_sequence: Mapped[int] = mapped_column(Integer)
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("runtime.executions.id"), nullable=True
    )
    epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class Command(Record, Base):
    __tablename__ = "commands"
    __table_args__: Any = {"schema": "control"}
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime.runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Outbox(Record, Base):
    __tablename__ = "outbox"
    __table_args__: Any = {"schema": "runtime"}
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("runtime.executions.id"), unique=True
    )
    delivered: Mapped[bool] = mapped_column(default=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class Idempotency(Base):
    __tablename__ = "idempotency"
    __table_args__: Any = {"schema": "control"}
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Artifact(Record, Base):
    __tablename__ = "artifacts"
    __table_args__: Any = {"schema": "runtime"}
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime.runs.id"))
    execution_id: Mapped[str] = mapped_column(ForeignKey("runtime.executions.id"))
    kind: Mapped[str] = mapped_column(String(128))
    media_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(128), unique=True)


class Audit(Record, Base):
    __tablename__ = "records"
    __table_args__: Any = {"schema": "audit"}
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
