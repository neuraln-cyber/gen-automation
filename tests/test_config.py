from pathlib import Path

import pytest
from pydantic import ValidationError

from gen_automation.config import Environment, SaladContainerPriority, Settings, XAuthMode
from gen_automation.domain.deliverability import PATREON_MAX_ARCHIVE_BYTES
from gen_automation.domain.enums import SemanticEnforcementMode
from gen_automation.domain.signing import derive_public_key, encode_base64url

WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
WORKER_VERIFICATION_PUBLIC_KEY = derive_public_key(WORKER_SIGNING_PRIVATE_KEY)
TOTP_ENCRYPTION_KEY = encode_base64url(bytes(range(32)))
SESSION_SECRET = encode_base64url(bytes(range(32, 64)))
TRUSTED_PROXY_CIDRS = ("10.20.30.0/24",)


def test_semantic_retry_defaults_cover_scale_to_zero_cold_start() -> None:
    settings = Settings()

    assert settings.background_semantic_max_attempts == 5
    assert settings.background_semantic_retry_base_seconds == 30
    assert settings.background_semantic_retry_max_seconds == 120


def test_salad_reconciliation_timeout_default_covers_eight_pages_and_cancel() -> None:
    assert Settings().background_reconcile_timeout_seconds == 300


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


def test_enabled_authentication_accepts_bounded_single_owner_session() -> None:
    settings = Settings(
        environment=Environment.TEST,
        auth_enabled=True,
        session_secret=SESSION_SECRET,
        auth_totp_active_key_id="totp-key-1",
        auth_totp_encryption_keys={"totp-key-1": TOTP_ENCRYPTION_KEY},
        auth_session_absolute_seconds=90 * 86400,
        auth_session_idle_seconds=30 * 86400,
        auth_recent_auth_seconds=3600,
    )

    assert settings.auth_session_absolute_seconds == 90 * 86400
    assert settings.auth_session_idle_seconds == 30 * 86400
    assert settings.auth_recent_auth_seconds == 3600


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("auth_session_absolute_seconds", 90 * 86400 + 1),
        ("auth_session_idle_seconds", 30 * 86400 + 1),
        ("auth_recent_auth_seconds", 3601),
    ),
)
def test_authentication_rejects_session_values_above_single_owner_bounds(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


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


def test_semantic_anatomy_defaults_to_non_blocking_shadow_mode() -> None:
    settings = Settings()

    assert settings.semantic_anatomy_mode == SemanticEnforcementMode.SHADOW
    assert settings.semantic_anatomy_max_assessments_per_profile == 0
    assert settings.semantic_anatomy_asset_allowlist == ()


def test_enabled_semantic_anatomy_requires_positive_assessment_limit() -> None:
    with pytest.raises(ValidationError, match="positive per-scoring-run assessment limit"):
        Settings(semantic_anatomy_enabled=True)


def test_protected_semantic_endpoint_allows_only_exact_loopback_gateway() -> None:
    common = {
        "environment": Environment.STAGING,
        "database_url": "postgresql+psycopg://user:pass@db/example",
        "public_base_url": "https://studio.example.com",
        "session_secret": SESSION_SECRET,
        "auth_enabled": True,
        "auth_totp_active_key_id": "totp-key-1",
        "auth_totp_encryption_keys": {"totp-key-1": TOTP_ENCRYPTION_KEY},
        "trusted_proxy_cidrs": TRUSTED_PROXY_CIDRS,
        "ingress_rate_limit_configured": True,
        "ingress_request_guards_configured": True,
        "storage_enabled": True,
        "storage_bucket": "private-assets",
        "background_runtime_enabled": True,
        "quality_scoring_enabled": True,
        "semantic_anatomy_enabled": True,
        "semantic_anatomy_max_assessments_per_profile": 1,
        "semantic_anatomy_model_revision": "60595ebc30ec8e3b1d3b9e65d4943ca011c0006a",
    }
    settings = Settings(
        **common,  # type: ignore[arg-type]
        semantic_anatomy_endpoint_url="http://127.0.0.1:8091/v1/anatomy/assess",
    )
    assert str(settings.semantic_anatomy_endpoint_url) == (
        "http://127.0.0.1:8091/v1/anatomy/assess"
    )

    for unsafe_url in (
        "http://localhost:8091/v1/anatomy/assess",
        "http://127.0.0.1:8092/v1/anatomy/assess",
        "http://127.0.0.1:8091/health/ready",
        "http://127.0.0.1:8091/v1/anatomy/assess?redirect=true",
    ):
        with pytest.raises(ValidationError, match="exact loopback"):
            Settings(
                **common,  # type: ignore[arg-type]
                semantic_anatomy_endpoint_url=unsafe_url,
            )

    with pytest.raises(ValidationError, match="semantic lease must exceed"):
        Settings(
            **common,  # type: ignore[arg-type]
            semantic_anatomy_endpoint_url="http://127.0.0.1:8091/v1/anatomy/assess",
            background_semantic_lease_seconds=210,
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


def test_x_auth_mode_defaults_to_oauth2_and_oauth1_does_not_require_postgres_rotation() -> None:
    reference = (
        "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
        "123456789012:secret:gen-automation-staging/x/oauth1-AbCdEf"
    )

    assert Settings().x_auth_mode == XAuthMode.OAUTH2
    oauth1 = Settings(
        environment=Environment.TEST,
        database_url="sqlite+aiosqlite:///oauth1-test.db",
        x_auth_mode=XAuthMode.OAUTH1,
        x_oauth_secret_reference=reference,
        x_creator_user_id="2244994945",
    )
    assert oauth1.x_auth_mode == XAuthMode.OAUTH1

    with pytest.raises(ValidationError, match="serialization requires PostgreSQL"):
        Settings(
            environment=Environment.TEST,
            database_url="sqlite+aiosqlite:///oauth2-test.db",
            x_auth_mode=XAuthMode.OAUTH2,
            x_oauth_secret_reference=reference,
            x_creator_user_id="2244994945",
        )
    with pytest.raises(ValidationError):
        Settings(x_auth_mode="implicit-or-unknown")


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
    assert settings.salad_attempt_watchdog_seconds == 105 * 60


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


def test_gpu_allocation_requires_watchdog_margin_before_signature_expiry() -> None:
    with pytest.raises(ValidationError, match="watchdog must expire at least 300 seconds"):
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
            salad_gpu_class_ids=("3c90c3cc-0d44-4b50-8888-8dd25736052a",),
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
            worker_signing_key_id="worker-key-1",
            worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            worker_signature_ttl_seconds=6500,
            salad_attempt_watchdog_seconds=6300,
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
    assert settings.salad_max_queued_jobs == 3
    assert settings.salad_container_priority == SaladContainerPriority.LOW


@pytest.mark.parametrize("priority", tuple(SaladContainerPriority))
def test_salad_accepts_each_provider_container_priority(
    priority: SaladContainerPriority,
) -> None:
    assert Settings(salad_container_priority=priority.value).salad_container_priority == priority


def test_salad_rejects_unknown_container_priority() -> None:
    with pytest.raises(ValidationError, match="salad_container_priority"):
        Settings(salad_container_priority="urgent")  # type: ignore[arg-type]


def test_salad_deployment_timeout_covers_observe_then_immediate_stop_chain() -> None:
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
        "background_deployment_timeout_seconds": 360,
        "background_reconcile_timeout_seconds": 545,
        "session_secret": "test-session-secret-with-more-than-32-characters",
    }

    with pytest.raises(ValidationError, match="six bounded SaladCloud requests"):
        Settings(**values)  # type: ignore[arg-type]

    values["background_deployment_timeout_seconds"] = 365
    settings = Settings(**values)  # type: ignore[arg-type]
    assert settings.background_deployment_timeout_seconds == 365


def test_salad_attempt_reconcile_timeout_covers_eight_list_pages_then_cancel() -> None:
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
        "salad_request_timeout_seconds": 30,
        "background_reconcile_timeout_seconds": 274,
        "session_secret": "test-session-secret-with-more-than-32-characters",
    }

    with pytest.raises(ValidationError, match="nine bounded SaladCloud requests"):
        Settings(**values)  # type: ignore[arg-type]

    values["background_reconcile_timeout_seconds"] = 275
    settings = Settings(**values)  # type: ignore[arg-type]
    assert settings.background_reconcile_timeout_seconds == 275


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


