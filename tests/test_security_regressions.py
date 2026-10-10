"""Regressions for security boundaries and durable metadata on actual HTTP wire."""

import base64
import hashlib
from typing import Any
from uuid import uuid4

from conftest import approve
from sqlalchemy import select
from test_worker_ledger import allocation

from adf_api.models import Command, Execution, Run


def dispatch(seeded: dict[str, Any], **fields: Any) -> Any:
    return seeded["client"].post(
        "/internal/runs/" + seeded["run"]["id"] + "/executions",
        headers=seeded["orch"],
        json={
            "execution_id": str(uuid4()),
            "node_id": "dev",
            "attempt": 1,
            "operation": "agent_task",
            **fields,
        },
    )


def test_incomplete_approval_and_scope_enlargement_denied(
    seeded: dict[str, Any],
) -> None:
    run = seeded["run"]
    body = {
        "action": "execute_plan",
        "snapshot": {
            "graph_hash": run["graph_hash"],
            "plan_hash": run["plan_hash"],
            "root_base_sha": run["root_base_sha"],
            "policy_hash": run["policy"]["config_hash"],
        },
    }
    response = seeded["client"].post(
        "/internal/runs/" + run["id"] + "/approvals", headers=seeded["orch"], json=body
    )
    assert response.json()["code"] == "SNAPSHOT_STALE"
    approve(seeded)
    assert (
        dispatch(seeded, request={"allowed_write_paths": ["**"]}).json()["code"]
        == "WRITE_SCOPE_DENIED"
    )
    result = dispatch(seeded)
    assert result.status_code == 201, result.text
    assert result.json()["request"]["allowed_write_paths"] == ["src/a.py"]
    assert (
        result.json()["request"]["agent"]["id"] == seeded["agents"]["Developer"]["id"]
    )
    assert (
        result.json()["request"]["agent_config_version"]
        == seeded["agents"]["Developer"]["config_version_id"]
    )


