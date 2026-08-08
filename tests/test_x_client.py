import base64
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import cast

import httpx2
import pytest

from gen_automation.integrations.x import (
    X_API_BASE_URL,
    X_MAX_MEDIA_PER_POST,
    X_MAX_POST_TEXT_BYTES,
    X_MAX_STATIC_IMAGE_BYTES,
    XAmbiguousTimeoutError,
    XAmbiguousTransportError,
    XClient,
    XProtocolError,
    XRetryableAPIError,
    XRetryableTransportError,
    XStaticImageMediaType,
    XTerminalAPIError,
)

BEARER_TOKEN = "oauth-user-access-token-secret"  # noqa: S105
MEDIA_ID = "1880028106020515840"
SECOND_MEDIA_ID = "1880028106020515841"
THIRD_MEDIA_ID = "1880028106020515842"
FOURTH_MEDIA_ID = "1880028106020515843"
POST_ID = "1880028106020515999"
IMAGE = b"\x89PNG\r\n\x1a\nmock-image"


def upload_payload(*, size: int = len(IMAGE)) -> dict[str, object]:
    return {
        "data": {
            "id": MEDIA_ID,
            "media_key": f"3_{MEDIA_ID}",
            "expires_after_secs": 86_400,
            "size": size,
        }
    }


def metadata_payload(*, adult_content: bool = True) -> dict[str, object]:
    return {
        "data": {
            "id": MEDIA_ID,
            "associated_metadata": {
                "sensitive_media_warning": {
                    "adult_content": adult_content,
                    "graphic_violence": False,
                    "other": False,
                }
            },
        }
    }


@asynccontextmanager
async def mocked_x_client(
    handler: Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]],
    *,
    request_timeout: httpx2.Timeout | float = 30.0,
) -> AsyncIterator[XClient]:
    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as http_client:
        yield XClient(
            http_client=http_client,
            bearer_token=BEARER_TOKEN,
            timeout=request_timeout,
        )


@pytest.mark.asyncio
async def test_upload_image_uses_official_v2_contract_and_attaches_adult_warning() -> None:
    call_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal call_count
        call_count += 1
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Content-Type"] == "application/json"
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }
        body: object = json.loads(request.content)
        if call_count == 1:
            assert request.method == "POST"
            assert str(request.url) == f"{X_API_BASE_URL}/2/media/upload"
            assert body == {
                "media": base64.b64encode(IMAGE).decode("ascii"),
                "media_category": "tweet_image",
                "media_type": "image/png",
                "shared": False,
            }
            return httpx2.Response(200, json=upload_payload())

        assert call_count == 2
        assert request.method == "POST"
        assert str(request.url) == f"{X_API_BASE_URL}/2/media/metadata"
        assert body == {
            "id": MEDIA_ID,
            "metadata": {
                "sensitive_media_warning": {
                    "adult_content": True,
                }
            },
        }
        return httpx2.Response(200, json=metadata_payload())

    async with mocked_x_client(handler) as client:
        uploaded = await client.upload_image(image=IMAGE, media_type="image/png")

    assert call_count == 2
    assert uploaded.id == MEDIA_ID
    assert uploaded.media_key == f"3_{MEDIA_ID}"
    assert uploaded.expires_after_seconds == 86_400
    assert uploaded.size == len(IMAGE)
    assert BEARER_TOKEN not in repr(client)
    assert "<redacted>" in repr(client)


@pytest.mark.asyncio
async def test_upload_image_skips_metadata_when_adult_warning_is_disabled() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        assert request.method == "POST"
        assert str(request.url) == f"{X_API_BASE_URL}/2/media/upload"
        return httpx2.Response(200, json=upload_payload())

    async with mocked_x_client(handler) as client:
        uploaded = await client.upload_image(
            image=IMAGE,
            media_type="image/png",
            adult_content=False,
        )

    assert request_count == 1
    assert uploaded.id == MEDIA_ID


@pytest.mark.asyncio
async def test_create_post_uses_ai_label_and_at_most_four_media_ids() -> None:
    media_ids = (MEDIA_ID, SECOND_MEDIA_ID, THIRD_MEDIA_ID, FOURTH_MEDIA_ID)
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        assert request.method == "POST"
        assert str(request.url) == f"{X_API_BASE_URL}/2/tweets"
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
        body: object = json.loads(request.content)
        assert body == {
            "text": "New preview",
            "made_with_ai": True,
            "media": {"media_ids": list(media_ids)},
        }
        return httpx2.Response(
            201,
            json={"data": {"id": POST_ID, "text": "New preview"}},
        )

    async with mocked_x_client(handler) as client:
        post = await client.create_post(text="New preview", media_ids=media_ids)

    assert request_count == 1
    assert post.id == POST_ID
    assert post.text == "New preview"
    assert len(media_ids) == X_MAX_MEDIA_PER_POST == 4


