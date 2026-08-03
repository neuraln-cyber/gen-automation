import hashlib
from dataclasses import asdict, replace
from io import BytesIO
from typing import Any, cast

import pytest
from PIL import Image, ImageChops, ImageDraw, PngImagePlugin

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.deliverability import (
    PATREON_MAX_IMAGE_BYTES,
    PATREON_MAX_TOTAL_IMAGE_BYTES,
    DeliverabilityError,
    patreon_full_output_byte_budget,
)
from gen_automation.services.derivatives import (
    DEFAULT_DERIVATIVE_LIMITS,
    DERIVATIVE_RENDERER_VERSION,
    BlurCensor,
    DerivativeInputError,
    DerivativeRecipe,
    DerivativeRecipeError,
    DerivativeRenderError,
    DerivativeSafetyLimits,
    DerivativeTarget,
    FullDerivativeSpec,
    JpegEncoding,
    MosaicCensor,
    OutputFormat,
    PngEncoding,
    RelativeRegion,
    TeaserFitMode,
    WatermarkPosition,
    WatermarkSpec,
    XTeaserSpec,
    derivative_recipe_sha256,
    estimate_derivative_peak_working_set_bytes,
    render_platform_derivatives,
)


def _encode(
    image: Image.Image,
    *,
    image_format: str = "PNG",
    **options: Any,
) -> bytes:
    output = BytesIO()
    image.save(output, format=image_format, **options)
    return output.getvalue()


def _pattern(size: tuple[int, int] = (96, 72)) -> Image.Image:
    image = Image.new("RGB", size)
    pixels = image.load()
    assert pixels is not None
    for y_position in range(image.height):
        for x_position in range(image.width):
            pixels[x_position, y_position] = (
                (x_position * 11 + y_position * 3) % 256,
                (x_position * 5 + y_position * 13) % 256,
                (x_position * 17 + y_position * 7) % 256,
            )
    return image


