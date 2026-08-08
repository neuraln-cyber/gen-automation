"""Authenticated access to exact, rendered X teaser derivatives.

The browser routes using this service must never become a generic derivative
download surface.  Every lookup is constrained to one completed review, its
currently selected X teaser target, and the immutable output/storage snapshot
recorded by the derivative worker.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    DerivativeJob,
    DerivativeOutput,
    ReleaseSelection,
    ReviewXSelection,
    XTeaserRevisionHead,
    XTeaserRevisionMember,
)
from gen_automation.domain.deliverability import X_STATIC_IMAGE_MAX_BYTES
from gen_automation.domain.enums import AssetKind, AssetState, DerivativeJobState
from gen_automation.storage.base import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)
from gen_automation.storage.images import FORMAT_CONTENT_TYPES

_SAFE_FORMAT = re.compile(r"^[A-Za-z0-9]{1,12}$")
_X_TEASER_TARGET = "x_teaser"


class XTeaserAccessError(Exception):
    """Base error for private X teaser access."""


class XTeaserAccessNotFoundError(XTeaserAccessError):
    pass


class XTeaserAccessConflictError(XTeaserAccessError):
    pass


class XTeaserAccessStorageError(XTeaserAccessError):
    pass


@dataclass(frozen=True, slots=True)
class XTeaserPayload:
    output_id: UUID
    display_order: int
    content_type: str
    image_format: str
    width: int
    height: int
    data: bytes

    @property
    def download_name(self) -> str:
        extension = self.image_format.lower()
        if _SAFE_FORMAT.fullmatch(extension) is None:
            extension = "img"
        return f"x-teaser-{self.display_order:04d}-{self.output_id}.{extension}"


async def read_review_x_teaser(
    session: AsyncSession,
    store: ObjectStore,
    *,
    review_task_id: UUID,
    output_id: UUID,
) -> XTeaserPayload:
    """Read one exact metadata-free teaser belonging to the requested review."""

    row = (
        await session.execute(
            select(DerivativeOutput, DerivativeJob, ReleaseSelection, Asset)
            .join(DerivativeJob, DerivativeJob.id == DerivativeOutput.derivative_job_id)
            .join(ReleaseSelection, ReleaseSelection.id == DerivativeOutput.release_selection_id)
            .join(
                ReviewXSelection,
                and_(
                    ReviewXSelection.review_task_id == ReleaseSelection.review_task_id,
                    ReviewXSelection.asset_id == ReleaseSelection.asset_id,
                ),
            )
            .join(Asset, Asset.id == DerivativeOutput.asset_id)
            .where(
                DerivativeOutput.id == output_id,
                DerivativeOutput.target == _X_TEASER_TARGET,
                ReleaseSelection.review_task_id == review_task_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise XTeaserAccessNotFoundError("X teaser was not found")
    output, job, selection, asset = row
    revision_head = await session.scalar(
        select(XTeaserRevisionHead).where(XTeaserRevisionHead.review_task_id == review_task_id)
    )
    if revision_head is None or revision_head.active_revision_id is None:
        raise XTeaserAccessNotFoundError("X teaser was not found")
    active_member = await session.scalar(
        select(XTeaserRevisionMember).where(
            XTeaserRevisionMember.revision_id == revision_head.active_revision_id,
            XTeaserRevisionMember.release_selection_id == selection.id,
        )
    )
    if active_member is None or not (
        active_member.derivative_output_id == output.id or active_member.derivative_job_id == job.id
    ):
        raise XTeaserAccessNotFoundError("X teaser was not found")
    _validate_snapshot(output=output, job=job, selection=selection, asset=asset, store=store)

    try:
        data = await store.read_bytes(
            output.asset_object_key,
            max_bytes=output.asset_byte_size,
            version_id=output.asset_object_version_id,
        )
    except (ObjectNotFoundError, ObjectTooLargeError) as error:
        raise XTeaserAccessConflictError("X teaser object is no longer available") from error
    except ObjectStoreError as error:
        raise XTeaserAccessStorageError("X teaser storage is unavailable") from error
    if len(data) != output.asset_byte_size or not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(),
        output.asset_sha256,
    ):
        raise XTeaserAccessConflictError("X teaser object identity changed")
    return XTeaserPayload(
        output_id=output.id,
        display_order=selection.display_order,
        content_type=output.asset_content_type,
        image_format=output.asset_image_format,
        width=output.asset_width,
        height=output.asset_height,
        data=data,
    )


def _validate_snapshot(
    *,
    output: DerivativeOutput,
    job: DerivativeJob,
    selection: ReleaseSelection,
    asset: Asset,
    store: ObjectStore,
) -> None:
    metadata = asset.asset_metadata
    format_details = FORMAT_CONTENT_TYPES.get(output.asset_image_format)
    if (
        job.state != DerivativeJobState.SUCCEEDED
        or job.release_selection_id != selection.id
        or job.release_version_id != selection.release_version_id
        or output.source_asset_id != selection.asset_id
        or output.target != _X_TEASER_TARGET
        or asset.kind != AssetKind.DERIVATIVE
        or asset.state != AssetState.AVAILABLE
        or asset.id != output.asset_id
        or asset.storage_backend != store.backend
        or asset.storage_bucket != store.bucket
        or asset.storage_backend != output.asset_storage_backend
        or asset.storage_bucket != output.asset_storage_bucket
        or asset.object_key != output.asset_object_key
        or asset.object_version_id != output.asset_object_version_id
        or asset.sha256 != output.asset_sha256
        or asset.content_type != output.asset_content_type
        or asset.image_format != output.asset_image_format
        or asset.width != output.asset_width
        or asset.height != output.asset_height
        or asset.byte_size != output.asset_byte_size
        or output.asset_byte_size <= 0
        or output.asset_byte_size > X_STATIC_IMAGE_MAX_BYTES
        or format_details is None
        or format_details[0] != output.asset_content_type
        or metadata.get("schema") != "derivative-output/v1"
        or metadata.get("target") != _X_TEASER_TARGET
        or metadata.get("derivative_job_id") != str(job.id)
        or metadata.get("release_selection_id") != str(selection.id)
    ):
        raise XTeaserAccessConflictError("X teaser output snapshot is invalid")
