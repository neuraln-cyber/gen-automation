import hashlib
import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, cast
from zipfile import ZIP_STORED, ZipFile

import pytest
from PIL import Image, PngImagePlugin

from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.integrations.patreon import (
    PATREON_HANDOFF_SCHEMA,
    PATREON_MAX_ARCHIVE_BYTES,
    PATREON_MAX_BODY_BYTES,
    PATREON_MAX_DERIVATIVE_IMAGES,
    PATREON_MAX_IMAGE_BYTES,
    PATREON_MAX_TITLE_BYTES,
    PATREON_MAX_TOTAL_IMAGE_BYTES,
    PATREON_PUBLIC_PREVIEW_ATTESTATION,
    PUBLICATION_CHECKLIST,
    PatreonHandoffError,
    PatreonHandoffPackage,
    PatreonHandoffPackagePart,
    PatreonImageValidationError,
    PatreonPackageImage,
    PatreonPreviewAttestationError,
    PatreonSetImageRecord,
    PublicPreviewSafetyAttestation,
    build_patreon_handoff_package,
    build_patreon_handoff_package_part,
    build_patreon_set_manifest,
)

ATTESTATION = PublicPreviewSafetyAttestation(
    safe_for_public=True,
    attested_by="operator-1",
    attested_at=datetime(2026, 7, 28, 13, 15, tzinfo=timezone(timedelta(hours=3))),
)
SCHEDULE = datetime(2026, 8, 1, 18, 30, tzinfo=timezone(timedelta(hours=2)))


def encoded_image(
    *,
    image_format: str = "PNG",
    size: tuple[int, int] = (32, 24),
    color: str = "red",
    **save_options: object,
) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", size, color)
    try:
        image.save(output, format=image_format, **save_options)
    finally:
        image.close()
    return output.getvalue()


def package_image(
    filename: str = "approved.png",
    *,
    image_format: str = "PNG",
    color: str = "red",
    data: bytes | None = None,
) -> PatreonPackageImage:
    return PatreonPackageImage(
        filename=filename,
        data=data if data is not None else encoded_image(image_format=image_format, color=color),
    )


def build_package(
    *,
    derivatives: tuple[PatreonPackageImage, ...] | None = None,
    preview: PatreonPackageImage | None = None,
    attestation: PublicPreviewSafetyAttestation | None = ATTESTATION,
    title: str = "Release title",
    body: str = "Line one.\r\nLine two.",
    tier: str = "Supporter",
    tags: tuple[str, ...] = ("Zelda", "Anime"),
    scheduled_at: datetime | None = SCHEDULE,
) -> PatreonHandoffPackage:
    return build_patreon_handoff_package(
        approved_derivatives=derivatives
        if derivatives is not None
        else (
            package_image("../../approved/First.png", color="red"),
            package_image(
                r"C:\exports\Second.jpeg",
                image_format="JPEG",
                color="blue",
            ),
        ),
        public_preview=preview
        if preview is not None
        else package_image(
            r"..\safe-previews\teaser.webp",
            image_format="WEBP",
            color="green",
        ),
        title=title,
        body=body,
        tier=tier,
        tags=tags,
        scheduled_at=scheduled_at,
        public_preview_attestation=attestation,
    )


