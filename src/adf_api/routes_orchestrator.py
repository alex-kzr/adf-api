"""Orchestrator-only persistence facade; no direct worker database access."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from .config import Settings
from .db import Database
from .definitions import get, identifier, view
from .errors import DomainError
from .ledger import (
    ACTIVE,
    RUN_TERMINAL,
    capability_audit,
    clock,
    control,
    create_execution,
    emit,
    execution_view,
    instant,
    lock_run,
    matches_approval,
    recorded_manual_continuation,
    run_view,
    transition,
)
from .models import Approval, Command, Execution, Node, Outbox, Run
from .plans import save_plan
from .privacy import digest, redact
from .schemas import ApprovalInput, ExecutionInput, NodeInput, TransitionInput


def build_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/internal")

    @router.get("/orchestrator/runs")
    def active_runs(
        status: str = "active", limit: int = Query(200, ge=1, le=200)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            query = select(Run).order_by(Run.created_at).limit(limit)
            query = (
                query.where(Run.status.not_in(RUN_TERMINAL))
                if status == "active"
                else query.where(Run.status == status)
            )
            items = []
            for row in session.scalars(query):
                run = lock_run(session, row.id)
                for execution in session.scalars(
                    select(Execution).where(
                        Execution.run_id == run.id,
                        Execution.status.in_(ACTIVE - {"created", "uncertain"}),
                    )
                ):
                    if execution.lease_expires_at and instant(
                        execution.lease_expires_at
                    ) <= clock(session):
                        execution.status = "uncertain"
                        emit(
                            session,
                            run,
                            "execution.uncertain",
                            {"execution_id": execution.id, "reason": "LEASE_EXPIRED"},
                        )
                items.append(run_view(session, run, internal=True))
            return {"items": items, "next_cursor": None}

    @router.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            return run_view(
                session, get(session, Run, identifier(run_id)), internal=True
            )

    @router.get("/orchestrator/commands")
    def commands() -> dict[str, Any]:
        with database.transaction() as session:
            return {
                "items": [
                    view(row)
                    for row in session.scalars(
                        select(Command)
                        .where(Command.status == "pending")
                        .order_by(Command.created_at)
                        .limit(200)
                    )
                ],
                "next_cursor": None,
            }

    @router.post("/commands/{command_id}/applied")
    def command_applied(
        command_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Command, identifier(command_id), lock=True)
            row.status = "applied"
            if body:
                row.data = {**row.data, "result": redact(body)}
            return view(row)

    @router.get("/orchestrator/outbox")
    def outbox() -> dict[str, Any]:
        with database.transaction() as session:
            # Created executions are reconstructible even after Redis loss and a prior delivered marker.
            query = (
                select(Outbox)
                .join(Execution, Outbox.execution_id == Execution.id)
                .where(Execution.status == "created")
                .order_by(Outbox.created_at)
                .limit(200)
            )
            return {
                "items": [view(row) for row in session.scalars(query)],
                "next_cursor": None,
            }

    @router.post("/outbox/{outbox_id}/delivered")
    def delivered(outbox_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Outbox, identifier(outbox_id), lock=True)
            row.delivered = True
            return view(row)

    @router.post("/runs/{run_id}/transition")
    def run_transition(run_id: str, body: TransitionInput) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)
            if body.revision is not None and run.revision != body.revision:
                raise DomainError("VERSION_CONFLICT", "Run revision changed.")
            return run_view(
                session,
                transition(
                    session, run, body.status, body.result, body.integration_status
                ),
                internal=True,
            )

    @router.post("/runs/{run_id}/control")
    def run_control(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with database.transaction() as session:
            return control(
                session,
                lock_run(session, run_id),
                body.get("action", ""),
                str(body.get("reason", "orchestrator"))[:200],
            )

    @router.post("/runs/{run_id}/base")
    def pin_base(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        import re

        with database.transaction() as session:
            run = lock_run(session, run_id)
            base = body.get("root_base_sha", "")
            if not isinstance(base, str) or not re.fullmatch("[0-9a-f]{40}", base):
                raise DomainError(
                    "INVALID_BASE", "Base must be a full Git commit SHA.", 422
                )
            previous = run.snapshot.get("root_base_sha")
            if previous and previous != base:
                raise DomainError(
                    "BASE_IMMUTABLE", "Run base is already pinned to another commit."
                )
            run.snapshot = {**run.snapshot, "root_base_sha": base}
            return run_view(session, run, internal=True)

    @router.post("/runs/{run_id}/nodes")
    def node_update(run_id: str, body: NodeInput) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)
            graph_ids = {
                node["id"]
                for node in (run.snapshot.get("graph_json") or {}).get("nodes", [])
            }
            if (
                body.node_id not in graph_ids
                and not body.node_id.startswith("__")
                and run.snapshot.get("mode") != "planning"
            ):
                raise DomainError(
                    "INVALID_NODE", "Node is not part of the pinned workflow.", 422
                )
            if body.attempt > run.snapshot["policy"]["max_retries"] + 1:
                continuation = recorded_manual_continuation(
                    session, run, body.node_id, body.attempt
                )
                if continuation is None or (
                    body.result.get("execution_id") is not None
                    and body.result["execution_id"] != continuation.id
                ):
                    raise DomainError("QUOTA_EXCEEDED", "Node attempt budget exceeded.")
            row = session.scalar(
                select(Node).where(
                    Node.run_id == run.id,
                    Node.node_id == body.node_id,
                    Node.attempt == body.attempt,
                )
            )
            if row:
                if row.status in {"succeeded", "failed", "cancelled", "skipped"} and (
                    row.status != body.status or row.result != redact(body.result)
                ):
                    raise DomainError(
                        "NODE_TERMINAL", "A terminal node attempt is immutable."
                    )
                if row.status == body.status and row.result == redact(body.result):
                    return view(row)
                row.status, row.result, row.revision = (
                    body.status,
                    redact(body.result),
                    row.revision + 1,
                )
            else:
                row = Node(
                    run_id=run.id,
                    node_id=body.node_id,
                    attempt=body.attempt,
                    status=body.status,
                    result=redact(body.result),
                )
                session.add(row)
            session.flush()
            emit(
                session,
                run,
                "node.status_changed",
                {
                    "node_id": body.node_id,
                    "attempt": body.attempt,
                    "status": body.status,
                },
            )
            return view(row)

    @router.post("/runs/{run_id}/executions", status_code=201)
    def execute(run_id: str, body: ExecutionInput) -> dict[str, Any]:
        with database.transaction() as session:
            return execution_view(
                create_execution(session, lock_run(session, run_id), body),
                internal=True,
            )

    @router.get("/executions/{execution_id}")
    def execution(execution_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Execution, identifier(execution_id))
            return {
                **execution_view(row, internal=True),
                "capability_audit": capability_audit(session, row),
            }

    @router.post("/runs/{run_id}/approvals", status_code=201)
    def approval_request(run_id: str, body: ApprovalInput) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)
            snapshot = body.snapshot
            if (
                not matches_approval(run, snapshot)
                or snapshot.get("action") != body.action
            ):
                raise DomainError(
                    "SNAPSHOT_STALE",
                    "Approval must bind the exact pinned graph, plan, base, policy, agents, registries, dependencies and write/tool scopes.",
                )
            snapshot_hash = digest(snapshot)
            previous = session.scalar(
                select(Approval).where(
                    Approval.run_id == run.id,
                    Approval.action == body.action,
                    Approval.snapshot_hash == snapshot_hash,
                )
            )
            if previous:
                return view(previous)
            expiry = (
                instant(body.expires_at)
                if body.expires_at
                else min(
                    instant(run.snapshot["deadline_at"]),
                    clock(session) + timedelta(hours=3),
                )
            )
            if expiry <= clock(session) or expiry > instant(
                run.snapshot["deadline_at"]
            ):
                raise DomainError(
                    "INVALID_EXPIRY",
                    "Approval expiry must be within the run deadline.",
                    422,
                )
            row = Approval(
                run_id=run.id,
                action=body.action,
                snapshot=snapshot,
                snapshot_hash=snapshot_hash,
                expires_at=expiry.isoformat(),
            )
            session.add(row)
            session.flush()
            emit(
                session,
                run,
                "approval.requested",
                {
                    "approval_id": row.id,
                    "action": body.action,
                    "snapshot_hash": snapshot_hash,
                },
            )
            return view(row)

    @router.get("/approvals/{approval_id}")
    def approval(approval_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            return view(get(session, Approval, identifier(approval_id)))

    @router.post("/runs/{run_id}/events")
    def domain_event(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from .ledger import event_view

        with database.transaction() as session:
            event = emit(
                session,
                lock_run(session, run_id),
                str(body.get("type", "domain.event"))[:128],
                body.get("payload", {}),
                event_id=body.get("event_id"),
            )
            return event_view(event)

    @router.post("/runs/{run_id}/plan")
    def plan(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)
            source_hash = body.get("source_plan_hash") or digest(body.get("tasks"))
            if run.snapshot.get("plan") and source_hash == run.snapshot.get(
                "source_plan_hash"
            ):
                return dict(run.snapshot["plan"])
            plan = save_plan(session, run, body)
            run.snapshot = {**run.snapshot, "source_plan_hash": source_hash}
            return plan

    return router
