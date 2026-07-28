import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO

from anyio import fail_after, to_process
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_WIDTH = 16_384
MAX_IMAGE_HEIGHT = 16_384
MAX_ASPECT_RATIO = 20
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

FORMAT_CONTENT_TYPES = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


class ImageVerificationError(Exception):
    pass


@dataclass(frozen=True)
class VerifiedImage:
    sha256: str
    byte_size: int
    width: int
    height: int
    image_format: str
    content_type: str
    extension: str


def verify_image_bytes(data: bytes) -> VerifiedImage:
    if not data:
        raise ImageVerificationError("image is empty")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                if image_format not in FORMAT_CONTENT_TYPES:
                    raise ImageVerificationError(f"unsupported image format: {image_format}")
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise ImageVerificationError("image dimensions must be positive")
                if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
                    raise ImageVerificationError("image dimensions exceed the safety limit")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ImageVerificationError("image dimensions exceed the safety limit")
                shorter_side = min(width, height)
                longer_side = max(width, height)
                if longer_side / shorter_side > MAX_ASPECT_RATIO:
                    raise ImageVerificationError("image aspect ratio is unsafe")
                if int(getattr(image, "n_frames", 1)) != 1:
                    raise ImageVerificationError("animated images are not supported")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
    except ImageVerificationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        MemoryError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
    ) as error:
        raise ImageVerificationError("invalid or unsafe image data") from error

    format_details = FORMAT_CONTENT_TYPES[image_format]
    content_type, extension = format_details
    return VerifiedImage(
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=width,
        height=height,
        image_format=image_format,
        content_type=content_type,
        extension=extension,
    )


async def verify_image_bytes_isolated(
    data: bytes,
    *,
    timeout_seconds: float = 30,
) -> VerifiedImage:
    """Decode untrusted image bytes outside the API process with a wall-time limit."""

    with fail_after(timeout_seconds):
        return await to_process.run_sync(
            verify_image_bytes,
            data,
            cancellable=True,
        )
