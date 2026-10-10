"""Definition validation and immutable configuration snapshots."""

import os
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .errors import DomainError
from .models import (
    Approval,
    Audit,
    Base,
    ConfigVersion,
    Definition,
    Project,
    Record,
    Task,
    uid,
)
from .privacy import digest, reject_credentials
from .schemas import (
    AgentInput,
    Limits,
    MCPManifest,
    PolicyManifest,
    ProjectInput,
    RegistryInput,
    SkillManifest,
    TaskInput,
    ToolManifest,
    WorkflowInput,
)


class Validator(Protocol):
    def validate(self, graph: dict[str, Any]) -> dict[str, Any]: ...


class RemoteValidator:
    def __init__(self, settings: Settings):
        self.url = os.getenv("ORCHESTRATOR_URL", "")
        self.token = settings.orchestrator_token

    def validate(self, graph: dict[str, Any]) -> dict[str, Any]:
        if not self.url:
            raise DomainError(
                "VALIDATOR_UNAVAILABLE",
                "Configure ORCHESTRATOR_URL before publishing executable workflows.",
                503,
            )
        try:
            response = httpx.post(
                self.url.rstrip("/") + "/validate",
                json={"graph_json": graph},
                headers={"Authorization": "Bearer " + self.token},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(
                result.get("valid"), bool
            ):
                raise TypeError("invalid validator response")
            return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise DomainError(
                "VALIDATOR_UNAVAILABLE",
                "Workflow semantic validator is unavailable.",
                503,
            ) from exc


def identifier(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError("INVALID_ID", "Expected a UUID identifier.", 422) from exc


def get[Model: Base](
    session: Session, model: type[Model], value: str, *, lock: bool = False
) -> Model:
    query = select(model).where(model.id == value)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    record = session.scalar(query)
    if record is None:
        raise DomainError("NOT_FOUND", "The requested resource does not exist.", 404)
    return record


def revision(record: Record, value: str | None) -> None:
    if value is None:
        raise DomainError(
            "PRECONDITION_REQUIRED", "If-Match revision is required.", 428
        )
    try:
        expected = int(value.strip('"'))
    except ValueError as exc:
        raise DomainError(
            "INVALID_REVISION", "If-Match must contain an integer revision.", 422
        ) from exc
    if expected != record.revision:
        raise DomainError(
            "VERSION_CONFLICT", "The resource changed; reload before updating.", 412
        )


def relative_paths(paths: list[str]) -> None:
    for value in paths:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or len(value) > 1024
        ):
            raise DomainError(
                "INVALID_PATH",
                "Paths must be bounded project-relative paths without traversal.",
                422,
            )


def project_data(value: dict[str, Any], settings: Settings) -> dict[str, Any]:
    data = ProjectInput.model_validate(value).model_dump()
    path, allowed = (
        PurePosixPath(data["repo_path"]),
        PurePosixPath(data["allowed_root"]),
    )
    configured = [PurePosixPath(root) for root in settings.allowed_roots]
    if (
        not path.is_absolute()
        or not allowed.is_absolute()
        or ".." in path.parts
        or str(path).startswith("/mnt/")
        or "\\" in str(path)
    ):
        raise DomainError(
            "INVALID_PATH",
            "Projects must use canonical WSL ext4 paths, never /mnt or traversal.",
            422,
        )
    if (
        not configured
        or allowed not in configured
        or not path.is_relative_to(allowed)
        or path == allowed
    ):
        raise DomainError(
            "ROOT_DENIED", "Project path is outside the configured worker roots.", 403
        )
    ids = [check["id"] for check in data["checks"]]
    if len(ids) != len(set(ids)):
        raise DomainError("INVALID_CHECK", "Check identifiers must be unique.", 422)
    for check in data["checks"]:
        if any(not arg or "\x00" in arg or len(arg) > 2048 for arg in check["argv"]):
            raise DomainError(
                "INVALID_CHECK", "Registered checks require bounded argv arrays.", 422
            )
        if PurePosixPath(check["argv"][0]).name in {
            "sudo",
            "su",
            "docker",
            "ssh",
            "powershell",
            "cmd",
        }:
            raise DomainError(
                "CHECK_DENIED",
                "Privileged or remote command launchers are prohibited.",
                403,
            )
    return data


def task_data(
    session: Session, value: dict[str, Any], task_id: str | None = None
) -> dict[str, Any]:
    data = TaskInput.model_validate(value).model_dump()
    get(session, Project, identifier(data["project_id"]))
    relative_paths(data["expected_files"])
    dependencies = data["dependency_ids"]
    if len(dependencies) != len(set(dependencies)) or task_id in dependencies:
        raise DomainError(
            "INVALID_DEPENDENCY",
            "Task dependencies must be unique and cannot contain itself.",
            422,
        )
    tasks = {
        task.id: task
        for task in session.scalars(
            select(Task).where(Task.project_id == data["project_id"])
        )
    }
    for dependency in dependencies:
        if dependency not in tasks:
            raise DomainError(
                "INVALID_DEPENDENCY",
                "All dependencies must exist in the same project.",
                422,
            )
    if data["parent_id"] and (
        data["parent_id"] not in tasks or data["parent_id"] == task_id
    ):
        raise DomainError(
            "INVALID_PARENT", "Parent task must exist in the same project.", 422
        )
    if task_id:
        adjacency = {
            key: task.data.get("dependency_ids", []) for key, task in tasks.items()
        }
        adjacency[task_id] = dependencies
        visited, active = set(), set()

        def visit(key: str) -> None:
            if key in active:
                raise DomainError(
                    "INVALID_DEPENDENCY", "Task dependencies must be acyclic.", 422
                )
            if key in visited:
                return
            active.add(key)
            for parent in adjacency.get(key, []):
                visit(parent)
            active.remove(key)
            visited.add(key)

        visit(task_id)
    return data


def registry_data(kind: str, value: dict[str, Any], session: Session) -> dict[str, Any]:
    reject_credentials(value)
    if kind == "agents":
        data = AgentInput.model_validate(value).model_dump()
        for field, target in [
            ("skill_ids", "skills"),
            ("tool_ids", "tools"),
            ("mcp_server_ids", "mcp-servers"),
        ]:
            for item in data[field]:
                definition = get(session, Definition, identifier(item))
                if definition.kind != target:
                    raise DomainError(
                        "INVALID_REFERENCE",
                        "Agent registry references have the wrong type.",
                        422,
                    )
        return data
    if kind == "workflows":
        return WorkflowInput.model_validate(value).model_dump()
    data = RegistryInput.model_validate(value).model_dump()
    manifest_models: dict[str, type[BaseModel]] = {
        "skills": SkillManifest,
        "tools": ToolManifest,
        "mcp-servers": MCPManifest,
        "policies": PolicyManifest,
    }
    data["manifest"] = (
        manifest_models[kind].model_validate(data["manifest"]).model_dump()
    )
    manifest = data["manifest"]
    if kind == "policies":
        limits = Limits.model_validate(
            {key: manifest[key] for key in Limits.model_fields if key in manifest}
        ).model_dump()
        data["manifest"] = {**limits, **manifest}
        provider_limits = manifest.get("provider_limits", {})
        if (
            not isinstance(provider_limits, dict)
            or set(provider_limits) - {"codex", "claude", "mock"}
            or any(
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 3
                for limit in provider_limits.values()
            )
            or manifest.get("provider_pin") not in {None, "codex", "claude", "mock"}
        ):
            raise DomainError(
                "INVALID_MANIFEST",
                "Provider limits must be bounded supported provider reservations.",
                422,
            )
        relative_paths(manifest.get("allowed_write_paths", ["**"]))
        if (
            manifest.get("permission_profile") == "unrestricted"
            or manifest.get("allow_push")
            or manifest.get("allow_sudo")
        ):
            raise DomainError(
                "POLICY_DENIED",
                "Unrestricted permissions, push and sudo are outside the worker policy.",
                403,
            )
    elif kind == "skills":
        if not all(key in manifest for key in ["path", "version", "hash"]):
            raise DomainError(
                "INVALID_MANIFEST",
                "Skills require trusted path, version and hash.",
                422,
            )
        if not isinstance(manifest["hash"], str) or not re.fullmatch(
            "[0-9a-f]{64}", manifest["hash"]
        ):
            raise DomainError(
                "INVALID_MANIFEST", "Skills require a SHA-256 content hash.", 422
            )
        if (
            not isinstance(manifest["path"], str)
            or not PurePosixPath(manifest["path"]).is_absolute()
            or ".." in PurePosixPath(manifest["path"]).parts
        ):
            raise DomainError(
                "INVALID_MANIFEST", "Skill path must be canonical and absolute.", 422
            )
    elif kind == "mcp-servers":
        if (
            not isinstance(manifest.get("allowed_tools"), list)
            or not manifest.get("trusted")
            or not isinstance(manifest.get("argv"), list)
        ):
            raise DomainError(
                "INVALID_MANIFEST",
                "MCP requires trusted argv and an explicit tool allowlist.",
                422,
            )
        if manifest.get("transport", "stdio") != "stdio":
            raise DomainError(
                "CAPABILITY_UNSUPPORTED",
                "Only local allowlisted stdio MCP servers are supported.",
                422,
            )
    elif kind == "tools":
        if not isinstance(manifest.get("input_schema"), dict) or manifest.get(
            "permission"
        ) not in {"read", "write", "check"}:
            raise DomainError(
                "INVALID_MANIFEST",
                "Tools require an input JSON Schema and scoped permission.",
                422,
            )
        mappings = manifest.get("provider_mappings")
        if (
            not isinstance(mappings, dict)
            or set(mappings) - {"claude", "mock"}
            or any(
                not isinstance(names, list)
                or any(
                    not isinstance(name, str)
                    or not re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_]*(?:\([^\r\n]{1,200}\))?", name
                    )
                    for name in names
                )
                for names in mappings.values()
            )
        ):
            raise DomainError(
                "INVALID_MANIFEST",
                "Tools require explicit supported provider_mappings; Codex has no verified narrow tool mapping.",
                422,
            )
    return data


