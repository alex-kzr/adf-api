"""Bounded declarative plans; modifications cannot retain a prior approval hash."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .definitions import relative_paths
from .errors import DomainError
from .ledger import emit
from .models import Execution, Run, Task, uid
from .privacy import digest
from .schemas import TaskInput


def save_plan(session: Session, run: Run, body: dict[str, Any]) -> dict[str, Any]:
    raw_tasks = body.get("tasks")
    if (
        not isinstance(raw_tasks, list)
        or not 1 <= len(raw_tasks) <= run.snapshot["policy"]["max_tasks_per_plan"]
        or any(not isinstance(task, dict) for task in raw_tasks)
    ):
        raise DomainError(
            "INVALID_PLAN", "Plans require between one and twelve bounded tasks.", 422
        )
    if session.scalar(
        select(Execution).where(
            Execution.run_id == run.id,
            Execution.operation.in_(["agent_task", "git_commit", "integrate"]),
        )
    ):
        raise DomainError(
            "PLAN_IN_USE", "A plan cannot change after writing execution begins."
        )
    mapping = {}
    for index, raw in enumerate(raw_tasks):
        local = str(raw.get("id", index))
        if local in mapping:
            raise DomainError(
                "INVALID_PLAN", "Plan task identifiers must be unique.", 422
            )
        mapping[local] = uid()
    adjacency = {}
    normalized = []
    for index, raw in enumerate(raw_tasks):
        local = str(raw.get("id", index))
        dependencies = [str(value) for value in raw.get("dependency_ids", [])]
        if any(value not in mapping or value == local for value in dependencies) or len(
            dependencies
        ) != len(set(dependencies)):
            raise DomainError(
                "INVALID_PLAN",
                "Plan dependencies must reference distinct plan tasks.",
                422,
            )
        adjacency[local] = dependencies
        data = TaskInput.model_validate(
            {
                "project_id": run.project_id,
                "parent_id": run.task_id,
                "title": raw.get("title"),
                "description": raw.get("description", ""),
                "acceptance_criteria": raw.get("acceptance_criteria", []),
                "expected_files": raw.get("expected_files", []),
                "status": "ready",
                "dependency_ids": [mapping[value] for value in dependencies],
            }
        ).model_dump()
        relative_paths(data["expected_files"])
        normalized.append({"id": mapping[local], "local_id": local, **data})
    visited, active = set(), set()

    def visit(key: str) -> None:
        if key in active:
            raise DomainError("INVALID_PLAN", "Plan dependencies contain a cycle.", 422)
        if key in visited:
            return
        active.add(key)
        for parent in adjacency[key]:
            visit(parent)
        active.remove(key)
        visited.add(key)

    for key in adjacency:
        visit(key)
    # Prior versions stay as immutable task evidence; only the run's active plan is replaced.
    for data in normalized:
        session.add(
            Task(
                id=data["id"],
                project_id=run.project_id,
                title=data["title"],
                status="ready",
                data={
                    key: value
                    for key, value in data.items()
                    if key not in {"id", "local_id"}
                },
            )
        )
    plan = {"tasks": normalized}
    run.snapshot = {
        **run.snapshot,
        "plan": plan,
        "plan_hash": digest(plan),
        "task_snapshots": {
            run.task_id: run.snapshot["task"],
            **{task["id"]: task for task in normalized},
        },
    }
    run.result = {
        key: value
        for key, value in run.result.items()
        if key not in {"graph_proposal", "graph_proposal_plan_hash"}
    }
    run.revision += 1
    session.flush()
    emit(
        session,
        run,
        "plan.created",
        {"plan_hash": run.snapshot["plan_hash"], "task_count": len(normalized)},
    )
    return plan