def _watermark(
    *,
    foreground: tuple[int, int, int] = (255, 32, 32),
    foreground_alpha: int = 220,
) -> bytes:
    image = Image.new("RGBA", (48, 18), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((3, 3, 44, 14), fill=(*foreground, foreground_alpha))
    draw.line((5, 8, 42, 8), fill=(255, 255, 255, foreground_alpha), width=2)
    try:
        return _encode(image)
    finally:
        image.close()


def _artifact(bundle: Any, target: DerivativeTarget) -> Any:
    return next(artifact for artifact in bundle.artifacts if artifact.target is target)


def _decoded(payload: bytes) -> Image.Image:
    image = Image.open(BytesIO(payload))
    image.load()
    return image


def _png_recipe(
    *,
    version: str = "test-v1",
    teaser: XTeaserSpec | None = None,
    watermark: WatermarkSpec | None = None,
    background_rgb: tuple[int, int, int] = (255, 255, 255),
) -> DerivativeRecipe:
    return DerivativeRecipe(
        version=version,
        background_rgb=background_rgb,
        full=FullDerivativeSpec(
            output_filename="full.png",
            max_width=512,
            max_height=512,
            encoding=PngEncoding(),
        ),
        x_teaser=teaser
        or XTeaserSpec(
            output_filename="teaser.png",
            width=512,
            height=512,
            encoding=PngEncoding(),
        ),
        watermark=watermark,
    )


def test_render_is_deterministic_non_destructive_and_lineage_complete() -> None:
    image = _pattern()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("prompt", "private prompt")
    metadata.add_text("Software", "/private/workstation/path")
    master = bytearray(_encode(image, pnginfo=metadata))
    image.close()
    watermark = bytearray(_watermark())
    original_master = bytes(master)
    original_watermark = bytes(watermark)
    recipe = _png_recipe(
        version="golden-v1",
        teaser=XTeaserSpec(
            output_filename="teaser.png",
            width=64,
            height=64,
            fit_mode=TeaserFitMode.CONTAIN,
            encoding=PngEncoding(compress_level=9),
            censor=MosaicCensor(
                RelativeRegion(x=250_000, y=250_000, width=500_000, height=500_000),
                block_size=8,
            ),
        ),
        watermark=WatermarkSpec(
            width=250_000,
            margin=20_000,
            opacity=210,
            position=WatermarkPosition.BOTTOM_RIGHT,
        ),
    )

    first = render_platform_derivatives(
        master,
        recipe=recipe,
        watermark_png=watermark,
    )
    second = render_platform_derivatives(
        memoryview(master),
        recipe=recipe,
        watermark_png=memoryview(watermark),
    )

    assert first == second
    assert bytes(master) == original_master
    assert bytes(watermark) == original_watermark
    assert first.source_sha256 == hashlib.sha256(original_master).hexdigest()
    assert first.recipe_sha256 == derivative_recipe_sha256(recipe)
    assert tuple(artifact.target for artifact in first.artifacts) == (
        DerivativeTarget.FULL_RESOLUTION,
        DerivativeTarget.X_TEASER,
    )
    for artifact in first.artifacts:
        assert artifact.sha256 == hashlib.sha256(artifact.data).hexdigest()
        assert artifact.byte_size == len(artifact.data)
        assert artifact.recipe_sha256 == first.recipe_sha256
        assert artifact.lineage.source_sha256 == first.source_sha256
        assert artifact.lineage.renderer_version == DERIVATIVE_RENDERER_VERSION
        assert artifact.lineage.recipe_version == "golden-v1"
        assert artifact.lineage_sha256 == canonical_sha256(asdict(artifact.lineage))
        assert any(operation.startswith("watermark:") for operation in artifact.lineage.operations)
        with _decoded(artifact.data) as rendered:
            assert not rendered.getexif()
            assert "icc_profile" not in rendered.info
            assert "comment" not in rendered.info
            assert "prompt" not in rendered.info
            assert "private prompt" not in artifact.data.decode("latin-1")
            assert "/private/workstation/path" not in artifact.data.decode("latin-1")
    assert first.artifacts[0].lineage.watermark_sha256 is None
    assert "watermark:none" in first.artifacts[0].lineage.operations
    assert (
        first.artifacts[1].lineage.watermark_sha256
        == hashlib.sha256(original_watermark).hexdigest()
    )


def test_golden_recipe_and_output_hashes_are_stable() -> None:
    master_image = _pattern((12, 8))
    master = _encode(master_image)
    master_image.close()
    recipe = _png_recipe(
        version="hash-golden-v1",
        teaser=XTeaserSpec(
            output_filename="teaser.png",
            width=8,
            height=8,
            fit_mode=TeaserFitMode.CONTAIN,
            encoding=PngEncoding(compress_level=9),
        ),
    )

    result = render_platform_derivatives(master, recipe=recipe)

    assert (
        result.recipe_sha256 == "24a62acf6860765f8c8506cbc5a1f1f5300375c631edf7271359c297d84ebd66"
    )
    assert tuple(artifact.sha256 for artifact in result.artifacts) == (
        "6c8ff090bf0ea578e20e9eb6c6a75051a0521c171c2aaf59d39974866a2ac9d6",
        "06b3d76c53e0722dbb41d92b64ad78b5b7fd9ddf7f2cb3145a165d22cb49a3fe",
    )


def test_default_jpeg_derivatives_are_byte_deterministic() -> None:
    image = _pattern((80, 60))
    master = _encode(image, image_format="JPEG", quality=93)
    image.close()

    first = render_platform_derivatives(master, recipe=DerivativeRecipe())
    second = render_platform_derivatives(master, recipe=DerivativeRecipe())

    assert tuple(artifact.data for artifact in first.artifacts) == tuple(
        artifact.data for artifact in second.artifacts
    )
    assert tuple(artifact.sha256 for artifact in first.artifacts) == tuple(
        artifact.sha256 for artifact in second.artifacts
    )
    assert all(artifact.image_format is OutputFormat.JPEG for artifact in first.artifacts)


def test_x_jpeg_adapts_deterministically_to_the_media_byte_cap() -> None:
    image = _pattern((512, 512))
    master = _encode(image)
    image.close()
    recipe = replace(
        DerivativeRecipe(),
        x_teaser=XTeaserSpec(
            width=512,
            height=512,
            encoding=JpegEncoding(quality=100),
        ),
    )
    limits = replace(DEFAULT_DERIVATIVE_LIMITS, max_x_teaser_bytes=20 * 1024)

    first = _artifact(
        render_platform_derivatives(
            master,
            recipe=recipe,
            targets=(DerivativeTarget.X_TEASER,),
            limits=limits,
        ),
        DerivativeTarget.X_TEASER,
    )
    second = _artifact(
        render_platform_derivatives(
            master,
            recipe=recipe,
            targets=(DerivativeTarget.X_TEASER,),
            limits=limits,
        ),
        DerivativeTarget.X_TEASER,
    )

    assert first.data == second.data
    assert first.byte_size <= limits.max_x_teaser_bytes
    assert (first.width, first.height) <= (512, 512)
    assert any(operation.startswith("x-cap-") for operation in first.lineage.operations)


@pytest.mark.parametrize("accepted_count", (1, 7, 8, 100, 400, 500))
def test_patreon_full_output_budget_reserves_the_duplicate_preview(
    accepted_count: int,
) -> None:
    budget = patreon_full_output_byte_budget(accepted_count)

    assert budget <= PATREON_MAX_IMAGE_BYTES
    images_in_largest_part = min(accepted_count, 100)
    assert (images_in_largest_part + 1) * budget <= PATREON_MAX_TOTAL_IMAGE_BYTES


@pytest.mark.parametrize("accepted_count", (True, 0, 501))
def test_patreon_full_output_budget_rejects_invalid_set_sizes(
    accepted_count: int,
) -> None:
    with pytest.raises(DeliverabilityError):
        patreon_full_output_byte_budget(accepted_count)


def test_full_jpeg_adapts_deterministically_to_the_release_byte_budget() -> None:
    image = _pattern((512, 512))
    master = _encode(image)
    image.close()
    recipe = replace(
        DerivativeRecipe(),
        full=FullDerivativeSpec(
            max_width=512,
            max_height=512,
            encoding=JpegEncoding(quality=100),
        ),
    )
    limits = replace(DEFAULT_DERIVATIVE_LIMITS, max_full_output_bytes=20 * 1024)

    first = _artifact(
        render_platform_derivatives(
            master,
            recipe=recipe,
            targets=(DerivativeTarget.FULL_RESOLUTION,),
            limits=limits,
        ),
        DerivativeTarget.FULL_RESOLUTION,
    )
    second = _artifact(
        render_platform_derivatives(
            master,
            recipe=recipe,
            targets=(DerivativeTarget.FULL_RESOLUTION,),
            limits=limits,
        ),
        DerivativeTarget.FULL_RESOLUTION,
    )

    assert first.data == second.data
    assert first.byte_size <= limits.max_full_output_bytes
    assert (first.width, first.height) <= (512, 512)
    assert f"full-budget-limit:{limits.max_full_output_bytes}" in first.lineage.operations
    assert any(
        operation.startswith(("full-budget-downscale:", "full-budget-jpeg-quality:"))
        for operation in first.lineage.operations
    )


def test_full_png_fails_closed_when_it_exceeds_the_release_byte_budget() -> None:
    image = _pattern((256, 256))
    master = _encode(image)
    image.close()
    recipe = replace(
        DerivativeRecipe(),
        full=FullDerivativeSpec(
            output_filename="member-full.png",
            max_width=256,
            max_height=256,
            encoding=PngEncoding(compress_level=0),
        ),
    )

    with pytest.raises(DerivativeRenderError, match="automatic lossy conversion is forbidden"):
        render_platform_derivatives(
            master,
            recipe=recipe,
            targets=(DerivativeTarget.FULL_RESOLUTION,),
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_full_output_bytes=20 * 1024),
        )


