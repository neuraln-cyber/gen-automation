from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from gen_automation.api.security import PublicationReader, Session
from gen_automation.db.models import (
    FinishedSetArchive,
    MegaDelivery,
    MegaSetDelivery,
    MegaSetDeliveryItem,
    PublicationIntent,
    PublicationPackage,
    ReleaseVersion,
)
from gen_automation.domain.mega import (
    MegaDeliveryRead,
    MegaSetDeliveryItemRead,
    MegaSetDeliveryRead,
)

router = APIRouter(tags=["mega-deliveries"])


@router.get(
    "/mega-deliveries/{delivery_id}",
    response_model=MegaDeliveryRead,
)
async def get_mega_delivery(
    delivery_id: UUID,
    session: Session,
    _principal: PublicationReader,
) -> MegaDeliveryRead:
    delivery = await session.get(MegaDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MEGA delivery was not found",
        )
    return _read(delivery)


@router.get(
    "/releases/{release_id}/mega-deliveries",
    response_model=list[MegaDeliveryRead],
)
async def get_release_mega_deliveries(
    release_id: UUID,
    session: Session,
    _principal: PublicationReader,
) -> list[MegaDeliveryRead]:
    deliveries = (
        await session.scalars(
            select(MegaDelivery)
            .join(
                PublicationPackage,
                PublicationPackage.id == MegaDelivery.publication_package_id,
            )
            .join(
                PublicationIntent,
                PublicationIntent.id == PublicationPackage.intent_id,
            )
            .where(PublicationIntent.release_id == release_id)
            .order_by(MegaDelivery.created_at, MegaDelivery.id)
        )
    ).all()
    return [_read(delivery) for delivery in deliveries]


@router.get(
    "/mega-set-deliveries/{delivery_id}",
    response_model=MegaSetDeliveryRead,
)
async def get_mega_set_delivery(
    delivery_id: UUID,
    session: Session,
    _principal: PublicationReader,
) -> MegaSetDeliveryRead:
    delivery = await session.scalar(
        select(MegaSetDelivery)
        .options(selectinload(MegaSetDelivery.items))
        .where(MegaSetDelivery.id == delivery_id)
    )
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MEGA set delivery was not found",
        )
    return _read_set(delivery)


@router.get(
    "/releases/{release_id}/mega-set-deliveries",
    response_model=list[MegaSetDeliveryRead],
)
async def get_release_mega_set_deliveries(
    release_id: UUID,
    session: Session,
    _principal: PublicationReader,
) -> list[MegaSetDeliveryRead]:
    deliveries = (
        await session.scalars(
            select(MegaSetDelivery)
            .options(selectinload(MegaSetDelivery.items))
            .join(
                FinishedSetArchive,
                FinishedSetArchive.id == MegaSetDelivery.finished_set_archive_id,
            )
            .join(
                ReleaseVersion,
                ReleaseVersion.id == FinishedSetArchive.release_version_id,
            )
            .where(ReleaseVersion.release_id == release_id)
            .order_by(MegaSetDelivery.created_at, MegaSetDelivery.id)
        )
    ).all()
    return [_read_set(delivery) for delivery in deliveries]


def _read(delivery: MegaDelivery) -> MegaDeliveryRead:
    return MegaDeliveryRead(
        id=delivery.id,
        publication_package_id=delivery.publication_package_id,
        state=delivery.state,
        remote_path=delivery.remote_path,
        sha256=delivery.sha256,
        byte_size=delivery.byte_size,
        attempts=delivery.attempts,
        available_at=delivery.available_at,
        remote_node_handle=delivery.remote_node_handle,
        verified_at=delivery.verified_at,
        completed_at=delivery.completed_at,
        last_error_code=delivery.last_error_code,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
    )


def _read_set(delivery: MegaSetDelivery) -> MegaSetDeliveryRead:
    return MegaSetDeliveryRead(
        id=delivery.id,
        finished_set_archive_id=delivery.finished_set_archive_id,
        state=delivery.state,
        remote_root=delivery.remote_root,
        remote_folder=delivery.remote_folder,
        manifest_sha256=delivery.manifest_sha256,
        total_item_count=delivery.total_item_count,
        uploaded_item_count=delivery.uploaded_item_count,
        total_byte_size=delivery.total_byte_size,
        uploaded_byte_size=delivery.uploaded_byte_size,
        attempts=delivery.attempts,
        available_at=delivery.available_at,
        completion_marker_node_handle=delivery.completion_marker_node_handle,
        planned_at=delivery.planned_at,
        started_at=delivery.started_at,
        verified_at=delivery.verified_at,
        completed_at=delivery.completed_at,
        last_error_code=delivery.last_error_code,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
        items=tuple(
            _read_set_item(item) for item in sorted(delivery.items, key=lambda row: row.ordinal)
        ),
    )


def _read_set_item(item: MegaSetDeliveryItem) -> MegaSetDeliveryItemRead:
    return MegaSetDeliveryItemRead(
        id=item.id,
        delivery_id=item.delivery_id,
        ordinal=item.ordinal,
        source_asset_id=item.source_asset_id,
        readiness_derivative_output_id=item.readiness_derivative_output_id,
        source_sha256=item.source_sha256,
        source_byte_size=item.source_byte_size,
        source_content_type=item.source_content_type,
        remote_path=item.remote_path,
        state=item.state,
        attempts=item.attempts,
        available_at=item.available_at,
        remote_node_handle=item.remote_node_handle,
        uploaded_at=item.uploaded_at,
        verified_at=item.verified_at,
        completed_at=item.completed_at,
        last_error_code=item.last_error_code,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
