"""Console ASGI subapplication, independent of the frozen host dispatcher."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException

from iris_memory_core.api.console.auth import SessionPrincipal, require_session
from iris_memory_core.api.console.composition import assemble
from iris_memory_core.api.console.config import ConsoleConfig, is_loopback
from iris_memory_core.api.console.contracts import load_contract
from iris_memory_core.api.console.crypto import ConsoleCrypto
from iris_memory_core.api.console.errors import ConsoleError, error_response
from iris_memory_core.api.console.routes_artifacts import router as artifacts_router
from iris_memory_core.api.console.routes_auth import router as authentication_router
from iris_memory_core.api.console.routes_credentials import router as credentials_router
from iris_memory_core.api.console.routes_forget import router as forget_router
from iris_memory_core.api.console.routes_identity import router as identity_router
from iris_memory_core.api.console.routes_memory import router as memory_router
from iris_memory_core.api.console.routes_operations import router as operations_router
from iris_memory_core.api.console.routes_persona_drafts import router as persona_drafts_router
from iris_memory_core.api.console.routes_persona_policy import router as persona_policy_router
from iris_memory_core.api.console.routes_persona_proposals import router as persona_proposals_router
from iris_memory_core.api.console.routes_persona_states import router as persona_states_router
from iris_memory_core.api.console.routes_personas import router as personas_router
from iris_memory_core.api.console.routes_providers import router as providers_router
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.backup_operations import TrustedBackupArchive
from iris_memory_core.application.ports.clock import Clock, SystemClock, Uuid7Generator
from iris_memory_core.application.ports.provider_generations import ProviderGenerations
from iris_memory_core.application.ports.provider_secrets import ConfiguredEmbeddingRuntime
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.observability.logging import LowSensitivityLogger
from iris_memory_core.storage.uow import Store

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
STATIC_SUFFIXES = frozenset({".html", ".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2"})


def _check_transport(request: Request, config: ConsoleConfig) -> None:
    # Uvicorn proxy rewriting is disabled for Console-enabled servers. Only
    # explicitly configured immediate peers may supply forwarding headers.
    host = urlsplit("//" + request.headers.get("host", "")).hostname
    if host not in config.allowed_hosts:
        raise ConsoleError("access_denied", kind="permission_denied", status=403)
    peer = request.client.host if request.client else ""
    scheme = request.url.scheme
    if peer in config.trusted_proxy_ips:
        scheme = request.headers.get("x-forwarded-proto", scheme)
    if config.dev_http:
        if scheme != "http" or not host or not is_loopback(host):
            raise ConsoleError("access_denied", kind="permission_denied", status=403)
    elif scheme != "https":
        raise ConsoleError("access_denied", kind="permission_denied", status=403)


def _static_file(root: Path | None, path: str) -> Path:
    if root is None or path == "v1" or path.startswith("v1/"):
        raise ConsoleError("not_found", status=404)
    relative = Path(path)
    if relative.is_absolute() or any(
        part in {"..", "."} or part.startswith(".") for part in relative.parts
    ):
        raise ConsoleError("not_found", status=404)
    target = root / relative
    # Do not follow symlinks, even to another file inside the static tree.
    if any(part.is_symlink() for part in (target, *target.parents) if part != root.parent):
        raise ConsoleError("not_found", status=404)
    if relative.suffix:
        if relative.suffix not in STATIC_SUFFIXES or not target.is_file():
            raise ConsoleError("not_found", status=404)
    else:
        target = root / "index.html"
    if target.is_symlink() or not target.is_file():
        raise ConsoleError("not_found", status=404)
    return target


def create_console_app(
    *,
    store: Store | None = None,
    config: ConsoleConfig | None = None,
    clock: Clock | None = None,
    logger: LowSensitivityLogger | None = None,
    archives: TrustedBackupArchive | None = None,
    embedding_runtime: ConfiguredEmbeddingRuntime | None = None,
    provider_generations: ProviderGenerations | None = None,
) -> FastAPI:
    deployment = config or ConsoleConfig()
    deployment.validate()
    wall_clock = clock or (store.clock if store else SystemClock())
    contract = load_contract()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.console_config = deployment
    app.state.archives = archives
    app.state.embedding_runtime = embedding_runtime
    app.state.provider_generations = provider_generations
    if store is not None:
        app.state.security, app.state.crypto = assemble(store)
    else:
        app.state.security = None
        app.state.crypto = ConsoleCrypto(secrets.token_bytes(32))
    app.openapi = lambda: contract  # type: ignore[method-assign]

    async def handle_error(request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, RequestValidationError):
            error = ConsoleError("invalid_request", kind="validation_failed", status=400)
        elif isinstance(error, HTTPException):
            error = ConsoleError(
                "not_found" if error.status_code == 404 else "invalid_request",
                status=error.status_code,
            )
        return error_response(request, error)

    for exception_type in (ConsoleError, DomainError, RequestValidationError, HTTPException):
        app.add_exception_handler(exception_type, handle_error)

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next: Any) -> Response:
        started = time.monotonic()
        supplied = request.headers.get("x-request-id", "")
        try:
            identifier = UUID(supplied)
            if identifier.version != 7:
                raise ValueError
            request.state.request_id = str(identifier)
        except ValueError:
            request.state.request_id = str(Uuid7Generator().new())
        try:
            _check_transport(request, deployment)
            response: Response = await call_next(request)
        except Exception as error:
            response = error_response(request, error)
        response.headers.update(
            {
                "X-Request-ID": request.state.request_id,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": CSP,
                "X-Frame-Options": "DENY",
            }
        )
        if not deployment.dev_http:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        (logger or LowSensitivityLogger()).emit(
            "console.request",
            id_hash=request.state.request_id,
            status=str(response.status_code),
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return response

    @app.get("/v1/bootstrap", operation_id="consoleBootstrap")
    def bootstrap(
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(require_session)],
    ) -> dict[str, Any]:
        return envelope(
            request,
            {
                "contract_version": load_contract()["info"]["version"],
                "permissions": list(principal.permissions),
                "modules": [
                    module
                    for permission, module in (
                        ("keys.manage", "keys"),
                        ("service_keys.manage", "service_credentials"),
                    )
                    if permission in principal.permissions
                ]
                + (
                    ["memory", "personas"]
                    + (["operations"] if "memory.forget" in principal.permissions else [])
                    if "memory.read" in principal.permissions
                    and hasattr(principal, "key")
                    and "console.manage" in principal.key.grant.data_purposes
                    else []
                )
                + (
                    ["operations"]
                    if "backups.write" in principal.permissions
                    and hasattr(principal, "key")
                    and "console.manage" in principal.key.grant.data_purposes
                    and not {"memory.read", "memory.forget"} <= set(principal.permissions)
                    else []
                )
                + (
                    ["providers"]
                    if {"system.read", "providers.manage"} & set(principal.permissions)
                    and hasattr(principal, "key")
                    and "console.manage" in principal.key.grant.data_purposes
                    else []
                ),
                "read_only": False,
                "maintenance": False,
                "upload_limits": {
                    "file_bytes": "52428800",
                    "records": "100000",
                    "record_bytes": "262144",
                    "json_depth": 20,
                },
                "display_timezone": "UTC",
                "pending_restart": False,
                "import_in_progress": False,
            },
            now_us=wall_clock.now_us(),
        )

    app.include_router(authentication_router)
    app.include_router(credentials_router)
    app.include_router(memory_router)
    app.include_router(forget_router)
    app.include_router(operations_router)
    app.include_router(providers_router)
    app.include_router(personas_router)
    app.include_router(persona_states_router)
    app.include_router(persona_proposals_router)
    app.include_router(persona_policy_router)
    app.include_router(persona_drafts_router)
    app.include_router(identity_router)
    app.include_router(artifacts_router)

    @app.get("/{path:path}", include_in_schema=False)
    def static(path: str) -> FileResponse:
        return FileResponse(_static_file(deployment.assets, path))

    return app