def save_definition(
    session: Session, kind: str, data: dict[str, Any], current: Definition | None = None
) -> Definition:
    version_id = uid()
    if current is None:
        current = Definition(
            id=uid(),
            kind=kind,
            name=data["name"],
            data=data,
            current_version_id=version_id,
        )
        session.add(current)
        session.flush()
    else:
        current.revision += 1
        current.name, current.data, current.current_version_id = (
            data["name"],
            data,
            version_id,
        )
    session.add(
        ConfigVersion(
            id=version_id,
            definition_id=current.id,
            version=current.revision,
            config_hash=digest(data),
            data=data,
        )
    )
    session.add(
        Audit(
            actor="admin",
            action=kind + ".version_created",
            resource_id=current.id,
            payload={"config_hash": digest(data), "version": current.revision},
        )
    )
    session.flush()
    return current


def view(record: Base) -> dict[str, Any]:
    result = {
        column.key: getattr(record, column.key) for column in record.__table__.columns
    }
    if "data" in result:
        data = result.pop("data")
        result = {**data, **result}
    if isinstance(record, Definition):
        result["config_version_id"], result["config_hash"] = (
            record.current_version_id,
            digest(record.data),
        )
    if isinstance(record, Approval):
        expires = datetime.fromisoformat(record.expires_at)
        result["status"] = (
            "approved"
            if record.decision == "approve"
            else "rejected"
            if record.decision == "reject"
            else "expired"
            if expires <= datetime.now(UTC)
            else "pending"
        )
    if "storage_key" in result:
        result.pop("storage_key")
        result["checksum"] = result["sha256"]
    return result
