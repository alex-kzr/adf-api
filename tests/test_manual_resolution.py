"""Exact operator integration continuation is independent of automatic retry budgets."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from conftest import approve
from sqlalchemy import func, select

from adf_api.models import Approval, Event, Execution, Outbox, Run
from adf_api.privacy import digest


def conflict_setup(seeded: dict[str, Any], *, resume: bool = True) -> dict[str, Any]:
    """Use HTTP controls/worker evidence; only the preaccepted task commit is a fixture."""
    client, admin, orch = seeded["client"], seeded["admin"], seeded["orch"]
    foreign_run_id = seeded["run"]["id"]
    task = client.post(
        "/api/tasks",
        headers=admin,
        json={
            "project_id": seeded["project"]["id"],
            "title": "Resolve integration conflict",
            "expected_files": ["src/a.py"],
            "status": "ready",
        },
    )
    assert task.status_code == 201, task.text
    seeded["task"] = task.json()
    policy = client.post(
        "/api/policies",
        headers=admin,
        json={
            "name": "No automatic retries",
            "manifest": {
                "max_retries": 0,
                "allow_commit": True,
                "trusted_repository": True,
                "allowed_write_paths": ["src/**"],
            },
        },
    )
    assert policy.status_code == 201, policy.text
    response = client.post(
        "/api/runs",
        headers={**admin, "Idempotency-Key": "resolution-run-" + str(uuid4())},
        json={
            **seeded["run_body"],
            "task_id": seeded["task"]["id"],
            "policy_id": policy.json()["id"],
        },
    )
    assert response.status_code == 201, response.text
    seeded["run"] = response.json()
    run_id = seeded["run"]["id"]
    route = "/internal/runs/" + run_id
    approve(seeded)
    with seeded["db"].transaction() as session:
        session.add(
            Execution(
                id=str(uuid4()),
                run_id=run_id,
                node_id="accepted-commit",
                attempt=1,
                operation="git_commit",
                status="succeeded",
                request_hash="d" * 64,
                request={"task_id": seeded["task"]["id"]},
                result={"commit_sha": "b" * 40, "tree_sha": "c" * 40},
            )
        )
    assert (
        client.post(
            route + "/transition", headers=orch, json={"status": "running"}
        ).status_code
        == 200
    )
    original = client.post(
        route + "/executions",
        headers=orch,
        json={
            "execution_id": str(uuid4()),
            "node_id": "end",
            "attempt": 1,
            "operation": "integrate",
            "request": {"integration_commits": ["b" * 40]},
        },
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]
    registration = client.post(
        "/internal/workers/register",
        headers=seeded["worker"],
        json={
            "worker_id": "resolution-worker",
            "capabilities": {"mock": {"ready": True}},
            "allowed_roots": ["/home/adf/ai-factory"],
        },
    )
    assert registration.status_code == 200, registration.text
    claim = client.post(
        "/internal/executions/" + original_id + "/claim",
        headers=seeded["worker"],
        json={"worker_id": "resolution-worker"},
    )
    assert claim.status_code == 200, claim.text
    failed = client.post(
        "/internal/executions/" + original_id + "/result",
        headers=seeded["worker"],
        json={
            "worker_id": "resolution-worker",
            "epoch": claim.json()["epoch"],
            "status": "failed",
            "result": {
                "error_code": "INTEGRATION_CONFLICT",
                "integration_status": "needs_resolution",
                "process_stopped": True,
                "conflicting_paths": ["src/a.py"],
            },
        },
    )
    assert failed.status_code == 200, failed.text
    paused = client.post(
        route + "/transition",
        headers=orch,
        json={"status": "paused", "integration_status": "needs_resolution"},
    )
    assert paused.status_code == 200, paused.text
    resolution = client.post(
        "/api/runs/" + run_id + "/resolution",
        headers={**admin, "Idempotency-Key": "resolution-tree"},
        json={"execution_id": original_id, "tree_sha": "e" * 40},
    )
    assert resolution.status_code == 201, resolution.text
    approval = resolution.json()
    if resume:
        decision = client.post(
            "/api/approvals/" + approval["id"] + "/decision",
            headers={**admin, "Idempotency-Key": "resolution-approve"},
            json={
                "decision": "approve",
                "snapshot_hash": approval["snapshot_hash"],
                "revision": approval["revision"],
            },
        )
        assert decision.status_code == 200, decision.text
        resumed = client.post(
            "/api/runs/" + run_id + "/resume",
            headers={**admin, "Idempotency-Key": "resolution-resume"},
        )
        assert resumed.status_code == 202, resumed.text
    return {
        "run_id": run_id,
        "foreign_run_id": foreign_run_id,
        "route": route,
        "original_id": original_id,
        "approval": approval,
        "body": {
            "execution_id": str(uuid4()),
            "node_id": "end",
            "attempt": 2,
            "operation": "integrate",
            "request": {
                "integration_commits": ["b" * 40],
                "manual_resolution": True,
                "target_execution_id": original_id,
                "resolution_tree_sha": "e" * 40,
                "resolution_approval_id": approval["id"],
            },
        },
    }


def ledger_counts(seeded: dict[str, Any], run_id: str) -> tuple[int, int, int]:
    with seeded["db"].transaction() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.run_id == run_id)
            ),
            session.scalar(
                select(func.count()).select_from(Event).where(Event.run_id == run_id)
            ),
            session.scalar(select(func.count()).select_from(Outbox)),
        )


def test_exact_operator_continuation_with_zero_retries_is_once_only(
    seeded: dict[str, Any],
) -> None:
    case = conflict_setup(seeded)
    client, orch = seeded["client"], seeded["orch"]
    response = client.post(
        case["route"] + "/executions", headers=orch, json=case["body"]
    )
    assert response.status_code == 201, response.text
    request = response.json()["request"]
    assert request["policy"]["max_retries"] == 0
    assert request["resolution_approved"] is True
    assert request["target_execution_id"] == case["original_id"]
    assert request["resolution_tree_sha"] == "e" * 40
    assert request["predecessor_commits"] == []
    counts = ledger_counts(seeded, case["run_id"])
    replay = client.post(case["route"] + "/executions", headers=orch, json=case["body"])
    assert replay.status_code == 201 and replay.json() == response.json()
    assert ledger_counts(seeded, case["run_id"]) == counts
    duplicate = client.post(
        case["route"] + "/executions",
        headers=orch,
        json={**case["body"], "execution_id": str(uuid4())},
    )
    assert duplicate.status_code == 409, duplicate.text
    assert ledger_counts(seeded, case["run_id"]) == counts
    node = {
        "node_id": "end",
        "attempt": 2,
        "status": "dispatched",
        "result": {"execution_id": response.json()["id"]},
    }
    projected = client.post(case["route"] + "/nodes", headers=orch, json=node)
    assert projected.status_code == 200, projected.text
    counts = ledger_counts(seeded, case["run_id"])
    assert (
        client.post(case["route"] + "/nodes", headers=orch, json=node).json()
        == projected.json()
    )
    assert ledger_counts(seeded, case["run_id"]) == counts
    with seeded["db"].transaction() as session:
        approval = session.get(Approval, case["approval"]["id"])
        approval.expires_at = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    # Recording the result of an already permitted attempt never grants a new execution.
    terminal = client.post(
        case["route"] + "/nodes", headers=orch, json={**node, "status": "succeeded"}
    )
    assert terminal.status_code == 200, terminal.text
    with seeded["db"].transaction() as session:
        original = session.get(Execution, case["original_id"])
        assert (
            original.status == "failed"
            and original.result["error_code"] == "INTEGRATION_CONFLICT"
        )


@pytest.mark.parametrize(
    "fault",
    [
        "pending",
        "expired",
        "foreign_approval",
        "wrong_action",
        "wrong_snapshot",
        "wrong_snapshot_hash",
        "no_resume",
        "not_conflict",
        "process_not_stopped",
        "foreign_original",
        "wrong_node",
        "wrong_execution",
        "wrong_tree",
        "wrong_approval_id",
        "wrong_base",
        "third_attempt",
    ],
)
def test_operator_continuation_fails_closed(seeded: dict[str, Any], fault: str) -> None:
    case = conflict_setup(seeded)
    body = case["body"]
    with seeded["db"].transaction() as session:
        run = session.get(Run, case["run_id"])
        original = session.get(Execution, case["original_id"])
        approval = session.get(Approval, case["approval"]["id"])
        if fault == "pending":
            approval.decision = None
        elif fault == "expired":
            approval.expires_at = (
                datetime.now(UTC) - timedelta(seconds=10)
            ).isoformat()
        elif fault == "foreign_approval":
            approval.run_id = case["foreign_run_id"]
        elif fault == "wrong_action":
            approval.action = "execute_plan"
        elif fault == "wrong_snapshot":
            approval.snapshot = {**approval.snapshot, "allowed_write_paths": ["**"]}
            approval.snapshot_hash = digest(approval.snapshot)
        elif fault == "wrong_snapshot_hash":
            approval.snapshot_hash = "f" * 64
        elif fault == "no_resume":
            run.snapshot = {**run.snapshot, "resolution_resume_requested": False}
        elif fault == "not_conflict":
            original.result = {**original.result, "error_code": "TEST_FAILED"}
        elif fault == "process_not_stopped":
            original.result = {**original.result, "process_stopped": False}
        elif fault == "foreign_original":
            original.run_id = case["foreign_run_id"]
        elif fault == "wrong_node":
            body["node_id"] = "dev"
        elif fault == "wrong_execution":
            body["request"]["target_execution_id"] = str(uuid4())
        elif fault == "wrong_tree":
            body["request"]["resolution_tree_sha"] = "f" * 40
        elif fault == "wrong_approval_id":
            body["request"]["resolution_approval_id"] = str(uuid4())
        elif fault == "wrong_base":
            body["request"]["execution_base_sha"] = "f" * 40
        elif fault == "third_attempt":
            body["attempt"] = 3
    counts = ledger_counts(seeded, case["run_id"])
    result = seeded["client"].post(
        case["route"] + "/executions", headers=seeded["orch"], json=body
    )
    assert (
        result.status_code == 403
        and result.json()["code"] == "RESOLUTION_APPROVAL_REQUIRED"
    ), result.text
    assert ledger_counts(seeded, case["run_id"]) == counts


@pytest.mark.parametrize(
    "operation", ["agent_task", "planner", "test", "review", "git_commit", "integrate"]
)
def test_automatic_zero_retry_quota_remains_enforced(
    seeded: dict[str, Any], operation: str
) -> None:
    case = conflict_setup(seeded)
    body = {**case["body"], "operation": operation, "request": {}}
    result = seeded["client"].post(
        case["route"] + "/executions", headers=seeded["orch"], json=body
    )
    assert result.status_code == 409 and result.json()["code"] == "QUOTA_EXCEEDED", (
        result.text
    )
    if operation != "integrate":
        body["request"] = case["body"]["request"]
        result = seeded["client"].post(
            case["route"] + "/executions", headers=seeded["orch"], json=body
        )
        assert result.json()["code"] == "QUOTA_EXCEEDED", result.text


def test_projection_cannot_authorize_operator_execution(seeded: dict[str, Any]) -> None:
    case = conflict_setup(seeded)
    node = {
        "node_id": "end",
        "attempt": 2,
        "status": "dispatched",
        "result": {"manual_resolution": True, "resolution_approved": True},
    }
    result = seeded["client"].post(
        case["route"] + "/nodes", headers=seeded["orch"], json=node
    )
    assert result.json()["code"] == "QUOTA_EXCEEDED"
    created = seeded["client"].post(
        case["route"] + "/executions", headers=seeded["orch"], json=case["body"]
    )
    assert created.status_code == 201, created.text
    for changed in [
        {**node, "node_id": "dev"},
        {**node, "attempt": 3},
        {**node, "result": {"execution_id": str(uuid4())}},
    ]:
        result = seeded["client"].post(
            case["route"] + "/nodes", headers=seeded["orch"], json=changed
        )
        assert result.json()["code"] == "QUOTA_EXCEEDED", result.text


def test_unapproved_resolution_cannot_resume_or_dispatch(
    seeded: dict[str, Any],
) -> None:
    case = conflict_setup(seeded, resume=False)
    client = seeded["client"]
    result = client.post(
        "/api/runs/" + case["run_id"] + "/resume",
        headers={**seeded["admin"], "Idempotency-Key": "unapproved-resume"},
    )
    assert result.json()["code"] == "RESOLUTION_APPROVAL_REQUIRED"
    result = client.post(
        case["route"] + "/executions", headers=seeded["orch"], json=case["body"]
    )
    assert result.json()["code"] == "CONTROL_PENDING"
    original = client.get(
        "/internal/executions/" + case["original_id"], headers=seeded["orch"]
    ).json()
    assert (
        original["status"] == "failed" and original["result"]["process_stopped"] is True
    )
