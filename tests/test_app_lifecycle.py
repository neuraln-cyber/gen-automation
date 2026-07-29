import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gen_automation.app as app_module
from gen_automation.app import create_app
from gen_automation.config import Environment, Settings
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.webhooks import SaladWebhookVerifier


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
