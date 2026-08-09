"""Narrow OAuth 1.0a request signing for the X API user context.

Only HMAC-SHA1 signatures for the fixed HTTPS X API host are supported.  The
signer deliberately accepts no arbitrary hosts, redirects, request bodies, or
credential-bearing query parameters so the four long-lived credentials cannot
be sent outside the intended provider boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from time import time
from urllib.parse import parse_qsl, quote, urlsplit

_X_API_HOST = "api.x.com"
_MAX_SECRET_BYTES = 16 * 1024
_ALLOWED_REQUESTS = frozenset(
    {
        ("GET", "/2/users/me"),
        ("POST", "/2/media/upload"),
        ("POST", "/2/media/metadata"),
        ("POST", "/2/tweets"),
    }
)


def _secret_text(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\r" in value
        or "\n" in value
        or len(value.encode("utf-8")) > _MAX_SECRET_BYTES
    ):
        raise ValueError(f"X OAuth 1.0a {label} is invalid")
    return value


def _percent_encode(value: str) -> str:
    return quote(value, safe="~-._", encoding="utf-8", errors="strict")


@dataclass(frozen=True, slots=True, repr=False)
class XOAuth1Credentials:
    """The four long-lived values required for OAuth 1.0a user context."""

    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "consumer_key",
            _secret_text(self.consumer_key, "consumer key"),
        )
        object.__setattr__(
            self,
            "consumer_secret",
            _secret_text(self.consumer_secret, "consumer secret"),
        )
        object.__setattr__(
            self,
            "access_token",
            _secret_text(self.access_token, "access token"),
        )
        object.__setattr__(
            self,
            "access_token_secret",
            _secret_text(self.access_token_secret, "access token secret"),
        )

    def __repr__(self) -> str:
        return "XOAuth1Credentials(<redacted>)"


class XOAuth1Authorization:
    """Create one fresh RFC 5849 HMAC-SHA1 Authorization header per request."""

    def __init__(
        self,
        credentials: XOAuth1Credentials,
        *,
        nonce_factory: Callable[[], str] | None = None,
        timestamp_factory: Callable[[], int] | None = None,
    ) -> None:
        self.__credentials: XOAuth1Credentials | None = credentials
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        self._timestamp_factory = timestamp_factory or (lambda: int(time()))

    def __repr__(self) -> str:
        return "XOAuth1Authorization(credentials=<redacted>)"

    def authorization_header(self, *, method: str, url: str) -> str:
        credentials = self.__credentials
        if credentials is None:
            raise ValueError("X OAuth 1.0a authorization has been cleared")
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise ValueError("X OAuth 1.0a request method is not allowed")
        base_uri, path, query_parameters = self._request_target(url)
        if query_parameters or (normalized_method, path) not in _ALLOWED_REQUESTS:
            raise ValueError("X OAuth 1.0a request target is not allowed")
        nonce = self._nonce_factory()
        timestamp = self._timestamp_factory()
        if (
            not isinstance(nonce, str)
            or not nonce
            or len(nonce.encode("utf-8")) > 256
            or "\r" in nonce
            or "\n" in nonce
        ):
            raise ValueError("X OAuth 1.0a nonce is invalid")
        if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
            raise ValueError("X OAuth 1.0a timestamp is invalid")

        oauth_parameters = {
            "oauth_consumer_key": credentials.consumer_key,
            "oauth_nonce": nonce,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(timestamp),
            "oauth_token": credentials.access_token,
            "oauth_version": "1.0",
        }
        signature_parameters = [*query_parameters, *oauth_parameters.items()]
        encoded_parameters = sorted(
            (_percent_encode(key), _percent_encode(value)) for key, value in signature_parameters
        )
        normalized_parameters = "&".join(f"{key}={value}" for key, value in encoded_parameters)
        signature_base = "&".join(
            (
                _percent_encode(normalized_method),
                _percent_encode(base_uri),
                _percent_encode(normalized_parameters),
            )
        )
        signing_key = "&".join(
            (
                _percent_encode(credentials.consumer_secret),
                _percent_encode(credentials.access_token_secret),
            )
        )
        signature = base64.b64encode(
            hmac.new(
                signing_key.encode("ascii"),
                signature_base.encode("ascii"),
                partial(hashlib.sha1, usedforsecurity=False),
            ).digest()
        ).decode("ascii")
        header_parameters = {**oauth_parameters, "oauth_signature": signature}
        return "OAuth " + ", ".join(
            f'{_percent_encode(key)}="{_percent_encode(value)}"'
            for key, value in sorted(header_parameters.items())
        )

    def redact(self, value: str) -> str:
        redacted = value
        credentials = self.__credentials
        if credentials is None:
            return redacted
        for secret in sorted(
            {
                credentials.consumer_key,
                credentials.consumer_secret,
                credentials.access_token,
                credentials.access_token_secret,
            },
            key=len,
            reverse=True,
        ):
            redacted = redacted.replace(secret, "[redacted]")
            redacted = redacted.replace(_percent_encode(secret), "[redacted]")
        return redacted

    def clear(self) -> None:
        self.__credentials = None

    @staticmethod
    def _request_target(url: str) -> tuple[str, str, list[tuple[str, str]]]:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("X OAuth 1.0a request URL is invalid") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != _X_API_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
        ):
            raise ValueError("X OAuth 1.0a request URL is not approved")
        path = parsed.path or "/"
        base_uri = f"https://{_X_API_HOST}{path}"
        return base_uri, path, parse_qsl(parsed.query, keep_blank_values=True)
