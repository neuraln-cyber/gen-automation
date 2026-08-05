from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from gen_automation.api.router import api_router
from gen_automation.api.routes.browser_authentication import (
    router as browser_authentication_router,
)
from gen_automation.api.routes.dashboard import router as dashboard_router
from gen_automation.api.routes.delivery_dashboard import (
    router as delivery_dashboard_router,
)
from gen_automation.api.routes.experiment_dashboard import (
    router as experiment_dashboard_router,
)
from gen_automation.api.routes.new_set_dashboard import router as new_set_dashboard_router
from gen_automation.api.routes.salad_webhooks import router as salad_webhook_router
from gen_automation.api.routes.wildcard_dashboard import (
    router as wildcard_dashboard_router,
)
from gen_automation.auth.runtime import (
    assert_authentication_bootstrapped,
    build_authentication_runtime,
)
from gen_automation.config import Environment, Settings, get_settings
from gen_automation.controller.runtime import ControllerRuntime, build_controller_runtime
from gen_automation.db import models as _models  # noqa: F401
from gen_automation.db.session import Database
from gen_automation.integrations.danbooru import DanbooruAutocompleteClient
from gen_automation.integrations.patreon import PatreonSidecarDriver
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.webhooks import SaladWebhookVerifier
from gen_automation.integrations.semantic_vlm import SemanticVlmClient
from gen_automation.logging import configure_logging
from gen_automation.middleware import RequestContextMiddleware
from gen_automation.services.admin_enrollment import AdminEnrollmentService
from gen_automation.services.authentication import AuthenticationService
from gen_automation.services.danbooru_tags import DanbooruTagAutocompleteService
from gen_automation.services.runtime_secrets import (
    RuntimeSecretResolver,
    build_runtime_secret_resolver,
)
from gen_automation.services.x_oauth import (
    AwsSecretsManagerXOAuthProvider,
    build_aws_secrets_manager_x_oauth_provider,
)
from gen_automation.storage.s3 import build_object_store


