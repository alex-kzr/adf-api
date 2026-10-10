# API development

Implemented: FastAPI diagnostics. `/healthz` returns service/status; `/readyz` returns 503 `SCAFFOLD_NOT_IMPLEMENTED`. No CRUD, authentication, migrations, database connection, SSE or worker endpoints exist. Generated app OpenAPI documents diagnostics only; umbrella OpenAPI is the broader **draft** design.

Install requirements.lock in a virtual environment, then editable package with --no-deps. Lock captures runtime/testing dependencies from a verified local environment. `python -m unittest discover -s tests` checks the diagnostic HTTP contract. `src/adf_api/main.py` is intentionally small; add schemas, services and repositories when E1 starts. API owns the single ADF Alembic migration chain and internal fenced persistence facade. No target code or CLI subprocess runs in this service.

Next: configuration/scoped auth and role grants, migrations/CRUD, schema-generated models, command/outbox intake, lease/events/artifact endpoints and fetch-compatible SSE. Read matching umbrella contracts and acceptance tests before adding endpoints. Do not mark ready merely because PostgreSQL accepts TCP connections.
