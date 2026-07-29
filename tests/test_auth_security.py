import base64
import hashlib
import hmac
import struct
from urllib.parse import parse_qs, urlsplit

import pytest
from argon2 import PasswordHasher, Type

from gen_automation.auth.security import (
    PasswordManager,
    PasswordPolicyError,
    SecretEncryptionError,
    TotpSecretCipher,
    generate_opaque_token,
    generate_totp_secret,
    hash_opaque_token,
    provisioning_uri,
    verify_opaque_token,
    verify_totp,
)


def _test_hasher(*, time_cost: int = 1) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=8192,
        parallelism=1,
        hash_len=16,
        salt_len=16,
        type=Type.ID,
    )


def _totp_reference(secret: str, counter: int) -> str:
    key = base64.b32decode(secret)
    digest = hmac.new(
        key,
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def test_password_manager_hashes_with_argon2id_and_verifies() -> None:
    manager = PasswordManager(_test_hasher())

    encoded = manager.hash("a long unique passphrase")
    verification = manager.verify(encoded, "a long unique passphrase")

    assert encoded.startswith("$argon2id$")
    assert verification.valid
    assert verification.replacement_hash is None
    assert not manager.verify(encoded, "incorrect password").valid


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_password_manager_rejects_out_of_policy_new_passwords(password: str) -> None:
    with pytest.raises(PasswordPolicyError):
        PasswordManager(_test_hasher()).hash(password)


def test_password_manager_performs_safe_failure_for_missing_or_invalid_hash() -> None:
    manager = PasswordManager(_test_hasher())

    assert not manager.verify(None, "attempted password").valid
    assert not manager.verify("not-an-argon-hash", "attempted password").valid
    assert not manager.verify(None, "x" * 1025).valid


def test_password_manager_never_verifies_attacker_controlled_excessive_cost_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PasswordManager(_test_hasher())
    valid_hash = manager.hash("a long unique passphrase")
    excessive_hash = valid_hash.replace("m=8192", "m=1048576", 1)
    verified_hashes: list[str] = []
    original_verify = PasswordHasher.verify

    def record_verify(
        hasher: PasswordHasher,
        encoded_hash: str,
        password: str,
    ) -> bool:
        verified_hashes.append(encoded_hash)
        return original_verify(hasher, encoded_hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", record_verify)

    assert not manager.verify(excessive_hash, "a long unique passphrase").valid
    assert verified_hashes
    assert excessive_hash not in verified_hashes


def test_password_manager_returns_rehash_for_old_parameters() -> None:
    old_hash = PasswordManager(_test_hasher(time_cost=1)).hash("a long unique passphrase")
    manager = PasswordManager(_test_hasher(time_cost=2))

    verification = manager.verify(old_hash, "a long unique passphrase")

    assert verification.valid
    assert verification.replacement_hash is not None
    assert manager.verify(
        verification.replacement_hash,
        "a long unique passphrase",
    ).valid


def test_opaque_tokens_are_random_strict_and_hash_verified() -> None:
    first = generate_opaque_token()
    second = generate_opaque_token()
    digest = hash_opaque_token(first)

    assert first != second
    assert len(first) == 43
    assert len(digest) == 64
    assert verify_opaque_token(digest, first)
    assert not verify_opaque_token(digest, second)
    assert not verify_opaque_token(digest, "malformed")
    with pytest.raises(ValueError):
        hash_opaque_token("malformed")


def test_totp_accepts_bounded_drift_and_rejects_replay() -> None:
    secret = generate_totp_secret()
    unix_time = 1_800_000_000
    counter = unix_time // 30
    prior_code = _totp_reference(secret, counter - 1)
    current_code = _totp_reference(secret, counter)

    assert verify_totp(secret, prior_code, unix_time=unix_time) == counter - 1
    assert verify_totp(secret, current_code, unix_time=unix_time) == counter
    assert (
        verify_totp(
            secret,
            current_code,
            unix_time=unix_time,
            last_used_counter=counter,
        )
        is None
    )
    assert verify_totp(secret, "00000x", unix_time=unix_time) is None


def test_totp_provisioning_uri_is_encoded_and_explicit() -> None:
    secret = generate_totp_secret()

    uri = provisioning_uri(
        secret,
        account_name="owner+admin@example.test",
        issuer="Gen Automation",
    )
    parsed = urlsplit(uri)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "otpauth"
    assert parsed.netloc == "totp"
    assert parsed.path == "/Gen%20Automation%3Aowner%2Badmin%40example.test"
    assert query == {
        "secret": [secret],
        "issuer": ["Gen Automation"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


def test_totp_secret_cipher_round_trips_and_detects_tampering() -> None:
    key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    cipher = TotpSecretCipher({"totp-key-1": key}, active_key_id="totp-key-1")
    secret = generate_totp_secret()

    envelope = cipher.encrypt(secret, subject="user:019fa795")

    assert envelope.startswith("v1.totp-key-1.")
    assert cipher.decrypt(envelope, subject="user:019fa795") == secret
    replacement = "A" if envelope[-1] != "A" else "B"
    with pytest.raises(SecretEncryptionError):
        cipher.decrypt(
            envelope[:-1] + replacement,
            subject="user:019fa795",
        )
    with pytest.raises(SecretEncryptionError):
        cipher.decrypt(envelope, subject="user:different")


@pytest.mark.parametrize("key", ["short", "!" * 43])
def test_totp_secret_cipher_rejects_invalid_keys(key: str) -> None:
    with pytest.raises(SecretEncryptionError):
        TotpSecretCipher({"totp-key-1": key}, active_key_id="totp-key-1")


def test_totp_secret_cipher_rejects_dot_delimited_key_id() -> None:
    key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")

    with pytest.raises(SecretEncryptionError):
        TotpSecretCipher({"invalid.key": key}, active_key_id="invalid.key")


def test_totp_secret_cipher_rejects_noncanonical_base64url_key() -> None:
    canonical = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    replacement = {"8": "9", "9": "8", "-": "_", "_": "-"}[canonical[-1]]
    noncanonical = canonical[:-1] + replacement

    with pytest.raises(SecretEncryptionError):
        TotpSecretCipher({"key-1": noncanonical}, active_key_id="key-1")


def test_totp_secret_cipher_decrypts_retained_rotation_key() -> None:
    old_key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    new_key = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
    old_cipher = TotpSecretCipher({"old": old_key}, active_key_id="old")
    secret = generate_totp_secret()
    envelope = old_cipher.encrypt(secret, subject="user:019fa795")
    rotated_cipher = TotpSecretCipher(
        {"old": old_key, "new": new_key},
        active_key_id="new",
    )

    assert rotated_cipher.decrypt(envelope, subject="user:019fa795") == secret
    assert rotated_cipher.encrypt(secret, subject="user:019fa795").startswith("v1.new.")
