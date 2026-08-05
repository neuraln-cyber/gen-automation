"""Durable, lock-version-independent fullscreen review inspections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    AssetRanking,
    ReviewAssetInspection,
    ReviewTask,
)
from gen_automation.domain.enums import AdminRole, ReviewTaskState
from gen_automation.domain.ids import uuid7
from gen_automation.services.review import (
    ReviewConflictError,
    ReviewInputError,
    ReviewNotFoundError,
)

MAX_REVIEW_INSPECTION_ASSETS = 500


@dataclass(frozen=True, slots=True)
class ReviewInspectionResult:
    task_id: UUID
    inspected_asset_ids: tuple[UUID, ...]
    created_count: int


async def record_review_inspections(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    asset_ids: Sequence[UUID],
    inspected_by_user_id: UUID,
    now: datetime | None = None,
) -> ReviewInspectionResult:
    """Union one bounded inspection batch into an open task without touching its lock.

    The unique task/asset/user key makes retries and overlapping batches naturally
    idempotent. The task row is locked only for the short insert transaction so a
    completion cannot race ahead of an inspection flush; its lock version is never
    read as a precondition and is never changed.
    """

    normalized_asset_ids = _asset_ids(asset_ids)
    inspected_at = _as_utc(now or datetime.now(UTC))
    actor = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == inspected_by_user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role.in_((AdminRole.OWNER, AdminRole.REVIEWER)),
        )
    )
    if actor is None:
        raise ReviewNotFoundError("authorized review actor was not found")

    task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if task is None:
        raise ReviewNotFoundError("review task was not found")
    if task.state != ReviewTaskState.OPEN:
        raise ReviewConflictError("review task is not open")

    ranked_asset_ids = set(
        await session.scalars(
            select(AssetRanking.asset_id).where(
                AssetRanking.scoring_run_id == task.scoring_run_id,
                AssetRanking.asset_id.in_(normalized_asset_ids),
            )
        )
    )
    if ranked_asset_ids != set(normalized_asset_ids):
        raise ReviewConflictError("inspection asset is not part of the review ranking")

    existing_asset_ids = set(
        await session.scalars(
            select(ReviewAssetInspection.asset_id).where(
                ReviewAssetInspection.review_task_id == task.id,
                ReviewAssetInspection.inspected_by_user_id == inspected_by_user_id,
                ReviewAssetInspection.asset_id.in_(normalized_asset_ids),
            )
        )
    )
    new_asset_ids = tuple(
        asset_id for asset_id in normalized_asset_ids if asset_id not in existing_asset_ids
    )
    created_count = 0
    if new_asset_ids:
        values = [
            {
                "id": uuid7(),
                "review_task_id": task.id,
                "scoring_run_id": task.scoring_run_id,
                "asset_id": asset_id,
                "inspected_by_user_id": inspected_by_user_id,
                "inspected_at": inspected_at,
            }
            for asset_id in new_asset_ids
        ]
        dialect = session.get_bind().dialect.name
        conflict_columns = ("review_task_id", "asset_id", "inspected_by_user_id")
        if dialect == "postgresql":
            statement = postgresql_insert(ReviewAssetInspection).values(values)
            statement = statement.on_conflict_do_nothing(index_elements=conflict_columns)
            inserted_asset_ids = await session.scalars(
                statement.returning(ReviewAssetInspection.asset_id)
            )
            created_count = len(tuple(inserted_asset_ids))
        elif dialect == "sqlite":
            sqlite_statement = sqlite_insert(ReviewAssetInspection).values(values)
            sqlite_statement = sqlite_statement.on_conflict_do_nothing(
                index_elements=conflict_columns
            )
            inserted_asset_ids = await session.scalars(
                sqlite_statement.returning(ReviewAssetInspection.asset_id)
            )
            created_count = len(tuple(inserted_asset_ids))
        else:
            session.add_all([ReviewAssetInspection(**value) for value in values])
            await session.flush()
            created_count = len(new_asset_ids)
    await session.commit()
    return ReviewInspectionResult(
        task_id=task.id,
        inspected_asset_ids=normalized_asset_ids,
        created_count=created_count,
    )


async def load_review_inspected_asset_ids(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    inspected_by_user_id: UUID,
) -> frozenset[UUID]:
    return frozenset(
        await session.scalars(
            select(ReviewAssetInspection.asset_id).where(
                ReviewAssetInspection.review_task_id == review_task_id,
                ReviewAssetInspection.inspected_by_user_id == inspected_by_user_id,
            )
        )
    )


def _asset_ids(values: Sequence[UUID]) -> tuple[UUID, ...]:
    if isinstance(values, (str, bytes)) or not 1 <= len(values) <= MAX_REVIEW_INSPECTION_ASSETS:
        raise ReviewInputError(
            f"asset_ids must contain between 1 and {MAX_REVIEW_INSPECTION_ASSETS} assets"
        )
    normalized = tuple(values)
    if any(not isinstance(asset_id, UUID) for asset_id in normalized):
        raise ReviewInputError("asset_ids must contain UUID values")
    if len(set(normalized)) != len(normalized):
        raise ReviewInputError("asset_ids must not contain duplicates")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewInputError("inspection timestamps must be timezone-aware")
    return value.astimezone(UTC)
