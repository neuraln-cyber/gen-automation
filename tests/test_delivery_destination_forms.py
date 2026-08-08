from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import status
from starlette.requests import Request

from gen_automation.api.browser_delivery_forms import (
    BrowserDeliveryFormError,
    read_prepare_mega_form,
    read_prepare_patreon_form,
    read_prepare_x_form,
)


def _request(fields: dict[str, str]) -> Request:
    body = urlencode(fields).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/dashboard/review-tasks/example/delivery",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
        },
        receive,
    )


def _common_fields() -> dict[str, str]:
    return {
        "csrf_token": "csrf-token",
        "idempotency_key": f"web-delivery-{'a' * 64}",
        "submission_id": str(uuid4()),
    }


def _patreon_fields() -> dict[str, str]:
    return {
        **_common_fields(),
        "patreon_title": "Independent Patreon set",
        "patreon_body": "The complete member set.",
        "patreon_tier": "Paid members",
        "patreon_tags": "art, complete set",
        "public_preview_output_id": str(uuid4()),
        "public_preview_attested_at": datetime(2026, 8, 8, 12, tzinfo=UTC).isoformat(),
        "public_preview_safe": "true",
    }


def _x_fields() -> dict[str, str]:
    return {
        **_common_fields(),
        "x_text": "New teaser set",
    }


@pytest.mark.asyncio
async def test_patreon_form_is_complete_without_any_x_fields() -> None:
    fields = _patreon_fields()

    form = await read_prepare_patreon_form(_request(fields))

    assert form.patreon_title == "Independent Patreon set"
    assert form.patreon_tags == ("art", "complete set")
    assert form.public_preview_output_id == UUID(fields["public_preview_output_id"])
    assert form.public_preview_safe
    assert not hasattr(form, "x_text")


@pytest.mark.asyncio
async def test_x_form_is_complete_without_any_patreon_fields() -> None:
    fields = _x_fields()

    form = await read_prepare_x_form(_request(fields))

    assert form.x_text == "New teaser set"
    assert not hasattr(form, "patreon_title")
    assert not hasattr(form, "public_preview_output_id")


@pytest.mark.asyncio
async def test_mega_form_has_no_publication_fields() -> None:
    form = await read_prepare_mega_form(_request(_common_fields()))

    assert form.submission_id
    assert not hasattr(form, "patreon_title")
    assert not hasattr(form, "x_text")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "fields", "foreign_field"),
    [
        (read_prepare_patreon_form, _patreon_fields, "x_text"),
        (read_prepare_x_form, _x_fields, "patreon_title"),
    ],
)
async def test_destination_forms_reject_cross_target_fields(
    reader: Callable[[Request], Awaitable[object]],
    fields: Callable[[], dict[str, str]],
    foreign_field: str,
) -> None:
    payload = fields()
    payload[foreign_field] = "must not be accepted"

    with pytest.raises(BrowserDeliveryFormError) as caught:
        await reader(_request(payload))

    assert caught.value.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_patreon_preview_attestation_is_not_required_by_x() -> None:
    x_form = await read_prepare_x_form(_request(_x_fields()))
    assert x_form.x_text

    unsafe = _patreon_fields()
    unsafe["public_preview_safe"] = ""
    with pytest.raises(BrowserDeliveryFormError) as caught:
        await read_prepare_patreon_form(_request(unsafe))

    assert caught.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
