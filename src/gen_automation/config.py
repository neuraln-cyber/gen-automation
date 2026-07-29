import re
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.auth.security import SecretEncryptionError, TotpSecretCipher
from gen_automation.domain.deliverability import PATREON_MAX_ARCHIVE_BYTES
from gen_automation.domain.runtime_bindings import (
    WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING,
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
    WORKER_ARTIFACT_BUCKET_BINDING,
    WORKER_ARTIFACT_REGION_BINDING,
    WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
    WORKER_MODEL_MANIFEST_JSON_BINDING,
    WORKER_MODEL_MANIFEST_SHA256_BINDING,
)
from gen_automation.domain.signing import (
    SigningMaterialError,
    derive_public_key,
    validate_private_key,
)

LOCAL_SESSION_SECRET = "local-development-only"  # noqa: S105
SALAD_API_BASE_URL = "https://api.salad.com/api/public"
SALAD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
SALAD_WORKER_IMAGE_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
WORKER_SIGNING_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORKER_ARTIFACT_BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,254}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
X_OAUTH_SECRET_REFERENCE_PATTERN = (
    r"^aws-secrets-manager://arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:"  # noqa: S105
    r"[a-z0-9-]{3,32}:[0-9]{12}:secret:"
    r"[A-Za-z0-9._/-]{1,400}-[A-Za-z0-9]{6}$"
)
SALAD_DEPLOYMENT_REQUESTS_PER_CYCLE = 3
SALAD_RECONCILIATION_REQUESTS_PER_CYCLE = 2
SALAD_OPERATION_TIMEOUT_MARGIN_SECONDS = 5
BACKGROUND_MAX_DELAY_JITTER_MULTIPLIER = 1.2
MAX_TRUSTED_PROXY_CIDRS = 64

type TrustedProxyNetwork = IPv4Network | IPv6Network


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