def test_same_definition_different_version_cannot_review_itself(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    client = seeded["client"]
    route = "/internal/executions/" + execution_id
    assert (
        client.post(
            route + "/result",
            headers=seeded["worker"],
            json={**lease, "status": "succeeded", "result": {"tree_sha": "b" * 40}},
        ).status_code
        == 200
    )
    agent = seeded["agents"]["Developer"]
    updated = client.patch(
        "/api/agents/" + agent["id"],
        headers={**seeded["admin"], "If-Match": "1"},
        json={"role": "Reviewer", "permission_profile": "observe"},
    ).json()
    # Seed the immutable version in this run as graph publication/run creation would.
    with seeded["db"].transaction() as session:
        run = session.get(Run, seeded["run"]["id"])
        old = dict(run.snapshot["agent"])
        reviewer = {
            **old,
            "role": "Reviewer",
            "config_version_id": updated["config_version_id"],
            "config_hash": updated["config_hash"],
        }
        run.snapshot = {
            **run.snapshot,
            "agent_versions": {
                **run.snapshot["agent_versions"],
                updated["config_version_id"]: reviewer,
            },
        }
    response = dispatch(
        seeded,
        operation="review",
        agent_config_version_id=updated["config_version_id"],
        request={"target_execution_id": execution_id, "tree_sha": "b" * 40},
    )
    assert (
        response.status_code == 403
        and response.json()["code"] == "INDEPENDENT_REVIEW_REQUIRED"
    )


def test_dependency_requires_accepted_commit_not_mere_success(
    seeded: dict[str, Any],
) -> None:
    client = seeded["client"]
    response = client.post(
        "/internal/runs/" + seeded["run"]["id"] + "/plan",
        headers=seeded["orch"],
        json={
            "tasks": [
                {"id": "a", "title": "A", "expected_files": ["src/a.py"]},
                {
                    "id": "b",
                    "title": "B",
                    "expected_files": ["src/b.py"],
                    "dependency_ids": ["a"],
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    tasks = response.json()["tasks"]
    seeded["run"] = client.get(
        "/api/runs/" + seeded["run"]["id"], headers=seeded["admin"]
    ).json()
    approve(seeded)
    first = dispatch(seeded, node_id="a", task_id=tasks[0]["id"]).json()
    with seeded["db"].transaction() as session:
        row = session.get(Execution, first["id"])
        row.status, row.result = "succeeded", {"tree_sha": "b" * 40}
    response = dispatch(
        seeded,
        node_id="b",
        task_id=tasks[1]["id"],
        dependency_execution_ids=[first["id"]],
    )
    assert response.json()["code"] == "DEPENDENCY_PENDING"


def test_manual_task_dependency_cannot_start_before_integration(
    seeded: dict[str, Any],
) -> None:
    client = seeded["client"]
    task = client.post(
        "/api/tasks",
        headers=seeded["admin"],
        json={
            "project_id": seeded["project"]["id"],
            "title": "B",
            "dependency_ids": [seeded["task"]["id"]],
        },
    ).json()
    response = client.post(
        "/api/runs",
        headers={**seeded["admin"], "Idempotency-Key": "blocked-B"},
        json={**seeded["run_body"], "task_id": task["id"]},
    )
    assert response.json()["code"] == "DEPENDENCY_PENDING"


def test_agent_limits_and_check_timeouts_survive_wire(seeded: dict[str, Any]) -> None:
    with seeded["db"].transaction() as session:
        run = session.get(Run, seeded["run"]["id"])
        agent = {
            **run.snapshot["agent"],
            "limits": {
                "max_execution_minutes": 1,
                "max_steps": 100,
                "max_output_bytes": 4096,
                "max_disk_bytes": 1048576,
            },
        }
        run.snapshot = {
            **run.snapshot,
            "agent": agent,
            "agents_by_role": {**run.snapshot["agents_by_role"], "Developer": agent},
        }
    approve(seeded)
    result = dispatch(seeded, request={"timeout_sec": 900, "max_steps": 500}).json()[
        "request"
    ]
    assert result["timeout_sec"] == 60 and result["max_steps"] == 100
    assert result["max_output_bytes"] == 4096 and result["max_disk_bytes"] == 1048576
    assert result["project"]["check_timeouts"]["unit"] == 300


def test_trace_context_cas_and_job_propagation(seeded: dict[str, Any]) -> None:
    route = "/internal/runs/" + seeded["run"]["id"] + "/trace"
    context = {
        "trace_id": "a" * 32,
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    }
    first = seeded["client"].post(route, headers=seeded["orch"], json=context)
    second = seeded["client"].post(
        route,
        headers=seeded["orch"],
        json={
            "trace_id": "c" * 32,
            "traceparent": "00-" + "c" * 32 + "-" + "d" * 16 + "-01",
        },
    )
    assert first.json() == second.json() == context
    approve(seeded)
    assert dispatch(seeded).json()["request"]["traceparent"] == context["traceparent"]


def test_plan_replay_keeps_ids_and_edit_clears_proposal(seeded: dict[str, Any]) -> None:
    client = seeded["client"]
    route = "/internal/runs/" + seeded["run"]["id"]
    plan = {"tasks": [{"id": "a", "title": "A", "expected_files": ["src/a.py"]}]}
    first = client.post(route + "/plan", headers=seeded["orch"], json=plan).json()
    assert (
        first == client.post(route + "/plan", headers=seeded["orch"], json=plan).json()
    )
    run = client.get("/api/runs/" + seeded["run"]["id"], headers=seeded["admin"]).json()
    graph = {"nodes": [{"id": "start", "type": "start"}], "edges": []}
    proposal = client.post(
        route + "/graph-proposal",
        headers=seeded["orch"],
        json={"plan_hash": run["plan_hash"], "graph_json": graph},
    )
    assert proposal.status_code == 200
    changed = client.patch(
        "/api/runs/" + run["id"] + "/plan",
        headers={**seeded["admin"], "Idempotency-Key": "edit"},
        json={"revision": run["revision"], "tasks": [{"id": "b", "title": "B"}]},
    ).json()
    assert "graph_proposal" not in changed["result"]
    with seeded["db"].transaction() as session:
        assert (
            session.scalar(
                select(Command).where(
                    Command.run_id == run["id"], Command.kind == "refresh_proposal"
                )
            )
            is not None
        )


def test_large_artifact_preserved_and_usage_aggregated(seeded: dict[str, Any]) -> None:
    execution_id, lease = allocation(seeded)
    client, route = seeded["client"], "/internal/executions/" + execution_id
    content = b"safe-content " * 10000
    response = client.post(
        route + "/artifacts",
        headers=seeded["worker"],
        json={
            **lease,
            "artifact_id": str(uuid4()),
            "kind": "log",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
            "checksum": hashlib.sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 201, response.text
    download = client.get(
        "/api/artifacts/" + response.json()["id"] + "/content", headers=seeded["admin"]
    )
    assert download.content == content
    result = client.post(
        route + "/result",
        headers=seeded["worker"],
        json={
            **lease,
            "status": "succeeded",
            "result": {
                "tree_sha": "b" * 40,
                "usage": {"input_tokens": 12, "output_tokens": 8, "cost": 0.01},
            },
        },
    )
    assert result.status_code == 200
    usage = client.get("/api/metrics/overview", headers=seeded["admin"]).json()["usage"]
    assert (
        usage["input_tokens"] == 12
        and usage["output_tokens"] == 8
        and usage["cost"] == 0.01
        and usage["is_complete"]
    )


def test_exact_tree_all_registered_checks_required(seeded: dict[str, Any]) -> None:
    execution_id, lease = allocation(seeded)
    client, route = seeded["client"], "/internal/executions/" + execution_id
    client.post(
        route + "/result",
        headers=seeded["worker"],
        json={**lease, "status": "succeeded", "result": {"tree_sha": "b" * 40}},
    )
    test = dispatch(
        seeded,
        operation="test",
        request={"target_execution_id": execution_id, "tree_sha": "b" * 40},
    ).json()
    route = "/internal/executions/" + test["id"]
    claim = client.post(
        route + "/claim", headers=seeded["worker"], json={"worker_id": "test-worker"}
    ).json()
    response = client.post(
        route + "/result",
        headers=seeded["worker"],
        json={
            "worker_id": "test-worker",
            "epoch": claim["epoch"],
            "status": "succeeded",
            "result": {
                "tree_sha": "b" * 40,
                "test_results": [
                    {
                        "check_id": "unit",
                        "status": "passed",
                        "exit_code": 0,
                        "tree_sha": "c" * 40,
                    }
                ],
            },
        },
    )
    assert response.json()["code"] == "TEST_EVIDENCE_REQUIRED"


def test_full_sse_frame_budget_and_replay(seeded: dict[str, Any]) -> None:
    client, run_id = seeded["client"], seeded["run"]["id"]
    route = "/internal/runs/" + run_id
    assert (
        client.post(
            route + "/events",
            headers=seeded["orch"],
            json={"type": "output", "payload": {"text": "ж" * 29990}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            route + "/events",
            headers=seeded["orch"],
            json={"type": "output", "payload": {"text": "ж" * 30000}},
        ).status_code
        == 413
    )
    client.post(
        route + "/transition", headers=seeded["orch"], json={"status": "failed"}
    )
    response = client.get(
        "/api/runs/" + run_id + "/events",
        headers={**seeded["admin"], "Last-Event-ID": "1"},
    )
    frames = response.text.split("\n\n")
    assert all(len(frame.encode()) + 2 <= 65536 for frame in frames)
    assert "id: 1\n" not in response.text and "id: 2\n" in response.text


def test_dead_letter_is_durable_sanitized_and_idempotent(
    seeded: dict[str, Any],
) -> None:
    allocation(seeded)
    body = {
        "message_id": "123-1",
        "stream": "adf:executions",
        "reason": seeded["settings"].admin_token,
        "field_names": ["cwd"],
    }
    route = "/internal/workers/test-worker/dead-letter"
    first = seeded["client"].post(route, headers=seeded["worker"], json=body)
    assert (
        first.json()
        == seeded["client"].post(route, headers=seeded["worker"], json=body).json()
    )
    assert (
        first.json()["persisted"] and seeded["settings"].admin_token not in first.text
    )


def test_chunked_body_bound_and_secret_settings_repr(harness: dict[str, Any]) -> None:
    response = harness["client"].post(
        "/api/projects",
        headers={**harness["admin"], "Content-Type": "application/json"},
        content=iter([b'{"name":"', b"a" * 1048576, b'"}']),
    )
    assert response.status_code == 413, response.text
    assert harness["settings"].admin_token not in repr(harness["settings"])
    schema = harness["client"].get("/openapi.json", headers=harness["admin"]).json()
    assert schema["paths"]["/api/runs"]["post"]["security"] == [{"ScopedBearer": []}]


def test_terminal_commit_tree_must_match_approved_target(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    # Build a claimed ledger record as a prior gated test/review/commit dispatch would.
    with seeded["db"].transaction() as session:
        row = session.get(Execution, execution_id)
        row.operation = "git_commit"
        row.request = {**row.request, "tree_sha": "b" * 40}
    response = seeded["client"].post(
        "/internal/executions/" + execution_id + "/result",
        headers=seeded["worker"],
        json={
            **lease,
            "status": "succeeded",
            "result": {"tree_sha": "c" * 40, "commit_sha": "d" * 40},
        },
    )
    assert response.json()["code"] == "TREE_CHANGED"


def test_integration_cannot_omit_an_accepted_task_commit(
    seeded: dict[str, Any],
) -> None:
    approve(seeded)
    with seeded["db"].transaction() as session:
        for node, commit in [("commit-a", "b" * 40), ("commit-b", "c" * 40)]:
            session.add(
                Execution(
                    id=str(uuid4()),
                    run_id=seeded["run"]["id"],
                    node_id=node,
                    attempt=1,
                    operation="git_commit",
                    status="succeeded",
                    request_hash="d" * 64,
                    request={"task_id": seeded["task"]["id"]},
                    result={"commit_sha": commit, "tree_sha": "e" * 40},
                )
            )
    response = dispatch(
        seeded, operation="integrate", request={"integration_commits": ["b" * 40]}
    )
    assert (
        response.status_code == 403
        and response.json()["code"] == "INTEGRATION_SCOPE_DENIED"
    )
    response = dispatch(
        seeded,
        operation="integrate",
        request={"integration_commits": ["b" * 40, "c" * 40]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["request"]["predecessor_commits"] == []


def test_completion_rejects_old_integration_missing_new_commit(
    seeded: dict[str, Any],
) -> None:
    client, run_id = seeded["client"], seeded["run"]["id"]
    with seeded["db"].transaction() as session:
        run = session.get(Run, run_id)
        run.status = "running"
        session.add(
            Execution(
                id=str(uuid4()),
                run_id=run_id,
                node_id="commit-b",
                attempt=1,
                operation="git_commit",
                status="succeeded",
                request_hash="d" * 64,
                request={"task_id": seeded["task"]["id"]},
                result={"commit_sha": "c" * 40, "tree_sha": "e" * 40},
            )
        )
        session.add(
            Execution(
                id=str(uuid4()),
                run_id=run_id,
                node_id="integration",
                attempt=1,
                operation="integrate",
                status="succeeded",
                request_hash="d" * 64,
                request={"integration_commits": ["b" * 40]},
                result={
                    "tree_sha": "e" * 40,
                    "test_results": [
                        {
                            "check_id": "unit",
                            "tree_sha": "e" * 40,
                            "status": "passed",
                            "exit_code": 0,
                        }
                    ],
                },
            )
        )
    response = client.post(
        "/internal/runs/" + run_id + "/transition",
        headers=seeded["orch"],
        json={"status": "completed", "integration_status": "passed"},
    )
    assert response.json()["code"] == "INTEGRATION_EVIDENCE_REQUIRED"
    task = client.get(
        "/api/tasks/" + seeded["task"]["id"], headers=seeded["admin"]
    ).json()
    assert task["status"] != "done"


def test_invalid_execution_limits_are_typed_errors(seeded: dict[str, Any]) -> None:
    approve(seeded)
    response = dispatch(seeded, request={"timeout_sec": "untrusted-input"})
    assert response.status_code == 422 and "untrusted-input" not in response.text


def test_cancelled_success_still_requires_stopped_process(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    route = "/internal/executions/" + execution_id
    seeded["client"].post(
        route + "/heartbeat",
        headers=seeded["worker"],
        json={
            **lease,
            "process_identity": {"pid": 123, "start_ticks": 99, "pgid": 123},
        },
    )
    seeded["client"].post(
        "/api/runs/" + seeded["run"]["id"] + "/cancel",
        headers={**seeded["admin"], "Idempotency-Key": "cancel"},
    )
    result = seeded["client"].post(
        route + "/result",
        headers=seeded["worker"],
        json={**lease, "status": "succeeded", "result": {"process_stopped": False}},
    )
    assert result.json()["code"] == "PROCESS_PROOF_REQUIRED"


def test_manifest_permissions_are_typed_not_truthy(harness: dict[str, Any]) -> None:
    response = harness["client"].post(
        "/api/policies",
        headers=harness["admin"],
        json={"name": "invalid", "manifest": {"allow_commit": "false"}},
    )
    assert response.status_code == 422
    response = harness["client"].post(
        "/api/tools",
        headers=harness["admin"],
        json={
            "name": "invalid",
            "manifest": {
                "permission": "read",
                "input_schema": {},
                "provider_mappings": {"codex": ["Read"]},
            },
        },
    )
    assert response.status_code == 422


def test_manual_graph_child_scope_survives_test_dispatch_without_plan(
    seeded: dict[str, Any],
) -> None:
    client, admin = seeded["client"], seeded["admin"]
    root = client.post(
        "/api/tasks",
        headers=admin,
        json={
            "project_id": seeded["project"]["id"],
            "title": "Manual parallel root",
            "expected_files": ["src/root.py"],
            "status": "ready",
        },
    ).json()
    child_paths = ["src/child.py", "src/test_child.py"]
    child = client.post(
        "/api/tasks",
        headers=admin,
        json={
            "project_id": seeded["project"]["id"],
            "parent_id": root["id"],
            "title": "Manual child",
            "expected_files": child_paths,
            "status": "ready",
        },
    ).json()
    graph = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "child_agent",
                "type": "agent_task",
                "config": {"task_id": child["id"], "expected_files": child_paths},
            },
            {
                "id": "child_test",
                "type": "test",
                "config": {"target_node": "child_agent", "check_ids": ["unit"]},
            },
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "child_agent"},
            {"source": "child_agent", "target": "child_test"},
            {"source": "child_test", "target": "end"},
        ],
    }
    workflow = client.post(
        "/api/workflows",
        headers=admin,
        json={"name": "Manual child wire", "graph_json": graph},
    ).json()
    version = client.post(
        "/api/workflows/" + workflow["id"] + "/versions", headers=admin
    ).json()
    response = client.post(
        "/api/runs",
        headers={**admin, "Idempotency-Key": "manual-child-run"},
        json={
            **seeded["run_body"],
            "task_id": root["id"],
            "workflow_version_id": version["id"],
        },
    )
    assert response.status_code == 201, response.text
    seeded["run"] = response.json()
    assert seeded["run"]["plan"] is None
    approval = approve(seeded)
    assert approval["snapshot"]["allowed_write_paths"] == sorted(
        ["src/root.py", *child_paths]
    )
    agent = dispatch(
        seeded,
        task_id=child["id"],
        node_id="child_agent",
        request={"allowed_write_paths": child_paths},
    )
    assert agent.status_code == 201, agent.text
    execution_id = agent.json()["id"]
    registration = client.post(
        "/internal/workers/register",
        headers=seeded["worker"],
        json={
            "worker_id": "manual-child-worker",
            "capabilities": {"mock": {"ready": True}},
            "allowed_roots": ["/home/adf/ai-factory"],
        },
    )
    assert registration.status_code == 200
    claim = client.post(
        "/internal/executions/" + execution_id + "/claim",
        headers=seeded["worker"],
        json={"worker_id": "manual-child-worker"},
    ).json()
    result = client.post(
        "/internal/executions/" + execution_id + "/result",
        headers=seeded["worker"],
        json={
            "worker_id": "manual-child-worker",
            "epoch": claim["epoch"],
            "status": "succeeded",
            "result": {"tree_sha": "b" * 40, "process_stopped": True},
        },
    )
    assert result.status_code == 200
    request = {
        "target_execution_id": execution_id,
        "tree_sha": "b" * 40,
        "allowed_write_paths": child_paths,
        "check_ids": ["unit"],
    }
    test = dispatch(
        seeded,
        operation="test",
        task_id=child["id"],
        node_id="child_test",
        dependency_execution_ids=[execution_id],
        request=request,
    )
    assert test.status_code == 201, test.text
    assert test.json()["request"]["task_id"] == child["id"]
    assert test.json()["request"]["allowed_write_paths"] == child_paths
    widened = dispatch(
        seeded,
        operation="test",
        task_id=child["id"],
        node_id="another_test",
        request={**request, "allowed_write_paths": ["src/root.py", *child_paths]},
    )
    assert widened.status_code == 403 and widened.json()["code"] == "WRITE_SCOPE_DENIED"
