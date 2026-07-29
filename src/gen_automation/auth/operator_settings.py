from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.auth.security import SecretEncryptionError, TotpSecretCipher


class AuthenticationOperatorSettings(BaseSettings):
    """Minimum secret scope for one-off authentication operator jobs."""

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
    auth_totp_active_key_id: str = Field(min_length=1)
    auth_totp_encryption_keys: dict[str, SecretStr]

    @model_validator(mode="after")
    def validate_keyring(self) -> "AuthenticationOperatorSettings":
        keys = {
            key_id: secret.get_secret_value()
            for key_id, secret in self.auth_totp_encryption_keys.items()
        }
        try:
            TotpSecretCipher(
                keys,
                active_key_id=self.auth_totp_active_key_id,
            )
        except SecretEncryptionError:
            raise ValueError("authentication operator keyring is invalid") from None
        return self
