"""Typed, bounded public and internal request boundaries."""

import re
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Check(Input):
    id: str = Field(min_length=1, max_length=128)
    argv: list[str] = Field(min_length=1, max_length=40)
    timeout_sec: int = Field(default=300, ge=1, le=1800)


class ProjectInput(Input):
    name: str = Field(min_length=1, max_length=200)
    repo_path: str = Field(min_length=1, max_length=1024)
    allowed_root: str = Field(min_length=1, max_length=1024)
    default_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_./-]{1,128}$")
    checks: list[Check] = Field(default_factory=list, max_length=20)
    trusted: bool = False


class TaskInput(Input):
    project_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=32768)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=0, ge=-100, le=100)
    status: Literal["draft", "ready", "blocked", "cancelled"] = "draft"
    parent_id: str | None = None
    dependency_ids: list[str] = Field(default_factory=list, max_length=12)
    expected_files: list[str] = Field(default_factory=list, max_length=100)


class Limits(Input):
    max_parallel_agents: int = Field(default=3, ge=1, le=3)
    max_tasks_per_plan: int = Field(default=12, ge=1, le=12)
    max_retries: int = Field(default=2, ge=0, le=2)
    max_execution_minutes: int = Field(default=30, ge=1, le=30)
    max_run_minutes: int = Field(default=180, ge=1, le=180)
    max_output_bytes: int = Field(default=20971520, ge=1, le=20971520)
    max_disk_bytes: int = Field(default=5368709120, ge=1, le=5368709120)
    max_steps: int = Field(default=100, ge=1, le=1000)


class AgentInput(Input):
    name: str = Field(min_length=1, max_length=200)
    role: Literal[
        "Planner", "Architect", "Developer", "Tester", "Reviewer", "Integrator"
    ]
    provider: Literal["codex", "claude", "mock"]
    model: str | None = Field(default=None, max_length=128)
    prompt: str = Field(default="", max_length=32768)
    skill_ids: list[str] = Field(default_factory=list, max_length=50)
    tool_ids: list[str] = Field(default_factory=list, max_length=100)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=20)
    permission_profile: Literal["observe", "trusted-development"] = "observe"
    cli_version: str | None = Field(default=None, max_length=64)
    limits: Limits = Field(default_factory=Limits)


class WorkflowInput(Input):
    name: str = Field(min_length=1, max_length=200)
    graph_json: dict[str, Any]


class RegistryInput(Input):
    name: str = Field(min_length=1, max_length=200)
    manifest: dict[str, Any]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="allow")


class SkillManifest(Manifest):
    path: str = Field(min_length=1, max_length=1024)
    version: str = Field(min_length=1, max_length=128)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def codex_tool_name(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"mcp__([A-Za-z][A-Za-z0-9_-]{0,63})__([A-Za-z][A-Za-z0-9_-]{0,63})", value
    )
    if match is None or any("__" in component for component in match.groups()):
        return None
    return match[1], match[2]


class ToolManifest(Manifest):
    input_schema: dict[str, Any]
    permission: Literal["read", "write", "check"]
    provider_mappings: dict[Literal["codex", "claude", "mock"], list[str]]

    @model_validator(mode="after")
    def validate_mappings(self) -> Self:
        for provider, names in self.provider_mappings.items():
            if len(names) > 100 or len(names) != len(set(names)):
                raise ValueError("Tool mappings must be bounded and unique.")
            for name in names:
                if provider == "codex":
                    if codex_tool_name(name) is None:
                        raise ValueError(
                            "Codex mappings require exact MCP server/tool identifiers."
                        )
                elif not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]*(?:\([^\r\n]{1,200}\))?", name
                ):
                    raise ValueError("Provider tool mapping is invalid.")
        if "codex" in self.provider_mappings and self.permission != "read":
            raise ValueError("Codex MCP mappings require read permission.")
        return self


class ScopedFilesScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: Literal["adf.scoped-files.v1"]
    root: Literal["$WORKTREE"]
    read_paths: list[
        Annotated[str, Field(min_length=1, max_length=1024, strict=True)]
    ] = Field(min_length=1, max_length=100)
    max_bytes: int = Field(default=4096, ge=1, le=65536, strict=True)

    @field_validator("read_paths")
    @classmethod
    def exact_read_paths(cls, paths: list[str]) -> list[str]:
        denied = re.compile(
            r"(?:^|/)(?:\.env(?:\..*)?|auth\.json|credentials[^/]*|[^/]*\.(?:pem|key)|node_modules|\.venv|__pycache__)(?:/|$)",
            re.I,
        )
        denied_components = {
            ".agents",
            ".codex",
            ".claude",
            ".git",
            ".ssh",
            ".aws",
            ".azure",
            ".kube",
            ".gnupg",
            ".config",
        }
        denied_basenames = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        if len(paths) != len(set(paths)):
            raise ValueError("Scoped read paths must be unique.")
        for value in paths:
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or not path.parts
                or value != value.strip()
                or str(path) != value
                or ".." in path.parts
                or any(part.lower() in denied_components for part in path.parts)
                or path.name.lower() in denied_basenames
                or any(character in value for character in "*?[\\:")
                or any(ord(character) < 32 for character in value)
                or denied.search(value)
            ):
                raise ValueError("Scoped MCP paths must be exact safe relative files.")
        return paths