def test_exif_orientation_and_cmyk_are_normalized_and_private_metadata_removed() -> None:
    image = Image.new("CMYK", (40, 20), (10, 120, 200, 20))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010E] = "private prompt"
    master = _encode(
        image,
        image_format="JPEG",
        exif=exif,
        comment=b"private-comment",
        icc_profile=b"private-icc-profile",
    )
    image.close()

    result = render_platform_derivatives(master, recipe=DerivativeRecipe())

    assert result.artifacts[0].lineage.source_width == 40
    assert result.artifacts[0].lineage.source_height == 20
    assert result.artifacts[0].lineage.normalized_width == 20
    assert result.artifacts[0].lineage.normalized_height == 40
    for artifact in result.artifacts:
        assert (artifact.width, artifact.height) == (20, 40)
        with _decoded(artifact.data) as rendered:
            assert rendered.mode == "RGB"
            assert not rendered.getexif()
            assert "icc_profile" not in rendered.info
            assert "comment" not in rendered.info
        assert b"private prompt" not in artifact.data
        assert b"private-comment" not in artifact.data
        assert b"private-icc-profile" not in artifact.data


def test_transparent_master_is_flattened_against_recipe_background() -> None:
    image = Image.new("RGBA", (16, 12), (200, 10, 20, 0))
    image.putpixel((8, 6), (255, 0, 0, 255))
    master = _encode(image)
    image.close()
    recipe = _png_recipe(background_rgb=(12, 34, 56))

    result = render_platform_derivatives(master, recipe=recipe)

    with _decoded(result.artifacts[0].data) as rendered:
        assert rendered.mode == "RGB"
        assert rendered.getpixel((0, 0)) == (12, 34, 56)
        assert rendered.getpixel((8, 6)) == (255, 0, 0)


