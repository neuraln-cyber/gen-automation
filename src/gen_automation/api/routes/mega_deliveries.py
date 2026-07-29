from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from gen_automation.api.security import PublicationReader, Session
from gen_automation.db.models import (
    MegaDelivery,
    PublicationIntent,
    PublicationPackage,
)
from gen_automation.domain.mega import MegaDeliveryRead

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
