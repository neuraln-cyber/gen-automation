import asyncio
import hashlib
import io
import json
import os
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

import gen_automation.gpu_worker.artifacts as worker_artifacts
from gen_automation.gpu_worker.artifacts import (
    MAX_SAFETENSORS_HEADER_BYTES,
    ArtifactBootstrapError,
    ArtifactBootstrapResult,
    ArtifactDownloader,
    ArtifactKind,
    ArtifactManifest,
    ModelArtifactSpec,
    calculate_manifest_sha256,
)
from gen_automation.gpu_worker.artifacts import (
    bootstrap_artifacts as _bootstrap_artifacts,
)


def _safetensors(
    *,
    header: object | None = None,
    body: bytes = b"\x00\x00\x00\x00",
) -> bytes:
    value = (
        {"weight": {"data_offsets": [0, len(body)], "dtype": "F32", "shape": [1]}}
        if header is None
        else header
    )
    encoded_header = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return len(encoded_header).to_bytes(8, "little") + encoded_header + body


def _detector_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("archive/data.pkl", b"verified-detector-metadata")
        archive.writestr("archive/data/0", b"\x00" * 128)
    return output.getvalue()


def _artifact(
    content: bytes,
    *,
    logical_name: str = "illustrious-xl",
    kind: ArtifactKind = ArtifactKind.CHECKPOINT,
    target_filename: str = "illustrious-xl.safetensors",
    exact_size_bytes: int | None = None,
    max_size_bytes: int | None = None,
    sha256: str | None = None,
    source_object_id: str | None = "private/models/illustrious",
    source_object_version_id: str | None = None,
    downloader_key: str | None = None,
) -> ModelArtifactSpec:
    size = len(content) if exact_size_bytes is None else exact_size_bytes
    maximum = size if max_size_bytes is None else max_size_bytes
    return ModelArtifactSpec(
        logical_name=logical_name,
        kind=kind,
        source_object_id=source_object_id,
        source_object_version_id=source_object_version_id,
        downloader_key=downloader_key,
        sha256=sha256 or hashlib.sha256(content).hexdigest(),
        exact_size_bytes=size,
        max_size_bytes=maximum,
        target_filename=target_filename,
    )


def _manifest(*artifacts: ModelArtifactSpec) -> ArtifactManifest:
    ordered = tuple(
        sorted(
            artifacts,
            key=lambda artifact: (
                artifact.kind.value,
                artifact.logical_name.casefold(),
                artifact.logical_name,
                artifact.target_filename.casefold(),
                artifact.target_filename,
                artifact.sha256,
            ),
        )
    )
    return ArtifactManifest(
        version="v1",
        artifacts=ordered,
        manifest_sha256=calculate_manifest_sha256(ordered),
    )


