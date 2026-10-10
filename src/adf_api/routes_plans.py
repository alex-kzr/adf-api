"""Human plan revisions and exact integration-resolution approvals."""

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Header

from .config import Settings
from .db import Database
from .definitions import get, identifier, view
from .errors import DomainError
from .ledger import (
    clock,
    conflicted_integration,
    emit,
    idempotent,
    instant,
    lock_run,
    resolution_bindings,
    run_view,
)
from .models import Approval, Command, Execution, uid
from .plans import save_plan
from .privacy import digest


def build_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.patch("/runs/{run_id}/plan")
    def edit_plan(
        run_id: str, body: dict[str, Any], idempotency_key: str | None = Header(None)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)

            def operation() -> dict[str, Any]:
                if body.get("revision") != run.revision:
                    raise DomainError("VERSION_CONFLICT", "Run revision changed.")
                save_plan(session, run, body)
                session.add(
                    Command(
                        id=uid(),
                        run_id=run.id,
                        kind="refresh_proposal",
                        data={"plan_hash": run.snapshot["plan_hash"]},
                    )
                )
                return run_view(session, run)

            return idempotent(
                session,
                idempotency_key,
                "admin:/api/runs/" + run_id + "/plan",
                body,
                operation,
            )

    @router.post("/runs/{run_id}/resolution", status_code=201)
    def resolution(
        run_id: str, body: dict[str, Any], idempotency_key: str | None = Header(None)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)

            def operation() -> dict[str, Any]:
                if (
                    run.status != "paused"
                    or run.integration_status != "needs_resolution"
                ):
                    raise DomainError(
                        "INVALID_STATE",
                        "Resolution requires a paused integration conflict.",
                    )
                execution = get(
                    session, Execution, identifier(body.get("execution_id", ""))
                )
                tree_sha = body.get("tree_sha", "")
                if (
                    not conflicted_integration(execution, run)
                    or not isinstance(tree_sha, str)
                    or not re.fullmatch("[0-9a-f]{40}", tree_sha)
                ):
                    raise DomainError(
                        "INVALID_RESOLUTION",
                        "Resolution must bind the conflicted integration execution and exact Git tree.",
                        422,
                    )
                snapshot = resolution_bindings(run, execution.id, tree_sha)
                approval = Approval(
                    id=uid(),
                    run_id=run.id,
                    action="integration_resolution",
                    snapshot=snapshot,
                    snapshot_hash=digest(snapshot),
                    expires_at=min(
                        clock(session) + timedelta(hours=1),
                        instant(run.snapshot["deadline_at"]),
                    ).isoformat(),
                )
                session.add(approval)
                run.snapshot = {
                    **run.snapshot,
                    "resolution": {
                        "execution_id": execution.id,
                        "tree_sha": tree_sha,
                        "approval_id": approval.id,
                    },
                }
                run.revision += 1
                session.flush()
                emit(
                    session,
                    run,
                    "approval.requested",
                    {
                        "approval_id": approval.id,
                        "action": approval.action,
                        "snapshot_hash": approval.snapshot_hash,
                    },
                )
                return view(approval)

            return idempotent(
                session,
                idempotency_key,
                "admin:/api/runs/" + run_id + "/resolution",
                body,
                operation,
            )

    return router