def test_opaque_rgb_normalization_avoids_rgba_compositing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _pattern((24, 16))
    master = _encode(image)
    image.close()

    def reject_alpha_composite(*args: object, **kwargs: object) -> Image.Image:
        raise AssertionError("opaque RGB normalization used the alpha stack")

    monkeypatch.setattr(Image, "alpha_composite", reject_alpha_composite)

    result = render_platform_derivatives(master, recipe=_png_recipe())

    assert all(
        "normalize_opaque_rgb" in artifact.lineage.operations for artifact in result.artifacts
    )


def test_alpha_master_uses_background_compositing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image.new("RGBA", (16, 12), (200, 10, 20, 80))
    master = _encode(image)
    image.close()
    original_alpha_composite = Image.alpha_composite
    calls = 0

    def observe_alpha_composite(
        image1: Image.Image,
        image2: Image.Image,
    ) -> Image.Image:
        nonlocal calls
        calls += 1
        return original_alpha_composite(image1, image2)

    monkeypatch.setattr(Image, "alpha_composite", observe_alpha_composite)

    result = render_platform_derivatives(master, recipe=_png_recipe())

    assert calls == 1
    assert all(
        "flatten_alpha_to_rgb" in artifact.lineage.operations for artifact in result.artifacts
    )


def test_palette_transparency_uses_alpha_normalization() -> None:
    image = Image.new("P", (8, 8), color=0)
    palette = [0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254
    image.putpalette(palette)
    image.putpixel((4, 4), 1)
    master = _encode(image, transparency=0)
    image.close()
    recipe = _png_recipe(background_rgb=(12, 34, 56))

    result = render_platform_derivatives(master, recipe=recipe)

    with _decoded(result.artifacts[0].data) as rendered:
        assert rendered.getpixel((0, 0)) == (12, 34, 56)
        assert rendered.getpixel((4, 4)) == (255, 0, 0)
    assert "flatten_alpha_to_rgb" in result.artifacts[0].lineage.operations


def test_peak_working_set_estimate_is_conservative_and_alpha_sensitive() -> None:
    recipe = _png_recipe(watermark=WatermarkSpec())
    opaque = estimate_derivative_peak_working_set_bytes(
        source_width=4000,
        source_height=2000,
        source_mode="RGB",
        source_has_alpha=False,
        master_byte_size=32 * 1024 * 1024,
        watermark_byte_size=4 * 1024 * 1024,
        recipe=recipe,
        limits=DEFAULT_DERIVATIVE_LIMITS,
    )
    alpha = estimate_derivative_peak_working_set_bytes(
        source_width=4000,
        source_height=2000,
        source_mode="RGBA",
        source_has_alpha=True,
        master_byte_size=8 * 1024 * 1024,
        watermark_byte_size=1024 * 1024,
        recipe=recipe,
        limits=DEFAULT_DERIVATIVE_LIMITS,
    )
    oversized = estimate_derivative_peak_working_set_bytes(
        source_width=4000,
        source_height=3000,
        source_mode="RGBA",
        source_has_alpha=True,
        master_byte_size=32 * 1024 * 1024,
        watermark_byte_size=4 * 1024 * 1024,
        recipe=DerivativeRecipe(watermark=WatermarkSpec()),
        limits=DEFAULT_DERIVATIVE_LIMITS,
    )

    assert opaque <= DEFAULT_DERIVATIVE_LIMITS.max_peak_working_set_bytes
    assert opaque < alpha <= DEFAULT_DERIVATIVE_LIMITS.max_peak_working_set_bytes
    assert oversized > DEFAULT_DERIVATIVE_LIMITS.max_peak_working_set_bytes


def test_peak_working_set_limit_rejects_before_pixel_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _pattern((100, 100))
    master = _encode(image)
    image.close()

    def reject_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("pixel decode occurred before peak-memory admission")

    monkeypatch.setattr(Image.Image, "load", reject_load)

    with pytest.raises(DerivativeInputError, match="peak working set"):
        render_platform_derivatives(
            master,
            recipe=DerivativeRecipe(),
            limits=replace(
                DEFAULT_DERIVATIVE_LIMITS,
                max_peak_working_set_bytes=64 * 1024 * 1024,
            ),
        )


def test_teaser_fit_modes_have_explicit_dimensions_and_padding() -> None:
    image = Image.new("RGB", (200, 100), (220, 10, 20))
    master = _encode(image)
    image.close()
    downscale_recipe = _png_recipe(
        teaser=XTeaserSpec(
            output_filename="teaser.png",
            width=100,
            height=100,
            fit_mode=TeaserFitMode.DOWNSCALE,
            encoding=PngEncoding(),
        )
    )
    contain_recipe = replace(
        downscale_recipe,
        background_rgb=(1, 2, 3),
        x_teaser=replace(
            downscale_recipe.x_teaser,
            fit_mode=TeaserFitMode.CONTAIN,
        ),
    )
    cover_recipe = replace(
        downscale_recipe,
        x_teaser=replace(
            downscale_recipe.x_teaser,
            fit_mode=TeaserFitMode.COVER,
        ),
    )

    downscale = _artifact(
        render_platform_derivatives(master, recipe=downscale_recipe),
        DerivativeTarget.X_TEASER,
    )
    contain = _artifact(
        render_platform_derivatives(master, recipe=contain_recipe),
        DerivativeTarget.X_TEASER,
    )
    cover = _artifact(
        render_platform_derivatives(master, recipe=cover_recipe),
        DerivativeTarget.X_TEASER,
    )

    assert (downscale.width, downscale.height) == (100, 50)
    assert (contain.width, contain.height) == (100, 100)
    assert (cover.width, cover.height) == (100, 100)
    with _decoded(contain.data) as rendered:
        assert rendered.getpixel((0, 0)) == (1, 2, 3)
        assert rendered.getpixel((50, 50)) == (220, 10, 20)


def test_cover_fit_rejects_prohibited_upscaling() -> None:
    image = Image.new("RGB", (20, 20), "red")
    master = _encode(image)
    image.close()
    recipe = _png_recipe(
        teaser=XTeaserSpec(
            output_filename="teaser.png",
            width=100,
            height=100,
            fit_mode=TeaserFitMode.COVER,
            allow_upscale=False,
            encoding=PngEncoding(),
        )
    )

    with pytest.raises(DerivativeRenderError, match="upscaling"):
        render_platform_derivatives(master, recipe=recipe)


@pytest.mark.parametrize(
    "censor",
    [
        MosaicCensor(
            RelativeRegion(x=250_000, y=250_000, width=500_000, height=500_000),
            block_size=8,
        ),
        BlurCensor(
            RelativeRegion(x=250_000, y=250_000, width=500_000, height=500_000),
            radius=5,
        ),
    ],
)
def test_censorship_changes_only_the_configured_teaser_region(censor: Any) -> None:
    image = _pattern((64, 64))
    master = _encode(image)
    image.close()
    base_teaser = XTeaserSpec(
        output_filename="teaser.png",
        width=64,
        height=64,
        encoding=PngEncoding(),
    )
    baseline = _artifact(
        render_platform_derivatives(
            master,
            recipe=_png_recipe(teaser=base_teaser),
        ),
        DerivativeTarget.X_TEASER,
    )
    transformed = _artifact(
        render_platform_derivatives(
            master,
            recipe=_png_recipe(teaser=replace(base_teaser, censor=censor)),
        ),
        DerivativeTarget.X_TEASER,
    )

    with _decoded(baseline.data) as original, _decoded(transformed.data) as changed:
        assert original.getpixel((8, 8)) == changed.getpixel((8, 8))
        differences = 0
        for y_position in range(16, 48):
            for x_position in range(16, 48):
                differences += int(
                    original.getpixel((x_position, y_position))
                    != changed.getpixel((x_position, y_position))
                )
        assert differences > 100


def test_watermark_is_applied_only_to_x_teaser_after_censorship() -> None:
    image = Image.new("RGB", (120, 80), (10, 40, 180))
    master = _encode(image)
    image.close()
    watermark = _watermark()
    base_recipe = _png_recipe(
        teaser=XTeaserSpec(
            output_filename="teaser.png",
            width=120,
            height=80,
            encoding=PngEncoding(),
            censor=BlurCensor(
                RelativeRegion(x=0, y=0, width=1_000_000, height=1_000_000),
                radius=4,
            ),
        )
    )
    watermarked_recipe = replace(
        base_recipe,
        watermark=WatermarkSpec(
            width=300_000,
            margin=20_000,
            opacity=255,
            position=WatermarkPosition.BOTTOM_RIGHT,
        ),
    )

    baseline = render_platform_derivatives(master, recipe=base_recipe)
    watermarked = render_platform_derivatives(
        master,
        recipe=watermarked_recipe,
        watermark_png=watermark,
    )

    clean_full, plain_teaser = baseline.artifacts
    marked_full, marked_teaser = watermarked.artifacts
    assert marked_full.data == clean_full.data
    assert marked_full.lineage.watermark_sha256 is None
    assert "watermark:none" in marked_full.lineage.operations
    with _decoded(plain_teaser.data) as plain_image, _decoded(marked_teaser.data) as marked_image:
        difference = ImageChops.difference(plain_image, marked_image)
        try:
            bounds = difference.getbbox()
        finally:
            difference.close()
        assert bounds is not None
        assert bounds[0] >= marked_teaser.width // 2
        assert bounds[1] >= marked_teaser.height // 2
    teaser_operations = watermarked.artifacts[1].lineage.operations
    assert teaser_operations.index("censor:blur") < next(
        index
        for index, operation in enumerate(teaser_operations)
        if operation.startswith("watermark:")
    )


@pytest.mark.parametrize(
    "watermark",
    [
        _encode(Image.new("RGB", (20, 10), "red")),
        _encode(Image.new("RGBA", (20, 10), (255, 0, 0, 0))),
        _encode(Image.new("RGBA", (20, 10), (255, 0, 0, 255))),
        _encode(Image.new("RGB", (20, 10), "red"), image_format="JPEG"),
    ],
)
def test_watermark_requires_safe_png_with_transparent_and_visible_alpha(
    watermark: bytes,
) -> None:
    master_image = Image.new("RGB", (64, 64), "blue")
    master = _encode(master_image)
    master_image.close()
    recipe = _png_recipe(watermark=WatermarkSpec())

    with pytest.raises(DerivativeInputError):
        render_platform_derivatives(master, recipe=recipe, watermark_png=watermark)


def test_pixel_invisible_watermark_is_rejected() -> None:
    image = Image.new("RGB", (100, 100), (20, 30, 40))
    master = _encode(image)
    image.close()
    watermark_image = Image.new("RGBA", (20, 10), (20, 30, 40, 0))
    draw = ImageDraw.Draw(watermark_image)
    draw.rectangle((2, 2, 17, 7), fill=(20, 30, 40, 255))
    watermark = _encode(watermark_image)
    watermark_image.close()
    recipe = _png_recipe(watermark=WatermarkSpec(width=200_000, margin=0, opacity=255))

    with pytest.raises(DerivativeRenderError, match="not visible"):
        render_platform_derivatives(master, recipe=recipe, watermark_png=watermark)


@pytest.mark.parametrize(
    "output_filename",
    [
        "../full.jpg",
        "folder/full.jpg",
        r"C:\full.jpg",
        "/tmp/full.jpg",  # noqa: S108
        "NUL.jpg",
        "full..jpg",
        "full.png",
        "full.",
    ],
)
def test_output_filenames_are_safe_logical_basenames(output_filename: str) -> None:
    with pytest.raises(DerivativeRecipeError):
        FullDerivativeSpec(output_filename=output_filename)


def test_recipe_rejects_invalid_types_regions_formats_and_duplicate_names() -> None:
    with pytest.raises(DerivativeRecipeError):
        FullDerivativeSpec(max_width=cast(Any, True))
    with pytest.raises(DerivativeRecipeError):
        FullDerivativeSpec(encoding=cast(Any, "GIF"))
    with pytest.raises(DerivativeRecipeError):
        XTeaserSpec(fit_mode=cast(Any, "cover"))
    with pytest.raises(DerivativeRecipeError):
        RelativeRegion(x=900_000, y=0, width=200_000, height=1)
    with pytest.raises(DerivativeRecipeError):
        DerivativeRecipe(background_rgb=cast(Any, [255, 255, 255]))
    with pytest.raises(DerivativeRecipeError, match="unique"):
        DerivativeRecipe(
            full=FullDerivativeSpec(output_filename="same.jpg"),
            x_teaser=XTeaserSpec(output_filename="same.jpg"),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-an-image",
        _encode(Image.new("RGB", (16, 16), "red"), image_format="GIF"),
        _encode(Image.new("RGB", (16, 16), "red"))[:-10],
    ],
)
def test_malformed_and_unsupported_masters_are_rejected(payload: bytes) -> None:
    with pytest.raises(DerivativeInputError):
        render_platform_derivatives(payload, recipe=DerivativeRecipe())


