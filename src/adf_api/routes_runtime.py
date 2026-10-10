"""Durable public run controls, approval decisions, artifacts and event replay."""

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select

from .config import Settings
from .db import Database
from .definitions import get, identifier, view
from .errors import DomainError
from .ledger import (
    RUN_TERMINAL,
    clock,
    control,
    create_run,
    emit,
    event_view,
    idempotent,
    instant,
    lock_run,
    run_view,
)
from .models import Approval, Artifact, Audit, Event, Execution, Node, Run, Worker
from .privacy import canonical
from .routes_definitions import page
from .schemas import DecisionInput, DecomposeInput, RunInput


def build_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api")

    def public_run(session: Any, record: Run) -> dict[str, Any]:
        result = run_view(session, record)
        trace_id = record.snapshot.get("trace_id", "")
        url = settings.langfuse_url
        if (
            url
            and re.fullmatch("[0-9a-f]{32}", trace_id)
            and re.fullmatch("[A-Za-z0-9_-]{1,128}", settings.langfuse_project_id)
        ):
            parsed = urlsplit(url)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            ):
                result["trace_url"] = (
                    url.rstrip("/")
                    + "/project/"
                    + settings.langfuse_project_id
                    + "/traces/"
                    + trace_id
                )
        return result

    @router.post("/runs", status_code=201)
    def start_run(
        body: RunInput, idempotency_key: str | None = Header(None)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            return idempotent(
                session,
                idempotency_key,
                "admin:/api/runs",
                body.model_dump(),
                lambda: public_run(session, create_run(session, body)),
            )

    @router.post("/tasks/{task_id}/decompose", status_code=202)
    def decompose(
        task_id: str, body: DecomposeInput, idempotency_key: str | None = Header(None)
    ) -> dict[str, Any]:
        data = RunInput(task_id=task_id, **body.model_dump())
        with database.transaction() as session:
            return idempotent(
                session,
                idempotency_key,
                "admin:/api/tasks/" + task_id + "/decompose",
                body.model_dump(),
                lambda: public_run(session, create_run(session, data, "planning")),
            )

    @router.get("/runs")
    def runs(
        project_id: str | None = None,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        with database.transaction() as session:
            query = select(Run).order_by(Run.id).limit(limit + 1)
            if project_id:
                query = query.where(Run.project_id == identifier(project_id))
            if status:
                query = query.where(Run.status == status)
            if cursor:
                query = query.where(Run.id > identifier(cursor))
            return page(
                [public_run(session, row) for row in session.scalars(query)], limit
            )

    @router.get("/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            return public_run(session, get(session, Run, identifier(run_id)))

    def control_route(action: str) -> None:
        @router.post("/runs/{run_id}/" + action, status_code=202, name="run_" + action)
        def command(
            run_id: str, idempotency_key: str | None = Header(None)
        ) -> dict[str, Any]:
            with database.transaction() as session:
                return idempotent(
                    session,
                    idempotency_key,
                    "admin:/api/runs/" + run_id + "/" + action,
                    {},
                    lambda: control(session, lock_run(session, run_id), action),
                )

    for action in ["pause", "resume", "cancel"]:
        control_route(action)

    @router.get("/runs/{run_id}/nodes")
    def nodes(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            get(session, Run, identifier(run_id))
            return {
                "items": [
                    view(row)
                    for row in session.scalars(
                        select(Node).where(Node.run_id == run_id)
                    )
                ],
                "next_cursor": None,
            }

    @router.get("/runs/{run_id}/executions")
    def executions(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            return {
                "items": run_view(session, get(session, Run, identifier(run_id)))[
                    "executions"
                ],
                "next_cursor": None,
            }

    @router.get("/executions/{execution_id}")
    def execution(execution_id: str) -> dict[str, Any]:
        from .ledger import execution_view

        with database.transaction() as session:
            return execution_view(get(session, Execution, identifier(execution_id)))

    @router.get("/runs/{run_id}/approvals")
    def approvals(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            get(session, Run, identifier(run_id))
            return {
                "items": [
                    view(row)
                    for row in session.scalars(
                        select(Approval).where(Approval.run_id == run_id)
                    )
                ],
                "next_cursor": None,
            }

    @router.post("/approvals/{approval_id}/decision")
    def decide(
        approval_id: str,
        body: DecisionInput,
        idempotency_key: str | None = Header(None),
    ) -> dict[str, Any]:
        with database.transaction() as session:
            approval = get(session, Approval, identifier(approval_id))
            run = lock_run(session, approval.run_id)
            approval = get(session, Approval, approval_id, lock=True)

            def operation() -> dict[str, Any]:
                if (
                    approval.decision is not None
                    or approval.revision != body.revision
                    or approval.snapshot_hash != body.snapshot_hash
                ):
                    raise DomainError(
                        "VERSION_CONFLICT",
                        "Approval is decided or its exact snapshot/revision changed.",
                    )
                if (
                    approval.snapshot.get("plan_hash") != run.snapshot.get("plan_hash")
                    or approval.snapshot.get("graph_hash")
                    != run.snapshot.get("graph_hash")
                    or approval.snapshot.get("root_base_sha")
                    != run.snapshot.get("root_base_sha")
                ):
                    raise DomainError(
                        "SNAPSHOT_STALE", "Approval snapshot no longer matches the run."
                    )
                if (
                    instant(approval.expires_at) <= clock(session)
                    or run.cancel_requested
                    or run.status in RUN_TERMINAL
                ):
                    raise DomainError(
                        "APPROVAL_EXPIRED",
                        "Approval expired or the run cannot accept a decision.",
                    )
                approval.decision, approval.decided_at, approval.revision = (
                    body.decision,
                    clock(session).isoformat(),
                    approval.revision + 1,
                )
                session.add(
                    Audit(
                        actor="admin",
                        action="approval." + body.decision,
                        resource_id=approval.id,
                        payload={"snapshot_hash": body.snapshot_hash},
                    )
                )
                emit(
                    session,
                    run,
                    "approval.decided",
                    {"approval_id": approval.id, "decision": body.decision},
                )
                return view(approval)

            return idempotent(
                session,
                idempotency_key,
                "admin:/api/approvals/" + approval_id,
                body.model_dump(),
                operation,
            )

    @router.get("/runs/{run_id}/artifacts")
    def artifacts(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            get(session, Run, identifier(run_id))
            return {
                "items": [
                    view(row)
                    for row in session.scalars(
                        select(Artifact).where(Artifact.run_id == run_id)
                    )
                ],
                "next_cursor": None,
            }

    @router.get("/artifacts/{artifact_id}/content")
    def artifact_content(artifact_id: str) -> FileResponse:
        with database.transaction() as session:
            artifact = get(session, Artifact, identifier(artifact_id))
            path = settings.artifact_root / artifact.storage_key
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != settings.artifact_root.resolve()
            ):
                raise DomainError(
                    "ARTIFACT_UNAVAILABLE", "Artifact is missing or unsafe.", 404
                )
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
                raise DomainError(
                    "ARTIFACT_CORRUPT", "Artifact integrity check failed.", 409
                )
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=artifact.id + ".artifact",
                headers={
                    "X-Content-Type-Options": "nosniff",
                    "X-Artifact-SHA256": artifact.sha256,
                },
            )

    @router.get("/runs/{run_id}/git-changes")
    def git_changes(run_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            run = get(session, Run, identifier(run_id))
            items = []
            for execution in session.scalars(
                select(Execution).where(Execution.run_id == run_id)
            ):
                if execution.result.get("commit_sha") or execution.result.get(
                    "conflict_paths"
                ):
                    items.append(
                        {
                            "id": execution.id,
                            "execution_id": execution.id,
                            "operation": execution.operation,
                            "status": execution.status,
                            **execution.result,
                        }
                    )
            return {
                "items": items,
                "next_cursor": None,
                "integration_status": run.integration_status,
            }

    @router.get("/runs/{run_id}/event-history")
    def event_history(
        run_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=200)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            get(session, Run, identifier(run_id))
            rows = list(
                session.scalars(
                    select(Event)
                    .where(Event.run_id == run_id, Event.run_sequence > after)
                    .order_by(Event.run_sequence)
                    .limit(limit + 1)
                )
            )
            return {
                "items": [event_view(row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1].run_sequence
                if len(rows) > limit
                else None,
            }

    @router.get("/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(None),
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        with database.transaction() as session:
            run = get(session, Run, identifier(run_id))
            try:
                cursor = int(last_event_id) if last_event_id is not None else after
            except ValueError as exc:
                raise DomainError(
                    "INVALID_CURSOR",
                    "Last-Event-ID must be a persisted integer sequence.",
                    422,
                ) from exc
            if cursor < 0 or cursor > run.event_sequence:
                raise DomainError(
                    "CURSOR_EXPIRED",
                    "Event cursor cannot be replayed; reload the snapshot.",
                    409,
                )

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                with database.transaction() as session:
                    run = get(session, Run, run_id)
                    rows = list(
                        session.scalars(
                            select(Event)
                            .where(Event.run_id == run_id, Event.run_sequence > cursor)
                            .order_by(Event.run_sequence)
                            .limit(100)
                        )
                    )
                    terminal = run.status in RUN_TERMINAL
                for row in rows:
                    cursor = row.run_sequence
                    yield (
                        "id: "
                        + str(cursor)
                        + "\nevent: "
                        + row.type
                        + "\ndata: "
                        + canonical(event_view(row))
                        + "\n\n"
                    )
                if terminal and not rows:
                    break
                heartbeat += 1
                if heartbeat >= 30:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/providers/health")
    def providers() -> dict[str, Any]:
        with database.transaction() as session:
            workers = list(session.scalars(select(Worker)))
            return {
                "items": [
                    {
                        "worker_id": worker.id,
                        "last_heartbeat": worker.last_heartbeat,
                        "capabilities": worker.capabilities,
                        "online": (
                            clock(session) - instant(worker.last_heartbeat)
                        ).total_seconds()
                        < 60,
                    }
                    for worker in workers
                ],
                "next_cursor": None,
            }

    @router.get("/metrics/overview")
    def metrics() -> dict[str, Any]:
        with database.transaction() as session:
            runs = {
                status: count
                for status, count in session.execute(
                    select(Run.status, func.count()).group_by(Run.status)
                )
            }
            executions = {
                status: count
                for status, count in session.execute(
                    select(Execution.status, func.count()).group_by(Execution.status)
                )
            }
            usage_rows = [
                row.result.get("usage", {})
                for row in session.scalars(select(Execution))
                if row.operation in {"agent_task", "planner", "review"}
                and row.status in {"succeeded", "failed", "cancelled"}
            ]
            usage: dict[str, Any] = {
                "source": "provider",
                "is_complete": bool(usage_rows),
                "execution_count": len(usage_rows),
            }
            for field in ["input_tokens", "output_tokens", "cost"]:
                known = [
                    row[field]
                    for row in usage_rows
                    if isinstance(row.get(field), (int, float))
                    and not isinstance(row[field], bool)
                    and row[field] >= 0
                ]
                usage[field] = sum(known) if known else None
                usage[field + "_known_count"] = len(known)
                usage["is_complete"] = usage["is_complete"] and len(known) == len(
                    usage_rows
                )
            return {
                "runs": runs,
                "executions": executions,
                "queue_depth": executions.get("created", 0),
                "active_executions": sum(
                    executions.get(status, 0)
                    for status in [
                        "leased",
                        "running",
                        "starting",
                        "validating",
                        "committing",
                    ]
                ),
                "usage": usage,
            }

    @router.get("/settings")
    def public_settings() -> dict[str, Any]:
        return {
            "allowed_roots": list(settings.allowed_roots),
            "telemetry_enabled": settings.telemetry_enabled,
            "langfuse_url": settings.langfuse_url,
            "authentication": "bearer_in_memory",
            "runtime_ready": False,
            "runtime_readiness_source": "umbrella acceptance evidence",
        }

    return router