async def _browser_authentication_exception_handler(
    request: Request,
    error: Exception,
) -> Response:
    if not isinstance(error, StarletteHTTPException):
        raise error
    path = request.url.path
    if (
        request.method in {"GET", "HEAD"}
        and (path == "/dashboard" or path.startswith("/dashboard/"))
        and error.status_code == status.HTTP_401_UNAUTHORIZED
        and error.detail == "authentication required"
    ):
        response = RedirectResponse(
            url="/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    return await http_exception_handler(request, error)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url)
        object_store = build_object_store(resolved_settings)
        danbooru_http_client = httpx2.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            limits=httpx2.Limits(max_connections=2, max_keepalive_connections=1),
        )
        danbooru_tag_autocomplete_service = DanbooruTagAutocompleteService(
            client=DanbooruAutocompleteClient(http_client=danbooru_http_client)
        )
        salad_http_client: httpx2.AsyncClient | None = None
        salad_client: SaladClient | None = None
        semantic_http_client: httpx2.AsyncClient | None = None
        semantic_vlm_client: SemanticVlmClient | None = None
        patreon_http_client: httpx2.AsyncClient | None = None
        patreon_driver: PatreonSidecarDriver | None = None
        controller_runtime: ControllerRuntime | None = None
        runtime_secret_resolver: RuntimeSecretResolver | None = None
        x_oauth_provider: AwsSecretsManagerXOAuthProvider | None = None
        authentication_service: AuthenticationService | None = None
        admin_enrollment_service: AdminEnrollmentService | None = None
        if resolved_settings.salad_enabled:
            api_key = resolved_settings.salad_api_key
            organization = resolved_settings.salad_organization
            project = resolved_settings.salad_project
            if api_key is None or organization is None or project is None:
                raise RuntimeError("validated SaladCloud settings are incomplete")
            salad_http_client = httpx2.AsyncClient(
                follow_redirects=False,
                trust_env=False,
            )
            salad_client = SaladClient(
                http_client=salad_http_client,
                api_key=api_key.get_secret_value(),
                organization=organization,
                project=project,
                base_url=str(resolved_settings.salad_api_base_url),
                timeout=resolved_settings.salad_request_timeout_seconds,
            )
        if resolved_settings.semantic_anatomy_enabled:
            endpoint = resolved_settings.semantic_anatomy_endpoint_url
            revision = resolved_settings.semantic_anatomy_model_revision
            if endpoint is None or revision is None:
                raise RuntimeError("validated semantic anatomy settings are incomplete")
            semantic_http_client = httpx2.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                limits=httpx2.Limits(max_connections=2, max_keepalive_connections=1),
            )
            semantic_vlm_client = SemanticVlmClient(
                http_client=semantic_http_client,
                endpoint_url=str(endpoint),
                model=resolved_settings.semantic_anatomy_model,
                model_revision=revision,
                timeout_seconds=(resolved_settings.background_semantic_request_timeout_seconds),
            )
        if resolved_settings.patreon_browser_publishing_enabled:
            endpoint = resolved_settings.patreon_browser_sidecar_url
            if endpoint is None:
                raise RuntimeError("validated Patreon browser settings are incomplete")
            shared_secret = resolved_settings.patreon_browser_shared_secret
            if shared_secret is None:
                raise RuntimeError("validated Patreon browser settings are incomplete")
            patreon_http_client = httpx2.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
            )
            patreon_driver = PatreonSidecarDriver(
                http_client=patreon_http_client,
                endpoint_url=str(endpoint),
                timeout_seconds=resolved_settings.patreon_browser_timeout_seconds,
                max_package_bytes=(resolved_settings.background_publication_max_package_bytes),
                shared_secret=shared_secret.get_secret_value(),
            )
        app.state.database = database
        app.state.object_store = object_store
        app.state.danbooru_tag_autocomplete_service = danbooru_tag_autocomplete_service
        app.state.salad_client = salad_client
        app.state.salad_webhook_verifier = (
            SaladWebhookVerifier(
                resolved_settings.salad_webhook_secret.get_secret_value(),
                max_body_bytes=resolved_settings.salad_webhook_max_body_bytes,
            )
            if resolved_settings.salad_enabled
            and resolved_settings.salad_webhook_secret is not None
            else None
        )
        try:
            if resolved_settings.auto_create_schema:
                await database.create_schema()
            if resolved_settings.auth_enabled:
                authentication_runtime = build_authentication_runtime(resolved_settings)
                async with database.sessions() as session:
                    await assert_authentication_bootstrapped(
                        session,
                        runtime=authentication_runtime,
                        require_totp=resolved_settings.auth_require_totp,
                    )
                authentication_service = authentication_runtime.service
                admin_enrollment_service = authentication_runtime.enrollment_service
                app.state.authentication_service = authentication_service
                app.state.admin_enrollment_service = admin_enrollment_service
            if resolved_settings.background_runtime_enabled:
                runtime_secret_resolver = build_runtime_secret_resolver(resolved_settings)
                if resolved_settings.x_oauth_secret_reference is not None:
                    creator_user_id = resolved_settings.x_creator_user_id
                    if creator_user_id is None:
                        raise RuntimeError("validated X OAuth settings are incomplete")
                    x_oauth_provider = build_aws_secrets_manager_x_oauth_provider(
                        engine=database.engine,
                        configured_reference=resolved_settings.x_oauth_secret_reference,
                        expected_creator_user_id=creator_user_id,
                        request_timeout_seconds=(resolved_settings.x_oauth_request_timeout_seconds),
                        lock_timeout_seconds=resolved_settings.x_oauth_lock_timeout_seconds,
                        refresh_margin_seconds=(resolved_settings.x_oauth_refresh_margin_seconds),
                    )
                    app.state.x_oauth_provider = x_oauth_provider
                controller_runtime = build_controller_runtime(
                    settings=resolved_settings,
                    sessions=database.sessions,
                    salad_client=salad_client,
                    object_store=object_store,
                    secret_resolver=runtime_secret_resolver,
                    x_oauth_provider=x_oauth_provider,
                    semantic_vlm_client=semantic_vlm_client,
                    patreon_driver=patreon_driver,
                )
                app.state.controller_runtime = controller_runtime
                await controller_runtime.start()
            yield
        finally:
            app.state.authentication_service = None
            app.state.admin_enrollment_service = None
            app.state.x_oauth_provider = None
            app.state.danbooru_tag_autocomplete_service = None
            try:
                if controller_runtime is not None:
                    await controller_runtime.stop()
            finally:
                try:
                    if x_oauth_provider is not None:
                        await x_oauth_provider.aclose()
                finally:
                    try:
                        if runtime_secret_resolver is not None:
                            await runtime_secret_resolver.aclose()
                    finally:
                        try:
                            if object_store is not None:
                                await object_store.close()
                        finally:
                            try:
                                if semantic_http_client is not None:
                                    await semantic_http_client.aclose()
                            finally:
                                try:
                                    if patreon_http_client is not None:
                                        await patreon_http_client.aclose()
                                finally:
                                    try:
                                        if salad_http_client is not None:
                                            await salad_http_client.aclose()
                                    finally:
                                        try:
                                            await danbooru_http_client.aclose()
                                        finally:
                                            await database.dispose()

    expose_docs = resolved_settings.environment in {
        Environment.LOCAL,
        Environment.TEST,
    }
    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.salad_client = None
    application.state.danbooru_tag_autocomplete_service = None
    application.state.salad_webhook_verifier = None
    application.state.controller_runtime = None
    application.state.x_oauth_provider = None
    application.state.authentication_service = None
    application.state.admin_enrollment_service = None
    application.add_exception_handler(
        StarletteHTTPException,
        _browser_authentication_exception_handler,
    )
    application.add_middleware(RequestContextMiddleware)
    application.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    application.include_router(api_router, prefix="/api/v1")
    application.include_router(browser_authentication_router)
    application.include_router(dashboard_router)
    application.include_router(delivery_dashboard_router)
    application.include_router(new_set_dashboard_router)
    application.include_router(experiment_dashboard_router)
    application.include_router(wildcard_dashboard_router)
    application.include_router(salad_webhook_router)
    return application


app = create_app()
