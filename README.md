# adf-api

Authenticated FastAPI control plane and sole PostgreSQL ledger authority for ADF. This independent Git repository is a submodule of [adf-meta](https://github.com/alex-kzr/adf-meta).

The service implements project/task definitions, immutable agent and registry versions, semantically validated workflow publication, planning commands, run controls, hash-bound human approvals, fenced worker leases, durable events/outbox, artifacts, Git evidence and metrics. It never launches target code or coding CLIs. The orchestrator compiles graphs; the WSL host worker executes them.

API readiness verifies migrations and scoped configuration. **It does not declare the entire MVP ready**: real CLI capability, Git isolation, telemetry and whole-wire acceptance belong to the umbrella gates.

- [Development and verification](docs/development.md)
- [Implemented wire contract](docs/wire-contract.md)
- [Architecture](../../docs/architecture.md)
- [Product and acceptance](../../docs/product.md)
- [Shared contracts](../../docs/contracts.md)
- [Security](../../docs/security.md)
- [Local Docker workflow](../../docs/local-development.md)

Shared relative links require the initialized umbrella checkout. Standalone readers can use [umbrella documentation](https://github.com/alex-kzr/adf-meta/tree/main/docs) at the matching published commits.

Use Python 3.12+ and a module virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
pytest -q
ruff check src tests
mypy src
```

Deployment runs `python -m adf_api.migrate` with the dedicated migrator role, then `python -m uvicorn adf_api.main:app --host 0.0.0.0 --port 8080` with the restricted API role. The umbrella Compose supplies token/password files, PostgreSQL, the semantic validator and artifact volume. No authenticated route is enabled by an empty token.
