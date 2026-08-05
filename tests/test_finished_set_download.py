# ruff: noqa: F811

from dataclasses import replace
from datetime import timedelta
from urllib.parse import unquote
from uuid import uuid4

import pytest
from sqlalchemy import select

from gen_automation.db.models import AdminUser, AuditEvent, PublicationIntent, PublicationPackage
from gen_automation.domain.enums import AdminRole, PublicationIntentState
from gen_automation.services.publication import (
    PUBLICATION_CONFIRM_PRESENT_ATTESTATION,
    PublicationConflictError,
    PublicationNotFoundError,
    get_publication_guard,
    presign_finished_set_package_download,
    reconcile_publication_present,
    set_publication_guard,
)
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_patreon_recovery import (
    UnknownPatreonContext,
    unknown_patreon_context,  # noqa: F401
)


@pytest.mark.asyncio
async def test_owner_downloads_published_finished_set_with_guard_off_and_expired_approval(
    unknown_patreon_context: UnknownPatreonContext,
) -> None:
    context = unknown_patreon_context
    published_at = context.action_at + timedelta(minutes=2)
    async with context.approved.database.sessions() as session:
        published = await reconcile_publication_present(
            session,
            intent_id=context.intent_id,
            expected_intent_digest=context.intent_digest,
            expected_lock_version=context.intent_lock_version,
            remote_identifier="123456789",
            remote_url="https://www.patreon.com/posts/finished-set-123456789",
            evidence="Published package confirmed before owner download test.",
            attestation=PUBLICATION_CONFIRM_PRESENT_ATTESTATION,
            actor_user_id=context.approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="finished-set-confirm-present",
            now=published_at,
        )
        guard = await get_publication_guard(session)
        stopped = await set_publication_guard(
            session,
            enabled=False,
            expected_epoch=guard.epoch,
            expected_lock_version=guard.lock_version,
            reason="Finished-set downloads are independent of publication effects.",
            actor_user_id=context.approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="finished-set-stop-publication",
            now=published_at + timedelta(minutes=1),
        )
        assert not stopped.enabled

        downloaded_at = context.action_at + timedelta(hours=2)
        result = await presign_finished_set_package_download(
            session,
            context.prepared.store,
            review_task_id=context.approved.review_task_id,
            intent_id=context.intent_id,
            expected_intent_digest=context.intent_digest,
            expected_lock_version=published.lock_version,
            actor_user_id=context.approved.owner_id,
            actor_role=AdminRole.OWNER,
            expires_in_seconds=600,
            now=downloaded_at,
        )
        intent = await session.get(PublicationIntent, context.intent_id)
        audit = await session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.resource_id == context.intent_id,
                AuditEvent.action == "review.finished_set_package_download_authorized",
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(1)
        )

    expected_filename = f"finished-ranked-set-{context.approved.review_task_id}.zip"
    assert intent is not None and intent.state == PublicationIntentState.PUBLISHED
    assert result.review_task_id == context.approved.review_task_id
    assert result.intent_id == context.intent_id
    assert result.filename == expected_filename
    assert result.part_number == result.part_count == 1
    assert (result.first_ordinal, result.last_ordinal) == (1, 2)
    assert f"name={expected_filename}" in unquote(result.url)
    assert "version=" in result.url
    assert result.expires_at == downloaded_at + timedelta(minutes=10)
    assert audit is not None
    assert audit.detail["review_task_id"] == str(context.approved.review_task_id)
    assert audit.detail["authorization_basis"] == "completed_review_owner"
    assert audit.detail["package_sha256"] == result.sha256


@pytest.mark.asyncio
async def test_finished_set_download_rejects_wrong_review_non_owner_and_tampered_storage(
    unknown_patreon_context: UnknownPatreonContext,
) -> None:
    context = unknown_patreon_context
    common = {
        "store": context.prepared.store,
        "intent_id": context.intent_id,
        "expected_intent_digest": context.intent_digest,
        "expected_lock_version": context.intent_lock_version,
        "actor_user_id": context.approved.owner_id,
        "actor_role": AdminRole.OWNER,
        "now": context.action_at + timedelta(minutes=2),
    }
    async with context.approved.database.sessions() as session:
        with pytest.raises(PublicationNotFoundError, match="completed review task"):
            await presign_finished_set_package_download(
                session,
                review_task_id=uuid4(),
                **common,
            )

        owner = await session.get(AdminUser, context.approved.owner_id)
        assert owner is not None
        owner.role = AdminRole.PUBLISHER
        await session.commit()
        with pytest.raises(PublicationConflictError, match="active owner"):
            await presign_finished_set_package_download(
                session,
                review_task_id=context.approved.review_task_id,
                actor_role=AdminRole.PUBLISHER,
                **{key: value for key, value in common.items() if key != "actor_role"},
            )

        owner.role = AdminRole.OWNER
        await session.commit()
        package = await session.scalar(
            select(PublicationPackage).where(PublicationPackage.intent_id == context.intent_id)
        )
        assert package is not None
        stored = context.prepared.store.objects[package.object_key]
        context.prepared.store.objects[package.object_key] = replace(
            stored,
            metadata={**stored.metadata, "manifest-sha256": "0" * 64},
        )
        with pytest.raises(PublicationConflictError, match="storage snapshot"):
            await presign_finished_set_package_download(
                session,
                review_task_id=context.approved.review_task_id,
                **common,
            )