def test_enabled_delivery_paths_cover_the_internal_patreon_archive_cap() -> None:
    common = {
        "environment": Environment.TEST,
        "background_runtime_enabled": True,
        "storage_enabled": True,
        "storage_bucket": "private-assets",
        "publishing_enabled": True,
    }
    with pytest.raises(ValidationError, match="publication package capacity"):
        Settings(
            **common,  # type: ignore[arg-type]
            background_publication_max_package_bytes=PATREON_MAX_ARCHIVE_BYTES - 1,
        )
    with pytest.raises(ValidationError, match="publication package capacity"):
        Settings(
            **common,  # type: ignore[arg-type]
            background_publication_max_package_bytes=PATREON_MAX_ARCHIVE_BYTES + 1,
        )

    with pytest.raises(ValidationError, match="MEGA source-part capacity"):
        Settings(
            **common,  # type: ignore[arg-type]
            mega_delivery_enabled=True,
            mega_profile_home="/var/lib/mega-profile",
            background_mega_max_package_bytes=PATREON_MAX_ARCHIVE_BYTES - 1,
        )


def test_mega_extracted_set_delivery_does_not_require_publishing(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        background_runtime_enabled=True,
        storage_enabled=True,
        storage_bucket="private-assets",
        publishing_enabled=False,
        mega_delivery_enabled=True,
        mega_profile_home=str(tmp_path / "mega-profile"),
    )

    assert settings.mega_delivery_enabled is True
    assert settings.publishing_enabled is False


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