def test_large_set_is_split_deterministically_with_one_ordered_manifest() -> None:
    derivative_bytes = encoded_image(color="red")
    preview_bytes = encoded_image(color="green")
    derivative_sha256 = hashlib.sha256(derivative_bytes).hexdigest()
    preview_sha256 = hashlib.sha256(preview_bytes).hexdigest()
    records = tuple(
        PatreonSetImageRecord(
            ordinal=ordinal,
            sha256=derivative_sha256,
            byte_size=len(derivative_bytes),
            width=32,
            height=24,
            image_format="PNG",
            content_type="image/png",
        )
        for ordinal in range(1, 401)
    )
    preview_record = PatreonSetImageRecord(
        ordinal=0,
        sha256=preview_sha256,
        byte_size=len(preview_bytes),
        width=32,
        height=24,
        image_format="PNG",
        content_type="image/png",
    )
    set_manifest_bytes, set_manifest_sha256 = build_patreon_set_manifest(
        approved_derivatives=records,
        public_preview=preview_record,
        title="Large release",
        body="One ordered set.",
        tier="Supporter",
        tags=("Anime",),
        scheduled_at=SCHEDULE,
        public_preview_attestation=ATTESTATION,
    )
    image = PatreonPackageImage("approved.png", derivative_bytes)
    preview = PatreonPackageImage("preview.png", preview_bytes)

    def build(part_number: int, count: int) -> PatreonHandoffPackagePart:
        return build_patreon_handoff_package_part(
            approved_derivatives=(image,) * count,
            public_preview=preview,
            set_manifest_bytes=set_manifest_bytes,
            part_number=part_number,
        )

    parts = (build(1, 100), build(2, 100), build(3, 100), build(4, 100))
    duplicate = build(2, 100)

    assert tuple((part.first_ordinal, part.last_ordinal) for part in parts) == (
        (1, 100),
        (101, 200),
        (201, 300),
        (301, 400),
    )
    assert all(part.part_count == 4 for part in parts)
    assert all(part.set_manifest_bytes == set_manifest_bytes for part in parts)
    assert all(part.set_manifest_sha256 == set_manifest_sha256 for part in parts)
    assert duplicate.archive_bytes == parts[1].archive_bytes
    assert duplicate.sha256 == parts[1].sha256

    manifest = json.loads(set_manifest_bytes)
    assert [record["ordinal"] for record in manifest["approved_derivatives"]] == list(range(1, 401))
    for part in parts:
        with ZipFile(BytesIO(part.archive_bytes)) as archive:
            assert archive.read("set-manifest.json") == set_manifest_bytes
            part_manifest = json.loads(archive.read("part-manifest.json"))
            assert part_manifest["set_manifest_sha256"] == set_manifest_sha256
            assert part_manifest["first_ordinal"] == part.first_ordinal
            assert part_manifest["last_ordinal"] == part.last_ordinal


def test_package_is_canonical_byte_deterministic_and_self_describing() -> None:
    first = build_package()
    second = build_package()

    assert first == second
    assert first.archive_bytes == second.archive_bytes
    assert len(first.archive_bytes) <= PATREON_MAX_ARCHIVE_BYTES
    assert first.sha256 == hashlib.sha256(first.archive_bytes).hexdigest()
    assert first.manifest_sha256 == hashlib.sha256(first.manifest_bytes).hexdigest()
    assert first.publication_checklist == PUBLICATION_CHECKLIST
    assert first.ordered_filenames == (
        "manifest.json",
        "PUBLICATION_CHECKLIST.md",
        "POST.txt",
        "public-preview/preview.webp",
        "content/001.png",
        "content/002.jpg",
    )

    with ZipFile(BytesIO(first.archive_bytes)) as archive:
        assert tuple(archive.namelist()) == first.ordered_filenames
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == ZIP_STORED
            assert info.create_system == 3
            assert info.external_attr >> 16 == stat.S_IFREG | 0o644
            assert info.extra == b""
            assert info.comment == b""
        assert archive.comment == b""
        assert archive.read("manifest.json") == first.manifest_bytes
        assert archive.read("content/001.png") == encoded_image(color="red")
        assert archive.read("content/002.jpg") == encoded_image(
            image_format="JPEG",
            color="blue",
        )
        checklist = archive.read("PUBLICATION_CHECKLIST.md").decode("utf-8")
        assert "Patreon's official UI" in checklist
        assert "read-API/webhook reconciliation" in checklist

    manifest: object = json.loads(first.manifest_bytes)
    assert isinstance(manifest, dict)
    assert first.manifest_bytes == canonical_json_bytes(manifest)
    assert manifest["schema"] == PATREON_HANDOFF_SCHEMA
    assert manifest["publication_mode"] == "human_official_ui"
    assert manifest["reconciliation_mode"] == "read_api_and_signed_webhooks_only"
    assert manifest["post"] == {
        "title": "Release title",
        "body": "Line one.\nLine two.",
        "tier": "Supporter",
        "tags": ["Anime", "Zelda"],
        "scheduled_at": "2026-08-01T16:30:00Z",
    }
    preview = manifest["public_preview"]
    assert isinstance(preview, dict)
    assert preview["path"] == "public-preview/preview.webp"
    assert preview["width"] == 32
    assert preview["height"] == 24
    attestation = preview["human_safety_attestation"]
    assert isinstance(attestation, dict)
    assert attestation == {
        "safe_for_public": True,
        "attested_by": "operator-1",
        "attested_at": "2026-07-28T10:15:00Z",
        "statement": PATREON_PUBLIC_PREVIEW_ATTESTATION,
    }