def _secret_value(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    raw_value = value.get_secret_value()
    return raw_value if raw_value.strip() else None


def _split_https_url(value: str) -> SplitResult | None:
    if (
        not value
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return parsed


def _is_https_origin(value: str) -> bool:
    parsed = _split_https_url(value)
    return parsed is not None and parsed.path in {"", "/"} and not parsed.query


def _is_https_url(value: str) -> bool:
    parsed = _split_https_url(value)
    return parsed is not None and not parsed.query


def _parse_trusted_proxy_cidrs(
    values: tuple[str, ...],
) -> tuple[TrustedProxyNetwork, ...]:
    networks: list[TrustedProxyNetwork] = []
    canonical_networks: set[str] = set()
    for value in values:
        if (
            not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("trusted proxy CIDR is invalid")
        try:
            network = ip_network(value, strict=True)
        except ValueError:
            raise ValueError("trusted proxy CIDR is invalid") from None
        if network.prefixlen == 0:
            raise ValueError("trusted proxy CIDR must not trust the entire address space")
        canonical = str(network)
        if canonical in canonical_networks:
            raise ValueError("trusted proxy CIDRs must be unique")
        canonical_networks.add(canonical)
        networks.append(network)
    return tuple(networks)


def _valid_mega_remote_root(value: str) -> bool:
    if value == "/":
        return True
    if (
        not value
        or not value.startswith("/")
        or len(value) > 1024
        or value != value.rstrip("/")
        or "\\" in value
        or "//" in value
        or any(character in value for character in ("\x00", "\r", "\n", "*", "?"))
    ):
        return False
    path = PurePosixPath(value)
    return str(path) == value and all(part not in {".", ".."} for part in path.parts)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEN_AUTOMATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    app_name: str = "Gen Automation"
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    database_url: str = "sqlite+aiosqlite:///:memory:"
    session_secret: SecretStr = SecretStr(LOCAL_SESSION_SECRET)
    auto_create_schema: bool = False
    auth_enabled: bool = False
    auth_development_bypass_enabled: bool = False
    auth_require_totp: bool = True
    auth_totp_active_key_id: str | None = None
    auth_totp_encryption_keys: dict[str, SecretStr] = Field(default_factory=dict)
    auth_session_absolute_seconds: int = Field(default=12 * 60 * 60, ge=900, le=7 * 86400)
    auth_session_idle_seconds: int = Field(default=30 * 60, ge=300, le=86400)
    auth_recent_auth_seconds: int = Field(default=10 * 60, ge=60, le=3600)
    auth_login_window_seconds: int = Field(default=15 * 60, ge=60, le=3600)
    auth_login_max_failures: int = Field(default=5, ge=3, le=20)
    auth_login_lockout_seconds: int = Field(default=15 * 60, ge=60, le=86400)
    auth_enrollment_invite_ttl_seconds: int = Field(
        default=24 * 60 * 60,
        ge=10 * 60,
        le=7 * 24 * 60 * 60,
    )
    trusted_proxy_cidrs: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_TRUSTED_PROXY_CIDRS,
    )
    ingress_rate_limit_configured: bool = False
    ingress_request_guards_configured: bool = False
    background_runtime_enabled: bool = False
    background_poll_interval_seconds: float = Field(default=2.0, ge=0.05, le=60)
    background_error_backoff_max_seconds: float = Field(default=60, ge=1, le=300)
    background_submit_timeout_seconds: float = Field(default=180, ge=5, le=600)
    background_deployment_timeout_seconds: float = Field(default=180, ge=5, le=600)
    background_reconcile_timeout_seconds: float = Field(default=180, ge=5, le=600)
    background_inbox_timeout_seconds: float = Field(default=30, ge=1, le=300)
    background_collection_timeout_seconds: float = Field(default=300, ge=5, le=1800)
    quality_scoring_enabled: bool = False
    background_quality_timeout_seconds: float = Field(default=75, ge=10, le=600)
    background_quality_analysis_timeout_seconds: float = Field(default=45, ge=1, le=300)
    background_quality_memory_limit_bytes: int = Field(
        default=768 * 1024 * 1024,
        ge=256 * 1024 * 1024,
        le=4 * 1024 * 1024 * 1024,
    )
    background_quality_lease_seconds: int = Field(default=120, ge=10, le=3600)
    background_quality_max_attempts: int = Field(default=3, ge=1, le=10)
    background_quality_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    background_quality_retry_max_seconds: int = Field(default=900, ge=1, le=86400)
    semantic_anatomy_enabled: bool = False
    semantic_anatomy_endpoint_url: AnyHttpUrl | None = None
    semantic_anatomy_model: str = Field(
        default="Qwen/Qwen3-VL-8B-Instruct",
        min_length=1,
        max_length=200,
    )
    semantic_anatomy_model_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    semantic_anatomy_severe_confidence_micros: int = Field(
        default=900_000,
        ge=0,
        le=1_000_000,
    )
    background_semantic_timeout_seconds: float = Field(default=210, ge=15, le=900)
    background_semantic_request_timeout_seconds: float = Field(default=180, ge=10, le=840)
    background_semantic_lease_seconds: int = Field(default=300, ge=30, le=3600)
    background_semantic_max_attempts: int = Field(default=3, ge=1, le=10)
    background_semantic_retry_base_seconds: int = Field(default=60, ge=1, le=3600)
    background_semantic_retry_max_seconds: int = Field(default=1800, ge=1, le=86400)
    derivative_rendering_enabled: bool = False
    background_derivative_timeout_seconds: float = Field(
        default=150,
        ge=10,
        le=900,
    )
    background_derivative_render_timeout_seconds: float = Field(
        default=120,
        ge=1,
        le=900,
    )
    background_derivative_memory_limit_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=512 * 1024 * 1024,
        le=8 * 1024 * 1024 * 1024,
    )
    background_derivative_lease_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
    )
    background_derivative_retry_base_seconds: int = Field(
        default=30,
        ge=1,
        le=3600,
    )
    background_derivative_retry_max_seconds: int = Field(
        default=900,
        ge=1,
        le=86400,
    )
    background_publication_timeout_seconds: float = Field(
        default=300,
        ge=10,
        le=900,
    )
    background_publication_lease_seconds: int = Field(
        default=600,
        ge=30,
        le=3600,
    )
    background_publication_retry_base_seconds: int = Field(
        default=30,
        ge=1,
        le=86400,
    )
    background_publication_retry_max_seconds: int = Field(
        default=900,
        ge=1,
        le=7 * 86400,
    )
    background_publication_max_package_bytes: int = Field(
        default=160 * 1024 * 1024,
        ge=1024,
        le=5 * 1024 * 1024 * 1024,
    )
    mega_delivery_enabled: bool = False
    mega_profile_home: Path | None = None
    mega_remote_root: str = Field(default="/AutomatedSets", min_length=1, max_length=1024)
    background_mega_timeout_seconds: float = Field(
        default=660,
        ge=60,
        le=7200,
    )
    background_mega_command_timeout_seconds: float = Field(
        default=300,
        ge=1,
        le=3600,
    )
    background_mega_lease_seconds: int = Field(
        default=900,
        ge=120,
        le=7200,
    )
    background_mega_retry_base_seconds: int = Field(
        default=300,
        ge=1,
        le=7 * 86400,
    )
    background_mega_retry_max_seconds: int = Field(
        default=3600,
        ge=1,
        le=7 * 86400,
    )
    background_mega_max_package_bytes: int = Field(
        default=160 * 1024 * 1024,
        ge=1024,
        le=512 * 1024 * 1024,
    )
    background_outbox_lease_seconds: int = Field(default=300, ge=10, le=3600)
    background_inbox_lease_seconds: int = Field(default=120, ge=10, le=3600)
    background_collection_lease_seconds: int = Field(default=900, ge=60, le=3600)
    background_reconciliation_interval_seconds: int = Field(default=30, ge=5, le=3600)
    background_retry_delay_seconds: int = Field(default=60, ge=1, le=3600)
    background_shutdown_grace_seconds: float = Field(default=30, ge=1, le=300)
    background_shutdown_cancel_seconds: float = Field(default=5, ge=0.1, le=60)
    background_readiness_failure_threshold: int = Field(default=3, ge=1, le=100)
    background_liveness_failure_threshold: int = Field(default=10, ge=1, le=100)
    background_loop_stale_after_seconds: float = Field(default=900, ge=10, le=7200)
    gpu_allocation_enabled: bool = False
    publishing_enabled: bool = False
    patreon_browser_publishing_enabled: bool = False
    patreon_browser_sidecar_url: AnyHttpUrl | None = None
    patreon_browser_profile_reference: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        max_length=64,
    )
    patreon_browser_shared_secret: SecretStr | None = None
    patreon_browser_timeout_seconds: float = Field(default=240, ge=10, le=840)
    x_creator_user_id: str | None = Field(
        default=None,
        pattern=r"^[1-9][0-9]{0,18}$",
        max_length=19,
    )
    x_oauth_secret_reference: str | None = Field(
        default=None,
        pattern=X_OAUTH_SECRET_REFERENCE_PATTERN,
        max_length=500,
    )
    x_oauth_request_timeout_seconds: float = Field(default=30, gt=0, le=60)
    x_oauth_lock_timeout_seconds: float = Field(default=15, gt=0, le=60)
    x_oauth_refresh_margin_seconds: int = Field(default=300, ge=30, le=1800)
    salad_enabled: bool = False
    salad_api_base_url: AnyHttpUrl = AnyHttpUrl(SALAD_API_BASE_URL)
    salad_api_key: SecretStr | None = None
    salad_organization: str | None = None
    salad_project: str | None = None
    salad_queue_name: str | None = None
    salad_container_group_name: str | None = None
    salad_webhook_secret: SecretStr | None = None
    salad_worker_image: str | None = None
    worker_signing_key_id: str | None = None
    worker_signing_private_key: SecretStr | None = None
    worker_signature_ttl_seconds: int = Field(default=7200, ge=5, le=7200)
    worker_upload_grant_ttl_seconds: int = Field(default=10800, ge=3600, le=14400)
    salad_worker_allowed_upload_origin: SecretStr | None = None
    salad_worker_model_manifest_json: SecretStr | None = None
    salad_worker_model_manifest_sha256: SecretStr | None = None
    salad_worker_artifact_bucket: SecretStr | None = None
    salad_worker_artifact_region: SecretStr | None = None
    salad_worker_artifact_endpoint_url: SecretStr | None = None
    salad_worker_artifact_access_key_id: SecretStr | None = None
    salad_worker_artifact_secret_access_key: SecretStr | None = None
    salad_worker_artifact_session_token: SecretStr | None = None
    salad_request_timeout_seconds: float = Field(default=30, ge=1, le=120)
    salad_webhook_max_body_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        le=1024 * 1024,
    )
    salad_max_replicas: int = Field(default=1, ge=1, le=1)
    salad_max_queued_jobs: int = Field(default=1, ge=1, le=1)
    salad_gpu_class_ids: tuple[UUID, ...] = Field(default=(), max_length=16)
    salad_container_cpu: int = Field(default=4, ge=1, le=16)
    salad_container_memory_mb: int = Field(default=16 * 1024, ge=1024, le=64 * 1024)
    salad_container_storage_bytes: int = Field(
        default=50 * 1024 * 1024 * 1024,
        ge=10 * 1024 * 1024 * 1024,
        le=250 * 1024 * 1024 * 1024,
    )
    salad_max_hourly_cost_usd: Decimal = Field(
        default=Decimal("1.00"),
        gt=0,
        le=Decimal("100.00"),
        decimal_places=6,
    )
    salad_daily_budget_usd: Decimal = Field(
        default=Decimal("25.00"),
        gt=0,
        decimal_places=2,
    )
    salad_monthly_budget_usd: Decimal = Field(
        default=Decimal("250.00"),
        gt=0,
        decimal_places=2,
    )
    storage_enabled: bool = False
    storage_endpoint_url: AnyHttpUrl | None = None
    storage_region: str = "us-east-1"
    storage_bucket: str | None = None
    storage_access_key_id: SecretStr | None = None
    storage_secret_access_key: SecretStr | None = None
    storage_session_token: SecretStr | None = None
    storage_presign_ttl_seconds: int = Field(default=10800, ge=60, le=14400)
    storage_verification_lease_seconds: int = Field(default=900, ge=60, le=3600)
    storage_max_image_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,
        le=1024 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def validate_environment_safety(self) -> "Settings":
        errors: list[str] = []
        protected_environment = self.environment in {
            Environment.STAGING,
            Environment.PRODUCTION,
        }
        if protected_environment:
            if not self.database_url.startswith("postgresql+psycopg://"):
                errors.append("staging and production require PostgreSQL via psycopg")
            if not str(self.public_base_url).startswith("https://"):
                errors.append("staging and production require an HTTPS public URL")
            secret = self.session_secret.get_secret_value()
            try:
                TotpSecretCipher({"session": secret}, active_key_id="session")
            except SecretEncryptionError:
                errors.append("staging and production require a random 32-byte session secret")
            if self.auto_create_schema:
                errors.append("automatic schema creation is forbidden outside local/test")
            if not self.auth_enabled:
                errors.append("staging and production require administrative authentication")
            if not self.trusted_proxy_cidrs:
                errors.append("staging and production require at least one trusted proxy CIDR")
            if not self.ingress_rate_limit_configured:
                errors.append(
                    "staging and production require an explicit ingress rate-limit assertion"
                )
            if not self.ingress_request_guards_configured:
                errors.append(
                    "staging and production require an explicit ingress request-guard assertion"
                )
            if not self.storage_enabled:
                errors.append("staging and production require private object storage")
            if self.storage_endpoint_url is not None and not str(
                self.storage_endpoint_url
            ).startswith("https://"):
                errors.append("staging and production require an HTTPS object-storage endpoint")

        if self.auth_development_bypass_enabled:
            if self.environment not in {Environment.LOCAL, Environment.TEST}:
                errors.append("authentication development bypass is forbidden outside local/test")
            if self.auth_enabled:
                errors.append(
                    "authentication development bypass and authentication are mutually exclusive"
                )

        try:
            _parse_trusted_proxy_cidrs(self.trusted_proxy_cidrs)
        except ValueError as error:
            errors.append(str(error))

        if self.auth_enabled:
            session_secret = self.session_secret.get_secret_value()
            try:
                TotpSecretCipher(
                    {"session": session_secret},
                    active_key_id="session",
                )
            except SecretEncryptionError:
                errors.append("enabled authentication requires a random 32-byte session secret")
            totp_keys = {
                key_id: key.get_secret_value()
                for key_id, key in self.auth_totp_encryption_keys.items()
            }
            try:
                TotpSecretCipher(
                    totp_keys,
                    active_key_id=self.auth_totp_active_key_id or "",
                )
            except SecretEncryptionError:
                errors.append("enabled authentication requires a valid TOTP encryption keyring")
            if self.auth_session_idle_seconds > self.auth_session_absolute_seconds:
                errors.append("authentication idle timeout cannot exceed absolute timeout")
            if self.auth_recent_auth_seconds > self.auth_session_idle_seconds:
                errors.append("recent-authentication window cannot exceed idle timeout")
            if self.environment == Environment.PRODUCTION and not self.auth_require_totp:
                errors.append("production authentication requires TOTP")

        if self.storage_enabled and not self.storage_bucket:
            errors.append("enabled object storage requires a bucket")
        storage_access_key_configured = self.storage_access_key_id is not None
        storage_secret_key_configured = self.storage_secret_access_key is not None
        storage_session_token_configured = self.storage_session_token is not None
        if storage_access_key_configured != storage_secret_key_configured:
            errors.append("object-storage access key ID and secret must be provided together")
        if storage_session_token_configured and not (
            storage_access_key_configured and storage_secret_key_configured
        ):
            errors.append("object-storage session token requires an access key ID and secret")
        if self.quality_scoring_enabled and not self.storage_enabled:
            errors.append("automatic quality scoring requires private object storage")
        if self.quality_scoring_enabled and not self.background_runtime_enabled:
            errors.append("automatic quality scoring requires the background runtime")
        if self.background_quality_retry_max_seconds < self.background_quality_retry_base_seconds:
            errors.append("quality retry maximum cannot be lower than its base delay")
        if (
            self.background_quality_timeout_seconds
            < self.background_quality_analysis_timeout_seconds + 5
        ):
            errors.append("quality cycle timeout must cover isolated analysis plus cleanup")
        if self.semantic_anatomy_enabled:
            if not self.storage_enabled:
                errors.append("semantic anatomy QC requires private object storage")
            if not self.background_runtime_enabled:
                errors.append("semantic anatomy QC requires the background runtime")
            if not self.quality_scoring_enabled:
                errors.append("semantic anatomy QC requires automatic quality scoring")
            if self.semantic_anatomy_endpoint_url is None:
                errors.append("semantic anatomy QC requires a private VLM endpoint")
            elif protected_environment and not _is_https_url(
                str(self.semantic_anatomy_endpoint_url)
            ):
                errors.append("staging and production semantic VLM endpoints require HTTPS")
            if self.semantic_anatomy_model_revision is None:
                errors.append("semantic anatomy QC requires a pinned model revision")
        if self.background_semantic_retry_max_seconds < self.background_semantic_retry_base_seconds:
            errors.append("semantic retry maximum cannot be lower than its base delay")
        if (
            self.background_semantic_timeout_seconds
            < self.background_semantic_request_timeout_seconds + 10
        ):
            errors.append("semantic cycle timeout must cover the VLM request plus cleanup")
        if self.derivative_rendering_enabled and not self.storage_enabled:
            errors.append("automatic derivative rendering requires private object storage")
        if self.derivative_rendering_enabled and not self.background_runtime_enabled:
            errors.append("automatic derivative rendering requires the background runtime")
        if (
            self.background_derivative_retry_max_seconds
            < self.background_derivative_retry_base_seconds
        ):
            errors.append("derivative retry maximum cannot be lower than its base delay")
        if (
            self.background_derivative_timeout_seconds
            < self.background_derivative_render_timeout_seconds + 15
        ):
            errors.append("derivative cycle timeout must cover isolated rendering plus cleanup")
        if self.publishing_enabled and not self.storage_enabled:
            errors.append("publication orchestration requires private object storage")
        if self.publishing_enabled and not self.background_runtime_enabled:
            errors.append("publication orchestration requires the background runtime")
        if (
            self.publishing_enabled
            and self.background_publication_max_package_bytes != PATREON_MAX_ARCHIVE_BYTES
        ):
            errors.append(
                "enabled publication package capacity must equal the shared "
                f"{PATREON_MAX_ARCHIVE_BYTES}-byte Patreon archive cap"
            )
        if self.patreon_browser_publishing_enabled:
            if not self.publishing_enabled:
                errors.append("Patreon browser publishing requires publication orchestration")
            if self.patreon_browser_sidecar_url is None:
                errors.append("Patreon browser publishing requires a sidecar URL")
            if self.patreon_browser_profile_reference is None:
                errors.append("Patreon browser publishing requires a browser profile reference")
            shared_secret = _secret_value(self.patreon_browser_shared_secret)
            try:
                shared_secret_bytes = shared_secret.encode("utf-8") if shared_secret else b""
            except UnicodeEncodeError:
                shared_secret_bytes = b""
            if not 32 <= len(shared_secret_bytes) <= 4096:
                errors.append("Patreon browser publishing requires a 32-4096 byte shared secret")
            if self.patreon_browser_timeout_seconds >= self.background_publication_timeout_seconds:
                errors.append(
                    "Patreon browser timeout must be lower than the publication cycle timeout"
                )
        x_oauth_reference_configured = self.x_oauth_secret_reference is not None
        x_creator_binding_configured = self.x_creator_user_id is not None
        if x_oauth_reference_configured != x_creator_binding_configured:
            errors.append(
                "X OAuth secret reference and creator user ID must be configured together"
            )
        if x_oauth_reference_configured and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            errors.append("X OAuth credential serialization requires PostgreSQL via psycopg")
        if (
            self.background_publication_retry_max_seconds
            < self.background_publication_retry_base_seconds
        ):
            errors.append("publication retry maximum cannot be lower than its base delay")
        if self.mega_delivery_enabled:
            if not self.publishing_enabled:
                errors.append("MEGA completed-set delivery requires publication orchestration")
            if not self.storage_enabled:
                errors.append("MEGA completed-set delivery requires private object storage")
            if not self.background_runtime_enabled:
                errors.append("MEGA completed-set delivery requires the background runtime")
            if self.mega_profile_home is None:
                errors.append(
                    "MEGA completed-set delivery requires a pre-authenticated profile HOME"
                )
            else:
                profile_home = self.mega_profile_home
                if not profile_home.is_absolute() or profile_home == Path(profile_home.anchor):
                    errors.append("MEGA profile HOME must be an absolute non-root directory")
            if not _valid_mega_remote_root(self.mega_remote_root):
                errors.append("MEGA remote root must be a normalized absolute remote path")
            if self.background_mega_max_package_bytes < PATREON_MAX_ARCHIVE_BYTES:
                errors.append(
                    "MEGA package capacity must cover the "
                    f"{PATREON_MAX_ARCHIVE_BYTES}-byte Patreon archive contract"
                )
        if self.background_mega_retry_max_seconds < self.background_mega_retry_base_seconds:
            errors.append("MEGA retry maximum cannot be lower than its base delay")
        if (
            self.mega_delivery_enabled
            and self.background_mega_timeout_seconds
            < (2 * self.background_mega_command_timeout_seconds) + 60
        ):
            errors.append(
                "MEGA cycle timeout must cover one upload, one verification download, and cleanup"
            )
        if self.gpu_allocation_enabled and not self.salad_enabled:
            errors.append("GPU allocation requires the SaladCloud integration")
        if self.gpu_allocation_enabled and not self.storage_enabled:
            errors.append("GPU allocation requires private object storage")
        if self.gpu_allocation_enabled:
            if not self.salad_gpu_class_ids:
                errors.append("GPU allocation requires at least one Salad GPU class ID")
            if len(set(self.salad_gpu_class_ids)) != len(self.salad_gpu_class_ids):
                errors.append("Salad GPU class IDs must be unique")
            if (
                self.worker_signing_key_id is None
                or WORKER_SIGNING_KEY_ID_PATTERN.fullmatch(self.worker_signing_key_id) is None
            ):
                errors.append("GPU allocation requires a valid worker signing key ID")
            worker_private_key = (
                self.worker_signing_private_key.get_secret_value()
                if self.worker_signing_private_key is not None
                else ""
            )
            try:
                validate_private_key(worker_private_key)
            except SigningMaterialError:
                errors.append("GPU allocation requires a valid Ed25519 private signing key")
            if self.worker_upload_grant_ttl_seconds < self.worker_signature_ttl_seconds + 3600:
                errors.append(
                    "worker upload grant TTL must cover signature TTL plus execution time"
                )
            runtime_values = {
                WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING: (self.salad_worker_allowed_upload_origin),
                WORKER_MODEL_MANIFEST_JSON_BINDING: (self.salad_worker_model_manifest_json),
                WORKER_MODEL_MANIFEST_SHA256_BINDING: (self.salad_worker_model_manifest_sha256),
                WORKER_ARTIFACT_BUCKET_BINDING: self.salad_worker_artifact_bucket,
                WORKER_ARTIFACT_REGION_BINDING: self.salad_worker_artifact_region,
                WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: (self.salad_worker_artifact_access_key_id),
                WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING: (
                    self.salad_worker_artifact_secret_access_key
                ),
            }
            missing_runtime_values = [
                name
                for name, value in runtime_values.items()
                if value is None or not value.get_secret_value().strip()
            ]
            if protected_environment and missing_runtime_values:
                errors.append(
                    "staging and production GPU allocation requires complete "
                    "Salad worker runtime bindings: " + ", ".join(sorted(missing_runtime_values))
                )
        self._validate_worker_runtime_values(errors)
        if self.salad_enabled:
            if protected_environment and not self.background_runtime_enabled:
                errors.append(
                    "staging and production SaladCloud integration requires the background runtime"
                )
            required_salad_settings = {
                "API key": self.salad_api_key,
                "organization": self.salad_organization,
                "project": self.salad_project,
                "queue name": self.salad_queue_name,
                "container group name": self.salad_container_group_name,
                "webhook secret": self.salad_webhook_secret,
                "worker image": self.salad_worker_image,
            }
            missing = [
                label
                for label, value in required_salad_settings.items()
                if value is None
                or (isinstance(value, str) and not value.strip())
                or (isinstance(value, SecretStr) and not value.get_secret_value().strip())
            ]
            if missing:
                errors.append("enabled SaladCloud integration requires " + ", ".join(missing))
            if not str(self.salad_api_base_url).startswith("https://"):
                errors.append("SaladCloud requires an HTTPS API base URL")
            if protected_environment and (
                str(self.salad_api_base_url).rstrip("/") != SALAD_API_BASE_URL
            ):
                errors.append("staging and production require the official SaladCloud API base URL")
            if self.environment != Environment.TEST and not str(self.public_base_url).startswith(
                "https://"
            ):
                errors.append("enabled SaladCloud integration requires an HTTPS public URL")
            provider_names = {
                "organization": self.salad_organization,
                "project": self.salad_project,
                "queue name": self.salad_queue_name,
                "container group name": self.salad_container_group_name,
            }
            invalid_names = [
                label
                for label, value in provider_names.items()
                if value is not None and SALAD_NAME_PATTERN.fullmatch(value) is None
            ]
            if invalid_names:
                errors.append("invalid SaladCloud resource name: " + ", ".join(invalid_names))
            if self.salad_worker_image and (
                SALAD_WORKER_IMAGE_PATTERN.fullmatch(self.salad_worker_image) is None
            ):
                errors.append("SaladCloud worker image must be pinned by digest")
            if self.salad_monthly_budget_usd < self.salad_daily_budget_usd:
                errors.append("SaladCloud monthly budget cannot be lower than the daily budget")
            if self.salad_max_hourly_cost_usd > self.salad_daily_budget_usd:
                errors.append("SaladCloud maximum hourly cost cannot exceed the daily budget")
            minimum_provider_timeout = (
                SALAD_DEPLOYMENT_REQUESTS_PER_CYCLE * self.salad_request_timeout_seconds
            ) + SALAD_OPERATION_TIMEOUT_MARGIN_SECONDS
            if self.background_deployment_timeout_seconds < minimum_provider_timeout:
                errors.append("deployment timeout must cover three bounded SaladCloud requests")
            minimum_reconciliation_timeout = (
                SALAD_RECONCILIATION_REQUESTS_PER_CYCLE * self.salad_request_timeout_seconds
            ) + SALAD_OPERATION_TIMEOUT_MARGIN_SECONDS
            if self.background_reconcile_timeout_seconds < minimum_reconciliation_timeout:
                errors.append("reconciliation timeout must cover two bounded SaladCloud requests")
            minimum_submission_timeout = (
                self.salad_request_timeout_seconds + SALAD_OPERATION_TIMEOUT_MARGIN_SECONDS
            )
            if self.background_submit_timeout_seconds < minimum_submission_timeout:
                errors.append(
                    "submission timeout must cover a bounded SaladCloud request and local work"
                )

        if self.background_readiness_failure_threshold > self.background_liveness_failure_threshold:
            errors.append(
                "background liveness failure threshold cannot be lower than readiness threshold"
            )
        if self.background_runtime_enabled:
            cycle_timeouts: list[float] = []
            if self.salad_enabled:
                cycle_timeouts.extend(
                    (
                        30.0,
                        self.background_deployment_timeout_seconds + 15,
                        self.background_inbox_timeout_seconds + 5,
                        self.background_reconcile_timeout_seconds + 5,
                    )
                )
                if self.gpu_allocation_enabled:
                    cycle_timeouts.extend(
                        (
                            30.0,
                            self.background_submit_timeout_seconds + 15,
                        )
                    )
            if self.storage_enabled:
                cycle_timeouts.append(self.background_collection_timeout_seconds + 15)
            if self.storage_enabled and self.quality_scoring_enabled:
                cycle_timeouts.append(self.background_quality_timeout_seconds + 5)
            if self.storage_enabled and self.semantic_anatomy_enabled:
                cycle_timeouts.append(self.background_semantic_timeout_seconds + 5)
            if self.storage_enabled and self.derivative_rendering_enabled:
                cycle_timeouts.append(self.background_derivative_timeout_seconds + 5)
            if self.storage_enabled and self.publishing_enabled:
                cycle_timeouts.append(self.background_publication_timeout_seconds + 5)
            if self.storage_enabled and self.mega_delivery_enabled:
                cycle_timeouts.append(self.background_mega_timeout_seconds + 5)
            maximum_idle_interval = self.background_poll_interval_seconds
            if self.salad_enabled:
                maximum_idle_interval = max(maximum_idle_interval, 30)
            maximum_delay = BACKGROUND_MAX_DELAY_JITTER_MULTIPLIER * max(
                maximum_idle_interval,
                self.background_error_backoff_max_seconds,
            )
            minimum_stale_after = max(cycle_timeouts) + maximum_delay if cycle_timeouts else None
            if (
                minimum_stale_after is not None
                and self.background_loop_stale_after_seconds <= minimum_stale_after
            ):
                errors.append(
                    "background loop staleness threshold must exceed the longest cycle timeout "
                    "plus maximum jittered delay"
                )

        if self.background_outbox_lease_seconds <= self.background_submit_timeout_seconds:
            errors.append("outbox lease must exceed the submission timeout")
        if self.background_inbox_lease_seconds <= self.background_inbox_timeout_seconds:
            errors.append("inbox lease must exceed the inbox processing timeout")
        if self.background_collection_lease_seconds <= self.background_collection_timeout_seconds:
            errors.append("collection lease must exceed the collection timeout")
        if (
            self.quality_scoring_enabled
            and self.background_quality_lease_seconds <= self.background_quality_timeout_seconds
        ):
            errors.append("quality lease must exceed the quality cycle timeout")
        if (
            self.derivative_rendering_enabled
            and self.background_derivative_lease_seconds
            <= self.background_derivative_timeout_seconds
        ):
            errors.append("derivative lease must exceed the derivative cycle timeout")
        if (
            self.publishing_enabled
            and self.background_publication_lease_seconds
            <= self.background_publication_timeout_seconds
        ):
            errors.append("publication lease must exceed the publication cycle timeout")
        if (
            self.mega_delivery_enabled
            and self.background_mega_lease_seconds <= self.background_mega_timeout_seconds
        ):
            errors.append("MEGA delivery lease must exceed the MEGA cycle timeout")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _validate_worker_runtime_values(self, errors: list[str]) -> None:
        allowed_origin = _secret_value(self.salad_worker_allowed_upload_origin)
        if allowed_origin is not None and not _is_https_origin(allowed_origin):
            errors.append("Salad worker upload origin must be an exact HTTPS origin")

        manifest_json = _secret_value(self.salad_worker_model_manifest_json)
        if manifest_json is not None and len(manifest_json.encode("utf-8")) > 256 * 1024:
            errors.append("Salad worker model manifest exceeds the runtime size limit")

        manifest_sha256 = _secret_value(self.salad_worker_model_manifest_sha256)
        if manifest_sha256 is not None and SHA256_PATTERN.fullmatch(manifest_sha256) is None:
            errors.append("Salad worker model manifest digest must be lowercase SHA-256")

        artifact_bucket = _secret_value(self.salad_worker_artifact_bucket)
        if (
            artifact_bucket is not None
            and WORKER_ARTIFACT_BUCKET_PATTERN.fullmatch(artifact_bucket) is None
        ):
            errors.append("Salad worker artifact bucket is invalid")

        artifact_region = _secret_value(self.salad_worker_artifact_region)
        if artifact_region is not None and (
            not artifact_region.strip() or len(artifact_region) > 128
        ):
            errors.append("Salad worker artifact region is invalid")

        artifact_endpoint = _secret_value(self.salad_worker_artifact_endpoint_url)
        if artifact_endpoint is not None and not _is_https_url(artifact_endpoint):
            errors.append("Salad worker artifact endpoint must use HTTPS")

        artifact_access_key = _secret_value(self.salad_worker_artifact_access_key_id)
        artifact_secret_key = _secret_value(self.salad_worker_artifact_secret_access_key)
        if (artifact_access_key is None) != (artifact_secret_key is None):
            errors.append("Salad worker artifact access key and secret must be provided together")
        if self.salad_worker_artifact_session_token is not None and artifact_access_key is None:
            errors.append("Salad worker artifact session token requires an access key")

    @property
    def worker_verification_public_key(self) -> str | None:
        if self.worker_signing_private_key is None:
            return None
        try:
            return derive_public_key(self.worker_signing_private_key.get_secret_value())
        except SigningMaterialError:
            return None

    @property
    def trusted_proxy_networks(self) -> tuple[TrustedProxyNetwork, ...]:
        return _parse_trusted_proxy_cidrs(self.trusted_proxy_cidrs)

    @property
    def auth_session_cookie_name(self) -> str:
        if self.environment in {Environment.STAGING, Environment.PRODUCTION}:
            return "__Host-gen_session"
        return "gen_session"

    @property
    def auth_csrf_cookie_name(self) -> str:
        if self.environment in {Environment.STAGING, Environment.PRODUCTION}:
            return "__Host-gen_csrf"
        return "gen_csrf"


@lru_cache
def get_settings() -> Settings:
    return Settings()
