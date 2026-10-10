"""Actual HTTP registry/assignment/claim contract for owned scoped Codex MCP tools."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from conftest import approve
from sqlalchemy import func, select

from adf_api.models import Execution, Run


def server_manifest() -> dict[str, Any]:
    return {
        "transport": "stdio",
        "argv": ["python3", "-m", "adf_runtime_worker.scoped_mcp"],
        "trusted": True,
        "allowed_tools": ["read_marker"],
        "scope": {
            "contract": "adf.scoped-files.v1",
            "root": "$WORKTREE",
            "read_paths": ["marker.txt"],
            "max_bytes": 4096,
        },
        "timeout_sec": 2,
    }


def tool_manifest(mapping: str = "mcp__factoryfiles__read_marker") -> dict[str, Any]:
    return {
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        "permission": "read",
        "provider_mappings": {"codex": [mapping], "mock": []},
    }


def registries(
    seeded: dict[str, Any],
    *,
    mapping: str = "mcp__factoryfiles__read_marker",
    manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client, admin = seeded["client"], seeded["admin"]
    server = client.post(
        "/api/mcp-servers",
        headers=admin,
        json={"name": "factoryfiles", "manifest": manifest or server_manifest()},
    )
    assert server.status_code == 201, server.text
    tool = client.post(
        "/api/tools",
        headers=admin,
        json={"name": "Read marker", "manifest": tool_manifest(mapping)},
    )
    assert tool.status_code == 201, tool.text
    return server.json(), tool.json()


def agent_body(server: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "Scoped developer",
        "role": "Developer",
        "provider": "codex",
        "cli_version": "0.157.0",
        "permission_profile": "trusted-development",
        "tool_ids": [tool["id"]],
        "mcp_server_ids": [server["id"]],
    }


def start_scoped_run(seeded: dict[str, Any]) -> dict[str, Any]:
    server, tool = registries(seeded)
    client, admin = seeded["client"], seeded["admin"]
    agent = client.post("/api/agents", headers=admin, json=agent_body(server, tool))
    assert agent.status_code == 201, agent.text
    task = client.post(
        "/api/tasks",
        headers=admin,
        json={
            "project_id": seeded["project"]["id"],
            "title": "Scoped fixture",
            "status": "ready",
            "expected_files": ["src/scoped.py"],
        },
    ).json()
    response = client.post(
        "/api/runs",
        headers={**admin, "Idempotency-Key": "scoped-run"},
        json={
            **seeded["run_body"],
            "task_id": task["id"],
            "agent_config_version_id": agent.json()["config_version_id"],
        },
    )
    assert response.status_code == 201, response.text
    seeded["run"] = response.json()
    approve(seeded)
    return {
        "server": server,
        "tool": tool,
        "agent": agent.json(),
        "run": response.json(),
    }


def dispatch(seeded: dict[str, Any]) -> Any:
    return seeded["client"].post(
        "/internal/runs/" + seeded["run"]["id"] + "/executions",
        headers=seeded["orch"],
        json={
            "execution_id": str(uuid4()),
            "node_id": "dev",
            "attempt": 1,
            "operation": "agent_task",
        },
    )


def test_scoped_assignment_claim_pins_names_and_manifest_versions(
    seeded: dict[str, Any],
) -> None:
    case = start_scoped_run(seeded)
    client, admin = seeded["client"], seeded["admin"]
    # Published registry changes must not silently rename or broaden an approved run.
    changed_server = client.patch(
        "/api/mcp-servers/" + case["server"]["id"],
        headers={**admin, "If-Match": "1"},
        json={
            "name": "renamed",
            "manifest": {
                **server_manifest(),
                "allowed_tools": ["read_file"],
                "scope": {**server_manifest()["scope"], "read_paths": ["other.txt"]},
            },
        },
    )
    assert changed_server.status_code == 200, changed_server.text
    changed_tool = client.patch(
        "/api/tools/" + case["tool"]["id"],
        headers={**admin, "If-Match": "1"},
        json={"manifest": tool_manifest("mcp__renamed__read_file")},
    )
    assert changed_tool.status_code == 200, changed_tool.text
    execution = dispatch(seeded)
    assert execution.status_code == 201, execution.text
    worker = "scoped-contract-worker"
    assert (
        client.post(
            "/internal/workers/register",
            headers=seeded["worker"],
            json={
                "worker_id": worker,
                "capabilities": {"codex": {"ready": True, "version": "0.157.0"}},
                "allowed_roots": ["/home/adf/ai-factory"],
            },
        ).status_code
        == 200
    )
    claim = client.post(
        "/internal/executions/" + execution.json()["id"] + "/claim",
        headers=seeded["worker"],
        json={"worker_id": worker},
    )
    assert claim.status_code == 200, claim.text
    agent = claim.json()["request"]["agent"]
    assert agent["tools"][0]["provider_mappings"]["codex"] == [
        "mcp__factoryfiles__read_marker"
    ]
    assert agent["tools"][0]["version_id"] == case["tool"]["config_version_id"]
    assert agent["mcp_servers"][0]["name"] == "factoryfiles"
    assert agent["mcp_servers"][0]["allowed_tools"] == ["read_marker"]
    assert agent["mcp_servers"][0]["scope"]["read_paths"] == ["marker.txt"]
    assert agent["mcp_servers"][0]["version_id"] == case["server"]["config_version_id"]
    assert agent["mcp_servers"][0]["timeout_sec"] == 2


@pytest.mark.parametrize(
    "mapping",
    [
        "Read",
        "shell",
        "exec_command",
        "Bash(cat marker.txt)",
        "mcp__factoryfiles__*",
        "mcp__factoryfiles__read__marker",
        "mcp__factory__files__read_marker",
        "mcp__factoryfiles__read_marker\n",
    ],
)
def test_codex_registry_rejects_builtin_shell_and_ambiguous_identifiers(
    harness: dict[str, Any], mapping: str
) -> None:
    response = harness["client"].post(
        "/api/tools",
        headers=harness["admin"],
        json={"name": "Denied", "manifest": tool_manifest(mapping)},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("permission", ["write", "check"])
def test_codex_registry_requires_read_permission(
    harness: dict[str, Any], permission: str
) -> None:
    manifest = {**tool_manifest(), "permission": permission}
    response = harness["client"].post(
        "/api/tools",
        headers=harness["admin"],
        json={"name": "Denied", "manifest": manifest},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "mapping",
    [
        "mcp__foreign__read_marker",
        "mcp__factoryfiles__read_file",
        "mcp__factoryfiles__forbidden_tool",
    ],
)
def test_assignment_rejects_unassigned_or_unallowlisted_tool(
    seeded: dict[str, Any], mapping: str
) -> None:
    server, tool = registries(seeded, mapping=mapping)
    response = seeded["client"].post(
        "/api/agents", headers=seeded["admin"], json=agent_body(server, tool)
    )
    assert (
        response.status_code == 403 and response.json()["code"] == "TOOL_SCOPE_DENIED"
    ), response.text


def test_agent_update_cannot_drop_assigned_mcp_or_publish_invalid_version(
    seeded: dict[str, Any],
) -> None:
    server, tool = registries(seeded)
    created = (
        seeded["client"]
        .post("/api/agents", headers=seeded["admin"], json=agent_body(server, tool))
        .json()
    )
    route = "/api/agents/" + created["id"]
    response = seeded["client"].patch(
        route, headers={**seeded["admin"], "If-Match": "1"}, json={"mcp_server_ids": []}
    )
    assert response.json()["code"] == "TOOL_SCOPE_DENIED"
    current = seeded["client"].get(route, headers=seeded["admin"]).json()
    assert (
        current["revision"] == 1
        and current["config_version_id"] == created["config_version_id"]
    )


def test_explicit_manifest_name_is_immutable_assignment_identity(
    seeded: dict[str, Any],
) -> None:
    manifest = {**server_manifest(), "name": "factoryfiles"}
    server, tool = registries(seeded, manifest=manifest)
    renamed = (
        seeded["client"]
        .patch(
            "/api/mcp-servers/" + server["id"],
            headers={**seeded["admin"], "If-Match": "1"},
            json={"name": "Display label"},
        )
        .json()
    )
    created = seeded["client"].post(
        "/api/agents", headers=seeded["admin"], json=agent_body(renamed, tool)
    )
    assert created.status_code == 201, created.text


@pytest.mark.parametrize(
    "scope",
    [
        {"root": "/home/adf/ai-factory"},
        {"read_paths": ["../secret.txt"]},
        {"read_paths": ["."]},
        {"read_paths": [".env"]},
        {"read_paths": ["auth.json"]},
        {"read_paths": [".git/config"]},
        {"read_paths": [".agents/SKILL.md"]},
        {"read_paths": [".codex/config.toml"]},
        {"read_paths": ["keys/private.pem"]},
        {"read_paths": ["*.txt"]},
        {"read_paths": [" marker.txt"]},
        {"read_paths": ["marker.txt\u0000"]},
        {"read_paths": []},
        {"read_paths": ["marker.txt", "marker.txt"]},
        {"max_bytes": True},
        {"max_bytes": 0},
        {"max_bytes": 65537},
        {"network": True},
    ],
)
def test_scoped_registry_rejects_wider_unsafe_or_unbounded_scope(
    harness: dict[str, Any], scope: dict[str, Any]
) -> None:
    manifest = server_manifest()
    manifest["scope"] = {**manifest["scope"], **scope}
    response = harness["client"].post(
        "/api/mcp-servers",
        headers=harness["admin"],
        json={"name": "factoryfiles", "manifest": manifest},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"argv": ["bash", "-c", "cat marker.txt"]},
        {"argv": ["python3", "-m", "adf_runtime_worker.scoped_mcp", "--root", "/"]},
        {"allowed_tools": ["forbidden_tool"]},
        {"allowed_tools": ["read_marker", "read_marker"]},
        {"allowed_tools": []},
        {"name": "unsafe__name"},
        {"trusted": False},
        {"timeout_sec": True},
    ],
)
def test_scoped_registry_rejects_foreign_commands_tools_and_quotas(
    harness: dict[str, Any], changes: dict[str, Any]
) -> None:
    response = harness["client"].post(
        "/api/mcp-servers",
        headers=harness["admin"],
        json={"name": "factoryfiles", "manifest": {**server_manifest(), **changes}},
    )
    assert response.status_code in {403, 422}, response.text


def test_generic_server_scope_cannot_be_assigned_to_codex(
    seeded: dict[str, Any],
) -> None:
    manifest = {**server_manifest(), "scope": {"roots": ["/home/adf/ai-factory"]}}
    server, tool = registries(seeded, manifest=manifest)
    result = seeded["client"].post(
        "/api/agents", headers=seeded["admin"], json=agent_body(server, tool)
    )
    assert result.status_code == 422, result.text


def test_duplicate_assigned_server_names_are_denied(seeded: dict[str, Any]) -> None:
    server, tool = registries(seeded)
    second = (
        seeded["client"]
        .post(
            "/api/mcp-servers",
            headers=seeded["admin"],
            json={"name": "factoryfiles", "manifest": server_manifest()},
        )
        .json()
    )
    result = seeded["client"].post(
        "/api/agents",
        headers=seeded["admin"],
        json={
            **agent_body(server, tool),
            "mcp_server_ids": [server["id"], second["id"]],
        },
    )
    assert result.status_code == 422 and result.json()["code"] == "INVALID_MANIFEST", (
        result.text
    )


@pytest.mark.parametrize("fault", ["foreign_tool", "write_permission", "wide_root"])
def test_runtime_revalidates_pinned_capability_before_dispatch(
    seeded: dict[str, Any], fault: str
) -> None:
    case = start_scoped_run(seeded)
    with seeded["db"].transaction() as session:
        run = session.get(Run, case["run"]["id"])
        snapshot = deepcopy(run.snapshot)
        agent = snapshot["agents_by_role"]["Developer"]
        if fault == "foreign_tool":
            agent["registry_snapshots"]["tool_ids"][0]["provider_mappings"]["codex"] = [
                "mcp__foreign__read_marker"
            ]
        elif fault == "write_permission":
            agent["registry_snapshots"]["tool_ids"][0]["permission"] = "write"
        else:
            agent["registry_snapshots"]["mcp_server_ids"][0]["scope"]["root"] = "/"
        run.snapshot = snapshot
        count = session.scalar(
            select(func.count())
            .select_from(Execution)
            .where(Execution.run_id == run.id)
        )
    response = dispatch(seeded)
    assert response.status_code in {403, 422}, response.text
    with seeded["db"].transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.run_id == case["run"]["id"])
            )
            == count
        )


def test_run_pin_rejects_registry_drift_that_breaks_assignment(
    seeded: dict[str, Any],
) -> None:
    server, tool = registries(seeded)
    agent = (
        seeded["client"]
        .post("/api/agents", headers=seeded["admin"], json=agent_body(server, tool))
        .json()
    )
    result = seeded["client"].patch(
        "/api/mcp-servers/" + server["id"],
        headers={**seeded["admin"], "If-Match": "1"},
        json={"name": "foreign"},
    )
    assert result.status_code == 200, result.text
    task = (
        seeded["client"]
        .post(
            "/api/tasks",
            headers=seeded["admin"],
            json={"project_id": seeded["project"]["id"], "title": "Fresh task"},
        )
        .json()
    )
    result = seeded["client"].post(
        "/api/runs",
        headers={**seeded["admin"], "Idempotency-Key": "drift-run"},
        json={
            **seeded["run_body"],
            "task_id": task["id"],
            "agent_config_version_id": agent["config_version_id"],
        },
    )
    assert result.status_code == 403 and result.json()["code"] == "TOOL_SCOPE_DENIED", (
        result.text
    )


@pytest.mark.parametrize(
    "path",
    [
        "fixtures/" + component + "/config"
        for component in [
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
            ".AgEnTs",
            ".CoDeX",
            ".ClAuDe",
            ".GiT",
            ".SsH",
            ".AwS",
            ".AzUrE",
            ".KuBe",
            ".GnUpG",
            ".CoNfIg",
        ]
    ]
    + [
        "fixtures/keys/" + name
        for name in [
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "ID_RSA",
            "ID_DSA",
            "ID_ECDSA",
            "ID_ED25519",
        ]
    ],
)
def test_scoped_registry_rejects_case_insensitive_credential_stores(
    harness: dict[str, Any], path: str
) -> None:
    manifest = server_manifest()
    manifest["scope"] = {**manifest["scope"], "read_paths": [path]}
    response = harness["client"].post(
        "/api/mcp-servers",
        headers=harness["admin"],
        json={"name": "factoryfiles", "manifest": manifest},
    )
    assert response.status_code == 422, response.text


def test_scoped_registry_keeps_exact_public_key_filename_boundary(
    harness: dict[str, Any],
) -> None:
    manifest = server_manifest()
    manifest["scope"] = {**manifest["scope"], "read_paths": ["fixtures/ID_RSA.pub"]}
    response = harness["client"].post(
        "/api/mcp-servers",
        headers=harness["admin"],
        json={"name": "factoryfiles", "manifest": manifest},
    )
    assert response.status_code == 201, response.text
