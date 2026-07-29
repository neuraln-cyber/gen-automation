import base64
import html
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, NoReturn

import httpx2

from gen_automation.domain.deliverability import X_STATIC_IMAGE_MAX_BYTES
from gen_automation.integrations.x.errors import (
    XAmbiguousTimeoutError,
    XAmbiguousTransportError,
    XProtocolError,
    XRetryableAPIError,
    XRetryableTransportError,
    XTerminalAPIError,
)
from gen_automation.integrations.x.models import (
    JSONObject,
    XPost,
    XUploadedMedia,
    as_json_object,
    required_bool,
    required_int,
    required_object,
    required_str,
)

X_API_BASE_URL = "https://api.x.com"
X_MAX_STATIC_IMAGE_BYTES = X_STATIC_IMAGE_MAX_BYTES
X_MAX_MEDIA_PER_POST = 4
X_MAX_POST_TEXT_BYTES = 4 * 1024
X_MAX_TIMEOUT_SECONDS = 60.0
X_DEFAULT_TIMEOUT = httpx2.Timeout(
    connect=5.0,
    read=30.0,
    write=30.0,
    pool=5.0,
)

type XStaticImageMediaType = Literal["image/jpeg", "image/png", "image/webp"]

_STATIC_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})
_SNOWFLAKE = re.compile(r"^[0-9]{1,19}$")
_HTML_TAG = re.compile(r"<[^>]*>")
_MAX_ERROR_BODY_LENGTH = 1_000


def _bounded_timeout(timeout: httpx2.Timeout | float) -> httpx2.Timeout:
    if isinstance(timeout, bool):
        raise ValueError("X API timeout must be a positive number")
    normalized = httpx2.Timeout(timeout) if isinstance(timeout, (int, float)) else timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(normalized, name)
        if value is None or not 0 < value <= X_MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"X API {name} timeout must be between 0 and {X_MAX_TIMEOUT_SECONDS:g} seconds"
            )
    return normalized


def _retry_after_seconds(response: httpx2.Response) -> float | None:
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())

    rate_limit_reset = response.headers.get("x-rate-limit-reset")
    if rate_limit_reset is None:
        return None
    try:
        return max(0.0, float(rate_limit_reset) - datetime.now(UTC).timestamp())
    except ValueError:
        return None


def _validated_snowflake(value: str, context: str) -> str:
    if _SNOWFLAKE.fullmatch(value) is None:
        raise ValueError(f"{context} must contain 1 to 19 decimal digits")
    return value


def _reject_embedded_errors(data: JSONObject, context: str) -> None:
    if "errors" not in data:
        return
    errors = data["errors"]
    if not isinstance(errors, list):
        raise ValueError(f"{context}.errors must be an array")
    if errors:
        raise ValueError(f"{context}.errors must be empty on a successful response")


