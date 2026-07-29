import pytest
from pydantic import ValidationError

from gen_automation.config import Environment, Settings
from gen_automation.domain.signing import derive_public_key, encode_base64url

WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
WORKER_VERIFICATION_PUBLIC_KEY = derive_public_key(WORKER_SIGNING_PRIVATE_KEY)
TOTP_ENCRYPTION_KEY = encode_base64url(bytes(range(32)))
SESSION_SECRET = encode_base64url(bytes(range(32, 64)))
TRUSTED_PROXY_CIDRS = ("10.20.30.0/24",)


def test_production_configuration_fails_closed() -> None:
    with pytest.raises(ValidationError):
        Settings(environment=Environment.PRODUCTION)


def test_production_requires_migrations() -> None:
    with pytest.raises(ValidationError, match="automatic schema creation"):
        Settings(
            environment=Environment.PRODUCTION,
            database_url="postgresql+psycopg://user:pass@db/example",
            public_base_url="https://studio.example.com",
            session_secret=SESSION_SECRET,
            auto_create_schema=True,
        )


def test_production_requires_authentication_and_totp() -> None:
    common = {
        "environment": Environment.PRODUCTION,
        "database_url": "postgresql+psycopg://user:pass@db/example",
        "public_base_url": "https://studio.example.com",
        "session_secret": SESSION_SECRET,
        "storage_enabled": True,
        "storage_bucket": "private-assets",
    }
    with pytest.raises(ValidationError, match="administrative authentication"):
        Settings(**common)  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="requires TOTP"):
        Settings(
            **common,  # type: ignore[arg-type]
            auth_enabled=True,
            auth_require_totp=False,
            auth_totp_active_key_id="totp-key-1",
            auth_totp_encryption_keys={"totp-key-1": TOTP_ENCRYPTION_KEY},
        )


def test_enabled_authentication_requires_keys_and_bounded_sessions() -> None:
    with pytest.raises(ValidationError, match="random 32-byte session secret"):
        Settings(
            environment=Environment.TEST,
            auth_enabled=True,
            auth_totp_active_key_id="totp-key-1",
            auth_totp_encryption_keys={"totp-key-1": TOTP_ENCRYPTION_KEY},
        )

    with pytest.raises(ValidationError, match="TOTP encryption keyring"):
        Settings(
            environment=Environment.TEST,
            auth_enabled=True,
            session_secret=SESSION_SECRET,
            auth_totp_active_key_id="totp-key-1",
            auth_totp_encryption_keys={"totp-key-1": "malformed"},
        )

    with pytest.raises(ValidationError, match="idle timeout"):
        Settings(
            environment=Environment.TEST,
            auth_enabled=True,
            session_secret=SESSION_SECRET,
            auth_totp_active_key_id="totp-key-1",
            auth_totp_encryption_keys={"totp-key-1": TOTP_ENCRYPTION_KEY},
            auth_session_idle_seconds=3600,
            auth_session_absolute_seconds=1800,
        )


def test_administrator_enrollment_expiry_is_bounded() -> None:
    assert Settings().auth_enrollment_invite_ttl_seconds == 86400
    with pytest.raises(ValidationError):
        Settings(auth_enrollment_invite_ttl_seconds=599)
    with pytest.raises(ValidationError):
        Settings(auth_enrollment_invite_ttl_seconds=7 * 86400 + 1)


def test_authentication_development_bypass_is_explicit_and_environment_bounded() -> None:
    assert Settings().auth_development_bypass_enabled is False
    assert Settings(
        environment=Environment.LOCAL,
        auth_development_bypass_enabled=True,
    ).auth_development_bypass_enabled
    assert Settings(
        environment=Environment.TEST,
        auth_development_bypass_enabled=True,
    ).auth_development_bypass_enabled

    with pytest.raises(ValidationError, match="mutually exclusive"):
        Settings(
            environment=Environment.TEST,
            auth_enabled=True,
            auth_development_bypass_enabled=True,
            session_secret=SESSION_SECRET,
            auth_totp_active_key_id="totp-key-1",
            auth_totp_encryption_keys={"totp-key-1": TOTP_ENCRYPTION_KEY},
        )

    with pytest.raises(ValidationError, match="forbidden outside local/test"):
        Settings(
            environment=Environment.PRODUCTION,
            auth_development_bypass_enabled=True,
        )


