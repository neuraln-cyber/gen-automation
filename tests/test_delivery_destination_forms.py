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
    read_prepare_output_form,
    read_prepare_patreon_form,
    read_prepare_x_form,
    read_retry_output_form,
)
from gen_automation.services.derivatives import WatermarkPosition


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
        "x_adult_content": "true",
        "x_scheduled_local": "",
        "x_timezone": "Europe/Sofia",
    }


def _x_output_fields() -> dict[str, str]:
    return {
        **_common_fields(),
        "watermark_asset_id": str(uuid4()),
        "watermark_position": "bottom_right",
    }


@pytest.mark.asyncio
async def test_output_form_accepts_exact_per_image_watermark_placements() -> None:
    first = uuid4()
    second = uuid4()
    fields = {
        **_x_output_fields(),
        "watermark_placements": (f'{{"{first}":"top_left","{second}":"bottom_right"}}'),
    }

    form = await read_prepare_output_form(_request(fields))

    assert dict(form.watermark_placements) == {
        first: WatermarkPosition.TOP_LEFT,
        second: WatermarkPosition.BOTTOM_RIGHT,
    }


@pytest.mark.asyncio
async def test_output_form_keeps_legacy_single_corner_fallback() -> None:
    form = await read_prepare_output_form(_request(_x_output_fields()))

    assert form.watermark_position == WatermarkPosition.BOTTOM_RIGHT
    assert form.watermark_placements == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "placements",
    [
        "{}",
        '{"not-a-uuid":"top_left"}',
        None,
    ],
)
async def test_output_form_rejects_invalid_watermark_placement_maps(
    placements: str | None,
) -> None:
    fields = _x_output_fields()
    if placements is None:
        asset_id = uuid4()
        fields["watermark_placements"] = f'{{"{asset_id}":"top_left","{asset_id}":"bottom_right"}}'
    else:
        fields["watermark_placements"] = placements

    with pytest.raises(BrowserDeliveryFormError) as caught:
        await read_prepare_output_form(_request(fields))

    assert caught.value.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_output_form_rejects_more_than_four_watermark_placements() -> None:
    placements = ",".join(f'"{uuid4()}":"top_left"' for _index in range(5))
    fields = {
        **_x_output_fields(),
        "watermark_placements": "{" + placements + "}",
    }

    with pytest.raises(BrowserDeliveryFormError) as caught:
        await read_prepare_output_form(_request(fields))

    assert caught.value.status_code == status.HTTP_400_BAD_REQUEST


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
    assert form.adult_content
    assert form.scheduled_at is None
    assert not hasattr(form, "patreon_title")
    assert not hasattr(form, "public_preview_output_id")


@pytest.mark.asyncio
async def test_mega_form_has_no_publication_fields() -> None:
    form = await read_prepare_mega_form(_request(_common_fields()))

    assert form.submission_id
    assert not hasattr(form, "patreon_title")
    assert not hasattr(form, "x_text")


@pytest.mark.asyncio
async def test_retry_output_form_accepts_only_the_signed_retry_identity() -> None:
    fields = _common_fields()

    form = await read_retry_output_form(_request(fields))

    assert form.submission_id == UUID(fields["submission_id"])
    assert form.idempotency_key == fields["idempotency_key"]


@pytest.mark.asyncio
async def test_retry_output_form_rejects_prepare_output_fields() -> None:
    fields = {**_common_fields(), "watermark_asset_id": ""}

    with pytest.raises(BrowserDeliveryFormError) as caught:
        await read_retry_output_form(_request(fields))

    assert caught.value.status_code == status.HTTP_400_BAD_REQUEST


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


@pytest.mark.asyncio
async def test_x_form_accepts_sfw_scheduled_post_in_browser_timezone() -> None:
    fields = _x_fields()
    fields["x_adult_content"] = "false"
    fields["x_scheduled_local"] = "2026-08-10T18:30"

    form = await read_prepare_x_form(_request(fields))

    assert not form.adult_content
    assert form.scheduled_at == datetime(2026, 8, 10, 15, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_x_form_ignores_timezone_when_schedule_is_blank() -> None:
    fields = _x_fields()
    fields["x_scheduled_local"] = ""
    fields["x_timezone"] = "not/a-timezone"

    form = await read_prepare_x_form(_request(fields))

    assert form.scheduled_at is None


@pytest.mark.asyncio
async def test_x_form_rejects_an_unrecognized_adult_toggle_value() -> None:
    fields = _x_fields()
    fields["x_adult_content"] = "sometimes"

    with pytest.raises(BrowserDeliveryFormError) as caught:
        await read_prepare_x_form(_request(fields))

    assert caught.value.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_value", "expected_message"),
    [
        ("2026-03-29T03:30", "does not exist"),
        ("2026-10-25T03:30", "is ambiguous"),
    ],
)
async def test_x_form_rejects_dst_gap_and_overlap(
    local_value: str,
    expected_message: str,
) -> None:
    fields = _x_fields()
    fields["x_scheduled_local"] = local_value

    with pytest.raises(BrowserDeliveryFormError) as caught:
        await read_prepare_x_form(_request(fields))

    assert caught.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert expected_message in caught.value.message