def test_source_directories_are_stripped_and_never_enter_the_package() -> None:
    first = build_package(
        derivatives=(package_image(r"C:\secret-vault\secret-name.png"),),
        preview=package_image("/private/review/hidden-preview.png"),
    )
    second = build_package(
        derivatives=(package_image("/different/source/unrelated-name.png"),),
        preview=package_image(r"D:\other\different-name.png"),
    )

    assert first.archive_bytes == second.archive_bytes
    archive_text = first.archive_bytes.decode("latin-1")
    assert "secret-vault" not in archive_text
    assert "secret-name" not in archive_text
    assert "private/review" not in archive_text
    assert "hidden-preview" not in archive_text
    assert ".." not in first.ordered_filenames
    assert first.ordered_filenames[-2:] == (
        "public-preview/preview.png",
        "content/001.png",
    )


@pytest.mark.parametrize(
    "attestation",
    [
        None,
        replace(ATTESTATION, safe_for_public=False),
        replace(ATTESTATION, attested_by=""),
        replace(ATTESTATION, attested_at=datetime(2026, 7, 28, 10, 0)),
    ],
)
def test_public_preview_attestation_fails_closed(
    attestation: PublicPreviewSafetyAttestation | None,
) -> None:
    expected_error = (
        PatreonPreviewAttestationError
        if attestation is None or attestation.safe_for_public is False
        else PatreonHandoffError
    )
    with pytest.raises(expected_error):
        build_package(attestation=attestation)


def test_image_count_individual_and_total_bounds_fail_before_decode() -> None:
    preview = package_image("preview.png")
    with pytest.raises(PatreonHandoffError, match="at least one"):
        build_package(derivatives=(), preview=preview)

    small = package_image("small.png")
    with pytest.raises(PatreonHandoffError, match=str(PATREON_MAX_DERIVATIVE_IMAGES)):
        build_package(
            derivatives=(small,) * (PATREON_MAX_DERIVATIVE_IMAGES + 1),
            preview=preview,
        )

    oversized = PatreonPackageImage(
        filename="oversized.png",
        data=b"x" * (PATREON_MAX_IMAGE_BYTES + 1),
    )
    with pytest.raises(PatreonImageValidationError, match=str(PATREON_MAX_IMAGE_BYTES)):
        build_package(derivatives=(oversized,), preview=preview)

    maximum_sized_invalid = PatreonPackageImage(
        filename="bounded.png",
        data=b"x" * PATREON_MAX_IMAGE_BYTES,
    )
    with pytest.raises(PatreonImageValidationError, match="combined"):
        build_package(
            derivatives=(maximum_sized_invalid,) * 8,
            preview=maximum_sized_invalid,
        )
    assert PATREON_MAX_TOTAL_IMAGE_BYTES == 128 * 1024 * 1024


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (PatreonPackageImage("empty.png", b""), "must not be empty"),
        (PatreonPackageImage("broken.png", b"not an image"), "safe static image"),
        (
            PatreonPackageImage(
                "wrong.jpg",
                encoded_image(image_format="PNG"),
            ),
            "does not match",
        ),
        (
            PatreonPackageImage(
                "unsupported.gif",
                encoded_image(image_format="GIF"),
            ),
            "unsupported image extension",
        ),
        (
            PatreonPackageImage(
                "unsafe.png",
                encoded_image(size=(21, 1)),
            ),
            "safe static image",
        ),
        (
            PatreonPackageImage(
                "truncated.png",
                encoded_image()[:-8],
            ),
            "safe static image",
        ),
        (
            PatreonPackageImage("no-extension", encoded_image()),
            "supported image extension",
        ),
        (
            PatreonPackageImage("../", encoded_image()),
            "safe basename",
        ),
    ],
)
def test_malformed_unsafe_and_mislabeled_images_are_rejected(
    image: PatreonPackageImage,
    message: str,
) -> None:
    with pytest.raises(PatreonImageValidationError, match=message):
        build_package(derivatives=(image,))


