"""Independent ledger gates for checks, review, commits and integration."""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError
from .models import Execution, Run


def full_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{40}", value) is not None


def passing_checks(result: dict[str, Any], check_ids: list[str]) -> bool:
    checks = result.get("test_results")
    tree = result.get("tree_sha")
    return (
        bool(check_ids)
        and full_sha(tree)
        and isinstance(checks, list)
        and {item.get("check_id") for item in checks if isinstance(item, dict)}
        == set(check_ids)
        and len(checks) == len(check_ids)
        and all(
            isinstance(item, dict)
            and item.get("status") == "passed"
            and item.get("exit_code") == 0
            and item.get("tree_sha") == tree
            for item in checks
        )
    )


def review_valid(review: Any, tree: str, reviewer_id: str | None = None) -> bool:
    return (
        isinstance(review, dict)
        and review.get("approved") is True
        and review.get("tree_sha") == tree
        and full_sha(tree)
        and bool(review.get("reviewer_id"))
        and bool(review.get("developer_id"))
        and review["reviewer_id"] != review["developer_id"]
        and (reviewer_id is None or review["reviewer_id"] == reviewer_id)
    )


def target_evidence(
    session: Session, run: Run, request: dict[str, Any], operation: str
) -> None:
    target_id = request.get("target_execution_id")
    if operation not in {"test", "review", "git_commit"}:
        return
    target = session.get(Execution, target_id) if target_id else None
    if (
        target is None
        or target.run_id != run.id
        or target.status != "succeeded"
        or target.operation != "agent_task"
    ):
        raise DomainError(
            "TARGET_REQUIRED",
            "This operation requires a successful writing execution in the same run.",
        )
    tree = target.result.get("tree_sha")
    if not full_sha(tree) or request.get("tree_sha") != tree:
        raise DomainError(
            "TREE_CHANGED", "Execution must bind the target's exact tree."
        )
    if operation == "test":
        return
    checks = list(
        session.scalars(
            select(Execution).where(
                Execution.run_id == run.id,
                Execution.operation == "test",
                Execution.status == "succeeded",
            )
        )
    )
    check_ids = [check["id"] for check in run.snapshot["project"].get("checks", [])]
    if not any(
        execution.request.get("target_execution_id") == target_id
        and execution.result.get("tree_sha") == tree
        and passing_checks(execution.result, check_ids)
        for execution in checks
    ):
        raise DomainError(
            "TEST_EVIDENCE_REQUIRED",
            "Exact-tree registered passing checks are required before review/commit.",
        )
    if operation == "git_commit":
        reviews = session.scalars(
            select(Execution).where(
                Execution.run_id == run.id,
                Execution.operation == "review",
                Execution.status == "succeeded",
            )
        )
        if not any(
            execution.request.get("target_execution_id") == target_id
            and review_valid(
                execution.result.get("review"),
                str(tree),
                execution.request["agent"]["id"],
            )
            for execution in reviews
        ):
            raise DomainError(
                "REVIEW_EVIDENCE_REQUIRED",
                "Independent exact-tree review is required before commit.",
            )
        if not run.snapshot["policy"].get("allow_commit"):
            raise DomainError(
                "COMMIT_DENIED",
                "The approved policy does not permit local commits.",
                403,
            )


def successful_result(
    session: Session, run: Run, execution: Execution, payload: dict[str, Any]
) -> None:
    operation = execution.operation
    if execution.process_identity and payload.get("process_stopped") is not True:
        raise DomainError(
            "PROCESS_PROOF_REQUIRED",
            "A started process must be confirmed stopped before success.",
        )
    if operation in {
        "agent_task",
        "test",
        "review",
        "git_commit",
        "integrate",
    } and not full_sha(payload.get("tree_sha")):
        raise DomainError(
            "EVIDENCE_REQUIRED",
            "Successful writing/validation operations require an exact Git tree.",
        )
    if operation in {"test", "review", "git_commit"} and payload.get(
        "tree_sha"
    ) != execution.request.get("tree_sha"):
        raise DomainError(
            "TREE_CHANGED",
            "Terminal evidence must bind the exact requested target tree.",
        )
    if operation in {"test", "integrate"}:
        check_ids = execution.request.get("check_ids", [])
        if not passing_checks(payload, check_ids):
            raise DomainError(
                "TEST_EVIDENCE_REQUIRED",
                "All registered checks must pass on the exact tree.",
            )
    if operation == "review":
        target = session.get(Execution, execution.request.get("target_execution_id"))
        if (
            target is None
            or not review_valid(
                payload.get("review"),
                payload["tree_sha"],
                execution.request["agent"]["id"],
            )
            or payload["review"].get("developer_id") != target.request["agent"]["id"]
        ):
            raise DomainError(
                "REVIEW_EVIDENCE_REQUIRED",
                "Review must bind the distinct stable developer/reviewer identities and exact accepted tree.",
            )
    if operation in {"git_commit", "integrate"} and not full_sha(
        payload.get("commit_sha")
    ):
        raise DomainError(
            "COMMIT_EVIDENCE_REQUIRED", "Git completion requires an actual commit SHA."
        )
    if operation == "integrate" and (
        payload.get("integration_status") != "passed"
        or payload.get("branch") != "factory/integration/" + run.id
    ):
        raise DomainError(
            "INTEGRATION_EVIDENCE_REQUIRED",
            "Integration must pass on the separate integration branch.",
        )
