"""Authentication and session-security primitives."""

from gen_automation.auth.security import (
    PasswordManager,
    PasswordPolicyError,
    PasswordVerification,
    SecretEncryptionError,
    TotpSecretCipher,
    generate_opaque_token,
    generate_totp_secret,
    hash_opaque_token,
    provisioning_uri,
    verify_opaque_token,
    verify_totp,
)

__all__ = [
    "PasswordManager",
    "PasswordPolicyError",
    "PasswordVerification",
    "SecretEncryptionError",
    "TotpSecretCipher",
    "generate_opaque_token",
    "generate_totp_secret",
    "hash_opaque_token",
    "provisioning_uri",
    "verify_opaque_token",
    "verify_totp",
]
