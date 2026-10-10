"""Local authenticated ADF control API; runtime readiness is a separate acceptance gate."""

import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException

from .body_limit import BodyLimit
from .config import Settings
from .db import Database
from .definitions import RemoteValidator, Validator
from .errors import DomainError
from .privacy import redact
from .routes_definitions import build_router as definition_router
from .routes_metadata import build_router as metadata_router
from .routes_orchestrator import build_router as orchestrator_router
from .routes_plans import build_router as plan_router
from .routes_runtime import build_router as runtime_router
from .routes_worker import build_router as worker_router


def error(
    request: Request,
    code: str,
    message: str,
    status: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": getattr(request.state, "request_id", "diagnostic"),
        },
    )


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    validator: Validator | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    database = database or Database(settings.database_url)
    app = FastAPI(title="ADF control API", version="0.1.0")
    app.state.settings, app.state.database = settings, database
    app.add_middleware(BodyLimit)
    tracer = trace.get_tracer("adf-api")
    if settings.telemetry_enabled:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "adf-api"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        tracer = provider.get_tracer("adf-api")
        app.state.tracer_provider = provider

    @app.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        path = request.url.path
        if path not in {"/healthz", "/readyz"}:
            origin = request.headers.get("origin")
            if origin and origin not in settings.allowed_origins:
                return error(
                    request, "ORIGIN_DENIED", "This origin is not permitted.", 403
                )
            worker_route = path.startswith("/internal/workers") or (
                path.startswith("/internal/executions/")
                and path.rsplit("/", 1)[-1]
                in {
                    "claim",
                    "heartbeat",
                    "control",
                    "events",
                    "artifacts",
                    "result",
                    "reconcile",
                }
            )
            expected = (
                settings.worker_token
                if worker_route
                else settings.orchestrator_token
                if path.startswith("/internal/")
                else settings.admin_token
            )
            if not expected:
                return error(
                    request,
                    "AUTH_NOT_CONFIGURED",
                    "Configure scoped local bearer token files.",
                    503,
                )
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(
                supplied.encode(), ("Bearer " + expected).encode()
            ):
                return error(
                    request,
                    "AUTH_REQUIRED",
                    "A valid scoped bearer token is required.",
                    401,
                )
            if request.method in {"POST", "PATCH", "PUT"}:
                length = request.headers.get("content-length")
                maximum = (
                    30 * 1024 * 1024 if path.endswith("/artifacts") else 1024 * 1024
                )
                if length and (not length.isdecimal() or int(length) > maximum):
                    return error(
                        request,
                        "QUOTA_EXCEEDED",
                        "Request body exceeds the configured limit.",
                        413,
                    )
        context = TraceContextTextMapPropagator().extract(
            {"traceparent": request.headers.get("traceparent", "")}
        )
        with tracer.start_as_current_span(
            "api.request",
            context=context,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("adf.request_id", request.state.request_id)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error(
            request,
            exc.code,
            exc.message,
            exc.status,
            redact(
                exc.details,
                (
                    settings.admin_token,
                    settings.orchestrator_token,
                    settings.worker_token,
                ),
            ),
        )

    async def validation_error(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, (RequestValidationError, ValidationError)):
            return error(
                request, "INTERNAL_ERROR", "Unexpected validation failure.", 500
            )
        issues = [
            {"loc": list(issue["loc"]), "type": issue["type"]} for issue in exc.errors()
        ]
        return error(
            request,
            "VALIDATION_ERROR",
            "Request validation failed.",
            422,
            {"issues": issues},
        )

    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(ValidationError, validation_error)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error(
            request,
            "QUOTA_EXCEEDED" if exc.status_code == 413 else "HTTP_ERROR",
            "Request body exceeds the configured limit."
            if exc.status_code == 413
            else "HTTP request rejected.",
            exc.status_code,
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, _exc: IntegrityError) -> JSONResponse:
        return error(
            request,
            "VERSION_CONFLICT",
            "Unique identity or reference constraints rejected the change.",
            409,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        return error(
            request,
            "PERSISTENCE_UNAVAILABLE",
            "Persistence is unavailable; retry with the same idempotency key.",
            503,
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {
            "service": "adf-api",
            "status": "alive",
            "implementation": "control-plane",
        }

    @app.get("/readyz", response_model=None)
    def ready() -> dict[str, Any] | JSONResponse:
        try:
            with database.transaction() as session:
                current = session.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            configured = all(
                [
                    settings.admin_token,
                    settings.orchestrator_token,
                    settings.worker_token,
                    settings.allowed_roots,
                ]
            )
            if current != "0001_control_plane" or not configured:
                return JSONResponse(
                    status_code=503,
                    content={
                        "code": "CONFIGURATION_INCOMPLETE",
                        "service": "adf-api",
                        "database_ready": current == "0001_control_plane",
                        "runtime_ready": False,
                    },
                )
            return {
                "service": "adf-api",
                "status": "ready",
                "database_ready": True,
                "runtime_ready": False,
                "runtime_readiness_source": "umbrella acceptance evidence",
            }
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={
                    "code": "MIGRATIONS_REQUIRED",
                    "service": "adf-api",
                    "database_ready": False,
                    "runtime_ready": False,
                },
            )

    app.include_router(
        definition_router(database, settings, validator or RemoteValidator(settings))
    )
    app.include_router(runtime_router(database, settings))
    app.include_router(orchestrator_router(database, settings))
    app.include_router(worker_router(database, settings))
    app.include_router(plan_router(database, settings))
    app.include_router(
        metadata_router(database, validator or RemoteValidator(settings))
    )
    from fastapi.openapi.utils import get_openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title, version=app.version, routes=app.routes
            )
            from .schemas import AgentInput, RegistryInput, WorkflowInput

            definitions: dict[str, type[BaseModel]] = {
                "agents": AgentInput,
                "workflows": WorkflowInput,
                "skills": RegistryInput,
                "tools": RegistryInput,
                "mcp-servers": RegistryInput,
                "policies": RegistryInput,
            }
            for kind, model in definitions.items():
                model_schema = model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
                schema.setdefault("components", {}).setdefault("schemas", {}).update(
                    model_schema.pop("$defs", {})
                )
                schema["components"]["schemas"][model.__name__] = model_schema
                schema["paths"]["/api/" + kind]["post"]["requestBody"]["content"][
                    "application/json"
                ]["schema"] = {"$ref": "#/components/schemas/" + model.__name__}
            schema.setdefault("components", {}).setdefault("securitySchemes", {})[
                "ScopedBearer"
            ] = {
                "type": "http",
                "scheme": "bearer",
                "description": "Admin for /api, orchestrator for ledger routes, worker for lease routes; scoped token files.",
            }
            for path, methods in schema["paths"].items():
                if path not in {"/healthz", "/readyz"}:
                    for operation in methods.values():
                        if isinstance(operation, dict):
                            operation["security"] = [{"ScopedBearer": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI documents this extension point.
    return app


app = create_app()
