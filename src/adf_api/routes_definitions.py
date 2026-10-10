"""Authenticated control-plane definitions and workflow publishing."""

from typing import Any

from fastapi import APIRouter, Header, Query
from sqlalchemy import select

from .config import Settings
from .db import Database
from .definitions import (
    Validator,
    get,
    identifier,
    project_data,
    registry_data,
    revision,
    save_definition,
    task_data,
    view,
)
from .errors import DomainError
from .models import Definition, Project, Task, WorkflowVersion
from .privacy import digest
from .schemas import ProjectInput, TaskInput


def page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {
        "items": rows[:limit],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def build_router(
    database: Database, settings: Settings, validator: Validator
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/projects")
    def projects(
        limit: int = Query(50, ge=1, le=200), cursor: str | None = None
    ) -> dict[str, Any]:
        with database.transaction() as session:
            query = select(Project).order_by(Project.id).limit(limit + 1)
            if cursor:
                query = query.where(Project.id > identifier(cursor))
            return page([view(row) for row in session.scalars(query)], limit)

    @router.post("/projects", status_code=201)
    def create_project(body: ProjectInput) -> dict[str, Any]:
        with database.transaction() as session:
            data = project_data(body.model_dump(), settings)
            row = Project(name=data["name"], repo_path=data["repo_path"], data=data)
            session.add(row)
            session.flush()
            return view(row)

    @router.get("/projects/{project_id}")
    def project(project_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            return view(get(session, Project, identifier(project_id)))

    @router.patch("/projects/{project_id}")
    def update_project(
        project_id: str, body: dict[str, Any], if_match: str | None = Header(None)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Project, identifier(project_id), lock=True)
            revision(row, if_match)
            data = project_data({**row.data, **body}, settings)
            if data["repo_path"] != row.repo_path:
                raise DomainError(
                    "IMMUTABLE_PATH",
                    "Create a new project for a different repository path.",
                    422,
                )
            row.name, row.data, row.revision = data["name"], data, row.revision + 1
            return view(row)

    @router.get("/tasks")
    def tasks(
        project_id: str | None = None,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        with database.transaction() as session:
            query = select(Task).order_by(Task.id).limit(limit + 1)
            if project_id:
                query = query.where(Task.project_id == identifier(project_id))
            if status:
                query = query.where(Task.status == status)
            if cursor:
                query = query.where(Task.id > identifier(cursor))
            return page([view(row) for row in session.scalars(query)], limit)

    @router.post("/tasks", status_code=201)
    def create_task(body: TaskInput) -> dict[str, Any]:
        with database.transaction() as session:
            data = task_data(session, body.model_dump())
            row = Task(
                project_id=data["project_id"],
                title=data["title"],
                status=data["status"],
                data=data,
            )
            session.add(row)
            session.flush()
            return view(row)

    @router.get("/tasks/{task_id}")
    def task(task_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            return view(get(session, Task, identifier(task_id)))

    @router.patch("/tasks/{task_id}")
    def update_task(
        task_id: str, body: dict[str, Any], if_match: str | None = Header(None)
    ) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Task, identifier(task_id))
            get(session, Project, row.project_id, lock=True)
            row = get(session, Task, row.id, lock=True)
            revision(row, if_match)
            if row.status in {"running", "review", "done"}:
                raise DomainError(
                    "TASK_IN_USE",
                    "Running and completed task definitions are immutable.",
                )
            data = task_data(session, {**row.data, **body}, row.id)
            if data["project_id"] != row.project_id:
                raise DomainError(
                    "INVALID_REFERENCE", "Task project cannot be changed.", 422
                )
            row.title, row.status, row.data, row.revision = (
                data["title"],
                data["status"],
                data,
                row.revision + 1,
            )
            return view(row)

    def registry_routes(kind: str) -> None:
        @router.get("/" + kind, name="list_" + kind)
        def list_definitions(
            limit: int = Query(50, ge=1, le=200), cursor: str | None = None
        ) -> dict[str, Any]:
            with database.transaction() as session:
                query = (
                    select(Definition)
                    .where(Definition.kind == kind)
                    .order_by(Definition.id)
                    .limit(limit + 1)
                )
                if cursor:
                    query = query.where(Definition.id > identifier(cursor))
                return page([view(row) for row in session.scalars(query)], limit)

        @router.post("/" + kind, status_code=201, name="create_" + kind)
        def create_definition(body: dict[str, Any]) -> dict[str, Any]:
            with database.transaction() as session:
                data = registry_data(kind, body, session)
                return view(save_definition(session, kind, data))

        @router.get("/" + kind + "/{definition_id}", name="get_" + kind)
        def definition(definition_id: str) -> dict[str, Any]:
            with database.transaction() as session:
                row = get(session, Definition, identifier(definition_id))
                if row.kind != kind:
                    raise DomainError("NOT_FOUND", "Definition not found.", 404)
                return view(row)

        @router.patch("/" + kind + "/{definition_id}", name="update_" + kind)
        def update_definition(
            definition_id: str,
            body: dict[str, Any],
            if_match: str | None = Header(None),
        ) -> dict[str, Any]:
            with database.transaction() as session:
                row = get(session, Definition, identifier(definition_id), lock=True)
                if row.kind != kind:
                    raise DomainError("NOT_FOUND", "Definition not found.", 404)
                revision(row, if_match)
                data = registry_data(kind, {**row.data, **body}, session)
                return view(save_definition(session, kind, data, row))

    for kind in ["agents", "workflows", "skills", "tools", "mcp-servers", "policies"]:
        registry_routes(kind)

    @router.post("/workflows/{workflow_id}/validate")
    def validate_workflow(
        workflow_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Definition, identifier(workflow_id))
            if row.kind != "workflows":
                raise DomainError("NOT_FOUND", "Workflow not found.", 404)
            graph = (body or {}).get("graph_json", row.data["graph_json"])
        return validator.validate(graph)

    @router.get("/workflows/{workflow_id}/versions")
    def workflow_versions(workflow_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            get(session, Definition, identifier(workflow_id))
            return {
                "items": [
                    view(row)
                    for row in session.scalars(
                        select(WorkflowVersion)
                        .where(WorkflowVersion.workflow_id == workflow_id)
                        .order_by(WorkflowVersion.version)
                    )
                ],
                "next_cursor": None,
            }

    @router.post("/workflows/{workflow_id}/versions", status_code=201)
    def publish_workflow(workflow_id: str) -> dict[str, Any]:
        with database.transaction() as session:
            row = get(session, Definition, identifier(workflow_id), lock=True)
            if row.kind != "workflows":
                raise DomainError("NOT_FOUND", "Workflow not found.", 404)
            graph = row.data["graph_json"]
            result = validator.validate(graph)
            if not result["valid"]:
                raise DomainError(
                    "INVALID_WORKFLOW",
                    "Workflow failed semantic validation.",
                    422,
                    {"errors": result.get("errors", [])},
                )
            versions = list(
                session.scalars(
                    select(WorkflowVersion).where(
                        WorkflowVersion.workflow_id == workflow_id
                    )
                )
            )
            latest = (
                max(versions, key=lambda value: value.version) if versions else None
            )
            if latest and latest.graph_hash == digest(graph):
                return view(latest)
            version = WorkflowVersion(
                workflow_id=workflow_id,
                version=(latest.version + 1) if latest else 1,
                graph_hash=digest(graph),
                graph_json=graph,
            )
            session.add(version)
            session.flush()
            return view(version)

    return router