class MCPManifest(Manifest):
    transport: Literal["stdio"] = "stdio"
    argv: list[str] = Field(min_length=1, max_length=40)
    trusted: bool = Field(strict=True)
    allowed_tools: list[str] = Field(max_length=100)
    scope: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=30, ge=1, le=1800, strict=True)


class PolicyManifest(Limits):
    model_config = ConfigDict(extra="allow")
    allowed_write_paths: list[str] = Field(
        default_factory=lambda: ["**"], max_length=100
    )
    allow_commit: bool = Field(default=False, strict=True)
    trusted_repository: bool = Field(default=False, strict=True)
    allow_push: bool = Field(default=False, strict=True)
    allow_sudo: bool = Field(default=False, strict=True)
    provider_pin: Literal["codex", "claude", "mock"] | None = None
    provider_limits: dict[Literal["codex", "claude", "mock"], int] = Field(
        default_factory=dict
    )


class RunInput(Input):
    task_id: str
    workflow_version_id: str | None = None
    agent_config_version_id: str | None = None
    policy_id: str | None = None
    root_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    plan: dict[str, Any] | None = None


class DecomposeInput(Input):
    agent_config_version_id: str
    workflow_version_id: str | None = None
    policy_id: str | None = None


class DecisionInput(Input):
    decision: Literal["approve", "reject"]
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    execution_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    root_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    task_prompt: str | None = Field(default=None, max_length=32768)
    tree_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    target_execution_id: str | None = Field(default=None, max_length=36)
    check_ids: list[str] = Field(default_factory=list, max_length=20)
    integration_commits: list[str] = Field(default_factory=list, max_length=100)
    allowed_write_paths: list[str] = Field(default_factory=list, max_length=100)
    env_allowlist: list[str] = Field(default_factory=list, max_length=20)
    timeout_sec: int = Field(default=1800, ge=1, le=1800, strict=True)
    max_steps: int = Field(default=100, ge=1, le=1000, strict=True)
    manual_resolution: bool = Field(default=False, strict=True)
    resolution_tree_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    resolution_approval_id: str | None = Field(default=None, max_length=36)


class ExecutionInput(Input):
    execution_id: str
    node_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1, le=3)
    operation: Literal[
        "agent_task",
        "planner",
        "repo_inspect",
        "test",
        "review",
        "git_commit",
        "integrate",
    ]
    task_id: str | None = None
    agent_config_version_id: str | None = None
    dependency_execution_ids: list[str] = Field(default_factory=list, max_length=50)
    request: dict[str, Any] = Field(default_factory=dict)


class WorkerInput(Input):
    worker_id: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9_.-]+$")
    capabilities: dict[str, Any]
    allowed_roots: list[str] = Field(max_length=20)
    project_ids: list[str] = Field(default_factory=list, max_length=100)


class LeaseInput(Input):
    worker_id: str
    epoch: int = Field(ge=1)
    process_identity: dict[str, Any] | None = None


class WorkerEvent(Input):
    event_id: str
    sequence: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=128)
    time: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBatch(LeaseInput):
    events: list[WorkerEvent] = Field(max_length=100)


class ResultInput(LeaseInput):
    status: Literal["succeeded", "failed", "cancelled", "uncertain"]
    result: dict[str, Any] = Field(default_factory=dict)


class ArtifactInput(LeaseInput):
    artifact_id: str | None = None
    kind: str = Field(max_length=128)
    media_type: str = Field(default="text/plain", max_length=128)
    content_base64: str = Field(max_length=27962032)
    checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class NodeInput(Input):
    node_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(default=1, ge=1, le=3)
    status: Literal[
        "pending",
        "ready",
        "dispatched",
        "running",
        "waiting_approval",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
    ]
    result: dict[str, Any] = Field(default_factory=dict)


class TransitionInput(Input):
    status: Literal["queued", "running", "paused", "completed", "failed", "cancelled"]
    revision: int | None = None
    integration_status: (
        Literal["pending", "running", "passed", "needs_resolution", "failed"] | None
    ) = None
    result: dict[str, Any] | None = None


class ApprovalInput(Input):
    action: str = Field(min_length=1, max_length=128)
    snapshot: dict[str, Any]
    expires_at: str | None = None


class ReconcileInput(LeaseInput):
    process_stopped: bool
    evidence: dict[str, Any]
