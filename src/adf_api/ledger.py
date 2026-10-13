"""Transactional command, approval, execution and delivery state machine."""

import fnmatch
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .definitions import get, identifier, registry_snapshots, view
from .errors import DomainError
from .evidence import full_sha, passing_checks, target_evidence
from .models import (
    Approval,
    Audit,
    Command,
    ConfigVersion,
    Definition,
    Event,
    Execution,
    Idempotency,
    Node,
    Outbox,
    Project,
    Run,
    Task,
    WorkflowVersion,
    uid,
)
from .privacy import canonical, digest, redact
from .schemas import ExecutionInput, ExecutionRequest, Limits, RunInput
from .tool_assignments import validate_tool_assignments

TERMINAL = {"succeeded", "failed", "cancelled"}
RUN_TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {
    "created",
    "leased",
    "starting",
    "running",
    "validating",
    "committing",
    "cancelling",
    "uncertain",
}
RUN_TRANSITIONS = {
    "queued": {"running", "failed", "cancelled"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "failed", "cancelled"},
}


def clock(session: Session) -> datetime:
    value = session.scalar(select(func.current_timestamp()))
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is None:
        raise DomainError("CLOCK_UNAVAILABLE", "Database clock is unavailable.", 503)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DomainError(
            "INVALID_TIME", "A valid timestamp is required.", 422
        ) from exc
    if result.tzinfo is None:
        raise DomainError(
            "INVALID_TIME",
            "A UTC timestamp with an explicit timezone is required.",
            422,
        )
    return result.astimezone(UTC)


def idempotent(
    session: Session,
    key: str | None,
    scope: str,
    body: Any,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if not key or len(key) > 128:
        raise DomainError(
            "IDEMPOTENCY_REQUIRED", "A bounded Idempotency-Key header is required.", 422
        )
    storage_key, request_hash = scope + ":" + key, digest(body)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        lock = int(digest(storage_key)[:15], 16)
        session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
    previous = session.get(Idempotency, storage_key)
    if previous:
        if previous.request_hash != request_hash:
            raise DomainError(
                "IDEMPOTENCY_CONFLICT",
                "This key has already been used with a different request.",
            )
        return previous.response
    response = operation()
    session.add(
        Idempotency(key=storage_key, request_hash=request_hash, response=response)
    )
    return response


def lock_run(session: Session, run_id: str) -> Run:
    run = get(session, Run, identifier(run_id))
    get(session, Project, run.project_id, lock=True)
    return get(session, Run, run_id, lock=True)


def emit(
    session: Session,
    run: Run,
    kind: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    execution: Execution | None = None,
    sequence: int | None = None,
    secrets: tuple[str, ...] = (),
) -> Event:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", kind):
        raise DomainError(
            "INVALID_EVENT", "Event type contains invalid characters.", 422
        )
    event_id = identifier(event_id) if event_id else uid()
    payload = redact(payload, secrets)
    if len(canonical(payload).encode()) > 60000:
        raise DomainError(
            "QUOTA_EXCEEDED",
            "Event payload exceeds the envelope-safe 60,000-byte budget.",
            413,
        )
    previous = session.get(Event, event_id)
    if previous:
        if (
            previous.run_id != run.id
            or previous.type != kind
            or previous.payload != payload
        ):
            raise DomainError(
                "IDEMPOTENCY_CONFLICT",
                "An event identity was reused with different content.",
            )
        return previous
    if execution and sequence is not None:
        previous = session.scalar(
            select(Event).where(
                Event.execution_id == execution.id,
                Event.epoch == execution.epoch,
                Event.sequence == sequence,
            )
        )
        if previous:
            if (
                previous.type != kind
                or previous.payload != payload
                or previous.id != event_id
            ):
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT",
                    "An execution sequence was reused with different content.",
                )
            return previous
    run.event_sequence += 1
    event = Event(
        id=event_id,
        run_id=run.id,
        run_sequence=run.event_sequence,
        execution_id=execution.id if execution else None,
        epoch=execution.epoch if execution else None,
        sequence=sequence,
        type=kind,
        payload=payload,
    )
    session.add(event)
    session.flush()
    return event


def event_view(event: Event) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "event_id": event.id,
        "run_id": event.run_id,
        "run_sequence": event.run_sequence,
        "execution_id": event.execution_id,
        "epoch": event.epoch,
        "sequence": event.sequence,
        "type": event.type,
        "time": event.created_at,
        "payload": event.payload,
    }


def run_view(session: Session, run: Run, *, internal: bool = False) -> dict[str, Any]:
    result = view(run)
    result.update(run.snapshot)
    result["thread_id"] = run.id
    result["control"] = {
        "pause_requested": run.pause_requested,
        "cancel_requested": run.cancel_requested,
    }
    result["nodes"] = [
        view(row) for row in session.scalars(select(Node).where(Node.run_id == run.id))
    ]
    result["executions"] = [
        execution_view(row, internal=internal)
        for row in session.scalars(select(Execution).where(Execution.run_id == run.id))
    ]
    result["approvals"] = [
        view(row)
        for row in session.scalars(select(Approval).where(Approval.run_id == run.id))
    ]
    result.pop("snapshot", None)
    if not internal:
        result.pop("agents_by_role", None)
        result.pop("agent_versions", None)
    return result


def execution_view(execution: Execution, *, internal: bool = False) -> dict[str, Any]:
    result = view(execution)
    if not internal:
        request = result.pop("request")
        result["base_sha"], result["branch"] = (
            request.get("execution_base_sha"),
            request.get("git_branch"),
        )
        result["provider"] = request.get("agent", {}).get("provider")
    return result


