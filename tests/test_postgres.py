"""Real PostgreSQL concurrency/grant tests against an explicitly separate test database."""

import os
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from conftest import TestValidator
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from adf_api.config import Settings
from adf_api.db import Database
from adf_api.main import create_app
from adf_api.models import Execution, Run

pytestmark = pytest.mark.skipif(
    not os.getenv("ADF_TEST_DATABASE_URL"),
    reason="Set an explicitly separate PostgreSQL test database URL.",
)


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[dict[str, Any]]:
    url = os.environ["ADF_TEST_DATABASE_URL"]
    if not url.rsplit("/", 1)[-1].startswith("adf_api_test"):
        raise RuntimeError(
            "PostgreSQL tests refuse a database outside the dedicated adf_api_test namespace"
        )
    settings = replace(
        Settings.from_environment(),
        admin_token=secrets.token_urlsafe(32),
        orchestrator_token=secrets.token_urlsafe(32),
        worker_token=secrets.token_urlsafe(32),
        allowed_roots=("/home/adf/ai-factory",),
        artifact_root=tmp_path,
    )
    database = Database(settings.database_url)
    with database.transaction() as session:
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0001_control_plane"
        )
    owned_run_ids: list[str] = []
    with TestClient(create_app(settings, database, TestValidator())) as client:
        yield {
            "client": client,
            "db": database,
            "settings": settings,
            "owned": owned_run_ids,
            "admin": {"Authorization": "Bearer " + settings.admin_token},
            "orch": {"Authorization": "Bearer " + settings.orchestrator_token},
            "worker": {"Authorization": "Bearer " + settings.worker_token},
        }
    # Only synthetic IDs created by this fixture are reconciled; no running CLI or operational DB is involved.
    with database.transaction() as session:
        for execution in session.scalars(
            select(Execution).where(Execution.run_id.in_(owned_run_ids))
        ):
            execution.status, execution.result = (
                "cancelled",
                {"process_stopped": True, "reason": "TEST_FIXTURE_CLEANUP"},
            )
        for run in session.scalars(select(Run).where(Run.id.in_(owned_run_ids))):
            run.status = "cancelled"
    database.engine.dispose()


def seed(pg: dict[str, Any], create_run: bool = True) -> dict[str, Any]:
    client, headers = pg["client"], pg["admin"]
    suffix = str(uuid4())
    project = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "adf-test-" + suffix,
            "repo_path": "/home/adf/ai-factory/projects/adf-test-" + suffix + "/repo",
            "allowed_root": "/home/adf/ai-factory",
            "trusted": True,
        },
    ).json()
    task = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "project_id": project["id"],
            "title": "adf-test-task-" + suffix,
            "expected_files": ["src/test-" + suffix + ".py"],
        },
    ).json()
    graph = {
        "schema_version": "1",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "end"}],
    }
    workflow = client.post(
        "/api/workflows",
        headers=headers,
        json={"name": "adf-test-flow-" + suffix, "graph_json": graph},
    ).json()
    version = client.post(
        "/api/workflows/" + workflow["id"] + "/versions", headers=headers
    ).json()
    body = {"task_id": task["id"], "workflow_version_id": version["id"]}
    result = {
        "project": project,
        "task": task,
        "body": body,
        "key": "adf-test-" + suffix,
    }
    if create_run:
        response = client.post(
            "/api/runs",
            headers={**headers, "Idempotency-Key": result["key"]},
            json=body,
        )
        assert response.status_code == 201, response.text
        result["run"] = response.json()
        pg["owned"].append(result["run"]["id"])
    return result


def test_postgres_scoped_application_grants(pg: dict[str, Any]) -> None:
    with pg["db"].transaction() as session:
        assert session.scalar(text("SELECT current_user")) == "adf_api"
        assert (
            session.scalar(
                text("SELECT has_schema_privilege(current_user, 'control', 'USAGE')")
            )
            is True
        )
        assert (
            session.scalar(
                text("SELECT has_schema_privilege(current_user, 'control', 'CREATE')")
            )
            is False
        )
        assert (
            session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, 'control.projects', 'INSERT')"
                )
            )
            is True
        )
        assert (
            session.scalar(
                text(
                    "SELECT has_table_privilege(current_user, 'alembic_version', 'UPDATE')"
                )
            )
            is False
        )
        assert (
            session.scalar(
                text(
                    "SELECT has_schema_privilege(current_user, 'checkpoints', 'USAGE')"
                )
            )
            is False
        )
        assert (
            session.scalar(
                text(
                    "SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname=current_user"
                )
            )
            is False
        )