def test_protected_network_boundary_requires_ingress_guards() -> None:
    common = {
        "environment": Environment.STAGING,
        "database_url": "postgresql+psycopg://user:pass@db/example",
        "public_base_url": "https://studio.example.com",
        "session_secret": SESSION_SECRET,
        "auth_enabled": True,
        "auth_totp_active_key_id": "totp-key-1",
        "auth_totp_encryption_keys": {"totp-key-1": TOTP_ENCRYPTION_KEY},
        "storage_enabled": True,
        "storage_bucket": "private-assets",
    }
    with pytest.raises(ValidationError, match="trusted proxy CIDR"):
        Settings(
            **common,  # type: ignore[arg-type]
            ingress_rate_limit_configured=True,
            ingress_request_guards_configured=True,
        )

    with pytest.raises(ValidationError, match="ingress rate-limit assertion"):
        Settings(
            **common,  # type: ignore[arg-type]
            trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS,
            ingress_request_guards_configured=True,
        )

    with pytest.raises(ValidationError, match="ingress request-guard assertion"):
        Settings(
            **common,  # type: ignore[arg-type]
            trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS,
            ingress_rate_limit_configured=True,
        )


@pytest.mark.parametrize(
    ("cidrs", "message"),
    [
        (("10.20.30.1/24",), "CIDR is invalid"),
        (("0.0.0.0/0",), "entire address space"),
        (("::/0",), "entire address space"),
        ((" 10.20.30.0/24",), "CIDR is invalid"),
        (
            ("10.20.30.0/24", "10.20.30.0/24"),
            "must be unique",
        ),
    ],
)
def test_trusted_proxy_cidrs_are_strict_bounded_networks(
    cidrs: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(trusted_proxy_cidrs=cidrs)


def test_authentication_cookie_names_are_host_prefixed_only_on_https_environments() -> None:
    local = Settings()
    production = Settings(
        environment=Environment.PRODUCTION,
        database_url="postgresql+psycopg://user:pass@db/example",
        public_base_url="https://studio.example.com",
        session_secret=SESSION_SECRET,
        auth_enabled=True,
        auth_totp_active_key_id="totp-key-1",
        auth_totp_encryption_keys={"totp-key-1": TOTP_ENCRYPTION_KEY},
        trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS,
        ingress_rate_limit_configured=True,
        ingress_request_guards_configured=True,
        storage_enabled=True,
        storage_bucket="private-assets",
    )

    assert local.auth_session_cookie_name == "gen_session"
    assert local.auth_csrf_cookie_name == "gen_csrf"
    assert production.auth_session_cookie_name == "__Host-gen_session"
    assert production.auth_csrf_cookie_name == "__Host-gen_csrf"


def test_salad_integration_fails_closed_when_incomplete() -> None:
    with pytest.raises(ValidationError, match="enabled SaladCloud"):
        Settings(
            environment=Environment.TEST,
            salad_enabled=True,
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )


def test_gpu_allocation_requires_salad() -> None:
    with pytest.raises(ValidationError, match="GPU allocation"):
        Settings(
            environment=Environment.TEST,
            gpu_allocation_enabled=True,
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )


def test_gpu_allocation_requires_storage_and_worker_signing_key() -> None:
    with pytest.raises(ValidationError, match="private object storage"):
        Settings(
            environment=Environment.TEST,
            gpu_allocation_enabled=True,
            salad_enabled=True,
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="generation-queue",
            salad_container_group_name="generation-workers",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )

    with pytest.raises(ValidationError, match="worker signing"):
        Settings(
            environment=Environment.TEST,
            gpu_allocation_enabled=True,
            storage_enabled=True,
            storage_bucket="private-assets",
            salad_enabled=True,
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="generation-queue",
            salad_container_group_name="generation-workers",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )


@pytest.mark.parametrize(
    "credentials",
    [
        {"storage_access_key_id": "temporary-access"},
        {"storage_secret_access_key": "temporary-secret"},
        {"storage_session_token": "temporary-token"},
        {
            "storage_access_key_id": "temporary-access",
            "storage_session_token": "temporary-token",
        },
        {
            "storage_secret_access_key": "temporary-secret",
            "storage_session_token": "temporary-token",
        },
    ],
)
def test_storage_credentials_reject_half_pairs_and_unbound_session_tokens(
    credentials: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="object-storage"):
        Settings(**credentials)


def test_storage_session_token_is_redacted_in_settings_representation() -> None:
    token = "temporary-session-token-that-must-not-appear"  # noqa: S105
    settings = Settings(
        storage_access_key_id="temporary-access",
        storage_secret_access_key="temporary-secret",  # noqa: S106
        storage_session_token=token,
    )

    assert token not in repr(settings)
    assert str(settings.storage_session_token) == "**********"


def test_x_oauth_requires_one_exact_arn_and_matching_numeric_creator_binding() -> None:
    reference = (
        "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
        "123456789012:secret:gen-automation/x/creator-AbCdEf"
    )
    configured = Settings(
        environment=Environment.TEST,
        database_url="postgresql+psycopg://user:pass@db/example",
        x_oauth_secret_reference=reference,
        x_creator_user_id="2244994945",
    )

    assert configured.x_oauth_secret_reference == reference
    with pytest.raises(ValidationError, match="configured together"):
        Settings(
            environment=Environment.TEST,
            database_url="postgresql+psycopg://user:pass@db/example",
            x_oauth_secret_reference=reference,
        )
    with pytest.raises(ValidationError):
        Settings(
            environment=Environment.TEST,
            database_url="postgresql+psycopg://user:pass@db/example",
            x_oauth_secret_reference=reference,
            x_creator_user_id="1" * 20,
        )
    with pytest.raises(ValidationError):
        Settings(
            environment=Environment.TEST,
            database_url="postgresql+psycopg://user:pass@db/example",
            x_oauth_secret_reference="aws-secrets-manager://creator-secret",  # noqa: S106
            x_creator_user_id="2244994945",
        )


def test_gpu_allocation_accepts_complete_worker_security_configuration() -> None:
    settings = Settings(
        environment=Environment.TEST,
        gpu_allocation_enabled=True,
        storage_enabled=True,
        storage_bucket="private-assets",
        salad_enabled=True,
        salad_api_key="test-key",
        salad_organization="organization",
        salad_project="project",
        salad_queue_name="generation-queue",
        salad_container_group_name="generation-workers",
        salad_gpu_class_ids=("3c90c3cc-0d44-4b50-8888-8dd25736052a",),
        salad_webhook_secret="whsec_test",  # noqa: S106
        salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
        worker_signing_key_id="worker-key-1",
        worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )

    assert settings.gpu_allocation_enabled is True
    assert settings.worker_verification_public_key == WORKER_VERIFICATION_PUBLIC_KEY


@pytest.mark.parametrize(
    "private_key",
    [
        "not-base64url",
        WORKER_SIGNING_PRIVATE_KEY + "=",
        encode_base64url(b"x" * 31),
    ],
)
def test_gpu_allocation_rejects_malformed_or_noncanonical_private_keys(
    private_key: str,
) -> None:
    with pytest.raises(ValidationError, match="Ed25519 private signing key"):
        Settings(
            environment=Environment.TEST,
            gpu_allocation_enabled=True,
            storage_enabled=True,
            storage_bucket="private-assets",
            salad_enabled=True,
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="generation-queue",
            salad_container_group_name="generation-workers",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
            worker_signing_key_id="worker-key-1",
            worker_signing_private_key=private_key,
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )


def test_gpu_allocation_rejects_upload_grants_that_expire_during_execution() -> None:
    with pytest.raises(ValidationError, match="upload grant TTL"):
        Settings(
            environment=Environment.TEST,
            gpu_allocation_enabled=True,
            storage_enabled=True,
            storage_bucket="private-assets",
            salad_enabled=True,
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="generation-queue",
            salad_container_group_name="generation-workers",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
            worker_signing_key_id="worker-key-1",
            worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            worker_signature_ttl_seconds=7200,
            worker_upload_grant_ttl_seconds=8000,
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )


def test_salad_requires_digest_pinned_worker_and_budget_ordering() -> None:
    with pytest.raises(ValidationError, match="pinned by digest"):
        Settings(
            environment=Environment.TEST,
            salad_enabled=True,
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="queue",
            salad_container_group_name="group",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image="registry.example/worker:latest",
            salad_daily_budget_usd="25.00",
            salad_monthly_budget_usd="20.00",
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
        )


def test_salad_accepts_a_complete_fail_closed_test_configuration() -> None:
    settings = Settings(
        environment=Environment.TEST,
        salad_enabled=True,
        salad_api_key="test-key",
        salad_organization="organization",
        salad_project="project",
        salad_queue_name="generation-queue",
        salad_container_group_name="generation-workers",
        salad_webhook_secret="whsec_test",  # noqa: S106
        salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
        salad_daily_budget_usd="25.00",
        salad_monthly_budget_usd="250.00",
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )

    assert settings.salad_max_replicas == 1


def test_salad_deployment_timeout_covers_full_three_request_provisioning_chain() -> None:
    values = {
        "environment": Environment.TEST,
        "salad_enabled": True,
        "salad_api_key": "test-key",
        "salad_organization": "organization",
        "salad_project": "project",
        "salad_queue_name": "generation-queue",
        "salad_container_group_name": "generation-workers",
        "salad_webhook_secret": "whsec_test",
        "salad_worker_image": f"registry.example/worker@sha256:{'a' * 64}",
        "salad_request_timeout_seconds": 60,
        "background_deployment_timeout_seconds": 180,
        "session_secret": "test-session-secret-with-more-than-32-characters",
    }

    with pytest.raises(ValidationError, match="three bounded SaladCloud requests"):
        Settings(**values)  # type: ignore[arg-type]

    values["background_deployment_timeout_seconds"] = 185
    settings = Settings(**values)  # type: ignore[arg-type]
    assert settings.background_deployment_timeout_seconds == 185


def test_background_health_thresholds_and_staleness_are_ordered() -> None:
    with pytest.raises(ValidationError, match="cannot be lower than readiness"):
        Settings(
            background_readiness_failure_threshold=5,
            background_liveness_failure_threshold=4,
        )

    with pytest.raises(ValidationError, match="longest cycle timeout plus maximum jittered delay"):
        Settings(
            environment=Environment.TEST,
            background_runtime_enabled=True,
            storage_enabled=True,
            storage_bucket="private-assets",
            background_collection_timeout_seconds=300,
            background_error_backoff_max_seconds=60,
            background_loop_stale_after_seconds=375,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("salad_organization", "Uppercase"),
        ("salad_project", "a"),
        ("salad_queue_name", "queue_with_underscore"),
        ("salad_container_group_name", "-leading-hyphen"),
    ],
)
def test_salad_rejects_invalid_resource_names(field: str, value: str) -> None:
    values = {
        "environment": Environment.TEST,
        "salad_enabled": True,
        "salad_api_key": "test-key",
        "salad_organization": "organization",
        "salad_project": "project",
        "salad_queue_name": "generation-queue",
        "salad_container_group_name": "generation-workers",
        "salad_webhook_secret": "whsec_test",
        "salad_worker_image": f"registry.example/worker@sha256:{'a' * 64}",
        "session_secret": "test-session-secret-with-more-than-32-characters",
        field: value,
    }

    with pytest.raises(ValidationError, match="invalid SaladCloud resource name"):
        Settings(**values)  # type: ignore[arg-type]


def test_production_salad_api_host_is_pinned() -> None:
    with pytest.raises(ValidationError, match="official SaladCloud API"):
        Settings(
            environment=Environment.PRODUCTION,
            database_url="postgresql+psycopg://user:pass@db/example",
            public_base_url="https://studio.example.com",
            session_secret="a-secure-random-session-secret-that-is-long",  # noqa: S106
            storage_enabled=True,
            storage_bucket="private-assets",
            salad_enabled=True,
            salad_api_base_url="https://attacker.example/api",
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="generation-queue",
            salad_container_group_name="generation-workers",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
        )