def capability_audit(session: Session, execution: Execution) -> dict[str, Any]:
    """Project selected lease-bound metadata, never raw provider/tool payloads."""
    agent = execution.request.get("agent", {})
    hashes = {
        item.get("manifest", item).get("sha256", item.get("manifest", item).get("hash"))
        for item in agent.get("skills", [])
    }
    servers = {
        item.get("manifest", item).get("name", item.get("name")): set(
            item.get("manifest", item).get("allowed_tools", [])
        )
        for item in agent.get("mcp_servers", [])
    }
    rows = list(
        session.scalars(
            select(Event)
            .where(
                Event.execution_id == execution.id,
                Event.run_id == execution.run_id,
                Event.epoch == execution.epoch,
                Event.type.in_(["skill_loaded", "tool_finished"]),
            )
            .order_by(Event.sequence)
            .limit(129)
        )
    )
    evidence = []
    for event in rows[:128]:
        payload = event.payload
        fields: dict[str, Any] = {}
        if event.type == "skill_loaded":
            sha, name = payload.get("sha256"), payload.get("name")
            if (
                isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha)
                and sha in hashes
                and isinstance(name, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name)
                and type(payload.get("native")) is bool
            ):
                fields = {
                    "kind": "skill_loaded",
                    "name": name,
                    "sha256": sha,
                    "native": payload["native"],
                }
        elif (
            payload.get("tool_id") == "mcp_tool_call"
            and payload.get("status") == "completed"
        ):
            server, tool = payload.get("server_id"), payload.get("tool_name")
            if (
                isinstance(server, str)
                and isinstance(tool, str)
                and tool in servers.get(server, set())
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", server)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", tool)
            ):
                fields = {
                    "kind": "mcp_tool_call",
                    "server_id": server,
                    "tool_name": tool,
                    "status": "completed",
                }
        if fields:
            evidence.append(
                {"event_id": event.id, "sequence": event.sequence, **fields}
            )
    return {
        "execution_id": execution.id,
        "run_id": execution.run_id,
        "epoch": execution.epoch,
        "tree_sha": execution.result.get("tree_sha"),
        "truncated": len(rows) > 128,
        "events": evidence,
    }


def pin_agent(session: Session, snapshot: dict[str, Any]) -> dict[str, Any]:
    registries = registry_snapshots(session, snapshot)
    validate_tool_assignments(
        snapshot["provider"], registries["tool_ids"], registries["mcp_server_ids"]
    )
    return {**snapshot, "registry_snapshots": registries}


