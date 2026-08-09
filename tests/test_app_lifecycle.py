import base64
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import gen_automation.app as app_module
from gen_automation.app import create_app
from gen_automation.config import Environment, Settings, XAuthMode
from gen_automation.gpu_worker.artifacts import (
    ArtifactKind,
    ModelArtifactSpec,
    create_artifact_manifest,
)
from gen_automation.integrations.civitai import CivitaiClient
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.webhooks import SaladWebhookVerifier
from gen_automation.services.danbooru_tags import DanbooruTagAutocompleteService
from gen_automation.storage.model_artifacts import ModelArtifactStore
from gen_automation.storage.s3 import S3ObjectStore


def _lora_lifecycle_settings(tmp_path: Path, **overrides: object) -> Settings:
    baseline = create_artifact_manifest(
        (
            ModelArtifactSpec(
                logical_name="lifecycle-checkpoint",
                kind=ArtifactKind.CHECKPOINT,
                source_object_id="models/checkpoint.safetensors",
                source_object_version_id="version-1",
                sha256="f" * 64,
                exact_size_bytes=1_024,
                max_size_bytes=1_024,
                target_filename="checkpoint.safetensors",
            ),
        )
    )
    values: dict[str, object] = {
        "environment": Environment.TEST,
        "database_url": (f"sqlite+aiosqlite:///{(tmp_path / 'lora-lifecycle.db').as_posix()}"),
        "auto_create_schema": True,
        "background_runtime_enabled": True,
        "lora_manager_enabled": True,
        "civitai_api_key": SecretStr("test-civitai-key"),
        "salad_worker_artifact_bucket": SecretStr("test-model-bucket"),
        "salad_worker_artifact_region": SecretStr("eu-central-1"),
        "salad_worker_model_manifest_json": SecretStr(baseline.model_dump_json()),
        "salad_worker_model_manifest_sha256": SecretStr(baseline.manifest_sha256),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("environment", "expected_docs_url"),
    [
        (Environment.LOCAL, "/docs"),
        (Environment.TEST, "/docs"),
        (Environment.STAGING, None),
        (Environment.PRODUCTION, None),
    ],
)
def test_api_schema_and_interactive_docs_are_local_test_only(
    environment: Environment,
    expected_docs_url: str | None,
) -> None:
    settings = Settings.model_construct(environment=environment)

    app = create_app(settings)

    assert app.docs_url == expected_docs_url
    assert app.redoc_url == ("/redoc" if expected_docs_url else None)
    assert app.openapi_url == ("/openapi.json" if expected_docs_url else None)


def test_salad_clients_are_built_without_contacting_provider(tmp_path: Path) -> None:
    webhook_secret = "whsec_" + base64.b64encode(b"salad-lifecycle-test-secret-material").decode()
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        salad_enabled=True,
        salad_api_key="dedicated-test-key",
        salad_organization="organization",
        salad_project="project",
        salad_queue_name="generation-queue",
        salad_container_group_name="generation-workers",
        salad_webhook_secret=webhook_secret,
        salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/live")

        assert response.status_code == 200
        assert isinstance(client.app.state.salad_client, SaladClient)
        assert isinstance(client.app.state.salad_webhook_verifier, SaladWebhookVerifier)
        assert "dedicated-test-key" not in repr(client.app.state.salad_client)


def test_danbooru_client_is_long_lived_bounded_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            created.append(self)
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    created: list[FakeAsyncClient] = []

    monkeypatch.setattr(app_module.httpx2, "AsyncClient", FakeAsyncClient)
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'danbooru-lifecycle.db').as_posix()}",
        auto_create_schema=True,
        auth_development_bypass_enabled=True,
    )

    with TestClient(create_app(settings)) as client:
        assert isinstance(
            client.app.state.danbooru_tag_autocomplete_service,
            DanbooruTagAutocompleteService,
        )
        assert len(created) == 1
        assert not created[0].closed

    assert created[0].closed


def test_x_oauth_provider_is_injected_and_closed_without_startup_secret_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = (
        "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
        "123456789012:secret:gen-automation/x/creator-AbCdEf"
    )

    class FakeProvider:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeRuntime:
        started = False
        stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    provider = FakeProvider()
    runtime = FakeRuntime()
    captured_provider: object | None = None

    def build_provider(**kwargs: object) -> FakeProvider:
        assert kwargs["configured_reference"] == reference
        assert kwargs["expected_creator_user_id"] == "2244994945"
        return provider

    def build_runtime(**kwargs: object) -> FakeRuntime:
        nonlocal captured_provider
        captured_provider = kwargs["x_oauth_provider"]
        return runtime

    monkeypatch.setattr(
        app_module,
        "build_aws_secrets_manager_x_oauth_provider",
        build_provider,
    )
    monkeypatch.setattr(app_module, "build_controller_runtime", build_runtime)
    settings = Settings(
        environment=Environment.TEST,
        database_url="postgresql+psycopg://user:pass@db/example",
        background_runtime_enabled=True,
        x_oauth_secret_reference=reference,
        x_creator_user_id="2244994945",
    )

    with TestClient(create_app(settings)) as client:
        assert client.app.state.x_oauth_provider is provider
        assert captured_provider is provider
        assert runtime.started
        assert not provider.closed

    assert runtime.stopped
    assert provider.closed


