"""Fenced worker mutations, bounded artifact ingestion and supervisor reconciliation."""

import base64
import hashlib
import os
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Database
from .definitions import get, identifier, view
from .errors import DomainError
from .evidence import successful_result
from .ledger import (
    TERMINAL,
    clock,
    emit,
    event_view,
    instant,
    lease,
    lock_run,
)
from .models import Artifact, Audit, Event, Execution, Run, Worker, uid
from .privacy import canonical, redact
from .schemas import (
    ArtifactInput,
    EventBatch,
    LeaseInput,
    ReconcileInput,
    ResultInput,
    WorkerInput,
)


def build_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/internal")
    secrets = (settings.admin_token, settings.orchestrator_token, settings.worker_token)

    def records(session: Session, execution_id: str) -> tuple[Run, Execution]:
        execution = get(session, Execution, identifier(execution_id))
        run = lock_run(session, execution.run_id)
        return run, get(session, Execution, execution_id, lock=True)

    @router.post("/workers/register")
    def register(body: WorkerInput) -> dict[str, Any]:
        if any(root not in settings.allowed_roots for root in body.allowed_roots):
            raise DomainError(
                "ROOT_DENIED", "Worker roots exceed the server-configured roots.", 403
            )
        if settings.worker_project_ids and set(body.project_ids) - set(
            settings.worker_project_ids
        ):
            raise DomainError(
                "PROJECT_DENIED",
                "Worker projects exceed token-configured project scope.",
                403,
            )
        with database.transaction() as session:
            row = session.get(Worker, body.worker_id)
            if row is None:
                row = Worker(
                    id=body.worker_id,
                    capabilities=redact(body.capabilities, secrets),
                    allowed_roots=body.allowed_roots,
                    project_ids=body.project_ids,
                    last_heartbeat=clock(session).isoformat(),
                )
                session.add(row)
            else:
                if (
                    row.allowed_roots != body.allowed_roots
                    or row.project_ids != body.project_ids
                ):
                    raise DomainError(
                        "WORKER_SCOPE_CHANGED",
                        "A registered worker identity cannot broaden its scope.",
                        403,
                    )
                row.capabilities, row.last_heartbeat = (
                    redact(body.capabilities, secrets),
                    clock(session).isoformat(),
                )
            session.flush()
            return view(row)

    @router.post("/workers/{worker_id}/heartbeat")
    def worker_heartbeat(worker_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Worker, worker_id, lock=True)
            running = body.get("running_executions", [])
            if not isinstance(running, list) or len(running) > 3:
                raise DomainError(
                    "INVALID_HEARTBEAT",
                    "Worker heartbeat has an invalid execution list.",
                    422,
                )
            row.last_heartbeat, row.running_executions = (
                clock(session).isoformat(),
                running,
            )
            return {"worker_id": worker_id, "last_heartbeat": row.last_heartbeat}

    @router.post("/executions/{execution_id}/claim")
    def claim(execution_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            worker_id = body.get("worker_id", "")
            worker = get(session, Worker, worker_id)
            project = execution.request["project"]
            if (
                project["allowed_root"] not in worker.allowed_roots
                or (worker.project_ids and run.project_id not in worker.project_ids)
                or (
                    settings.worker_project_ids
                    and run.project_id not in settings.worker_project_ids
                )
            ):
                raise DomainError(
                    "WORKER_SCOPE_DENIED",
                    "This worker cannot execute the project.",
                    403,
                )
            if execution.status in TERMINAL:
                return {
                    "execution_id": execution.id,
                    "epoch": execution.epoch,
                    "status": execution.status,
                    "terminal": True,
                    "result": execution.result,
                    "request": execution.request,
                    "lease_expires_at": execution.lease_expires_at,
                }
            if execution.status == "uncertain":
                raise DomainError(
                    "EXECUTION_UNCERTAIN",
                    "Reconcile the previous supervisor before reassignment.",
                )
            if execution.status != "created":
                if execution.worker_id != worker_id:
                    raise DomainError(
                        "LEASE_OWNED", "Execution is already owned by another worker."
                    )
                lease(session, run, execution, worker_id, execution.epoch)
            else:
                if run.cancel_requested:
                    execution.status, execution.result = (
                        "cancelled",
                        {"reason": "CANCEL_BEFORE_CLAIM", "process_stopped": True},
                    )
                    emit(
                        session,
                        run,
                        "execution.cancelled",
                        {"execution_id": execution.id},
                    )
                    return {
                        "execution_id": execution.id,
                        "epoch": 0,
                        "status": "cancelled",
                        "terminal": True,
                        "request": execution.request,
                        "result": execution.result,
                    }
                if run.pause_requested or run.status == "paused":
                    raise DomainError(
                        "CONTROL_PENDING", "Pause prevents new execution claims."
                    )
                execution.worker_id, execution.epoch, execution.status = (
                    worker_id,
                    execution.epoch + 1,
                    "leased",
                )
                execution.lease_expires_at = (
                    clock(session) + timedelta(seconds=settings.lease_seconds)
                ).isoformat()
                execution.request = {
                    **execution.request,
                    "lease_epoch": execution.epoch,
                    "deadline_at": min(
                        instant(run.snapshot["deadline_at"]),
                        clock(session)
                        + timedelta(seconds=execution.request["timeout_sec"]),
                    ).isoformat(),
                }
                emit(
                    session,
                    run,
                    "execution.leased",
                    {
                        "execution_id": execution.id,
                        "worker_id": worker_id,
                        "epoch": execution.epoch,
                    },
                )
            return {
                "execution_id": execution.id,
                "epoch": execution.epoch,
                "status": execution.status,
                "terminal": False,
                "request": execution.request,
                "lease_expires_at": execution.lease_expires_at,
            }

    @router.post("/executions/{execution_id}/heartbeat")
    def heartbeat(execution_id: str, body: LeaseInput) -> dict[str, Any]:
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            lease(session, run, execution, body.worker_id, body.epoch)
            execution.lease_expires_at = (
                clock(session) + timedelta(seconds=settings.lease_seconds)
            ).isoformat()
            if body.process_identity:
                if (
                    execution.process_identity
                    and execution.process_identity != body.process_identity
                ):
                    raise DomainError(
                        "PROCESS_IDENTITY_CHANGED",
                        "Supervisor process identity cannot change in the same lease.",
                    )
                execution.process_identity = redact(body.process_identity, secrets)
            if execution.status == "leased":
                execution.status = "running"
            return {
                "lease_expires_at": execution.lease_expires_at,
                "cancel_requested": run.cancel_requested,
                "pause_requested": run.pause_requested,
                "status": execution.status,
            }

    @router.get("/executions/{execution_id}/control")
    def execution_control(
        execution_id: str, worker_id: str, epoch: int = Query(ge=1)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            lease(session, run, execution, worker_id, epoch, allow_terminal=True)
            return {
                "status": execution.status,
                "pause_requested": run.pause_requested,
                "cancel_requested": run.cancel_requested
                or execution.status == "uncertain",
                "lease_expires_at": execution.lease_expires_at,
            }

    @router.post("/executions/{execution_id}/events")
    def events(execution_id: str, body: EventBatch) -> dict[str, Any]:
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            lease(
                session, run, execution, body.worker_id, body.epoch, allow_terminal=True
            )
            result = []
            for item in body.events:
                event = emit(
                    session,
                    run,
                    item.type,
                    item.payload,
                    event_id=item.event_id,
                    execution=execution,
                    sequence=item.sequence,
                    secrets=secrets,
                )
                result.append(event_view(event))
            output_bytes = sum(
                len(canonical(event.payload).encode())
                for event in session.scalars(
                    select(Event).where(Event.execution_id == execution.id)
                )
            )
            if output_bytes > execution.request["max_output_bytes"]:
                raise DomainError(
                    "QUOTA_EXCEEDED", "Execution output quota exceeded.", 413
                )
            execution.output_bytes = output_bytes
            return {"items": result, "persisted": True}

    @router.post("/executions/{execution_id}/result")
    def result(execution_id: str, body: ResultInput) -> dict[str, Any]:
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            lease(
                session,
                run,
                execution,
                body.worker_id,
                body.epoch,
                allow_terminal=True,
                allow_expired=body.status in {"failed", "cancelled", "uncertain"},
            )
            payload = redact(body.result, secrets)
            status = (
                "cancelled"
                if run.cancel_requested and body.status == "succeeded"
                else body.status
            )
            if execution.status in TERMINAL:
                if execution.status != status or execution.result != payload:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT",
                        "Terminal execution result is immutable.",
                    )
                return {
                    "persisted": True,
                    "terminal": True,
                    "execution_id": execution.id,
                    "status": execution.status,
                }
            if (
                status in {"cancelled", "failed"}
                and execution.process_identity
                and payload.get("process_stopped") is not True
            ):
                raise DomainError(
                    "PROCESS_PROOF_REQUIRED",
                    "A spawned process must be confirmed stopped before terminal failure/cancellation.",
                )
            if status == "succeeded":
                successful_result(session, run, execution, payload)
            execution.status, execution.result, execution.revision = (
                status,
                payload,
                execution.revision + 1,
            )
            session.add(
                Audit(
                    actor=body.worker_id,
                    action="execution." + status,
                    resource_id=execution.id,
                    payload={
                        "epoch": execution.epoch,
                        "result_hash": hashlib.sha256(
                            canonical(payload).encode()
                        ).hexdigest(),
                    },
                )
            )
            emit(
                session,
                run,
                "execution." + status,
                {
                    "execution_id": execution.id,
                    "status": status,
                    "commit_sha": payload.get("commit_sha"),
                    "error_code": payload.get("error_code"),
                },
            )
            return {
                "persisted": True,
                "terminal": status in TERMINAL,
                "execution_id": execution.id,
                "status": status,
            }

    @router.post("/executions/{execution_id}/reconcile")
    def reconcile(execution_id: str, body: ReconcileInput) -> dict[str, Any]:
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            lease(
                session,
                run,
                execution,
                body.worker_id,
                body.epoch,
                allow_expired=True,
                allow_terminal=True,
            )
            proof = body.evidence
            if (
                not body.process_stopped
                or not proof.get("journal_id")
                or proof.get("worktree_lock_reacquired") is not True
            ):
                raise DomainError(
                    "PROCESS_PROOF_REQUIRED",
                    "Reconciliation requires a bound supervisor journal and reacquired worktree lock.",
                )
            identity = execution.process_identity
            if identity and any(
                proof.get(key) != identity.get(key)
                for key in ["pid", "start_ticks", "pgid", "session_id", "journal_id"]
            ):
                raise DomainError(
                    "PROCESS_IDENTITY_CHANGED",
                    "Stopped proof does not match the recorded process identity.",
                )
            if identity and proof.get("child_group_stopped") is not True:
                raise DomainError(
                    "PROCESS_PROOF_REQUIRED",
                    "Every child process group must be confirmed stopped.",
                )
            if execution.status in TERMINAL:
                return {"persisted": True, "terminal": True, "status": execution.status}
            execution.status = "cancelled" if run.cancel_requested else "failed"
            execution.result = {
                "reason": "SUPERVISOR_RECONCILED",
                "process_stopped": True,
                "reconciliation": redact(proof, secrets),
            }
            emit(
                session,
                run,
                "execution.reconciled",
                {"execution_id": execution.id, "status": execution.status},
            )
            return {"persisted": True, "terminal": True, "status": execution.status}

    @router.post("/executions/{execution_id}/artifacts", status_code=201)
    def artifact(execution_id: str, body: ArtifactInput) -> dict[str, Any]:
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except ValueError as exc:
            raise DomainError(
                "INVALID_ARTIFACT", "Artifact is not valid base64.", 422
            ) from exc
        if len(content) > 20971520:
            raise DomainError("QUOTA_EXCEEDED", "Artifact exceeds 20 MiB.", 413)
        checksum = hashlib.sha256(content).hexdigest()
        if (body.checksum or body.sha256) and (
            body.checksum or body.sha256
        ) != checksum:
            raise DomainError(
                "CHECKSUM_MISMATCH",
                "Artifact checksum does not match its content.",
                422,
            )
        if body.media_type not in {"text/plain", "text/x-diff", "application/json"}:
            raise DomainError(
                "ARTIFACT_TYPE_DENIED",
                "Worker artifact ingestion accepts UTF-8 text, diffs and JSON only.",
                422,
            )
        try:
            content = redact(content.decode("utf-8"), secrets).encode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError(
                "INVALID_ARTIFACT", "Artifact must contain UTF-8 text.", 422
            ) from exc
        checksum = hashlib.sha256(content).hexdigest()
        artifact_id = identifier(body.artifact_id) if body.artifact_id else uid()
        with database.transaction() as session:
            run, execution = records(session, execution_id)
            lease(
                session, run, execution, body.worker_id, body.epoch, allow_terminal=True
            )
            previous = session.get(Artifact, artifact_id)
            if previous:
                if (
                    previous.execution_id != execution.id
                    or previous.sha256 != checksum
                    or previous.kind != body.kind
                ):
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT",
                        "Artifact identity was reused with different content.",
                    )
                return view(previous)
            total = sum(
                row.size
                for row in session.scalars(
                    select(Artifact).where(Artifact.run_id == run.id)
                )
            )
            if total + len(content) > run.snapshot["policy"]["max_disk_bytes"]:
                raise DomainError("QUOTA_EXCEEDED", "Run artifact quota exceeded.", 413)
            settings.artifact_root.mkdir(parents=True, exist_ok=True)
            root = settings.artifact_root.resolve()
            key = uid()
            temporary = root / (key + ".tmp")
            destination = root / key
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
            row = Artifact(
                id=artifact_id,
                run_id=run.id,
                execution_id=execution.id,
                kind=body.kind,
                media_type=body.media_type,
                size=len(content),
                sha256=checksum,
                storage_key=key,
            )
            session.add(row)
            session.flush()
            emit(
                session,
                run,
                "artifact.created",
                {"artifact_id": row.id, "kind": row.kind, "sha256": checksum},
            )
            return view(row)

    return router