def create_run(session: Session, data: RunInput, mode: str = "execution") -> Run:
    task = get(session, Task, identifier(data.task_id))
    project = get(session, Project, task.project_id, lock=True)
    task = get(session, Task, task.id, lock=True)
    if task.status in {"running", "done", "cancelled"}:
        raise DomainError(
            "INVALID_STATE", "The task cannot start a new run in its current state."
        )
    if session.scalar(
        select(Run).where(Run.task_id == task.id, Run.status.not_in(RUN_TERMINAL))
    ):
        raise DomainError("RUN_ACTIVE", "The task already has an active run.")
    workflow = (
        get(session, WorkflowVersion, identifier(data.workflow_version_id))
        if data.workflow_version_id
        else None
    )
    if mode == "execution" and workflow is None:
        raise DomainError(
            "WORKFLOW_REQUIRED",
            "Execution runs require a published workflow version.",
            422,
        )
    agent_versions = {}
    agents_by_role: dict[str, dict[str, Any]] = {}
    for definition in session.scalars(
        select(Definition).where(Definition.kind == "agents")
    ):
        version = get(session, ConfigVersion, definition.current_version_id)
        snapshot = pin_agent(
            session,
            {
                **version.data,
                "id": definition.id,
                "agent_id": definition.id,
                "config_version_id": version.id,
                "config_hash": version.config_hash,
            },
        )
        agent_versions[version.id] = snapshot
        agents_by_role.setdefault(version.data["role"], snapshot)
    selected_agent = agent_versions.get(data.agent_config_version_id or "")
    if data.agent_config_version_id and selected_agent is None:
        version = get(session, ConfigVersion, identifier(data.agent_config_version_id))
        definition = get(session, Definition, version.definition_id)
        if definition.kind != "agents":
            raise DomainError(
                "INVALID_REFERENCE", "Expected an immutable agent config version.", 422
            )
        selected_agent = pin_agent(
            session,
            {
                **version.data,
                "id": definition.id,
                "agent_id": definition.id,
                "config_version_id": version.id,
                "config_hash": version.config_hash,
            },
        )
        agent_versions[version.id] = selected_agent
    if selected_agent:
        agents_by_role[selected_agent["role"]] = selected_agent
    policy = {
        **Limits().model_dump(),
        "allowed_write_paths": ["**"],
        "allow_commit": False,
        "trusted_repository": False,
    }
    if data.policy_id:
        definition = get(session, Definition, identifier(data.policy_id))
        if definition.kind != "policies":
            raise DomainError("INVALID_REFERENCE", "Expected a policy definition.", 422)
        policy = {
            **policy,
            **definition.data["manifest"],
            "id": definition.id,
            "version_id": definition.current_version_id,
            "config_hash": digest(definition.data),
        }
    task_snapshots = {task.id: view(task)}
    planned_tasks = (data.plan or {}).get("tasks", [])
    if data.plan is not None and (
        not isinstance(planned_tasks, list)
        or not 1 <= len(planned_tasks) <= policy["max_tasks_per_plan"]
        or any(not isinstance(item, dict) for item in planned_tasks)
    ):
        raise DomainError(
            "INVALID_PLAN",
            "Execution plans require a bounded list of persisted tasks.",
            422,
        )
    planned_ids: set[str] = set()
    for planned in planned_tasks:
        planned_task = get(session, Task, identifier(planned.get("id", "")))
        if planned_task.project_id != project.id:
            raise DomainError(
                "INVALID_REFERENCE", "Plan tasks must belong to the run project.", 422
            )
        if (
            planned_task.id in planned_ids
            or planned_task.data.get("parent_id") != task.id
        ):
            raise DomainError(
                "INVALID_PLAN",
                "Plan tasks must be distinct children of this root task.",
                422,
            )
        planned_ids.add(planned_task.id)
        for field in [
            "title",
            "description",
            "acceptance_criteria",
            "expected_files",
            "dependency_ids",
        ]:
            if planned.get(field) != planned_task.data.get(field):
                raise DomainError(
                    "PLAN_STALE",
                    "Execution plan differs from its persisted task definition.",
                )
        task_snapshots[planned_task.id] = view(planned_task)
    for graph_node in (workflow.graph_json if workflow else {}).get("nodes", []):
        config = graph_node.get("config", {})
        if config.get("task_id"):
            bound = get(session, Task, identifier(config["task_id"]))
            if bound.project_id != project.id:
                raise DomainError(
                    "INVALID_REFERENCE",
                    "Workflow task belongs to another project.",
                    422,
                )
            task_snapshots[bound.id] = view(bound)
        config_id = config.get("agent_config_version_id")
        if config_id and config_id not in agent_versions:
            config_version = get(session, ConfigVersion, identifier(config_id))
            owner = get(session, Definition, config_version.definition_id)
            if owner.kind != "agents":
                raise DomainError(
                    "INVALID_REFERENCE",
                    "Workflow agent configuration reference has the wrong kind.",
                    422,
                )
            agent_versions[config_id] = pin_agent(
                session,
                {
                    **config_version.data,
                    "id": owner.id,
                    "agent_id": owner.id,
                    "config_version_id": config_id,
                    "config_hash": config_version.config_hash,
                },
            )
    external_dependencies = []
    completed_runs = list(
        session.scalars(
            select(Run).where(Run.project_id == project.id, Run.status == "completed")
        )
    )
    for dependency_id in task.data.get("dependency_ids", []):
        dependency = get(session, Task, identifier(dependency_id))
        sources = [
            source
            for source in completed_runs
            if dependency_id in source.snapshot.get("task_snapshots", {})
        ]
        sources.sort(key=lambda source: source.created_at, reverse=True)
        if dependency.status != "done" or not sources:
            raise DomainError(
                "DEPENDENCY_PENDING",
                "Manual task dependencies require approved completed integration before a run can start.",
            )
        integrations = list(
            session.scalars(
                select(Execution)
                .where(
                    Execution.run_id == sources[0].id,
                    Execution.operation == "integrate",
                    Execution.status == "succeeded",
                )
                .order_by(Execution.created_at.desc())
            )
        )
        if (
            not integrations
            or not full_sha(integrations[0].result.get("commit_sha"))
            or not integrations[0].request.get("integration_commits")
        ):
            raise DomainError(
                "DEPENDENCY_EVIDENCE_REQUIRED",
                "Dependency integration commits are unavailable.",
            )
        external_dependencies.append(
            {
                "task_id": dependency_id,
                "run_id": sources[0].id,
                "integration_commit_sha": integrations[0].result.get("commit_sha"),
                "commit_shas": integrations[0].request["integration_commits"],
            }
        )
    snapshot = {
        "external_dependencies": external_dependencies,
        "dependency_hash": digest(external_dependencies),
        "task_snapshots": task_snapshots,
        "mode": mode,
        "task": view(task),
        "project": view(project),
        "workflow_version": view(workflow) if workflow else None,
        "graph_json": workflow.graph_json if workflow else None,
        "graph_hash": workflow.graph_hash if workflow else None,
        "agent": selected_agent,
        "agents_by_role": agents_by_role,
        "agent_versions": agent_versions,
        "policy": policy,
        "root_base_sha": data.root_base_sha,
        "plan": data.plan,
        "plan_hash": digest(data.plan) if data.plan else None,
        "deadline_at": (
            clock(session) + timedelta(minutes=policy["max_run_minutes"])
        ).isoformat(),
    }
    run = Run(
        id=uid(),
        task_id=task.id,
        project_id=project.id,
        workflow_version_id=workflow.id if workflow else None,
        status="queued",
        snapshot=snapshot,
    )
    session.add(run)
    session.flush()
    session.add(
        Command(
            run_id=run.id, kind="decompose" if mode == "planning" else "start", data={}
        )
    )
    emit(session, run, "run.status_changed", {"status": "queued"})
    return run