def test_decompression_bomb_warning_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image.new("RGB", (20, 20), "red")
    master = _encode(image)
    image.close()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(DerivativeInputError, match="malformed or unsafe"):
        render_platform_derivatives(master, recipe=DerivativeRecipe())


def test_animated_master_is_rejected() -> None:
    first = Image.new("RGB", (16, 16), "red")
    second = Image.new("RGB", (16, 16), "blue")
    output = BytesIO()
    try:
        first.save(
            output,
            format="WEBP",
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
        )
    finally:
        first.close()
        second.close()

    with pytest.raises(DerivativeInputError, match="multi-frame"):
        render_platform_derivatives(output.getvalue(), recipe=DerivativeRecipe())


def test_master_geometry_and_watermark_byte_limits_are_enforced() -> None:
    wide = Image.new("RGB", (21, 1), "red")
    wide_master = _encode(wide)
    wide.close()
    with pytest.raises(DerivativeInputError, match="aspect ratio"):
        render_platform_derivatives(wide_master, recipe=DerivativeRecipe())

    too_wide = Image.new("RGB", (20, 10), "red")
    too_wide_master = _encode(too_wide)
    too_wide.close()
    with pytest.raises(DerivativeInputError, match="dimensions"):
        render_platform_derivatives(
            too_wide_master,
            recipe=DerivativeRecipe(),
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_input_width=10),
        )

    too_many_pixels = Image.new("RGB", (11, 10), "red")
    too_many_pixels_master = _encode(too_many_pixels)
    too_many_pixels.close()
    with pytest.raises(DerivativeInputError, match="pixels"):
        render_platform_derivatives(
            too_many_pixels_master,
            recipe=DerivativeRecipe(),
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_input_pixels=100),
        )

    watermark = _watermark()
    assert len(watermark) > 128
    with pytest.raises(DerivativeInputError, match="byte limit"):
        render_platform_derivatives(
            too_wide_master,
            recipe=DerivativeRecipe(watermark=WatermarkSpec()),
            watermark_png=watermark,
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_watermark_bytes=128),
        )


