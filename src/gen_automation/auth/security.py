import base64
import binascii
import hashlib
import hmac
import re
import secrets
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MINIMUM_PASSWORD_CHARACTERS = 14
MAXIMUM_PASSWORD_BYTES = 1024
OPAQUE_TOKEN_BYTES = 32
OPAQUE_TOKEN_LENGTH = 43
TOTP_SECRET_BYTES = 20
TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_ALLOWED_DRIFT_STEPS = 1
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_BASE32_PATTERN = re.compile(r"^[A-Z2-7]{32}$")
_BASE64URL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOTP_AAD = b"gen-automation:totp:v1"
_TOTP_CIPHERTEXT_VERSION = "v1"


class PasswordPolicyError(ValueError):
    """A new password does not meet the bounded length policy."""


class SecretEncryptionError(ValueError):
    """MFA secret material is malformed or failed authenticated decryption."""


@dataclass(frozen=True)
class PasswordVerification:
    valid: bool
    replacement_hash: str | None = None


class PasswordManager:
    """Hash and verify passwords with Argon2id and timing-safe dummy work."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(OPAQUE_TOKEN_BYTES))
        self._maximum_parameters = extract_parameters(self._dummy_hash)

    def hash(self, password: str) -> str:
        _validate_new_password(password)
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str | None, password: str) -> PasswordVerification:
        hash_is_bounded = self.is_encoded_hash_acceptable(encoded_hash)
        candidate_hash = (
            encoded_hash if hash_is_bounded and encoded_hash is not None else self._dummy_hash
        )
        candidate_password = (
            password if _valid_verification_password(password) else "invalid-password"
        )
        try:
            valid = self._hasher.verify(candidate_hash, candidate_password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return PasswordVerification(valid=False)
        if encoded_hash is None or not hash_is_bounded or not valid:
            return PasswordVerification(valid=False)
        replacement_hash = (
            self._hasher.hash(password) if self._hasher.check_needs_rehash(encoded_hash) else None
        )
        return PasswordVerification(valid=True, replacement_hash=replacement_hash)

    def is_encoded_hash_acceptable(self, encoded_hash: str | None) -> bool:
        """Reject malformed, weak, or resource-exhausting stored Argon2 hashes."""

        if encoded_hash is None:
            return False
        try:
            parameters = extract_parameters(encoded_hash)
        except InvalidHashError:
            return False
        maximum = self._maximum_parameters
        return (
            parameters.type == Type.ID
            and parameters.version in {16, 19}
            and parameters.time_cost >= 1
            and parameters.time_cost <= maximum.time_cost
            and parameters.memory_cost >= 8192
            and parameters.memory_cost <= maximum.memory_cost
            and parameters.parallelism >= 1
            and parameters.parallelism <= maximum.parallelism
            and parameters.hash_len >= 16
            and parameters.hash_len <= maximum.hash_len
            and parameters.salt_len >= 16
            and parameters.salt_len <= maximum.salt_len
            and _argon2_encoding_is_canonical(
                encoded_hash,
                salt_len=parameters.salt_len,
                hash_len=parameters.hash_len,
            )
        )


def _validate_new_password(password: str) -> None:
    encoded_size = len(password.encode("utf-8"))
    if len(password) < MINIMUM_PASSWORD_CHARACTERS or encoded_size > MAXIMUM_PASSWORD_BYTES:
        raise PasswordPolicyError("password does not meet the length policy")


def _valid_verification_password(password: str) -> bool:
    return bool(password) and len(password.encode("utf-8")) <= MAXIMUM_PASSWORD_BYTES


def _argon2_encoding_is_canonical(
    encoded_hash: str,
    *,
    salt_len: int,
    hash_len: int,
) -> bool:
    parts = encoded_hash.split("$")
    if len(parts) != 6 or parts[0] != "":
        return False
    try:
        salt = base64.b64decode(
            parts[4] + ("=" * (-len(parts[4]) % 4)),
            validate=True,
        )
        digest = base64.b64decode(
            parts[5] + ("=" * (-len(parts[5]) % 4)),
            validate=True,
        )
    except (binascii.Error, ValueError):
        return False
    return (
        len(salt) == salt_len
        and len(digest) == hash_len
        and base64.b64encode(salt).rstrip(b"=").decode("ascii") == parts[4]
        and base64.b64encode(digest).rstrip(b"=").decode("ascii") == parts[5]
    )


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError):
        raise SecretEncryptionError("secret material is invalid") from None
    if _encode_base64url(decoded) != value:
        raise SecretEncryptionError("secret material is invalid")
    return decoded


def generate_opaque_token() -> str:
    return _encode_base64url(secrets.token_bytes(OPAQUE_TOKEN_BYTES))


def hash_opaque_token(token: str) -> str:
    if len(token) != OPAQUE_TOKEN_LENGTH or _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("opaque token is invalid")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def verify_opaque_token(expected_sha256: str, token: str) -> bool:
    try:
        observed_sha256 = hash_opaque_token(token)
    except ValueError:
        observed_sha256 = hashlib.sha256(b"invalid-opaque-token").hexdigest()
    return hmac.compare_digest(expected_sha256, observed_sha256)


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii")


def _decode_totp_secret(secret: str) -> bytes:
    normalized = secret.strip().upper().replace(" ", "")
    if _BASE32_PATTERN.fullmatch(normalized) is None:
        raise ValueError("TOTP secret is invalid")
    try:
        decoded = base64.b32decode(normalized, casefold=False)
    except binascii.Error:
        raise ValueError("TOTP secret is invalid") from None
    if len(decoded) != TOTP_SECRET_BYTES:
        raise ValueError("TOTP secret is invalid")
    return decoded


def _totp_code(secret: bytes, counter: int) -> str:
    if counter < 0:
        raise ValueError("TOTP counter cannot be negative")
    digest = hmac.new(
        secret,
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return f"{binary % (10**TOTP_DIGITS):0{TOTP_DIGITS}d}"


def verify_totp(
    secret: str,
    code: str,
    *,
    unix_time: int,
    last_used_counter: int | None = None,
    allowed_drift_steps: int = TOTP_ALLOWED_DRIFT_STEPS,
) -> int | None:
    """Return the accepted counter, rejecting malformed or replayed codes."""

    if (
        not code.isascii()
        or not code.isdecimal()
        or len(code) != TOTP_DIGITS
        or unix_time < 0
        or not 0 <= allowed_drift_steps <= 2
    ):
        return None
    try:
        decoded_secret = _decode_totp_secret(secret)
    except ValueError:
        return None
    current_counter = unix_time // TOTP_PERIOD_SECONDS
    matched_counter: int | None = None
    for drift in range(-allowed_drift_steps, allowed_drift_steps + 1):
        candidate_counter = current_counter + drift
        if candidate_counter < 0:
            continue
        candidate_code = _totp_code(decoded_secret, candidate_counter)
        if hmac.compare_digest(candidate_code, code):
            matched_counter = candidate_counter
    if matched_counter is None:
        return None
    if last_used_counter is not None and matched_counter <= last_used_counter:
        return None
    return matched_counter


def provisioning_uri(
    secret: str,
    *,
    account_name: str,
    issuer: str = "Gen Automation",
) -> str:
    _decode_totp_secret(secret)
    if not account_name.strip() or len(account_name) > 200:
        raise ValueError("TOTP account name is invalid")
    if not issuer.strip() or len(issuer) > 100:
        raise ValueError("TOTP issuer is invalid")
    label = quote(f"{issuer}:{account_name}", safe="")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": str(TOTP_DIGITS),
            "period": str(TOTP_PERIOD_SECONDS),
        }
    )
    return f"otpauth://totp/{label}?{query}"


class TotpSecretCipher:
    """Versioned AES-GCM envelope for TOTP seeds stored in the database."""

    def __init__(
        self,
        encryption_keys: Mapping[str, str],
        *,
        active_key_id: str,
    ) -> None:
        if (
            not 1 <= len(encryption_keys) <= 8
            or _KEY_ID_PATTERN.fullmatch(active_key_id) is None
            or active_key_id not in encryption_keys
        ):
            raise SecretEncryptionError("TOTP encryption keyring is invalid")
        ciphers: dict[str, AESGCM] = {}
        for key_id, encryption_key in encryption_keys.items():
            if (
                _KEY_ID_PATTERN.fullmatch(key_id) is None
                or len(encryption_key) != OPAQUE_TOKEN_LENGTH
                or _BASE64URL_KEY_PATTERN.fullmatch(encryption_key) is None
            ):
                raise SecretEncryptionError("TOTP encryption keyring is invalid")
            decoded_key = _decode_base64url(encryption_key)
            if len(decoded_key) != 32:
                raise SecretEncryptionError("TOTP encryption keyring is invalid")
            ciphers[key_id] = AESGCM(decoded_key)
        self._active_key_id = active_key_id
        self._ciphers = ciphers

    def encrypt(self, secret: str, *, subject: str) -> str:
        normalized = base64.b32encode(_decode_totp_secret(secret)).decode("ascii")
        nonce = secrets.token_bytes(12)
        aad = self._aad(subject)
        ciphertext = self._ciphers[self._active_key_id].encrypt(
            nonce,
            normalized.encode("ascii"),
            aad,
        )
        return ".".join(
            (
                _TOTP_CIPHERTEXT_VERSION,
                self._active_key_id,
                _encode_base64url(nonce + ciphertext),
            )
        )

    def decrypt(self, envelope: str, *, subject: str) -> str:
        parts = envelope.split(".")
        if len(parts) != 3 or parts[0] != _TOTP_CIPHERTEXT_VERSION:
            raise SecretEncryptionError("TOTP ciphertext is invalid")
        cipher = self._ciphers.get(parts[1])
        if cipher is None:
            raise SecretEncryptionError("TOTP ciphertext is invalid")
        payload = _decode_base64url(parts[2])
        if len(payload) < 12 + 16:
            raise SecretEncryptionError("TOTP ciphertext is invalid")
        nonce, ciphertext = payload[:12], payload[12:]
        try:
            plaintext = cipher.decrypt(nonce, ciphertext, self._aad(subject))
            secret = plaintext.decode("ascii")
            _decode_totp_secret(secret)
        except (InvalidTag, UnicodeDecodeError, ValueError):
            raise SecretEncryptionError("TOTP ciphertext is invalid") from None
        return secret

    @staticmethod
    def _aad(subject: str) -> bytes:
        try:
            encoded_subject = subject.encode("utf-8")
        except UnicodeEncodeError:
            raise SecretEncryptionError("TOTP encryption subject is invalid") from None
        if not 1 <= len(encoded_subject) <= 200 or any(
            character < 0x20 for character in encoded_subject
        ):
            raise SecretEncryptionError("TOTP encryption subject is invalid")
        return _TOTP_AAD + b":" + encoded_subject
