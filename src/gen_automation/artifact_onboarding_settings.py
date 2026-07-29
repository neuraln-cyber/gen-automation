import re

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,254}$")


class ArtifactOnboardingSettings(BaseSettings):
    """Minimum environment/secret scope for the one-off onboarding job."""

    model_config = SettingsConfigDict(
        env_prefix="GEN_AUTOMATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    database_url: str = Field(min_length=1)

    storage_bucket: str = Field(min_length=2, max_length=255)
    storage_region: str = Field(default="us-east-1", min_length=1, max_length=128)
    storage_endpoint_url: AnyHttpUrl | None = None
    storage_access_key_id: SecretStr | None = None
    storage_secret_access_key: SecretStr | None = None
    storage_session_token: SecretStr | None = None

    salad_worker_artifact_bucket: str = Field(min_length=2, max_length=255)
    salad_worker_artifact_region: str = Field(
        default="us-east-1",
        min_length=1,
        max_length=128,
    )
    salad_worker_artifact_endpoint_url: AnyHttpUrl | None = None
    salad_worker_artifact_access_key_id: SecretStr | None = None
    salad_worker_artifact_secret_access_key: SecretStr | None = None
    salad_worker_artifact_session_token: SecretStr | None = None

    @model_validator(mode="after")
    def validate_storage_identities(self) -> "ArtifactOnboardingSettings":
        if _BUCKET_PATTERN.fullmatch(self.storage_bucket) is None:
            raise ValueError("workflow storage bucket is invalid")
        if _BUCKET_PATTERN.fullmatch(self.salad_worker_artifact_bucket) is None:
            raise ValueError("worker artifact bucket is invalid")
        _validate_credentials(
            self.storage_access_key_id,
            self.storage_secret_access_key,
            self.storage_session_token,
            label="workflow storage",
        )
        _validate_credentials(
            self.salad_worker_artifact_access_key_id,
            self.salad_worker_artifact_secret_access_key,
            self.salad_worker_artifact_session_token,
            label="worker artifact storage",
        )
        return self


def _validate_credentials(
    access_key_id: SecretStr | None,
    secret_access_key: SecretStr | None,
    session_token: SecretStr | None,
    *,
    label: str,
) -> None:
    if (access_key_id is None) != (secret_access_key is None):
        raise ValueError(f"{label} access key ID and secret must be supplied together")
    if session_token is not None and access_key_id is None:
        raise ValueError(f"{label} session token requires an access key pair")
    for value in (access_key_id, secret_access_key, session_token):
        if value is None:
            continue
        secret = value.get_secret_value()
        if (
            not secret
            or secret != secret.strip()
            or any(ord(character) < 33 or ord(character) == 127 for character in secret)
        ):
            raise ValueError(f"{label} credentials must be trimmed visible text")
