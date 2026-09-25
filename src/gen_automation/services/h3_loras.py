"""Resolve owner-managed H3 selections against immutable, verified library rows."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import ManagedLoraArtifact, ModelArtifactApproval
from gen_automation.domain.enums import (
    ApprovalStatus,
    ManagedLoraLifecycle,
    ModelArtifactFamily,
    ModelArtifactKind,
)
from gen_automation.domain.lora_catalog import managed_lora_model_family
from gen_automation.i2v_worker.models import H3LoraSelection


class H3LoraUnavailableError(ValueError):
    pass


async def resolve_h3_loras(
    session: AsyncSession,
    settings: Mapping[str, Any],
    *,
    actor_user_id: UUID,
    existing_job: bool = False,
) -> tuple[ManagedLoraArtifact, ...]:
    selections = TypeAdapter(list[H3LoraSelection]).validate_python(settings.get("h3_loras", []))
    if not selections:
        return ()
    if settings.get("profile") != "minimax_h3":
        raise H3LoraUnavailableError("H3 LoRAs require the MiniMax H3 profile")
    allowed = {ManagedLoraLifecycle.ACTIVE}
    if existing_job:
        # Deletion hides a file from new jobs immediately, but existing queued
        # jobs retain their exact bytes until completion/cancellation.
        allowed |= {ManagedLoraLifecycle.RETIRING, ManagedLoraLifecycle.RETIRED}
    rows = list(
        await session.scalars(
            select(ManagedLoraArtifact)
            .where(ManagedLoraArtifact.id.in_([item.artifact_id for item in selections]))
            .order_by(ManagedLoraArtifact.id)
            .with_for_update()
        )
    )
    by_id = {row.id: row for row in rows}
    for selection in selections:
        row = by_id.get(selection.artifact_id)
        if row is None or row.registered_by_user_id != actor_user_id:
            raise H3LoraUnavailableError("A selected H3 LoRA is unavailable in your library")
        approval = await session.get(ModelArtifactApproval, row.approval_id)
        if (
            row.lifecycle not in allowed
            or row.artifact_sha256 != selection.sha256
            or managed_lora_model_family(row.provenance) != ModelArtifactFamily.MINIMAX_H3
            or approval is None
            or approval.model_family != ModelArtifactFamily.MINIMAX_H3
            or approval.kind != ModelArtifactKind.LORA
            or approval.status != ApprovalStatus.APPROVED
            or not approval.is_current
            or not approval.safetensors_verified
            or approval.artifact_sha256 != row.artifact_sha256
            or approval.storage_key != row.object_key
            or not row.object_version_id
        ):
            raise H3LoraUnavailableError(
                "A selected H3 LoRA was removed, changed or is not verified; refresh the library"
            )
    return tuple(by_id[item.artifact_id] for item in selections)