class XClient:
    """Isolated async transport for static-image publishing through X API v2.

    The caller owns ``http_client`` and resolves the OAuth user access token
    immediately before constructing this client. The token is held only in memory,
    is redacted from representations and provider error bodies, and is never logged
    or persisted by this adapter.

    Every HTTP operation is attempted exactly once. In particular, ``create_post``
    never retries because X does not document an idempotency key for ``POST /2/tweets``.
    """

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        bearer_token: str,
        timeout: httpx2.Timeout | float = X_DEFAULT_TIMEOUT,
    ) -> None:
        if not bearer_token or bearer_token.isspace():
            raise ValueError("X OAuth bearer token must not be empty")
        if "\r" in bearer_token or "\n" in bearer_token:
            raise ValueError("X OAuth bearer token must not contain line breaks")
        self._http_client = http_client
        self.__bearer_token = bearer_token
        self._timeout = _bounded_timeout(timeout)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={X_API_BASE_URL!r}, bearer_token=<redacted>)"

    def _redact(self, value: str) -> str:
        return value.replace(self.__bearer_token, "[redacted]")

    def _error_details(self, response: httpx2.Response) -> tuple[str, str]:
        raw_body = self._redact(response.text)
        normalized_body = " ".join(html.unescape(_HTML_TAG.sub(" ", raw_body)).split())[
            :_MAX_ERROR_BODY_LENGTH
        ]
        message_parts: list[str] = []
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("title", "detail", "type", "message", "code"):
                value = payload.get(key)
                if isinstance(value, (str, int, float)):
                    text = self._redact(str(value)).strip()
                    if text and text not in message_parts:
                        message_parts.append(text)
        message = ": ".join(message_parts) or normalized_body or response.reason_phrase
        return message[:_MAX_ERROR_BODY_LENGTH], normalized_body

    def _raise_api_error(self, response: httpx2.Response) -> NoReturn:
        message, response_body = self._error_details(response)
        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("x-transaction-id")
            or response.headers.get("x-response-time")
        )
        error_type = (
            XRetryableAPIError
            if response.status_code in {408, 429} or response.status_code >= 500
            else XTerminalAPIError
        )
        raise error_type(
            status_code=response.status_code,
            message=message,
            response_body=response_body,
            request_id=request_id,
            retry_after_seconds=_retry_after_seconds(response),
        )

    async def _request_json(
        self,
        *,
        path: str,
        expected_status: int,
        json_body: JSONObject | None,
        operation: str,
    ) -> JSONObject:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.__bearer_token}",
        }
        try:
            response = await self._http_client.request(
                "POST",
                f"{X_API_BASE_URL}{path}",
                headers=headers,
                json=json_body,
                follow_redirects=False,
                timeout=self._timeout,
            )
        except (httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ConnectError) as error:
            raise XRetryableTransportError(
                f"X API {operation} failed before request bytes were sent"
            ) from error
        except httpx2.TimeoutException as error:
            raise XAmbiguousTimeoutError(
                f"X API {operation} timed out after request bytes may have been sent; "
                "the outcome is unknown"
            ) from error
        except httpx2.RequestError as error:
            raise XAmbiguousTransportError(
                f"X API {operation} failed after request bytes may have been sent; "
                "the outcome is unknown"
            ) from error

        if response.status_code != expected_status:
            if 200 <= response.status_code < 300:
                raise XProtocolError(
                    f"X API {operation} returned HTTP {response.status_code}; "
                    f"expected HTTP {expected_status}"
                )
            self._raise_api_error(response)

        try:
            payload: object = response.json()
            data = as_json_object(payload, f"X API {operation} response")
            _reject_embedded_errors(data, f"X API {operation} response")
            return data
        except ValueError as error:
            raise XProtocolError(f"X API {operation} returned an invalid JSON response") from error

    async def upload_image(
        self,
        *,
        image: bytes,
        media_type: XStaticImageMediaType,
    ) -> XUploadedMedia:
        """Upload one static image and require confirmation of its adult warning."""
        if not isinstance(image, bytes):
            raise TypeError("X image must be supplied as bytes")
        if not image:
            raise ValueError("X image must not be empty")
        if len(image) > X_MAX_STATIC_IMAGE_BYTES:
            raise ValueError(f"X static images must not exceed {X_MAX_STATIC_IMAGE_BYTES} bytes")
        if media_type not in _STATIC_IMAGE_MEDIA_TYPES:
            raise ValueError("X static image media type must be JPEG, PNG, or WEBP")

        upload_response = await self._request_json(
            path="/2/media/upload",
            expected_status=200,
            json_body={
                "media": base64.b64encode(image).decode("ascii"),
                "media_category": "tweet_image",
                "media_type": media_type,
                "shared": False,
            },
            operation="image upload",
        )
        try:
            data = required_object(upload_response, "data", "image upload response")
            media_id = _validated_snowflake(
                required_str(data, "id", "image upload response.data"),
                "image upload response.data.id",
            )
            media_key = required_str(data, "media_key", "image upload response.data")
            expires_after_seconds = required_int(
                data, "expires_after_secs", "image upload response.data"
            )
            size = required_int(data, "size", "image upload response.data")
            if expires_after_seconds <= 0:
                raise ValueError("image upload response.data.expires_after_secs must be positive")
            if size != len(image):
                raise ValueError("image upload response.data.size must match uploaded bytes")
        except ValueError as error:
            raise XProtocolError(str(error)) from error

        metadata_response = await self._request_json(
            path="/2/media/metadata",
            expected_status=200,
            json_body={
                "id": media_id,
                "metadata": {
                    "sensitive_media_warning": {
                        "adult_content": True,
                    }
                },
            },
            operation="adult-content metadata attachment",
        )
        self._validate_adult_metadata(metadata_response, media_id)
        return XUploadedMedia(
            id=media_id,
            media_key=media_key,
            expires_after_seconds=expires_after_seconds,
            size=size,
        )

    @staticmethod
    def _validate_adult_metadata(response: JSONObject, expected_media_id: str) -> None:
        try:
            data = required_object(response, "data", "media metadata response")
            media_id = _validated_snowflake(
                required_str(data, "id", "media metadata response.data"),
                "media metadata response.data.id",
            )
            if media_id != expected_media_id:
                raise ValueError("media metadata response.data.id does not match uploaded media")
            associated = required_object(
                data,
                "associated_metadata",
                "media metadata response.data",
            )
            sensitive = required_object(
                associated,
                "sensitive_media_warning",
                "media metadata response.data.associated_metadata",
            )
            if not required_bool(
                sensitive,
                "adult_content",
                "media metadata response.data.associated_metadata.sensitive_media_warning",
            ):
                raise ValueError("X did not confirm the adult-content media warning")
        except ValueError as error:
            raise XProtocolError(str(error)) from error

    async def create_post(self, *, text: str, media_ids: Sequence[str]) -> XPost:
        """Create one AI-labelled image post, without any automatic retry."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("X post text must not be empty")
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > X_MAX_POST_TEXT_BYTES:
            raise ValueError(f"X post text must not exceed {X_MAX_POST_TEXT_BYTES} UTF-8 bytes")
        if isinstance(media_ids, (str, bytes)):
            raise TypeError("X media IDs must be a sequence of strings")
        normalized_media_ids = tuple(media_ids)
        if not 1 <= len(normalized_media_ids) <= X_MAX_MEDIA_PER_POST:
            raise ValueError(f"X posts require between 1 and {X_MAX_MEDIA_PER_POST} media IDs")
        if len(set(normalized_media_ids)) != len(normalized_media_ids):
            raise ValueError("X post media IDs must be unique")
        try:
            for index, media_id in enumerate(normalized_media_ids):
                _validated_snowflake(media_id, f"X media_ids[{index}]")
        except (TypeError, ValueError) as error:
            raise ValueError("X media IDs must contain 1 to 19 decimal digits") from error

        response = await self._request_json(
            path="/2/tweets",
            expected_status=201,
            json_body={
                "text": text,
                "made_with_ai": True,
                "media": {"media_ids": list(normalized_media_ids)},
            },
            operation="post creation",
        )
        try:
            data = required_object(response, "data", "post creation response")
            post_id = _validated_snowflake(
                required_str(data, "id", "post creation response.data"),
                "post creation response.data.id",
            )
            returned_text = required_str(data, "text", "post creation response.data")
        except ValueError as error:
            raise XProtocolError(str(error)) from error
        return XPost(id=post_id, text=returned_text)
