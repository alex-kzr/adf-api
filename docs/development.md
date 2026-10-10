# API development

## Boundaries and implementation

`src/adf_api` separates typed input/configuration (`schemas`, `config`), SQLAlchemy persistence/migrations (`models`, `db`, `migrate`), definition validation, the transactional runtime ledger, exact-tree evidence guards and public/internal routes. The API owns all control/runtime/audit writes. Workers use authenticated persistence endpoints; the orchestrator has a separate checkpoint role and communicates through the ledger facade. No executable code or shell command from a transport message is trusted.

Definition edits produce immutable config versions. Run creation pins task/child task, graph, agent, skill/tool/MCP and policy snapshots. Manual external dependencies require prior accepted integration and pin those committed deltas. Agent identity is the stable definition UUID; `config_version_id` identifies an immutable version. A reviewer cannot be the developer under a different configuration version.

Every writing dispatch requires an unexpired human grant binding the current graph, plan, base, policy, full pinned agent/registry hash, dependency hash, paths and tool IDs. The server derives the executable scope and rejects enlarged caller paths. Planner/reviewer permissions are read-only. Provider/agent and global concurrency reservations are transactionally serialized. Agent limits can narrow policy limits. Checks retain their registered timeouts.

PostgreSQL is the deployment authority. SQLite is used only for isolated HTTP/fault fixtures; it is not concurrency evidence. Lock reads refresh the SQLAlchemy identity map after waiting, with Project -> Run -> Execution/Task ordering. Monotonic cancellation, uncertain expired leases, process reconciliation, exact terminal replay and outbox reconstruction survive transport retries. An expired lease never authorizes another live CLI.

Tests/review/local commit bind the requested exact tree. Integration must include the complete selected accepted commit set, apply each delta once and pass every registered check on the final tree. Completion cannot mark omitted child tasks done. Conflict resolution requires a separate exact-tree approval and explicit resume.

## Configuration and startup

Provide `DATABASE_URL` as `postgresql+psycopg://adf_api@postgres/adf`. Password values come from `ADF_DB_PASSWORD_FILE`, never from API responses. The migrator uses `adf_migrator` and its own password file. `python -m adf_api.migrate` runs the frozen Alembic chain under an advisory lock and grants API table/sequence DML, while retaining read-only access to the Alembic marker. API cannot create schemas/tables or access checkpoint data.

Required scoped secret files: `ADF_ADMIN_TOKEN_FILE`, `ADF_ORCHESTRATOR_TOKEN_FILE`, `ADF_WORKER_TOKEN_FILE`. Tokens must be distinct, generated outside source control and shared only with their owning process. `ADF_ALLOWED_ROOTS` is a JSON array of canonical WSL ext4 roots; an empty list disables projects. `ADF_WORKER_PROJECT_IDS` optionally limits registration to approved projects. `ADF_ALLOWED_ORIGINS` limits browser origins. `ADF_ARTIFACT_ROOT` selects the API-owned artifact volume. `ORCHESTRATOR_URL` enables the authenticated `/validate` semantic compiler; publication fails closed when unavailable.

`ADF_TELEMETRY_ENABLED=true` enables safe OpenTelemetry request spans and OTLP HTTP export using the standard `OTEL_EXPORTER_OTLP_*` configuration. API spans never include prompts, source, output, environment, authorization or exception text. W3C trace context is propagated without baggage; the orchestrator persists one immutable run context before dispatch. Optional `ADF_LANGFUSE_URL` and `ADF_LANGFUSE_PROJECT_ID` produce validated loopback trace links only for actual persisted trace IDs.

`/healthz` proves the process answers. `/readyz` additionally requires the current migration marker, configured scoped tokens and roots. `runtime_ready:false` remains explicit until umbrella acceptance verifies the full workflow. OpenAPI is generated from the actual application, documents scoped bearer auth and typed definition requests, and requires the admin token to read.

## Verification

```sh
pytest -q
ruff check src tests
ruff format --check src tests
mypy src
```

For the meaningful PostgreSQL suite, create a separate `adf_api_test` database with the same migrator/API grants. Run migrations with the migrator URL/password file, then set both `DATABASE_URL` and `ADF_TEST_DATABASE_URL` to `postgresql+psycopg://adf_api@localhost:5432/adf_api_test`, and set `ADF_DB_PASSWORD_FILE` to the API role password file. Tests refuse a database outside the `adf_api_test` namespace and use unique synthetic IDs. Tests exercise concurrent claim/idempotency/revision/global reservations, explicit stale identity-map barriers and grant isolation. They never launch providers or write an operational database.

The pinned GitHub workflow provisions isolated roles/password files on its disposable PostgreSQL service, applies migrations and runs all tests, formatting, security lint and strict source typing. CI results are not claimed until the workflow has run remotely. Local checks cover HTTP authorization, approval scope, dependency commits, quotas, cancellation, lease expiry/reconciliation, events/SSE replay, redacted artifacts, terminal evidence and telemetry metadata. The current Starlette TestClient emits an upstream httpx compatibility deprecation warning; it does not bypass assertions.

Applied local Python skills: python-project-structure (shallow source layers), python-type-safety (strict typing and Validator Protocol), python-testing-patterns (isolated synthetic fixtures plus real database faults), fastapi-templates (typed boundaries/error handlers), py-security (scoped secrets, origins, quota and path guards), python-configuration (immutable file-backed settings), python-error-handling (stable sanitized errors and persisted uncertainty). Their paths are local tooling, not runtime dependencies.

## Limits and follow-up

No destructive resource deletion or arbitrary executable plugin loading is exposed. Historical definitions and ledger evidence are retained. Artifact file creation precedes the database commit; a process crash can leave an opaque orphan file. Operator retention/garbage collection is a follow-up and must compare database references before deletion. Artifacts/events are bounded and redacted; text artifacts are downloads, never executable HTML. SSE is local authenticated replay, not a public multi-tenant subscription platform.

The worker proves filesystem containment, process death and real provider capabilities. A bearer worker token trusts that host's evidence and is not remote attestation. Codex narrow per-tool allowlists are unsupported and fail closed; Claude mappings require the verified adapter contract. Live provider authentication, budgets/cost completeness, actual MCP behavior and full Docker/WSL integration remain umbrella acceptance gates.

Primary references: [SQLAlchemy identity-map refresh](https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html#populate-existing), [SQLAlchemy sessions](https://docs.sqlalchemy.org/en/20/orm/session_basics.html), [Alembic migration tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html). Dependency versions are pinned in `requirements.lock`; adapter upgrades require corresponding worker/orchestrator contract tests.