def test_non_bytes_master_is_rejected() -> None:
    with pytest.raises(DerivativeInputError, match="bytes-like"):
        render_platform_derivatives(
            cast(Any, "not bytes"),
            recipe=DerivativeRecipe(),
        )


def test_input_recipe_output_and_dimension_bounds_fail_before_partial_results() -> None:
    image = _pattern((128, 128))
    master = _encode(image)
    image.close()

    with pytest.raises(DerivativeInputError, match="byte limit"):
        render_platform_derivatives(
            b"x" * 1025,
            recipe=DerivativeRecipe(),
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_master_bytes=1024),
        )
    with pytest.raises(DerivativeRecipeError, match="serialized"):
        render_platform_derivatives(
            master,
            recipe=DerivativeRecipe(),
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_recipe_bytes=256),
        )
    with pytest.raises(DerivativeRecipeError, match="dimensions"):
        render_platform_derivatives(
            master,
            recipe=DerivativeRecipe(),
            limits=replace(
                DEFAULT_DERIVATIVE_LIMITS,
                max_output_width=1000,
                max_output_height=1000,
                max_output_pixels=1_000_000,
            ),
        )
    png_recipe = _png_recipe(
        teaser=XTeaserSpec(
            output_filename="teaser.png",
            width=128,
            height=128,
            encoding=PngEncoding(compress_level=0),
        )
    )
    with pytest.raises(DerivativeRenderError, match="output byte limit"):
        render_platform_derivatives(
            master,
            recipe=png_recipe,
            limits=replace(DEFAULT_DERIVATIVE_LIMITS, max_output_bytes=1024),
        )