def test_postgres_concurrent_idempotency_returns_one_run(pg: dict[str, Any]) -> None:
    data = seed(pg, create_run=False)

    def invoke(_index: int):
        return pg["client"].post(
            "/api/runs",
            headers={**pg["admin"], "Idempotency-Key": data["key"]},
            json=data["body"],
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(invoke, range(8)))
    assert all(response.status_code == 201 for response in responses), [
        response.text for response in responses
    ]
    ids = {response.json()["id"] for response in responses}
    assert len(ids) == 1
    pg["owned"].extend(ids)


def test_postgres_concurrent_claim_one_owner_and_epoch(pg: dict[str, Any]) -> None:
    data = seed(pg)
    execution_id = str(uuid4())
    result = pg["client"].post(
        "/internal/runs/" + data["run"]["id"] + "/executions",
        headers=pg["orch"],
        json={
            "execution_id": execution_id,
            "node_id": "__prepare",
            "attempt": 1,
            "operation": "repo_inspect",
        },
    )
    assert result.status_code == 201, result.text
    workers = ["test-" + str(uuid4())[:12] for _ in range(8)]
    for worker in workers:
        assert (
            pg["client"]
            .post(
                "/internal/workers/register",
                headers=pg["worker"],
                json={
                    "worker_id": worker,
                    "capabilities": {},
                    "allowed_roots": ["/home/adf/ai-factory"],
                },
            )
            .status_code
            == 200
        )

    def invoke(worker: str):
        return pg["client"].post(
            "/internal/executions/" + execution_id + "/claim",
            headers=pg["worker"],
            json={"worker_id": worker},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(invoke, workers))
    assert sum(response.status_code == 200 for response in responses) == 1
    assert all(response.status_code in {200, 409} for response in responses)
    success = next(
        response.json() for response in responses if response.status_code == 200
    )
    assert success["epoch"] == 1