@dataclass
class FakeDownloader:
    blobs: dict[str, bytes]
    chunk_size: int = 7
    error: Exception | None = None
    non_bytes_chunk: bool = False
    calls: list[str] = field(default_factory=list)

    async def stream(self, artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        self.calls.append(artifact.logical_name)
        if self.error is not None:
            raise self.error
        if self.non_bytes_chunk:
            yield "not bytes"  # type: ignore[misc]
            return
        content = self.blobs[artifact.logical_name]
        for offset in range(0, len(content), self.chunk_size):
            yield content[offset : offset + self.chunk_size]


@dataclass
class CoordinatedDownloader:
    blobs: dict[str, bytes]
    expected_concurrency: int
    active: int = 0
    peak: int = 0
    all_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def stream(self, artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= self.expected_concurrency:
            self.all_started.set()
        try:
            await asyncio.wait_for(self.all_started.wait(), timeout=1)
            yield self.blobs[artifact.logical_name]
        finally:
            self.active -= 1


@dataclass
class RangedFakeDownloader:
    content: bytes
    stream_calls: int = 0
    ranged_calls: int = 0

    async def stream(self, _artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        self.stream_calls += 1
        yield self.content

    async def ranged_stream(self, _artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        self.ranged_calls += 1
        yield self.content


async def bootstrap_artifacts(
    manifest: ArtifactManifest,
    downloader: ArtifactDownloader,
    *,
    checkpoint_root: Path,
    lora_root: Path,
    detector_root: Path | None = None,
) -> ArtifactBootstrapResult:
    return await _bootstrap_artifacts(
        manifest,
        downloader,
        expected_manifest_sha256=manifest.manifest_sha256,
        checkpoint_root=checkpoint_root,
        lora_root=lora_root,
        detector_root=detector_root,
    )


@pytest.fixture
def model_roots(tmp_path: Path) -> tuple[Path, Path]:
    checkpoint_root = tmp_path / "checkpoints"
    lora_root = tmp_path / "loras"
    checkpoint_root.mkdir()
    lora_root.mkdir()
    return checkpoint_root, lora_root


@pytest.mark.asyncio
async def test_streams_verifies_and_atomically_materializes_in_deterministic_order(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    checkpoint_content = _safetensors(body=b"\x01\x02\x03\x04")
    lora_content = _safetensors(body=b"\x05\x06")
    checkpoint = _artifact(checkpoint_content)
    lora = _artifact(
        lora_content,
        logical_name="cinematic-style",
        kind=ArtifactKind.LORA,
        target_filename="cinematic-style.safetensors",
        source_object_id=None,
        downloader_key="cinematic-style-v1",
    )
    manifest = _manifest(lora, checkpoint)
    downloader = FakeDownloader(
        {
            checkpoint.logical_name: checkpoint_content,
            lora.logical_name: lora_content,
        },
        chunk_size=3,
    )

    result = await bootstrap_artifacts(
        manifest,
        downloader,
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
    )

    assert downloader.calls == ["illustrious-xl", "cinematic-style"]
    assert (checkpoint_root / checkpoint.target_filename).read_bytes() == checkpoint_content
    assert (lora_root / lora.target_filename).read_bytes() == lora_content
    assert result.version == "v1"
    assert result.manifest_sha256 == manifest.manifest_sha256
    assert [item.logical_name for item in result.artifacts] == [
        "illustrious-xl",
        "cinematic-style",
    ]
    assert all(not item.adopted_existing for item in result.artifacts)
    assert all(path.suffix == ".safetensors" for path in checkpoint_root.iterdir())
    assert all(path.suffix == ".safetensors" for path in lora_root.iterdir())


@pytest.mark.asyncio
async def test_downloads_independent_model_artifacts_concurrently(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    contents = (
        _safetensors(body=b"\x01"),
        _safetensors(body=b"\x02"),
        _safetensors(body=b"\x03"),
    )
    artifacts = (
        _artifact(contents[0]),
        _artifact(
            contents[1],
            logical_name="style-one",
            kind=ArtifactKind.LORA,
            target_filename="style-one.safetensors",
            source_object_id="private/models/style-one",
        ),
        _artifact(
            contents[2],
            logical_name="style-two",
            kind=ArtifactKind.LORA,
            target_filename="style-two.safetensors",
            source_object_id="private/models/style-two",
        ),
    )
    downloader = CoordinatedDownloader(
        blobs={
            artifact.logical_name: content
            for artifact, content in zip(artifacts, contents, strict=True)
        },
        expected_concurrency=len(artifacts),
    )

    result = await bootstrap_artifacts(
        _manifest(*artifacts),
        downloader,
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
    )

    assert downloader.peak == len(artifacts)
    assert [item.logical_name for item in result.artifacts] == [
        "illustrious-xl",
        "style-one",
        "style-two",
    ]


@pytest.mark.asyncio
async def test_large_version_pinned_artifact_uses_optional_ranged_stream(
    model_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors(body=b"ranged-download")
    artifact = _artifact(content, source_object_version_id="version-1")
    downloader = RangedFakeDownloader(content)
    monkeypatch.setattr(worker_artifacts, "MULTIPART_ARTIFACT_THRESHOLD_BYTES", len(content))

    await bootstrap_artifacts(
        _manifest(artifact),
        downloader,
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
    )

    assert downloader.ranged_calls == 1
    assert downloader.stream_calls == 0
    assert (checkpoint_root / artifact.target_filename).read_bytes() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True], ids=["unversioned-large", "pinned-small"])
async def test_single_stream_is_preserved_without_both_size_and_version_eligibility(
    model_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    pinned: bool,
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors(body=b"single-stream")
    artifact = _artifact(
        content,
        source_object_version_id="version-1" if pinned else None,
    )
    downloader = RangedFakeDownloader(content)
    threshold = len(content) + 1 if pinned else len(content)
    monkeypatch.setattr(worker_artifacts, "MULTIPART_ARTIFACT_THRESHOLD_BYTES", threshold)

    await bootstrap_artifacts(
        _manifest(artifact),
        downloader,
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
    )

    assert downloader.stream_calls == 1
    assert downloader.ranged_calls == 0


@pytest.mark.asyncio
async def test_ranged_stream_still_uses_hash_validation_and_temp_cleanup(
    model_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_root, lora_root = model_roots
    expected = _safetensors(body=b"expected-content")
    corrupt = expected[:-1] + bytes([expected[-1] ^ 0xFF])
    artifact = _artifact(expected, source_object_version_id="version-1")
    downloader = RangedFakeDownloader(corrupt)
    monkeypatch.setattr(worker_artifacts, "MULTIPART_ARTIFACT_THRESHOLD_BYTES", len(expected))

    with pytest.raises(ArtifactBootstrapError, match="artifact bootstrap failed"):
        await bootstrap_artifacts(
            _manifest(artifact),
            downloader,
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert downloader.ranged_calls == 1
    assert list(checkpoint_root.iterdir()) == []


@pytest.mark.asyncio
async def test_ranged_stream_cancellation_propagates_after_temp_cleanup(
    model_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors(body=b"cancelled-ranged-download")
    artifact = _artifact(content, source_object_version_id="version-1")
    monkeypatch.setattr(worker_artifacts, "MULTIPART_ARTIFACT_THRESHOLD_BYTES", len(content))

    class CancelledRangedDownloader(RangedFakeDownloader):
        async def ranged_stream(self, _artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
            self.ranged_calls += 1
            yield self.content[:8]
            raise asyncio.CancelledError

    downloader = CancelledRangedDownloader(content)
    with pytest.raises(asyncio.CancelledError):
        await bootstrap_artifacts(
            _manifest(artifact),
            downloader,
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert downloader.ranged_calls == 1
    assert list(checkpoint_root.iterdir()) == []


@pytest.mark.asyncio
async def test_materializes_a_hash_verified_detector_only_into_the_bbox_root(
    model_roots: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    checkpoint_root, lora_root = model_roots
    detector_root = tmp_path / "ultralytics" / "bbox"
    detector_root.mkdir(parents=True)
    detector_content = _detector_archive()
    detector = _artifact(
        detector_content,
        logical_name="face-yolov8m",
        kind=ArtifactKind.DETECTOR,
        target_filename="face-yolov8m.pt",
        source_object_id="private/detectors/face-yolov8m.pt",
    )

    result = await bootstrap_artifacts(
        _manifest(detector),
        FakeDownloader({detector.logical_name: detector_content}),
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
        detector_root=detector_root.resolve(),
    )

    assert result.artifacts[0].kind == ArtifactKind.DETECTOR
    assert (detector_root / "face-yolov8m.pt").read_bytes() == detector_content
    assert list(checkpoint_root.iterdir()) == []
    assert list(lora_root.iterdir()) == []


@pytest.mark.asyncio
async def test_adopts_only_an_existing_exact_regular_file_without_downloading(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)
    destination = checkpoint_root / artifact.target_filename
    destination.write_bytes(content)
    downloader = FakeDownloader({})

    result = await bootstrap_artifacts(
        _manifest(artifact),
        downloader,
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
    )

    assert downloader.calls == []
    assert result.artifacts[0].adopted_existing
    assert destination.read_bytes() == content


@pytest.mark.asyncio
async def test_replaces_a_corrupt_existing_regular_file_after_full_verification(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)
    destination = checkpoint_root / artifact.target_filename
    destination.write_bytes(b"x" * len(content))

    await bootstrap_artifacts(
        _manifest(artifact),
        FakeDownloader({artifact.logical_name: content}),
        checkpoint_root=checkpoint_root.resolve(),
        lora_root=lora_root.resolve(),
    )

    assert destination.read_bytes() == content


@pytest.mark.parametrize(
    "target",
    [
        "../escape.safetensors",
        "/absolute.safetensors",
        r"C:\absolute.safetensors",
        "not-a-model.ckpt",
        "CON.safetensors",
        "lpt9.any.safetensors",
        ".hidden.safetensors",
        "subdir/model.safetensors",
    ],
)
def test_rejects_unsafe_or_non_safetensors_target_names(target: str) -> None:
    with pytest.raises(ValidationError):
        _artifact(_safetensors(), target_filename=target)


def test_source_is_exactly_one_opaque_identifier_and_is_redacted() -> None:
    content = _safetensors()
    private_source = "private/models/model-with-sensitive-key"
    artifact = _artifact(content, source_object_id=private_source)
    assert private_source not in repr(artifact)
    assert private_source not in str(artifact)

    with pytest.raises(ValidationError) as both:
        _artifact(content, downloader_key="other")
    with pytest.raises(ValidationError):
        _artifact(content, source_object_id=None, downloader_key=None)
    with pytest.raises(ValidationError) as url:
        _artifact(content, source_object_id="https://secret.example/model?token=private")

    assert private_source not in str(both.value)
    assert "secret.example" not in str(url.value)


def test_manifest_requires_canonical_order_digest_and_unique_identity() -> None:
    first_content = _safetensors(body=b"\x00")
    second_content = _safetensors(body=b"\x01")
    first = _artifact(first_content, logical_name="a", target_filename="a.safetensors")
    second = _artifact(
        second_content,
        logical_name="b",
        target_filename="b.safetensors",
        kind=ArtifactKind.LORA,
    )
    ordered = (first, second)

    with pytest.raises(ValidationError, match="canonical order"):
        ArtifactManifest(
            version="v1",
            artifacts=tuple(reversed(ordered)),
            manifest_sha256=calculate_manifest_sha256(ordered),
        )
    with pytest.raises(ValidationError, match="digest mismatch"):
        ArtifactManifest(version="v1", artifacts=ordered, manifest_sha256="0" * 64)

    same_target = _artifact(
        second_content,
        logical_name="other",
        target_filename="A.safetensors",
    )
    with pytest.raises(ValidationError, match="duplicate artifact target"):
        _manifest(first, same_target)

    same_digest = _artifact(
        first_content,
        logical_name="other",
        target_filename="other.safetensors",
        kind=ArtifactKind.LORA,
    )
    with pytest.raises(ValidationError, match="duplicate artifact digest"):
        _manifest(first, same_digest)


def test_manifest_enforces_an_aggregate_disk_bound() -> None:
    declared_size = 24 * 1024 * 1024 * 1024
    artifacts = tuple(
        _artifact(
            _safetensors(body=bytes([index])),
            logical_name=f"artifact-{index}",
            target_filename=f"artifact-{index}.safetensors",
            exact_size_bytes=declared_size,
            max_size_bytes=declared_size,
            sha256=f"{index + 1:064x}",
        )
        for index in range(3)
    )

    with pytest.raises(ValidationError, match="aggregate size"):
        _manifest(*artifacts)


@pytest.mark.asyncio
async def test_bootstrap_requires_a_separately_pinned_manifest_digest(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    manifest = _manifest(_artifact(content))
    downloader = FakeDownloader({"illustrious-xl": content})

    with pytest.raises(ArtifactBootstrapError):
        await _bootstrap_artifacts(
            manifest,
            downloader,
            expected_manifest_sha256="0" * 64,
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert downloader.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("downloaded", "exact_adjustment", "max_adjustment", "digest"),
    [
        (_safetensors() + b"x", 0, 0, None),
        (_safetensors()[:-1], 0, 0, None),
        (_safetensors(), 1, 1, None),
        (_safetensors(), 0, 0, "0" * 64),
    ],
)
async def test_rejects_oversize_undersize_and_digest_mismatch_and_cleans_temp(
    model_roots: tuple[Path, Path],
    downloaded: bytes,
    exact_adjustment: int,
    max_adjustment: int,
    digest: str | None,
) -> None:
    checkpoint_root, lora_root = model_roots
    expected = _safetensors()
    artifact = _artifact(
        expected,
        exact_size_bytes=len(expected) + exact_adjustment,
        max_size_bytes=len(expected) + max_adjustment,
        sha256=digest,
    )

    with pytest.raises(ArtifactBootstrapError, match="artifact bootstrap failed"):
        await bootstrap_artifacts(
            _manifest(artifact),
            FakeDownloader({artifact.logical_name: downloaded}),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert list(checkpoint_root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        b"not safetensors",
        (MAX_SAFETENSORS_HEADER_BYTES + 1).to_bytes(8, "little") + b"{}",
        (2).to_bytes(8, "little") + b"[]",
        (3).to_bytes(8, "little") + b"{x}",
        (13).to_bytes(8, "little") + b'{"x":1,"x":2}',
        (3).to_bytes(8, "little") + b"{}\xff",
    ],
)
async def test_rejects_malformed_safetensors_headers_and_cleans_temp(
    model_roots: tuple[Path, Path],
    content: bytes,
) -> None:
    checkpoint_root, lora_root = model_roots
    artifact = _artifact(content)

    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            _manifest(artifact),
            FakeDownloader({artifact.logical_name: content}),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert list(checkpoint_root.iterdir()) == []


@pytest.mark.asyncio
async def test_rejects_symlink_and_other_non_regular_destinations(
    model_roots: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(content)
    directory_destination = checkpoint_root / artifact.target_filename
    directory_destination.mkdir()
    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            _manifest(artifact),
            FakeDownloader({artifact.logical_name: content}),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )
    directory_destination.rmdir()

    destination = checkpoint_root / artifact.target_filename
    try:
        destination.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            _manifest(artifact),
            FakeDownloader({artifact.logical_name: content}),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )
    assert destination.is_symlink()
    assert outside.read_bytes() == content


@pytest.mark.asyncio
async def test_rejects_relative_missing_same_or_symlink_roots(
    model_roots: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)
    manifest = _manifest(artifact)
    downloader = FakeDownloader({artifact.logical_name: content})

    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            manifest,
            downloader,
            checkpoint_root=Path("relative"),
            lora_root=lora_root.resolve(),
        )
    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            manifest,
            downloader,
            checkpoint_root=(tmp_path / "missing").resolve(),
            lora_root=lora_root.resolve(),
        )
    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            manifest,
            downloader,
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=checkpoint_root.resolve(),
        )

    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(checkpoint_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            manifest,
            downloader,
            checkpoint_root=linked_root.absolute(),
            lora_root=lora_root.resolve(),
        )


@pytest.mark.asyncio
async def test_downloader_errors_and_non_byte_chunks_are_redacted_and_cleaned(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)
    private_error = "https://secret.example/model?token=do-not-leak"

    with pytest.raises(ArtifactBootstrapError) as failure:
        await bootstrap_artifacts(
            _manifest(artifact),
            FakeDownloader({}, error=RuntimeError(private_error)),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )
    assert private_error not in str(failure.value)
    assert list(checkpoint_root.iterdir()) == []

    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            _manifest(artifact),
            FakeDownloader({}, non_bytes_chunk=True),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )
    assert list(checkpoint_root.iterdir()) == []


@pytest.mark.asyncio
async def test_temporary_file_is_restrictive_and_removed_on_cancellation_safe_failure(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)
    observed_modes: list[int] = []

    @dataclass
    class InspectingDownloader:
        async def stream(self, _artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
            temporary = next(checkpoint_root.glob(".artifact-*.tmp"))
            observed_modes.append(stat_mode(temporary))
            yield content[:-1]

    with pytest.raises(ArtifactBootstrapError):
        await bootstrap_artifacts(
            _manifest(artifact),
            InspectingDownloader(),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert observed_modes
    if os.name != "nt":
        assert observed_modes == [0o600]
    assert list(checkpoint_root.iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_propagates_after_cleaning_the_temporary_file(
    model_roots: tuple[Path, Path],
) -> None:
    checkpoint_root, lora_root = model_roots
    content = _safetensors()
    artifact = _artifact(content)

    @dataclass
    class CancelledDownloader:
        async def stream(self, _artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
            yield content[:8]
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await bootstrap_artifacts(
            _manifest(artifact),
            CancelledDownloader(),
            checkpoint_root=checkpoint_root.resolve(),
            lora_root=lora_root.resolve(),
        )

    assert list(checkpoint_root.iterdir()) == []


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