def test_non_bytes_image_and_invalid_filename_are_rejected() -> None:
    non_bytes = PatreonPackageImage(
        filename="approved.png",
        data=cast(Any, bytearray(encoded_image())),
    )
    with pytest.raises(PatreonImageValidationError, match="bytes"):
        build_package(derivatives=(non_bytes,))

    with pytest.raises(PatreonHandoffError, match="control character"):
        build_package(derivatives=(package_image("unsafe\x00.png"),))


def test_png_text_exif_icc_and_comment_metadata_are_rejected() -> None:
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("prompt", "private prompt")
    png_with_text = encoded_image(pnginfo=png_info)

    exif = Image.Exif()
    exif[0x010E] = "private prompt"
    jpeg_with_exif = encoded_image(image_format="JPEG", exif=exif)
    jpeg_with_icc = encoded_image(image_format="JPEG", icc_profile=b"private-icc")
    jpeg_with_comment = encoded_image(image_format="JPEG", comment=b"private-comment")

    for image in (
        PatreonPackageImage("text.png", png_with_text),
        PatreonPackageImage("exif.jpg", jpeg_with_exif),
        PatreonPackageImage("icc.jpg", jpeg_with_icc),
        PatreonPackageImage("comment.jpg", jpeg_with_comment),
    ):
        with pytest.raises(PatreonImageValidationError, match="metadata"):
            build_package(derivatives=(image,))


def test_animated_image_and_decompression_bomb_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    animated = PatreonPackageImage("animated.webp", output.getvalue())
    with pytest.raises(PatreonImageValidationError, match="safe static image"):
        build_package(derivatives=(animated,))

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    bomb = package_image("large.png", data=encoded_image(size=(20, 20)))
    with pytest.raises(PatreonImageValidationError, match="safe static image"):
        build_package(derivatives=(bomb,))


def test_metadata_text_and_tag_bounds_are_enforced_before_packaging() -> None:
    with pytest.raises(PatreonHandoffError, match=str(PATREON_MAX_TITLE_BYTES)):
        build_package(title="x" * (PATREON_MAX_TITLE_BYTES + 1))
    with pytest.raises(PatreonHandoffError, match=str(PATREON_MAX_BODY_BYTES)):
        build_package(body="x" * (PATREON_MAX_BODY_BYTES + 1))
    with pytest.raises(PatreonHandoffError, match="single line"):
        build_package(tier="Supporter\nAdmin")
    with pytest.raises(PatreonHandoffError, match="unique"):
        build_package(tags=("Anime", "anime"))
    with pytest.raises(PatreonHandoffError, match="sequence"):
        build_patreon_handoff_package(
            approved_derivatives=(package_image(),),
            public_preview=package_image("preview.png"),
            title="Title",
            body="Body",
            tier="Tier",
            tags=cast(Any, "not-a-sequence"),
            scheduled_at=None,
            public_preview_attestation=ATTESTATION,
        )


def test_schedule_must_be_timezone_aware_and_immediate_schedule_is_canonical() -> None:
    with pytest.raises(PatreonHandoffError, match="timezone"):
        build_package(scheduled_at=datetime(2026, 8, 1, 10, 0))

    package = build_package(scheduled_at=None, tags=())
    manifest: object = json.loads(package.manifest_bytes)
    assert isinstance(manifest, dict)
    post = manifest["post"]
    assert isinstance(post, dict)
    assert post["scheduled_at"] is None
    assert post["tags"] == []

    with ZipFile(BytesIO(package.archive_bytes)) as archive:
        post_text = archive.read("POST.txt").decode("utf-8")
    assert "Publish immediately" in post_text
    assert "(none)" in post_text
