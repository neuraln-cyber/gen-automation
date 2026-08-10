from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gen_automation.db.models import (
    AdminUser,
    Asset,
    Release,
    VideoGenerationJob,
    VideoGenerationOutput,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import AdminRole, AssetKind, AssetState
from gen_automation.domain.video import (
    VideoComplianceAttestations,
    VideoContentRating,
    VideoGenerationParameters,
    VideoGenerationRequest,
    VideoGenerationState,
    VideoSourceSnapshot,
)
from gen_automation.services.assets import (
    AssetConflictError,
    AssetNotFoundError,
    AssetStorageUnavailableError,
    presign_asset_download,
)
from gen_automation.storage.base import ObjectStore
from gen_automation.video_worker.models import (
    DEFAULT_MAX_IMAGE_DIMENSION,
    DEFAULT_MAX_IMAGE_PIXELS,
    DEFAULT_MAX_SOURCE_BYTES,
)
from gen_automation.video_worker.profiles import (
    PINNED_VIDEO_PROFILE,
    PINNED_VIDEO_PROFILE_SHA256,
)

VIDEO_SOURCE_CONTENT_TYPES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})
VIDEO_DURATION_SECONDS: Final = frozenset({3, 5})
MIN_VIDEO_VARIANTS: Final = 1
MAX_VIDEO_VARIANTS: Final = 3
MAX_VIDEO_PROMPT_LENGTH: Final = 4_000
DEFAULT_VIDEO_NEGATIVE_PROMPT: Final = ""
VIDEO_PROFILE_VERSION: Final = PINNED_VIDEO_PROFILE.adapter_revision
_TERMINAL_STATES: Final = frozenset(
    {
        VideoGenerationState.SUCCEEDED,
        VideoGenerationState.FAILED,
        VideoGenerationState.CANCELLED,
    }
)


class VideoStudioError(Exception):
    """Base error safe to translate at the browser boundary."""


class VideoStudioInputError(VideoStudioError, ValueError):
    pass


class VideoStudioNotFoundError(VideoStudioError, LookupError):
    pass


class VideoStudioConflictError(VideoStudioError):
    pass


class VideoStudioStorageError(VideoStudioError):
    pass


@dataclass(frozen=True, slots=True)
class VideoSourceOption:
    asset_id: UUID
    release_title: str
    width: int
    height: int
    byte_size: int
    image_format: str
    sha256: str
    available_at: datetime | None

    @property
    def label(self) -> str:
        return f"{self.release_title} - {self.width}x{self.height}"

    @property
    def preview_url(self) -> str:
        return f"/dashboard/animations/assets/{self.asset_id}/preview/{self.sha256[:16]}.jpg"


@dataclass(frozen=True, slots=True)
class CreateVideoSubmission:
    submission_id: UUID
    source_asset_id: UUID
    prompt: str
    content_rating: VideoContentRating
    duration_seconds: int
    variant_count: int
    source_rights_confirmed: bool
    lawful_use_confirmed: bool
    all_depicted_people_are_adults: bool = False
    consensual_adult_content_confirmed: bool = False
    no_real_person_sexual_content: bool = False


@dataclass(frozen=True, slots=True)
class VideoOutputRead:
    asset_id: UUID
    width: int
    height: int
    byte_size: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class VideoJobRead:
    id: UUID
    variant_index: int
    variant_count: int
    source_asset_id: UUID
    source_sha256: str
    prompt: str
    content_rating: VideoContentRating
    state: VideoGenerationState
    width: int
    height: int
    frame_count: int
    fps: int
    estimated_cost_microusd: int
    reserved_cost_microusd: int
    actual_cost_microusd: int
    billed_duration_ms: int
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None
    output: VideoOutputRead | None

    @property
    def requested_duration_seconds(self) -> int:
        return _requested_duration_from_frames(self.frame_count, self.fps)

    @property
    def preview_url(self) -> str:
        return (
            f"/dashboard/animations/assets/{self.source_asset_id}/preview/"
            f"{self.source_sha256[:16]}.jpg"
        )

    @property
    def output_url(self) -> str | None:
        if self.output is None:
            return None
        return f"/dashboard/animations/{self.id}/output"

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class VideoSubmissionRead:
    submission_id: UUID
    jobs: tuple[VideoJobRead, ...]

    @property
    def is_terminal(self) -> bool:
        return bool(self.jobs) and all(job.is_terminal for job in self.jobs)

    @property
    def estimated_cost_microusd(self) -> int:
        return sum(job.estimated_cost_microusd for job in self.jobs)

    @property
    def reserved_cost_microusd(self) -> int:
        return sum(job.reserved_cost_microusd for job in self.jobs)

    @property
    def actual_cost_microusd(self) -> int:
        return sum(job.actual_cost_microusd for job in self.jobs)