def test_postgres_revision_compare_and_swap(pg: dict[str, Any]) -> None:
    data = seed(pg, create_run=False)
    route = "/api/tasks/" + data["task"]["id"]

    def invoke(index: int):
        return pg["client"].patch(
            route,
            headers={**pg["admin"], "If-Match": "1"},
            json={"title": "adf-test-update-" + str(index)},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(invoke, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 412]


def test_postgres_global_concurrency_reservations_across_projects(
    pg: dict[str, Any],
) -> None:
    runs = [seed(pg)["run"] for _ in range(8)]

    def invoke(run: dict[str, Any]):
        return pg["client"].post(
            "/internal/runs/" + run["id"] + "/executions",
            headers=pg["orch"],
            json={
                "execution_id": str(uuid4()),
                "node_id": "__prepare",
                "attempt": 1,
                "operation": "repo_inspect",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(invoke, runs))
    assert sum(response.status_code == 201 for response in responses) == 3, [
        response.text for response in responses
    ]
    assert all(response.status_code in {201, 409} for response in responses)
    assert all(
        response.json()["code"] == "CONCURRENCY_LIMIT"
        for response in responses
        if response.status_code == 409
    )


def test_postgres_claim_refreshes_cached_execution_after_lock(
    pg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier

    import adf_api.routes_worker as routes

    data = seed(pg)
    execution_id = str(uuid4())
    response = pg["client"].post(
        "/internal/runs/" + data["run"]["id"] + "/executions",
        headers=pg["orch"],
        json={
            "execution_id": execution_id,
            "node_id": "__prepare",
            "attempt": 1,
            "operation": "repo_inspect",
        },
    )
    assert response.status_code == 201
    workers = ["barrier-" + str(uuid4())[:12] for _ in range(2)]
    for worker in workers:
        pg["client"].post(
            "/internal/workers/register",
            headers=pg["worker"],
            json={
                "worker_id": worker,
                "capabilities": {},
                "allowed_roots": ["/home/adf/ai-factory"],
            },
        )
    barrier = Barrier(2)
    original = routes.get

    def synchronized_read(
        session: Any, model: Any, value: str, *, lock: bool = False
    ) -> Any:
        record = original(session, model, value, lock=lock)
        if model is Execution and value == execution_id and not lock:
            barrier.wait(timeout=10)
        return record

    monkeypatch.setattr(routes, "get", synchronized_read)

    def invoke(worker: str) -> Any:
        return pg["client"].post(
            "/internal/executions/" + execution_id + "/claim",
            headers=pg["worker"],
            json={"worker_id": worker},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(invoke, workers))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with pg["db"].transaction() as session:
        row = session.get(Execution, execution_id)
        assert row is not None and row.epoch == 1


def test_postgres_run_lock_refreshes_cancel_flag(pg: dict[str, Any]) -> None:
    from adf_api.ledger import lock_run

    data = seed(pg)
    with pg["db"].transaction() as reader:
        stale = reader.get(Run, data["run"]["id"])
        assert stale is not None and not stale.cancel_requested
        result = pg["client"].post(
            "/api/runs/" + stale.id + "/cancel",
            headers={**pg["admin"], "Idempotency-Key": "cancel-" + stale.id},
        )
        assert result.status_code == 202
        current = lock_run(reader, stale.id)
        assert current.cancel_requested and current.revision > 1


def test_postgres_final_cancel_atomically_closes_node_and_preserves_proof(
    pg: dict[str, Any],
) -> None:
    data = seed(pg)
    client, run_id = pg["client"], data["run"]["id"]
    route = "/internal/runs/" + run_id
    execution_id, worker_id = str(uuid4()), str(uuid4())
    allocation = client.post(
        route + "/executions",
        headers=pg["orch"],
        json={
            "execution_id": execution_id,
            "node_id": "__prepare",
            "attempt": 1,
            "operation": "repo_inspect",
        },
    )
    assert allocation.status_code == 201
    client.post(route + "/transition", headers=pg["orch"], json={"status": "running"})
    node = client.post(
        route + "/nodes",
        headers=pg["orch"],
        json={
            "node_id": "__prepare",
            "attempt": 1,
            "status": "dispatched",
            "result": {"execution_id": execution_id},
        },
    ).json()
    client.post(
        "/internal/workers/register",
        headers=pg["worker"],
        json={
            "worker_id": worker_id,
            "capabilities": {},
            "allowed_roots": ["/home/adf/ai-factory"],
        },
    )
    worker_route = "/internal/executions/" + execution_id
    claim = client.post(
        worker_route + "/claim", headers=pg["worker"], json={"worker_id": worker_id}
    ).json()
    client.post(
        "/api/runs/" + run_id + "/cancel",
        headers={**pg["admin"], "Idempotency-Key": "cancel-" + run_id},
    )
    rejected = client.post(
        route + "/transition", headers=pg["orch"], json={"status": "cancelled"}
    )
    assert rejected.json()["code"] == "EXECUTIONS_ACTIVE"
    unchanged = client.get(route, headers=pg["orch"]).json()
    assert unchanged["nodes"] == [node]
    proof = {"process_stopped": True, "error_code": "CANCEL_REQUESTED"}
    completed = client.post(
        worker_route + "/result",
        headers=pg["worker"],
        json={
            "worker_id": worker_id,
            "epoch": claim["epoch"],
            "status": "cancelled",
            "result": proof,
        },
    )
    assert completed.status_code == 200
    final = client.post(
        route + "/transition", headers=pg["orch"], json={"status": "cancelled"}
    ).json()
    assert final["status"] == "cancelled" and final["nodes"][0]["status"] == "cancelled"
    assert final["nodes"][0]["result"]["result"] == proof
    assert final["nodes"][0]["revision"] == node["revision"] + 1
    replay = client.post(
        route + "/transition", headers=pg["orch"], json={"status": "cancelled"}
    ).json()
    assert (
        replay["event_sequence"] == final["event_sequence"]
        and replay["nodes"] == final["nodes"]
    )


def test_postgres_manual_continuation_is_one_durable_allocation(
    pg: dict[str, Any],
) -> None:
    from threading import Barrier

    from test_manual_resolution import conflict_setup

    from adf_api.models import Event, Outbox

    data = seed(pg)
    case = conflict_setup(
        {**pg, **data, "run_body": {**data["body"], "root_base_sha": "a" * 40}}
    )
    pg["owned"].append(case["run_id"])
    barrier = Barrier(2, timeout=10)

    def invoke(_index: int):
        barrier.wait()
        return pg["client"].post(
            case["route"] + "/executions",
            headers=pg["orch"],
            json={**case["body"], "execution_id": str(uuid4())},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(invoke, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409], [
        response.text for response in responses
    ]
    winner = next(
        response.json() for response in responses if response.status_code == 201
    )
    assert winner["request"]["policy"]["max_retries"] == 0
    assert winner["request"]["resolution_approved"] is True
    with pg["db"].transaction() as session:
        continuations = list(
            session.scalars(
                select(Execution).where(
                    Execution.run_id == case["run_id"], Execution.attempt == 2
                )
            )
        )
        assert [execution.id for execution in continuations] == [winner["id"]]
        assert (
            len(
                list(
                    session.scalars(
                        select(Outbox).where(Outbox.execution_id == winner["id"])
                    )
                )
            )
            == 1
        )
        events = list(
            session.scalars(
                select(Event).where(
                    Event.run_id == case["run_id"], Event.type == "execution.created"
                )
            )
        )
        assert sum(event.payload.get("attempt") == 2 for event in events) == 1
        original = session.get(Execution, case["original_id"])
        assert (
            original.status == "failed" and original.result["process_stopped"] is True
        )