def test_x_oauth1_mode_selects_static_provider_without_rotation_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = (
        "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
        "123456789012:secret:gen-automation-staging/x/oauth1-AbCdEf"
    )

    class FakeProvider:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeRuntime:
        started = False
        stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    provider = FakeProvider()
    runtime = FakeRuntime()
    captured_provider: object | None = None

    def build_oauth1_provider(**kwargs: object) -> FakeProvider:
        assert kwargs == {
            "configured_reference": reference,
            "expected_creator_user_id": "2244994945",
            "request_timeout_seconds": 30,
        }
        return provider

    def build_runtime(**kwargs: object) -> FakeRuntime:
        nonlocal captured_provider
        captured_provider = kwargs["x_oauth_provider"]
        return runtime

    monkeypatch.setattr(
        app_module,
        "build_aws_secrets_manager_x_oauth1_provider",
        build_oauth1_provider,
    )
    monkeypatch.setattr(
        app_module,
        "build_aws_secrets_manager_x_oauth_provider",
        lambda **_kwargs: pytest.fail("OAuth2 rotation provider must not be built in OAuth1 mode"),
    )
    monkeypatch.setattr(app_module, "build_controller_runtime", build_runtime)
    settings = Settings(
        environment=Environment.TEST,
        database_url="postgresql+psycopg://user:pass@db/example",
        background_runtime_enabled=True,
        x_auth_mode=XAuthMode.OAUTH1,
        x_oauth_secret_reference=reference,
        x_creator_user_id="2244994945",
    )

    with TestClient(create_app(settings)) as client:
        assert client.app.state.x_oauth_provider is provider
        assert captured_provider is provider
        assert runtime.started
        assert not provider.closed

    assert runtime.stopped
    assert provider.closed


def test_lora_integrations_are_closed_after_normal_lifespan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCloseable:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeRuntime:
        started = False
        stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    model_store = FakeCloseable()
    civitai = FakeCloseable()
    runtime = FakeRuntime()

    async def build_lora(
        _settings: Settings,
    ) -> tuple[S3ObjectStore, ModelArtifactStore, CivitaiClient]:
        return (
            cast(S3ObjectStore, model_store),
            cast(ModelArtifactStore, object()),
            cast(CivitaiClient, civitai),
        )

    monkeypatch.setattr(app_module, "_build_lora_integrations", build_lora)
    monkeypatch.setattr(app_module, "build_controller_runtime", lambda **_kwargs: runtime)

    with TestClient(create_app(_lora_lifecycle_settings(tmp_path))) as client:
        assert client.app.state.controller_runtime is runtime
        assert runtime.started
        assert not model_store.closed
        assert not civitai.closed

    assert runtime.stopped
    assert model_store.closed
    assert civitai.closed


def test_lora_integrations_close_when_a_later_client_constructor_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCloseable:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeHttpClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    model_store = FakeCloseable()
    civitai = FakeCloseable()
    danbooru_http = FakeHttpClient()
    client_constructions = 0

    async def build_lora(
        _settings: Settings,
    ) -> tuple[S3ObjectStore, ModelArtifactStore, CivitaiClient]:
        return (
            cast(S3ObjectStore, model_store),
            cast(ModelArtifactStore, object()),
            cast(CivitaiClient, civitai),
        )

    def build_http_client(**_kwargs: object) -> FakeHttpClient:
        nonlocal client_constructions
        client_constructions += 1
        if client_constructions == 1:
            return danbooru_http
        raise RuntimeError("simulated later integration construction failure")

    monkeypatch.setattr(app_module, "_build_lora_integrations", build_lora)
    monkeypatch.setattr(app_module.httpx2, "AsyncClient", build_http_client)
    settings = _lora_lifecycle_settings(
        tmp_path,
        salad_enabled=True,
        salad_api_key="dedicated-test-key",
        salad_organization="organization",
        salad_project="project",
        salad_queue_name="generation-queue",
        salad_container_group_name="generation-workers",
        salad_webhook_secret=(
            "whsec_" + base64.b64encode(b"salad-lora-cleanup-secret-material").decode()
        ),
        salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
    )

    with pytest.raises(RuntimeError, match="later integration construction failure"):
        with TestClient(create_app(settings)):
            pass

    assert danbooru_http.closed
    assert model_store.closed
    assert civitai.closed
