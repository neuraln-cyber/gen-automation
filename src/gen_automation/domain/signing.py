import base64
import binascii
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ED25519_PRIVATE_KEY_BYTES: Final = 32
ED25519_PUBLIC_KEY_BYTES: Final = 32
ED25519_SIGNATURE_BYTES: Final = 64

_DUMMY_PUBLIC_KEY = bytes.fromhex(
    "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"
)


class SigningMaterialError(ValueError):
    """Raised for malformed Ed25519 key or signature material."""


def encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str, *, expected_bytes: int) -> bytes:
    try:
        if not value or "=" in value or any(character.isspace() for character in value):
            raise SigningMaterialError("invalid Ed25519 material")
        raw_ascii = value.encode("ascii")
        padding = b"=" * (-len(raw_ascii) % 4)
        decoded = base64.b64decode(
            raw_ascii + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise SigningMaterialError("invalid Ed25519 material") from None
    if len(decoded) != expected_bytes or encode_base64url(decoded) != value:
        raise SigningMaterialError("invalid Ed25519 material")
    return decoded


def validate_private_key(value: str) -> None:
    raw = _decode_base64url(value, expected_bytes=ED25519_PRIVATE_KEY_BYTES)
    try:
        Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError:
        raise SigningMaterialError("invalid Ed25519 private key") from None


def validate_public_key(value: str) -> None:
    raw = _decode_base64url(value, expected_bytes=ED25519_PUBLIC_KEY_BYTES)
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise SigningMaterialError("invalid Ed25519 public key") from None


def derive_public_key(private_key: str) -> str:
    raw_private = _decode_base64url(
        private_key,
        expected_bytes=ED25519_PRIVATE_KEY_BYTES,
    )
    try:
        public = Ed25519PrivateKey.from_private_bytes(raw_private).public_key()
    except ValueError:
        raise SigningMaterialError("invalid Ed25519 private key") from None
    raw_public = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return encode_base64url(raw_public)


def sign_message(private_key: str, message: bytes) -> str:
    raw_private = _decode_base64url(
        private_key,
        expected_bytes=ED25519_PRIVATE_KEY_BYTES,
    )
    try:
        signature = Ed25519PrivateKey.from_private_bytes(raw_private).sign(message)
    except ValueError:
        raise SigningMaterialError("invalid Ed25519 private key") from None
    return encode_base64url(signature)


def verify_message(
    public_key: str | None,
    signature: str,
    message: bytes,
) -> bool:
    try:
        raw_public = (
            _decode_base64url(
                public_key,
                expected_bytes=ED25519_PUBLIC_KEY_BYTES,
            )
            if public_key is not None
            else _DUMMY_PUBLIC_KEY
        )
        raw_signature = _decode_base64url(
            signature,
            expected_bytes=ED25519_SIGNATURE_BYTES,
        )
        Ed25519PublicKey.from_public_bytes(raw_public).verify(
            raw_signature,
            message,
        )
    except (InvalidSignature, SigningMaterialError, ValueError):
        return False
    return public_key is not None