def test_watermark_bytes_and_recipe_must_be_supplied_together() -> None:
    image = Image.new("RGB", (32, 32), "red")
    master = _encode(image)
    image.close()

    with pytest.raises(DerivativeRecipeError, match="requires watermark bytes"):
        render_platform_derivatives(
            master,
            recipe=DerivativeRecipe(watermark=WatermarkSpec()),
        )
    with pytest.raises(DerivativeRecipeError, match="accepted only"):
        render_platform_derivatives(
            master,
            recipe=DerivativeRecipe(),
            watermark_png=_watermark(),
        )


def test_recipe_hash_is_canonical_and_changes_for_material_operations() -> None:
    base = DerivativeRecipe()
    same = DerivativeRecipe()
    censored = replace(
        base,
        x_teaser=replace(
            base.x_teaser,
            censor=BlurCensor(
                RelativeRegion(x=0, y=0, width=500_000, height=500_000),
            ),
        ),
    )
    watermarked = replace(base, watermark=WatermarkSpec())

    assert derivative_recipe_sha256(base) == derivative_recipe_sha256(same)
    assert derivative_recipe_sha256(base) != derivative_recipe_sha256(censored)
    assert derivative_recipe_sha256(base) != derivative_recipe_sha256(watermarked)
    assert len(derivative_recipe_sha256(base)) == 64
    assert isinstance(DEFAULT_DERIVATIVE_LIMITS, DerivativeSafetyLimits)
    assert JpegEncoding().image_format is OutputFormat.JPEG