def control(
    session: Session, run: Run, action: str, reason: str | None = None
) -> dict[str, Any]:
    if action not in {"pause", "resume", "cancel"}:
        raise DomainError("INVALID_CONTROL", "Unknown control action.", 422)
    if run.status in RUN_TERMINAL:
        raise DomainError("RUN_TERMINAL", "Terminal runs cannot be controlled.")
    if action == "resume" and run.cancel_requested:
        raise DomainError(
            "CANCEL_MONOTONIC", "Cancellation cannot be cleared by resume."
        )
    if action == "resume" and run.integration_status == "needs_resolution":
        resolution = run.snapshot.get("resolution", {})
        approval = (
            session.get(Approval, resolution.get("approval_id"))
            if resolution.get("approval_id")
            else None
        )
        if (
            approval is None
            or approval.decision != "approve"
            or instant(approval.expires_at) <= clock(session)
            or approval.snapshot.get("tree_sha") != resolution.get("tree_sha")
        ):
            raise DomainError(
                "RESOLUTION_APPROVAL_REQUIRED",
                "Approve the exact integration resolution tree before resuming.",
            )
        run.snapshot = {**run.snapshot, "resolution_resume_requested": True}
    if action == "cancel":
        run.cancel_requested = True
    else:
        run.pause_requested = action == "pause"
    if action == "resume" and run.integration_status == "needs_resolution":
        run.status = "running"
        emit(
            session,
            run,
            "run.status_changed",
            {"status": "running", "integration_status": "needs_resolution"},
        )
    run.revision += 1
    command = Command(id=uid(), run_id=run.id, kind=action, data={"reason": reason})
    session.add(command)
    session.add(
        Audit(
            actor="admin" if reason is None else "orchestrator",
            action="run." + action,
            resource_id=run.id,
            payload={"reason": reason},
        )
    )
    emit(session, run, "run.control_requested", {"action": action, "reason": reason})
    session.flush()
    return {"command_id": command.id, "run_id": run.id, "requested": action}


def cancel_nodes(session: Session, run: Run, executions: list[Execution]) -> None:
    """Close interrupted node attempts after execution drain in the run transaction."""
    by_id = {execution.id: execution for execution in executions}
    nodes = session.scalars(
        select(Node)
        .where(
            Node.run_id == run.id,
            Node.status.not_in({"succeeded", "failed", "cancelled", "skipped"}),
        )
        .order_by(Node.node_id, Node.attempt)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for node in nodes:
        reference = node.result.get("execution_id")
        execution = by_id.get(reference) if isinstance(reference, str) else None
        if execution is not None and (
            execution.node_id != node.node_id or execution.attempt != node.attempt
        ):
            execution = None
        if execution is None:
            matching = [
                item
                for item in executions
                if item.node_id == node.node_id and item.attempt == node.attempt
            ]
            execution = matching[0] if len(matching) == 1 else None
        result = {**node.result, "status": "cancelled", "error_code": "RUN_CANCELLED"}
        if execution is not None:
            result.update(execution_id=execution.id, result=execution.result)
        node.status, node.result, node.revision = "cancelled", result, node.revision + 1
        emit(
            session,
            run,
            "node.status_changed",
            {
                "node_id": node.node_id,
                "attempt": node.attempt,
                "status": "cancelled",
                "execution_id": execution.id if execution else None,
            },
        )


def transition(
    session: Session,
    run: Run,
    status: str,
    result: dict[str, Any] | None,
    integration_status: str | None,
) -> Run:
    if status == run.status:
        return run
    if status not in RUN_TRANSITIONS.get(run.status, set()):
        raise DomainError("INVALID_STATE", "Invalid run state transition.")
    executions = list(
        session.scalars(select(Execution).where(Execution.run_id == run.id))
    )
    active = [execution for execution in executions if execution.status in ACTIVE]
    if status in {"completed", "cancelled", "failed", "paused"} and active:
        raise DomainError(
            "EXECUTIONS_ACTIVE",
            "Drain or reconcile active executions before this transition.",
        )
    if status == "running" and (run.cancel_requested or run.pause_requested):
        raise DomainError(
            "CONTROL_PENDING", "Requested pause/cancel prevents dispatch."
        )
    if status == "completed":
        if run.cancel_requested:
            raise DomainError("CANCEL_MONOTONIC", "A cancelled run cannot complete.")
        if run.snapshot.get("mode") == "planning":
            if not run.snapshot.get("plan"):
                raise DomainError(
                    "EVIDENCE_REQUIRED",
                    "Planning completion requires a validated persisted plan.",
                )
        else:
            if (integration_status or run.integration_status) != "passed":
                raise DomainError(
                    "EVIDENCE_REQUIRED", "Completion requires a successful integration."
                )
            integrates = [
                execution
                for execution in executions
                if execution.operation == "integrate"
                and execution.status == "succeeded"
            ]
            integrates.sort(
                key=lambda execution: (execution.created_at, execution.attempt)
            )
            if not integrates or not passing_checks(
                integrates[-1].result,
                [check["id"] for check in run.snapshot["project"].get("checks", [])],
            ):
                raise DomainError(
                    "EVIDENCE_REQUIRED",
                    "Successful integration and test ledger evidence are required.",
                )
            selected_commits = {
                execution.result.get("commit_sha")
                for execution in executions
                if execution.operation == "git_commit"
                and execution.status == "succeeded"
                and execution.attempt
                == max(
                    other.attempt
                    for other in executions
                    if other.node_id == execution.node_id
                    and other.operation == "git_commit"
                )
            }
            external = {
                commit
                for dependency in run.snapshot.get("external_dependencies", [])
                for commit in dependency["commit_shas"]
            }
            if (
                set(integrates[-1].request.get("integration_commits", []))
                != selected_commits | external
            ):
                raise DomainError(
                    "INTEGRATION_EVIDENCE_REQUIRED",
                    "Final integration must include every selected accepted task commit exactly once.",
                )
            if any(
                execution.status != "succeeded"
                for execution in executions
                if execution.operation in {"agent_task", "review", "git_commit"}
                and execution.attempt
                == max(
                    other.attempt
                    for other in executions
                    if other.node_id == execution.node_id
                    and other.operation == execution.operation
                )
            ):
                raise DomainError(
                    "EVIDENCE_REQUIRED",
                    "Selected writing/review/commit attempts must succeed.",
                )
    if status == "cancelled":
        cancel_nodes(session, run, executions)
    run.status = status
    if integration_status:
        run.integration_status = integration_status
    if result is not None:
        run.result = {**run.result, **redact(result)}
    run.revision += 1
    task = get(session, Task, run.task_id)
    task.status = {
        "running": "running",
        "completed": "ready" if run.snapshot.get("mode") == "planning" else "done",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status, task.status)
    task.revision += 1
    if status == "completed" and run.snapshot.get("mode") != "planning":
        included_commits = set(integrates[-1].request.get("integration_commits", []))
        accepted_tasks = {
            execution.request["task_id"]
            for execution in executions
            if execution.operation == "git_commit"
            and execution.status == "succeeded"
            and execution.result.get("commit_sha") in included_commits
        }
        for task_id in accepted_tasks - {run.task_id}:
            child = get(session, Task, task_id)
            child.status, child.revision = "done", child.revision + 1
    emit(
        session,
        run,
        "run.status_changed",
        {"status": status, "integration_status": run.integration_status},
    )
    return run


def approval_bindings(run: Run) -> dict[str, Any]:
    snapshot = run.snapshot
    agent = snapshot.get("agent") or snapshot.get("agents_by_role", {}).get(
        "Developer", {}
    )
    paths = sorted(
        {
            path
            for task in snapshot.get(
                "task_snapshots", {run.task_id: snapshot["task"]}
            ).values()
            for path in task.get("expected_files", [])
        }
    )
    return {
        "graph_hash": snapshot.get("graph_hash"),
        "plan_hash": snapshot.get("plan_hash"),
        "root_base_sha": snapshot.get("root_base_sha"),
        "policy_hash": snapshot["policy"].get(
            "config_hash", digest(snapshot["policy"])
        ),
        "dependency_hash": snapshot.get("dependency_hash"),
        "agent_versions_hash": digest(snapshot["agent_versions"]),
        "agent_config_hash": agent.get("config_hash"),
        "allowed_write_paths": paths,
        "tool_ids": agent.get("tool_ids", []),
    }


def matches_approval(run: Run, snapshot: dict[str, Any]) -> bool:
    return all(
        key in snapshot and snapshot[key] == value
        for key, value in approval_bindings(run).items()
    )


def approved(session: Session, run: Run) -> bool:
    approvals = session.scalars(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.decision == "approve",
            Approval.action.in_(["execute_plan", "execution", "trusted-development"]),
        )
    )
    for approval in approvals:
        snapshot = approval.snapshot
        if instant(approval.expires_at) <= clock(session):
            continue
        if not matches_approval(run, snapshot):
            continue
        return True
    return False