async def list_video_sources(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    limit: int = 100,
) -> tuple[VideoSourceOption, ...]:
    await _require_actor(session, actor_user_id=actor_user_id, lock=False)
    safe_limit = _bounded_limit(limit)
    rows = list(
        (
            await session.execute(
                select(Asset, Release.title)
                .join(Release, Release.id == Asset.release_id)
                .where(
                    Asset.kind == AssetKind.RAW_MASTER,
                    Asset.state == AssetState.AVAILABLE,
                    Asset.object_key.is_not(None),
                    Asset.object_version_id.is_not(None),
                    Asset.sha256.is_not(None),
                    Asset.content_type.in_(VIDEO_SOURCE_CONTENT_TYPES),
                    Asset.image_format.is_not(None),
                    Asset.width.is_not(None),
                    Asset.height.is_not(None),
                    Asset.byte_size.is_not(None),
                )
                .order_by(Asset.available_at.desc(), Asset.created_at.desc(), Asset.id.desc())
                .limit(safe_limit)
            )
        ).all()
    )
    options: list[VideoSourceOption] = []
    for asset, release_title in rows:
        try:
            snapshot = _source_snapshot(asset)
            _validate_source_worker_limits(snapshot)
            _derive_video_dimensions(snapshot.width, snapshot.height)
        except VideoStudioConflictError:
            continue
        options.append(
            VideoSourceOption(
                asset_id=asset.id,
                release_title=release_title,
                width=snapshot.width,
                height=snapshot.height,
                byte_size=snapshot.byte_size,
                image_format=snapshot.image_format,
                sha256=snapshot.sha256,
                available_at=asset.available_at,
            )
        )
    return tuple(options)