@pytest.mark.asyncio
async def test_image_and_post_limits_fail_before_network_io() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        return httpx2.Response(500, request=request)

    async with mocked_x_client(handler) as client:
        with pytest.raises(ValueError, match="must not be empty"):
            await client.upload_image(image=b"", media_type="image/png")
        with pytest.raises(ValueError, match=str(X_MAX_STATIC_IMAGE_BYTES)):
            await client.upload_image(
                image=b"x" * (X_MAX_STATIC_IMAGE_BYTES + 1),
                media_type="image/png",
            )
        with pytest.raises(ValueError, match="JPEG, PNG, or WEBP"):
            await client.upload_image(
                image=IMAGE,
                media_type=cast(XStaticImageMediaType, "image/gif"),
            )
        with pytest.raises(TypeError, match="adult_content must be a boolean"):
            await client.upload_image(
                image=IMAGE,
                media_type="image/png",
                adult_content=cast(bool, 1),
            )
        with pytest.raises(ValueError, match="between 1 and 4"):
            await client.create_post(
                text="Too many",
                media_ids=[
                    MEDIA_ID,
                    SECOND_MEDIA_ID,
                    THIRD_MEDIA_ID,
                    FOURTH_MEDIA_ID,
                    "1880028106020515844",
                ],
            )
        with pytest.raises(ValueError, match="must be unique"):
            await client.create_post(
                text="Duplicates",
                media_ids=[MEDIA_ID, MEDIA_ID],
            )
        with pytest.raises(ValueError, match="decimal digits"):
            await client.create_post(text="Invalid ID", media_ids=["not-an-id"])
        with pytest.raises(ValueError, match="UTF-8 bytes"):
            await client.create_post(
                text="€" * ((X_MAX_POST_TEXT_BYTES // 3) + 1),
                media_ids=[MEDIA_ID],
            )

    assert request_count == 0


@pytest.mark.asyncio
async def test_post_text_utf8_byte_limit_accepts_exact_boundary() -> None:
    text = "x" * X_MAX_POST_TEXT_BYTES
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        body: object = json.loads(request.content)
        assert isinstance(body, dict)
        assert body["text"] == text
        return httpx2.Response(201, json={"data": {"id": POST_ID, "text": text}})

    async with mocked_x_client(handler) as client:
        post = await client.create_post(text=text, media_ids=[MEDIA_ID])

    assert request_count == 1
    assert len(post.text.encode("utf-8")) == X_MAX_POST_TEXT_BYTES


@pytest.mark.asyncio
async def test_exact_five_megabyte_image_is_accepted() -> None:
    boundary_image = b"x" * X_MAX_STATIC_IMAGE_BYTES
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        if request.url.path == "/2/media/upload":
            body: object = json.loads(request.content)
            assert isinstance(body, dict)
            encoded = body["media"]
            assert isinstance(encoded, str)
            assert base64.b64decode(encoded, validate=True) == boundary_image
            return httpx2.Response(200, json=upload_payload(size=len(boundary_image)))
        return httpx2.Response(200, json=metadata_payload())

    async with mocked_x_client(handler) as client:
        uploaded = await client.upload_image(image=boundary_image, media_type="image/jpeg")

    assert request_count == 2
    assert uploaded.size == X_MAX_STATIC_IMAGE_BYTES


@pytest.mark.asyncio
async def test_success_response_schema_is_strict() -> None:
    request_count = 0

    async def missing_size_handler(request: httpx2.Request) -> httpx2.Response:
        del request
        payload = upload_payload()
        data = payload["data"]
        assert isinstance(data, dict)
        del data["size"]
        return httpx2.Response(200, json=payload)

    async with mocked_x_client(missing_size_handler) as client:
        with pytest.raises(XProtocolError, match=r"data\.size"):
            await client.upload_image(image=IMAGE, media_type="image/png")

    async def adult_not_confirmed_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        if request.url.path == "/2/media/upload":
            return httpx2.Response(200, json=upload_payload())
        return httpx2.Response(200, json=metadata_payload(adult_content=False))

    async with mocked_x_client(adult_not_confirmed_handler) as client:
        with pytest.raises(XProtocolError, match="did not confirm"):
            await client.upload_image(image=IMAGE, media_type="image/png")

    assert request_count == 2

    async def malformed_post_handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(201, json={"data": {"id": 123, "text": "Preview"}})

    async with mocked_x_client(malformed_post_handler) as client:
        with pytest.raises(XProtocolError, match=r"data\.id"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    async def embedded_errors_handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            201,
            json={
                "data": {"id": POST_ID, "text": "Preview"},
                "errors": [{"title": "partial failure"}],
            },
        )

    async with mocked_x_client(embedded_errors_handler) as client:
        with pytest.raises(XProtocolError, match="invalid JSON response"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])


@pytest.mark.asyncio
async def test_upload_and_metadata_require_documented_http_200() -> None:
    upload_count = 0

    async def wrong_upload_status_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal upload_count
        upload_count += 1
        return httpx2.Response(201, json=upload_payload(), request=request)

    async with mocked_x_client(wrong_upload_status_handler) as client:
        with pytest.raises(XProtocolError, match="expected HTTP 200"):
            await client.upload_image(image=IMAGE, media_type="image/png")

    assert upload_count == 1

    metadata_count = 0

    async def wrong_metadata_status_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal metadata_count
        metadata_count += 1
        if request.url.path == "/2/media/upload":
            return httpx2.Response(200, json=upload_payload(), request=request)
        return httpx2.Response(201, json=metadata_payload(), request=request)

    async with mocked_x_client(wrong_metadata_status_handler) as client:
        with pytest.raises(XProtocolError, match="expected HTTP 200"):
            await client.upload_image(image=IMAGE, media_type="image/png")

    assert metadata_count == 2


@pytest.mark.asyncio
async def test_provider_errors_are_redacted_classified_and_never_retried() -> None:
    retryable_count = 0

    async def retryable_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal retryable_count
        retryable_count += 1
        return httpx2.Response(
            429,
            json={
                "title": "Too Many Requests",
                "detail": f"token {BEARER_TOKEN} is throttled",
            },
            headers={"retry-after": "2.5", "x-request-id": "request-123"},
            request=request,
        )

    async with mocked_x_client(retryable_handler) as client:
        with pytest.raises(XRetryableAPIError) as captured:
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert retryable_count == 1
    retryable = captured.value
    assert retryable.status_code == 429
    assert retryable.retry_after_seconds == 2.5
    assert retryable.request_id == "request-123"
    assert BEARER_TOKEN not in retryable.response_body
    assert BEARER_TOKEN not in str(retryable)

    terminal_count = 0

    async def terminal_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal terminal_count
        terminal_count += 1
        return httpx2.Response(
            400,
            text=f"<html><body>invalid {BEARER_TOKEN} {'x' * 2_000}</body></html>",
            request=request,
        )

    async with mocked_x_client(terminal_handler) as client:
        with pytest.raises(XTerminalAPIError) as captured_terminal:
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert terminal_count == 1
    terminal = captured_terminal.value
    assert "<html>" not in terminal.response_body
    assert BEARER_TOKEN not in terminal.response_body
    assert len(terminal.response_body) == 1_000


@pytest.mark.asyncio
async def test_create_post_timeout_is_ambiguous_and_is_never_retried() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise httpx2.ReadTimeout("response timed out", request=request)

    async with mocked_x_client(handler) as client:
        with pytest.raises(XAmbiguousTimeoutError, match="outcome is unknown"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert request_count == 1


@pytest.mark.asyncio
async def test_create_post_503_and_transport_failure_are_never_retried() -> None:
    server_error_count = 0

    async def server_error_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal server_error_count
        server_error_count += 1
        return httpx2.Response(503, json={"title": "Unavailable"}, request=request)

    async with mocked_x_client(server_error_handler) as client:
        with pytest.raises(XRetryableAPIError):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert server_error_count == 1

    transport_error_count = 0

    async def transport_error_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal transport_error_count
        transport_error_count += 1
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    async with mocked_x_client(transport_error_handler) as client:
        with pytest.raises(XAmbiguousTransportError, match="outcome is unknown"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert transport_error_count == 1


@pytest.mark.asyncio
async def test_pre_send_connection_failure_is_retryable_but_not_retried_internally() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise httpx2.ConnectTimeout("connection timed out", request=request)

    async with mocked_x_client(handler) as client:
        with pytest.raises(XRetryableTransportError, match="before request bytes"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert request_count == 1


@pytest.mark.asyncio
async def test_redirects_are_not_followed() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        return httpx2.Response(
            307,
            headers={"location": "https://attacker.example/collect"},
            request=request,
        )

    async with mocked_x_client(handler) as client:
        with pytest.raises(XTerminalAPIError) as captured:
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert captured.value.status_code == 307
    assert request_count == 1


def test_token_and_timeout_validation() -> None:
    http_client = httpx2.AsyncClient()
    try:
        with pytest.raises(ValueError, match="must not be empty"):
            XClient(http_client=http_client, bearer_token="")
        with pytest.raises(ValueError, match="line breaks"):
            XClient(
                http_client=http_client,
                bearer_token="unsafe\r\ntoken",  # noqa: S106
            )
        with pytest.raises(ValueError, match="between 0 and 60"):
            XClient(http_client=http_client, bearer_token=BEARER_TOKEN, timeout=61.0)
        with pytest.raises(ValueError, match="between 0 and 60"):
            XClient(
                http_client=http_client,
                bearer_token=BEARER_TOKEN,
                timeout=httpx2.Timeout(connect=None, read=1.0, write=1.0, pool=1.0),
            )
    finally:
        import asyncio

        asyncio.run(http_client.aclose())
