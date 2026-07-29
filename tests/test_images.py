from io import BytesIO

import pytest
from PIL import Image

from gen_automation.storage.images import (
    ImageVerificationError,
    verify_image_bytes,
    verify_image_bytes_isolated,
)


def encoded_image(
    *,
    image_format: str = "PNG",
    size: tuple[int, int] = (32, 24),
    color: str = "red",
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def test_png_is_fully_verified() -> None:
    data = encoded_image()

    verified = verify_image_bytes(data)

    assert verified.width == 32
    assert verified.height == 24
    assert verified.content_type == "image/png"
    assert verified.extension == "png"
    assert len(verified.sha256) == 64


@pytest.mark.asyncio
async def test_image_verification_runs_in_an_isolated_process() -> None:
    verified = await verify_image_bytes_isolated(encoded_image())

    assert verified.image_format == "PNG"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not an image",
        encoded_image()[:-8],
        encoded_image(image_format="GIF"),
        encoded_image(size=(21, 1)),
    ],
)
def test_invalid_or_unsafe_images_are_rejected(payload: bytes) -> None:
    with pytest.raises(ImageVerificationError):
        verify_image_bytes(payload)


def test_animated_webp_is_rejected() -> None:
    output = BytesIO()
    frames = [
        Image.new("RGB", (16, 16), "red"),
        Image.new("RGB", (16, 16), "blue"),
    ]
    frames[0].save(
        output,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )

    with pytest.raises(ImageVerificationError, match="animated"):
        verify_image_bytes(output.getvalue())