def resolution_bindings(run: Run, execution_id: str, tree_sha: str) -> dict[str, Any]:
    """Bind operator permission to one conflicted allocation and exact resolved tree."""
    return {
        "graph_hash": run.snapshot.get("graph_hash"),
        "plan_hash": run.snapshot.get("plan_hash"),
        "root_base_sha": run.snapshot.get("root_base_sha"),
        "policy_hash": run.snapshot["policy"].get(
            "config_hash", digest(run.snapshot["policy"])
        ),
        "execution_id": execution_id,
        "tree_sha": tree_sha,
        "allowed_write_paths": run.snapshot["policy"].get("allowed_write_paths", []),
        "action": "integration_resolution",
    }


def conflicted_integration(execution: Execution, run: Run) -> bool:
    return (
        execution.run_id == run.id
        and execution.operation == "integrate"
        and execution.attempt == 1
        and execution.status == "failed"
        and execution.result.get("error_code") == "INTEGRATION_CONFLICT"
        and execution.result.get("integration_status") == "needs_resolution"
        and execution.result.get("process_stopped") is True
    )


def authorize_manual_continuation(
    session: Session, run: Run, data: ExecutionInput, request: dict[str, Any]
) -> None:
    """One operator continuation does not consume or enlarge automatic retry budgets."""
    resolution = run.snapshot.get("resolution", {})
    original = (
        session.get(Execution, resolution.get("execution_id")) if resolution else None
    )
    approval = (
        session.get(Approval, resolution.get("approval_id"))
        if resolution.get("approval_id")
        else None
    )
    expected = resolution_bindings(
        run, resolution.get("execution_id", ""), resolution.get("tree_sha", "")
    )
    if (
        data.operation != "integrate"
        or data.attempt != 2
        or run.integration_status != "needs_resolution"
        or run.snapshot.get("resolution_resume_requested") is not True
        or original is None
        or not conflicted_integration(original, run)
        or original.node_id != data.node_id
        or original.request.get("task_id") != (data.task_id or run.task_id)
        or request.get("target_execution_id") != original.id
        or not full_sha(resolution.get("tree_sha"))
        or request.get("resolution_tree_sha") != resolution.get("tree_sha")
        or (
            "execution_base_sha" in request
            and request["execution_base_sha"]
            != original.request.get("execution_base_sha")
        )
        or approval is None
        or approval.run_id != run.id
        or approval.action != "integration_resolution"
        or approval.decision != "approve"
        or instant(approval.expires_at) <= clock(session)
        or approval.snapshot_hash != digest(approval.snapshot)
        or approval.snapshot != expected
        or (
            "resolution_approval_id" in request
            and request["resolution_approval_id"] != approval.id
        )
    ):
        raise DomainError(
            "RESOLUTION_APPROVAL_REQUIRED",
            "Continuation requires the stopped conflict, exact approved tree, and explicit resume.",
            403,
        )


