"""Durable tracing, graph proposals and sanitized transport dead letters."""

import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter

from .db import Database
from .definitions import Validator, get
from .errors import DomainError
from .ledger import lock_run
from .models import Audit, Worker


def build_router(database: Database, validator: Validator) -> APIRouter:
    router = APIRouter(prefix="/internal")

    @router.post("/runs/{run_id}/trace")
    def trace_context(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        trace_id, parent = body.get("trace_id", ""), body.get("traceparent", "")
        if (
            not isinstance(trace_id, str)
            or not re.fullmatch("[0-9a-f]{32}", trace_id)
            or trace_id == "0" * 32
            or not isinstance(parent, str)
            or not re.fullmatch("00-" + trace_id + "-[0-9a-f]{16}-01", parent)
            or parent[36:52] == "0" * 16
        ):
            raise DomainError(
                "INVALID_TRACE",
                "Tracing requires a valid nonzero W3C trace context.",
                422,
            )
        with database.transaction() as session:
            run = lock_run(session, run_id)
            if not run.snapshot.get("trace_id"):
                run.snapshot = {
                    **run.snapshot,
                    "trace_id": trace_id,
                    "traceparent": parent,
                }
            return {
                "trace_id": run.snapshot["trace_id"],
                "traceparent": run.snapshot["traceparent"],
            }

    @router.post("/runs/{run_id}/graph-proposal")
    def graph_proposal(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with database.transaction() as session:
            run = lock_run(session, run_id)
            if body.get("plan_hash") != run.snapshot.get("plan_hash"):
                raise DomainError(
                    "SNAPSHOT_STALE", "The graph proposal must bind the current plan."
                )
            graph = body.get("graph_json")
            if not isinstance(graph, dict):
                raise DomainError(
                    "INVALID_WORKFLOW",
                    "Graph proposal must be a declarative object.",
                    422,
                )
            if not validator.validate(graph)["valid"]:
                raise DomainError(
                    "INVALID_WORKFLOW",
                    "Graph proposal failed semantic validation.",
                    422,
                )
            previous = run.result.get("graph_proposal")
            if previous and previous != graph:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT",
                    "A different proposal is already persisted for this plan.",
                )
            run.result = {
                **run.result,
                "graph_proposal": graph,
                "graph_proposal_plan_hash": run.snapshot["plan_hash"],
            }
            return {"plan_hash": run.snapshot["plan_hash"], "graph_json": graph}

    @router.post("/workers/{worker_id}/dead-letter", status_code=201)
    def dead_letter(worker_id: str, body: dict[str, Any]) -> dict[str, Any]:
        message_id = body.get("message_id", "")
        fields = body.get("field_names", [])
        if (
            body.get("stream") != "adf:executions"
            or not isinstance(message_id, str)
            or not re.fullmatch("[0-9]{1,20}-[0-9]{1,20}", message_id)
            or not isinstance(fields, list)
            or len(fields) > 30
            or any(
                not isinstance(value, str)
                or not re.fullmatch("[A-Za-z0-9_-]{1,64}", value)
                for value in fields
            )
        ):
            raise DomainError(
                "INVALID_DEAD_LETTER",
                "Dead letters contain only bounded stream/message identities and field names.",
                422,
            )
        # Never store attacker-supplied field values or exception text.
        payload = {
            "message_id": message_id,
            "stream": "adf:executions",
            "reason": "INVALID_TRANSPORT",
            "field_names": fields,
        }
        marker_id = str(
            uuid5(NAMESPACE_URL, "adf:dead-letter:" + worker_id + ":" + message_id)
        )
        with database.transaction() as session:
            get(session, Worker, worker_id)
            previous = session.get(Audit, marker_id)
            if previous and previous.payload != payload:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "Dead-letter identity changed."
                )
            if previous is None:
                session.add(
                    Audit(
                        id=marker_id,
                        actor=worker_id,
                        action="transport.dead_letter",
                        resource_id=message_id,
                        payload=payload,
                    )
                )
            return {"persisted": True, "marker_id": marker_id}

    return router