async def create_video_submission(
    session: AsyncSession,
    *,
    command: CreateVideoSubmission,
    actor_user_id: UUID,
    max_hourly_cost_usd: Decimal,
    now: datetime | None = None,
) -> VideoSubmissionRead:
    created_at = _as_utc(now or datetime.now(UTC))
    actor = await _require_actor(session, actor_user_id=actor_user_id, lock=True)
    _validate_command(command)
    asset = await session.scalar(
        select(Asset).where(Asset.id == command.source_asset_id).with_for_update()
    )
    if asset is None:
        raise VideoStudioNotFoundError("source image was not found")
    snapshot = _source_snapshot(asset)
    _validate_source_worker_limits(snapshot)
    width, height = _derive_video_dimensions(snapshot.width, snapshot.height)
    compliance = _compliance(command)
    submission_request_sha256 = canonical_sha256(
        {
            "schema": "video-studio-submission/v1",
            "submission_id": str(command.submission_id),
            "actor_user_id": str(actor.id),
            "source": snapshot.model_dump(mode="json"),
            "prompt": command.prompt,
            "negative_prompt": DEFAULT_VIDEO_NEGATIVE_PROMPT,
            "content_rating": command.content_rating.value,
            "duration_seconds": command.duration_seconds,
            "variant_count": command.variant_count,
            "compliance": compliance.model_dump(mode="json"),
        }
    )
    replay = await _load_submission_by_id(
        session,
        actor_user_id=actor.id,
        submission_id=command.submission_id,
    )
    if replay:
        if any(
            job.cost_metadata.get("submission_request_sha256") != submission_request_sha256
            for job in replay
        ):
            raise VideoStudioConflictError("this form was already submitted with different choices")
        if len(replay) != command.variant_count:
            raise VideoStudioConflictError("the saved submission is incomplete")
        return _submission_read(command.submission_id, replay)

    fps, frame_count = _video_timing(command.duration_seconds)
    estimate_microusd = _estimated_cost_microusd(
        max_hourly_cost_usd=max_hourly_cost_usd,
        duration_seconds=command.duration_seconds,
    )
    jobs: list[VideoGenerationJob] = []
    for variant_index in range(1, command.variant_count + 1):
        seed = _variant_seed(
            submission_request_sha256=submission_request_sha256,
            variant_index=variant_index,
        )
        parameters = VideoGenerationParameters(
            prompt=command.prompt,
            # The UI intentionally has one concise motion field.  Keep the
            # second worker field server-owned and within the same 4,000-byte
            # contract so callers cannot smuggle a larger hidden prompt.
            negative_prompt=DEFAULT_VIDEO_NEGATIVE_PROMPT,
            profile_key=PINNED_VIDEO_PROFILE.profile_id,
            profile_version=VIDEO_PROFILE_VERSION,
            profile_sha256=PINNED_VIDEO_PROFILE_SHA256,
            seed=seed,
            frame_count=frame_count,
            fps=fps,
            width=width,
            height=height,
        )
        request = VideoGenerationRequest(
            source=snapshot,
            parameters=parameters,
            content_rating=command.content_rating,
            compliance=compliance,
        )
        job = VideoGenerationJob(
            source_asset_id=snapshot.asset_id,
            created_by_user_id=actor.id,
            source_storage_backend=snapshot.storage_backend,
            source_storage_bucket=snapshot.storage_bucket,
            source_object_key=snapshot.object_key,
            source_object_version_id=snapshot.object_version_id,
            source_sha256=snapshot.sha256,
            source_content_type=snapshot.content_type,
            source_image_format=snapshot.image_format,
            source_width=snapshot.width,
            source_height=snapshot.height,
            source_byte_size=snapshot.byte_size,
            prompt=parameters.prompt,
            negative_prompt=parameters.negative_prompt,
            profile_key=parameters.profile_key,
            profile_version=parameters.profile_version,
            profile_sha256=parameters.profile_sha256,
            seed=parameters.seed,
            frame_count=parameters.frame_count,
            fps=parameters.fps,
            width=parameters.width,
            height=parameters.height,
            loop_mode=parameters.loop_mode,
            content_rating=request.content_rating,
            attestation_policy_version=request.compliance.policy_version,
            source_rights_confirmed=request.compliance.source_rights_confirmed,
            lawful_use_confirmed=request.compliance.lawful_use_confirmed,
            all_depicted_people_are_adults=(request.compliance.all_depicted_people_are_adults),
            consensual_adult_content_confirmed=(
                request.compliance.consensual_adult_content_confirmed
            ),
            no_real_person_sexual_content=(request.compliance.no_real_person_sexual_content),
            request_sha256=request.request_sha256,
            provider="salad",
            state=VideoGenerationState.QUEUED,
            priority=100,
            attempt_count=0,
            max_attempts=3,
            lock_version=1,
            estimated_cost_microusd=estimate_microusd,
            reserved_cost_microusd=0,
            actual_cost_microusd=0,
            billed_duration_ms=0,
            cost_metadata={
                "schema": "video-cost-plan/v1",
                "submission_id": str(command.submission_id),
                "submission_request_sha256": submission_request_sha256,
                "variant_index": variant_index,
                "variant_count": command.variant_count,
                "requested_duration_seconds": command.duration_seconds,
                "estimate_basis": "configured-hourly-cap-and-planning-window",
                "estimated_runtime_seconds": _estimated_runtime_seconds(command.duration_seconds),
            },
            output=None,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(job)
        jobs.append(job)
    await session.flush()
    return _submission_read(command.submission_id, jobs)


async def load_video_submission(
    session: AsyncSession,
    *,
    job_id: UUID,
    actor_user_id: UUID,
) -> VideoSubmissionRead:
    await _require_actor(session, actor_user_id=actor_user_id, lock=False)
    primary = await session.scalar(
        select(VideoGenerationJob)
        .options(selectinload(VideoGenerationJob.output))
        .where(
            VideoGenerationJob.id == job_id,
            VideoGenerationJob.created_by_user_id == actor_user_id,
        )
    )
    if primary is None:
        raise VideoStudioNotFoundError("animation job was not found")
    try:
        submission_id = UUID(str(primary.cost_metadata["submission_id"]))
    except (KeyError, TypeError, ValueError):
        raise VideoStudioConflictError("animation submission metadata is invalid") from None
    jobs = await _load_submission_by_id(
        session,
        actor_user_id=actor_user_id,
        submission_id=submission_id,
    )
    if not jobs or all(job.id != primary.id for job in jobs):
        raise VideoStudioConflictError("animation submission metadata is inconsistent")
    return _submission_read(submission_id, jobs)


async def presign_video_output(
    session: AsyncSession,
    store: ObjectStore,
    *,
    job_id: UUID,
    actor_user_id: UUID,
    expires_in: int,
) -> str:
    submission = await load_video_submission(
        session,
        job_id=job_id,
        actor_user_id=actor_user_id,
    )
    job = next((item for item in submission.jobs if item.id == job_id), None)
    if job is None or job.output is None or job.state != VideoGenerationState.SUCCEEDED:
        raise VideoStudioConflictError("animation output is not available")
    output = await session.scalar(
        select(VideoGenerationOutput).where(VideoGenerationOutput.video_generation_job_id == job_id)
    )
    if output is None or output.content_type != "video/mp4":
        raise VideoStudioConflictError("animation output is invalid")
    asset = await session.get(Asset, output.asset_id)
    if asset is None:
        raise VideoStudioConflictError("animation output asset is unavailable")
    current_identity = (
        asset.storage_backend,
        asset.storage_bucket,
        asset.object_key,
        asset.object_version_id,
        asset.sha256,
        asset.content_type,
        asset.width,
        asset.height,
        asset.byte_size,
    )
    frozen_identity = (
        output.storage_backend,
        output.storage_bucket,
        output.object_key,
        output.object_version_id,
        output.sha256,
        output.content_type,
        output.width,
        output.height,
        output.byte_size,
    )
    if current_identity != frozen_identity:
        raise VideoStudioConflictError("animation output identity changed")
    try:
        return await presign_asset_download(
            session,
            store,
            asset_id=output.asset_id,
            expires_in=expires_in,
            download_name=None,
        )
    except AssetNotFoundError as error:
        raise VideoStudioNotFoundError("animation output was not found") from error
    except AssetConflictError as error:
        raise VideoStudioConflictError("animation output is unavailable") from error
    except AssetStorageUnavailableError as error:
        raise VideoStudioStorageError("animation output storage is unavailable") from error


def planning_estimate_microusd(
    *,
    max_hourly_cost_usd: Decimal,
    duration_seconds: int,
    variant_count: int,
) -> int:
    if duration_seconds not in VIDEO_DURATION_SECONDS:
        raise VideoStudioInputError("duration must be 3 or 5 seconds")
    if not MIN_VIDEO_VARIANTS <= variant_count <= MAX_VIDEO_VARIANTS:
        raise VideoStudioInputError("variant count must be between 1 and 3")
    return (
        _estimated_cost_microusd(
            max_hourly_cost_usd=max_hourly_cost_usd,
            duration_seconds=duration_seconds,
        )
        * variant_count
    )


def format_microusd(value: int) -> str:
    return f"${Decimal(value) / Decimal(1_000_000):.3f}"


def _validate_command(command: CreateVideoSubmission) -> None:
    if not isinstance(command.prompt, str) or len(command.prompt) > MAX_VIDEO_PROMPT_LENGTH:
        raise VideoStudioInputError(
            f"motion prompt must be at most {MAX_VIDEO_PROMPT_LENGTH} characters"
        )
    if command.prompt != command.prompt.strip():
        raise VideoStudioInputError("motion prompt must not have outer whitespace")
    if command.duration_seconds not in VIDEO_DURATION_SECONDS:
        raise VideoStudioInputError("duration must be 3 or 5 seconds")
    if not MIN_VIDEO_VARIANTS <= command.variant_count <= MAX_VIDEO_VARIANTS:
        raise VideoStudioInputError("variant count must be between 1 and 3")
    try:
        _compliance(command)
    except ValidationError as error:
        message = str(error.errors(include_url=False, include_input=False)[0]["msg"])
        raise VideoStudioInputError(message.removeprefix("Value error, ")) from None


def _compliance(command: CreateVideoSubmission) -> VideoComplianceAttestations:
    compliance = VideoComplianceAttestations(
        source_rights_confirmed=command.source_rights_confirmed,
        lawful_use_confirmed=command.lawful_use_confirmed,
        all_depicted_people_are_adults=command.all_depicted_people_are_adults,
        consensual_adult_content_confirmed=(command.consensual_adult_content_confirmed),
        no_real_person_sexual_content=command.no_real_person_sexual_content,
    )
    # The request model owns the rating-dependent attestation rule.  Build a
    # small valid parameter object solely to run that shared invariant here;
    # actual parameters are derived after the source is locked.
    VideoGenerationRequest(
        source=VideoSourceSnapshot(
            asset_id=UUID(int=0),
            storage_backend="validation",
            storage_bucket="validation",
            object_key="validation",
            sha256="0" * 64,
            content_type="image/png",
            image_format="png",
            width=64,
            height=64,
            byte_size=1,
        ),
        parameters=VideoGenerationParameters(
            profile_key=PINNED_VIDEO_PROFILE.profile_id,
            profile_version=VIDEO_PROFILE_VERSION,
            profile_sha256=PINNED_VIDEO_PROFILE_SHA256,
            seed=0,
            frame_count=73,
            fps=24,
            width=832,
            height=480,
        ),
        content_rating=command.content_rating,
        compliance=compliance,
    )
    return compliance


def _source_snapshot(asset: Asset) -> VideoSourceSnapshot:
    if asset.kind != AssetKind.RAW_MASTER or asset.state != AssetState.AVAILABLE:
        raise VideoStudioConflictError("source image is not an available library master")
    if asset.content_type not in VIDEO_SOURCE_CONTENT_TYPES:
        raise VideoStudioConflictError("source image type is not supported")
    values = (
        asset.object_key,
        asset.object_version_id,
        asset.sha256,
        asset.content_type,
        asset.image_format,
        asset.width,
        asset.height,
        asset.byte_size,
    )
    if any(value is None for value in values):
        raise VideoStudioConflictError("source image identity is incomplete")
    try:
        return VideoSourceSnapshot(
            asset_id=asset.id,
            storage_backend=asset.storage_backend,
            storage_bucket=asset.storage_bucket,
            object_key=str(asset.object_key),
            object_version_id=str(asset.object_version_id),
            sha256=str(asset.sha256),
            content_type=str(asset.content_type),
            image_format=str(asset.image_format),
            width=int(asset.width or 0),
            height=int(asset.height or 0),
            byte_size=int(asset.byte_size or 0),
        )
    except ValidationError as error:
        raise VideoStudioConflictError("source image identity is invalid") from error


def _derive_video_dimensions(
    source_width: int,
    source_height: int,
) -> tuple[Literal[832, 480], Literal[480, 832]]:
    if source_width <= 0 or source_height <= 0:
        raise VideoStudioConflictError("source image dimensions are invalid")
    aspect_ratio = source_width / source_height
    if not 1 / 3.25 <= aspect_ratio <= 3.25:
        raise VideoStudioConflictError("source image aspect ratio is outside the animation profile")
    if source_width >= source_height:
        return 832, 480
    return 480, 832


def _validate_source_worker_limits(snapshot: VideoSourceSnapshot) -> None:
    """Reject sources the pinned worker would deterministically refuse.

    The browser and controller share the worker's exact default bounds so an
    otherwise valid library master cannot consume retries or a GPU allocation
    merely to discover that it is too large to decode safely.
    """

    if snapshot.byte_size > DEFAULT_MAX_SOURCE_BYTES:
        raise VideoStudioConflictError("source image exceeds the animation byte limit")
    if (
        snapshot.width > DEFAULT_MAX_IMAGE_DIMENSION
        or snapshot.height > DEFAULT_MAX_IMAGE_DIMENSION
        or snapshot.width * snapshot.height > DEFAULT_MAX_IMAGE_PIXELS
    ):
        raise VideoStudioConflictError("source image exceeds the animation pixel limit")


def _video_timing(
    duration_seconds: int,
) -> tuple[Literal[24], Literal[73, 121]]:
    # Wan video latents are most efficient with 4n+1 frames.  The player loops
    # the short base clip, so these are intentionally near 3s/5s rather than a
    # costly long-form render.
    if duration_seconds == 3:
        return 24, 73
    return 24, 121


def _requested_duration_from_frames(frame_count: int, fps: int) -> int:
    if (frame_count, fps) == (73, PINNED_VIDEO_PROFILE.fps):
        return 3
    if (frame_count, fps) == (121, PINNED_VIDEO_PROFILE.fps):
        return 5
    return max(1, round(frame_count / fps))


def _variant_seed(*, submission_request_sha256: str, variant_index: int) -> int:
    material = f"{submission_request_sha256}:variant:{variant_index}".encode("ascii")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & ((1 << 63) - 1)


def _estimated_runtime_seconds(duration_seconds: int) -> int:
    # Conservative planning window around the published Wan 2.2 benchmark;
    # actual billing is reconciled from the Salad attempt instead of this cap.
    return 360 if duration_seconds == 3 else 600


def _estimated_cost_microusd(
    *,
    max_hourly_cost_usd: Decimal,
    duration_seconds: int,
) -> int:
    if max_hourly_cost_usd <= 0:
        raise VideoStudioInputError("configured video hourly cost must be positive")
    estimate = (
        max_hourly_cost_usd
        * Decimal(_estimated_runtime_seconds(duration_seconds))
        / Decimal(3600)
        * Decimal(1_000_000)
    )
    return int(estimate.to_integral_value(rounding=ROUND_CEILING))


async def _require_actor(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    lock: bool,
) -> AdminUser:
    statement = select(AdminUser).where(AdminUser.id == actor_user_id)
    if lock:
        statement = statement.with_for_update()
    actor = await session.scalar(statement)
    if actor is None or not actor.is_active or actor.role not in {AdminRole.OWNER, AdminRole.ADMIN}:
        raise VideoStudioNotFoundError("animation operator was not found")
    return actor


async def _load_submission_by_id(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    submission_id: UUID,
) -> list[VideoGenerationJob]:
    return list(
        (
            await session.scalars(
                select(VideoGenerationJob)
                .options(selectinload(VideoGenerationJob.output))
                .where(
                    VideoGenerationJob.created_by_user_id == actor_user_id,
                    VideoGenerationJob.cost_metadata["submission_id"].as_string()
                    == str(submission_id),
                )
                .order_by(VideoGenerationJob.created_at, VideoGenerationJob.id)
            )
        ).all()
    )


def _submission_read(
    submission_id: UUID,
    jobs: list[VideoGenerationJob],
) -> VideoSubmissionRead:
    reads = tuple(sorted((_job_read(job) for job in jobs), key=lambda item: item.variant_index))
    expected_count = len(reads)
    if not reads or any(
        item.variant_count != expected_count or item.variant_index != index
        for index, item in enumerate(reads, start=1)
    ):
        raise VideoStudioConflictError("animation submission variants are inconsistent")
    return VideoSubmissionRead(submission_id=submission_id, jobs=reads)


def _job_read(job: VideoGenerationJob) -> VideoJobRead:
    metadata = job.cost_metadata
    try:
        variant_index = int(metadata["variant_index"])
        variant_count = int(metadata["variant_count"])
    except (KeyError, TypeError, ValueError):
        raise VideoStudioConflictError("animation variant metadata is invalid") from None
    output = job.output
    output_read = (
        VideoOutputRead(
            asset_id=output.asset_id,
            width=output.width,
            height=output.height,
            byte_size=output.byte_size,
            duration_ms=output.duration_ms,
        )
        if output is not None
        else None
    )
    return VideoJobRead(
        id=job.id,
        variant_index=variant_index,
        variant_count=variant_count,
        source_asset_id=job.source_asset_id,
        source_sha256=job.source_sha256,
        prompt=job.prompt,
        content_rating=job.content_rating,
        state=job.state,
        width=job.width,
        height=job.height,
        frame_count=job.frame_count,
        fps=job.fps,
        estimated_cost_microusd=job.estimated_cost_microusd,
        reserved_cost_microusd=job.reserved_cost_microusd,
        actual_cost_microusd=job.actual_cost_microusd,
        billed_duration_ms=job.billed_duration_ms,
        created_at=job.created_at,
        completed_at=job.completed_at,
        error_message=_safe_error_message(job),
        output=output_read,
    )


def _safe_error_message(job: VideoGenerationJob) -> str | None:
    if job.state not in {VideoGenerationState.FAILED, VideoGenerationState.CANCELLED}:
        return None
    if job.state == VideoGenerationState.CANCELLED:
        return "This animation was cancelled."
    messages = {
        "source_unavailable": "The source image was unavailable to the worker.",
        "render_failed": "The animation could not be rendered.",
        "output_invalid": "The rendered video did not pass verification.",
        "budget_blocked": "The animation paused at the configured spend limit.",
    }
    return messages.get(job.last_error_code or "", "The animation could not be completed.")


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise VideoStudioInputError("source list limit must be between 1 and 200")
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