def recorded_manual_continuation(
    session: Session, run: Run, node_id: str, attempt: int
) -> Execution | None:
    """Projection permission comes from an already authorized durable execution."""
    if attempt != 2:
        return None
    execution = session.scalar(
        select(Execution).where(
            Execution.run_id == run.id,
            Execution.node_id == node_id,
            Execution.attempt == attempt,
            Execution.operation == "integrate",
        )
    )
    if (
        execution is not None
        and execution.request.get("manual_resolution") is True
        and execution.request.get("resolution_approved") is True
    ):
        return execution
    return None


def create_execution(session: Session, run: Run, data: ExecutionInput) -> Execution:
    execution_id = identifier(data.execution_id)
    raw_hash = digest(data.model_dump())
    previous = session.get(Execution, execution_id)
    if previous:
        if previous.run_id != run.id or previous.request_hash != raw_hash:
            raise DomainError(
                "IDEMPOTENCY_CONFLICT",
                "Execution identity was reused with a different request.",
            )
        return previous
    if (
        run.status not in {"queued", "running"}
        or run.pause_requested
        or run.cancel_requested
    ):
        raise DomainError("CONTROL_PENDING", "Run controls prevent dispatch.")
    if clock(session) >= instant(run.snapshot["deadline_at"]):
        raise DomainError("RUN_TIMEOUT", "Run deadline reached; request cancellation.")
    request = ExecutionRequest.model_validate(data.request).model_dump(
        exclude_unset=True
    )
    request.pop(
        "resolution_approved", None
    )  # Only the ledger can stamp operator permission.
    manual_continuation = (
        data.operation == "integrate" and request.get("manual_resolution") is True
    )
    if manual_continuation:
        authorize_manual_continuation(session, run, data, request)
    limits = run.snapshot["policy"]
    if data.attempt > limits["max_retries"] + 1 and not manual_continuation:
        raise DomainError("QUOTA_EXCEEDED", "Maximum attempts exceeded.")
    if request.get("manual_resolution") is True and not manual_continuation:
        raise DomainError(
            "INVALID_REQUEST", "Operator resolution is restricted to integration.", 422
        )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(184739013)"))
    active = list(
        session.scalars(select(Execution).where(Execution.status.in_(ACTIVE)))
    )
    if len(active) >= limits["max_parallel_agents"]:
        raise DomainError("CONCURRENCY_LIMIT", "Project concurrency limit reached.")
    task = get(session, Task, identifier(data.task_id or run.task_id))
    task_snapshot = run.snapshot.get("task_snapshots", {}).get(task.id)
    if task_snapshot is None:
        raise DomainError(
            "TASK_NOT_PINNED",
            "Execution task is not part of the approved run snapshot.",
            403,
        )
    if task.project_id != run.project_id:
        raise DomainError(
            "INVALID_REFERENCE", "Execution task belongs to another project.", 422
        )
    for execution in active:
        if (
            execution.request.get("project_id") == run.project_id
            and execution.operation == "agent_task"
            and data.operation == "agent_task"
        ):
            other = execution.request.get("expected_files", [])
            paths = task_snapshot.get("expected_files", [])
            if (
                not paths
                or not other
                or any(
                    fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left)
                    for left in paths
                    for right in other
                )
            ):
                raise DomainError(
                    "PATH_RESERVATION",
                    "Expected write paths overlap an active execution.",
                )
    predecessors = []
    for dependency_id in data.dependency_execution_ids:
        dependency = get(session, Execution, identifier(dependency_id))
        if dependency.run_id != run.id or dependency.status != "succeeded":
            raise DomainError(
                "DEPENDENCY_PENDING",
                "Execution dependencies must be successful in the same run.",
            )
        if dependency.result.get("commit_sha"):
            predecessors.append(dependency.result["commit_sha"])
    required_task_dependencies = task_snapshot.get("dependency_ids", [])
    bound_dependency_tasks = {
        dependency.request.get("task_id")
        for dependency_id in data.dependency_execution_ids
        if (dependency := get(session, Execution, identifier(dependency_id))).operation
        == "git_commit"
        and dependency.status == "succeeded"
        and full_sha(dependency.result.get("commit_sha"))
        and full_sha(dependency.result.get("tree_sha"))
    }
    external_task_ids = {
        dependency["task_id"]
        for dependency in run.snapshot.get("external_dependencies", [])
    }
    if (
        data.operation == "agent_task"
        and set(required_task_dependencies) - bound_dependency_tasks - external_task_ids
    ):
        raise DomainError(
            "DEPENDENCY_PENDING",
            "Writing execution requires accepted predecessor commits for every task dependency.",
        )
    if data.operation in {"agent_task", "planner"}:
        predecessors = list(
            dict.fromkeys(
                [
                    commit
                    for dependency in run.snapshot.get("external_dependencies", [])
                    for commit in dependency["commit_shas"]
                ]
                + predecessors
            )
        )
    role = {
        "planner": "Planner",
        "review": "Reviewer",
        "test": "Tester",
        "integrate": "Integrator",
    }.get(data.operation, "Developer")
    agent_id = data.agent_config_version_id or data.request.get("agent_config_version")
    agent = (
        run.snapshot["agent_versions"].get(agent_id)
        if agent_id
        else run.snapshot["agents_by_role"].get(role)
    )
    if agent is None and agent_id:
        raise DomainError(
            "AGENT_NOT_PINNED", "Execution agent is not part of the run snapshot.", 403
        )
    if agent is None:
        agent = run.snapshot.get("agent")
    if agent is None and data.operation in {
        "repo_inspect",
        "test",
        "git_commit",
        "integrate",
    }:
        agent = {
            "id": None,
            "role": "Observer",
            "provider": "none",
            "permission_profile": "observe",
        }
    if agent is None:
        raise DomainError(
            "AGENT_REQUIRED",
            "No pinned agent configuration exists for this operation.",
            422,
        )
    pinned_registries = agent.get("registry_snapshots", {})
    validate_tool_assignments(
        agent.get("provider", "none"),
        pinned_registries.get("tool_ids", []),
        pinned_registries.get("mcp_server_ids", []),
    )
    provider = agent.get("provider")
    if (
        limits.get("provider_pin")
        and limits["provider_pin"] != provider
        and data.operation in {"agent_task", "planner", "review"}
    ):
        raise DomainError(
            "PROVIDER_DENIED", "Agent provider differs from the pinned policy.", 403
        )
    provider_cap = limits.get("provider_limits", {}).get(provider, 3)
    if data.operation in {"agent_task", "planner", "review"}:
        matching_provider = [
            item
            for item in active
            if item.operation in {"agent_task", "planner", "review"}
            and item.request.get("agent", {}).get("provider") == provider
        ]
        matching_agent = [
            item
            for item in matching_provider
            if item.request.get("agent", {}).get("agent_id") == agent.get("agent_id")
        ]
        if len(matching_provider) >= provider_cap or len(matching_agent) >= agent.get(
            "limits", {}
        ).get("max_parallel_agents", 3):
            raise DomainError(
                "CONCURRENCY_LIMIT",
                "Pinned provider or agent concurrency limit reached.",
            )
    if data.operation == "review" and agent["role"] != "Reviewer":
        raise DomainError(
            "INDEPENDENT_REVIEW_REQUIRED",
            "Review requires a distinct pinned Reviewer agent.",
            403,
        )
    if data.operation == "planner" and agent["role"] not in {"Planner", "Architect"}:
        raise DomainError(
            "PLANNER_REQUIRED",
            "Planning requires a pinned Planner or Architect agent.",
            403,
        )
    if data.operation in {"planner", "review"}:
        agent = {**agent, "permission_profile": "observe"}
    if data.operation == "review":
        target = session.get(Execution, data.request.get("target_execution_id"))
        if target and target.request.get("agent", {}).get("agent_id") == agent.get(
            "agent_id"
        ):
            raise DomainError(
                "INDEPENDENT_REVIEW_REQUIRED",
                "Reviewer must be a different stable agent definition.",
                403,
            )
    if data.operation in {"agent_task", "git_commit", "integrate"} and not approved(
        session, run
    ):
        raise DomainError(
            "APPROVAL_REQUIRED",
            "An unexpired exact plan/base/policy approval is required.",
            403,
        )
    project = run.snapshot["project"]
    if data.operation in {"agent_task", "git_commit", "integrate"} and not project.get(
        "trusted"
    ):
        raise DomainError(
            "UNTRUSTED_REPOSITORY", "Writes require an explicitly trusted project.", 403
        )
    if (
        "workspace_path" in request
        or "cwd" in request
        or "argv" in request
        or "project" in request
        or "agent" in request
        or "policy" in request
    ):
        raise DomainError(
            "INVALID_REQUEST",
            "Transport requests cannot override trusted execution snapshots.",
            422,
        )
    base = request.get("execution_base_sha") or run.snapshot.get("root_base_sha")
    if data.operation not in {"planner", "repo_inspect"} and not base:
        raise DomainError(
            "BASE_REQUIRED",
            "Pin the repository base before dispatching execution.",
            422,
        )
    policy = {
        **limits,
        "approved": approved(session, run),
        "trusted_repository": project.get("trusted", False),
    }
    skills: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    mcp: list[dict[str, Any]] = []
    for field, destination in [
        ("skill_ids", skills),
        ("tool_ids", tools),
        ("mcp_server_ids", mcp),
    ]:
        destination.extend(agent.get("registry_snapshots", {}).get(field, []))
    agent = {
        **agent,
        "skills": skills,
        "allowed_tools": [item["id"] for item in tools],
        "tools": tools,
        "mcp_servers": mcp,
    }
    checks = project.get("checks", [])
    check_ids = request.get(
        "check_ids",
        [check["id"] for check in checks]
        if data.operation in {"test", "integrate"}
        else [],
    )
    if set(check_ids) - {check["id"] for check in checks}:
        raise DomainError(
            "CHECK_DENIED", "Only registered project checks can execute.", 403
        )
    policy_paths = limits.get("allowed_write_paths", [])
    allowed_paths = task_snapshot.get("expected_files", []) or policy_paths
    if data.operation == "agent_task" and any(
        not any(fnmatch.fnmatch(path, pattern) for pattern in policy_paths)
        for path in allowed_paths
    ):
        raise DomainError(
            "WRITE_SCOPE_DENIED", "Pinned task paths exceed the approved policy.", 403
        )
    if (
        "allowed_write_paths" in request
        and request["allowed_write_paths"] != allowed_paths
    ):
        raise DomainError(
            "WRITE_SCOPE_DENIED",
            "Dispatch cannot enlarge pinned task write scope.",
            403,
        )
    request["allowed_write_paths"] = allowed_paths
    target_evidence(session, run, request, data.operation)
    if data.operation == "integrate":
        predecessors = []
        requested_commits = request.get("integration_commits", [])
        commit_rows = list(
            session.scalars(
                select(Execution).where(
                    Execution.run_id == run.id, Execution.operation == "git_commit"
                )
            )
        )
        selected = [
            row
            for row in commit_rows
            if row.attempt
            == max(
                other.attempt for other in commit_rows if other.node_id == row.node_id
            )
        ]
        if any(row.status != "succeeded" for row in selected):
            raise DomainError(
                "INTEGRATION_SCOPE_DENIED",
                "Every selected task commit must succeed before integration.",
                403,
            )
        eligible = {row.result.get("commit_sha") for row in selected}

        external_commits = [
            commit
            for dependency in run.snapshot.get("external_dependencies", [])
            for commit in dependency["commit_shas"]
        ]
        request["integration_commits"] = list(
            dict.fromkeys(external_commits + requested_commits)
        )
        if (
            not requested_commits
            or len(requested_commits) != len(set(requested_commits))
            or set(requested_commits) != eligible
        ):
            raise DomainError(
                "INTEGRATION_SCOPE_DENIED",
                "Integration accepts only unique approved task commits from this run.",
                403,
            )
        if manual_continuation:
            request["resolution_approved"] = True
    agent_limits = agent.get("limits", {})
    request.update(
        {
            "schema_version": "1",
            "execution_id": execution_id,
            "run_id": run.id,
            "task_id": task.id,
            "node_run_id": str(uuid5(UUID(run.id), f"{data.node_id}:1:node")),
            "operation": data.operation,
            "project_id": run.project_id,
            "agent_config_version": agent.get("config_version_id"),
            "policy_version": limits.get("version_id"),
            "root_base_sha": run.snapshot.get("root_base_sha"),
            "execution_base_sha": base,
            "worktree_key": execution_id,
            "git_branch": "factory/integration/" + run.id
            if data.operation == "integrate"
            else "factory/task/" + task.id + "/" + execution_id,
            "task_prompt": request.get(
                "task_prompt", task_snapshot.get("description", "")
            ),
            "expected_files": task_snapshot.get("expected_files", []),
            "allowed_tool_ids": agent["allowed_tools"],
            "check_ids": check_ids,
            "env_allowlist": request.get("env_allowlist", []),
            "timeout_sec": min(
                request.get("timeout_sec", 1800),
                limits["max_execution_minutes"] * 60,
                agent_limits.get(
                    "max_execution_minutes", limits["max_execution_minutes"]
                )
                * 60,
            ),
            "max_steps": min(
                request.get("max_steps", limits["max_steps"]),
                limits["max_steps"],
                agent_limits.get("max_steps", limits["max_steps"]),
            ),
            "max_output_bytes": min(
                limits["max_output_bytes"],
                agent_limits.get("max_output_bytes", limits["max_output_bytes"]),
            ),
            "max_disk_bytes": min(
                limits["max_disk_bytes"],
                agent_limits.get("max_disk_bytes", limits["max_disk_bytes"]),
            ),
            "trace_id": run.snapshot.get("trace_id"),
            "traceparent": run.snapshot.get("traceparent"),
            "project": {
                **project,
                "check_registry": {check["id"]: check["argv"] for check in checks},
                "check_timeouts": {
                    check["id"]: check["timeout_sec"] for check in checks
                },
            },
            "agent": agent,
            "policy": policy,
            "predecessor_commits": predecessors,
        }
    )
    execution = Execution(
        id=execution_id,
        run_id=run.id,
        node_id=data.node_id,
        attempt=data.attempt,
        operation=data.operation,
        request_hash=raw_hash,
        request=request,
        status="created",
    )
    session.add(execution)
    session.flush()
    outbox_id = uid()
    session.add(
        Outbox(
            id=outbox_id,
            execution_id=execution.id,
            data={"execution_id": execution.id, "dispatch_id": outbox_id},
        )
    )
    emit(
        session,
        run,
        "execution.created",
        {
            "execution_id": execution.id,
            "node_id": data.node_id,
            "attempt": data.attempt,
            "operation": data.operation,
        },
    )
    return execution


def lease(
    session: Session,
    run: Run,
    execution: Execution,
    worker_id: str,
    epoch: int,
    *,
    allow_terminal: bool = False,
    allow_expired: bool = False,
) -> None:
    if execution.worker_id != worker_id or execution.epoch != epoch:
        raise DomainError(
            "LEASE_STALE", "Execution owner or lease epoch is stale.", 409
        )
    if allow_terminal and execution.status in TERMINAL:
        return
    if execution.status == "uncertain" and not allow_expired:
        raise DomainError(
            "EXECUTION_UNCERTAIN", "Supervisor reconciliation is required."
        )
    if not allow_expired and (
        not execution.lease_expires_at
        or instant(execution.lease_expires_at) <= clock(session)
    ):
        execution.status = "uncertain"
        emit(
            session,
            run,
            "execution.uncertain",
            {"execution_id": execution.id, "reason": "LEASE_EXPIRED"},
        )
        raise DomainError(
            "EXECUTION_UNCERTAIN",
            "Lease expired; stop the process and reconcile before any new execution.",
            persist=True,
        )
