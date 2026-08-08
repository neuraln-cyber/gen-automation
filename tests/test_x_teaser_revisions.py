# ruff: noqa: F811

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    DerivativeJob,
    DerivativeOutput,
    IdempotencyRecord,
    OutboxEvent,
    PublicationIntent,
    Release,
    ReviewXSelection,
    XTeaserRevision,
    XTeaserRevisionMember,
)
from gen_automation.domain.enums import (
    AdminRole,
    DerivativeJobState,
    PublicationIntentState,
    PublicationTarget,
    ReleasePhase,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services import publication as publication_service
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    claim_derivative_jobs,
    fail_derivative_job,
)
from gen_automation.services.derivative_runtime import run_derivative_cycle
from gen_automation.services.derivatives import WatermarkPosition
from gen_automation.services.operator_delivery import load_operator_delivery
from gen_automation.services.publication import (
    PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
    PublicationConflictError,
)
from gen_automation.services.review_derivatives import prepare_completed_review_x_teasers
from gen_automation.services.watermarks import register_watermark
from gen_automation.services.x_teaser_revisions import (
    active_x_teaser_outputs,
    x_teaser_revision_status,
)
from tests.test_derivative_pipeline import PLAN_AT, ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import (
    TrackingObjectStore,
    _trusted_renderer,
    _watermark_png,
)


@dataclass(frozen=True, slots=True)
class XRevisionFixture:
    context: ApprovedContext
    store: TrackingObjectStore
    watermark_asset_id: UUID


async def _seed_x_revision_fixture(
    context: ApprovedContext,
    *,
    selected_asset_ids: tuple[UUID, ...] | None = None,
) -> XRevisionFixture:
    store = TrackingObjectStore()
    for index, (asset_id, payload) in enumerate(
        zip(context.raw_asset_ids, context.raw_payloads, strict=True)
    ):
        await store.write_bytes_if_absent(
            key=f"raw/{asset_id}.png",
            body=payload,
            content_type="image/png",
            metadata={},
            max_bytes=len(payload),
        )
        stored = store.objects[f"raw/{asset_id}.png"]
        store.objects[f"raw/{asset_id}.png"] = stored.__class__(
            body=stored.body,
            content_type=stored.content_type,
            metadata=stored.metadata,
            version_id=f"raw-version-{index}",
        )

    async with context.database.sessions() as session:
        watermark = await register_watermark(
            session,
            store,
            release_id=context.release_id,
            display_name="Revision test watermark",
            png_bytes=_watermark_png(),
            registered_by_user_id=context.owner_id,
            idempotency_key="register-x-revision-watermark",
            now=PLAN_AT,
        )
        session.add_all(
            [
                ReviewXSelection(
                    id=uuid7(),
                    review_task_id=context.review_task_id,
                    asset_id=asset_id,
                    selected_by_user_id=context.owner_id,
                    selected_at=PLAN_AT + timedelta(seconds=index),
                )
                for index, asset_id in enumerate(selected_asset_ids or context.raw_asset_ids)
            ]
        )
        await session.commit()
    return XRevisionFixture(
        context=context,
        store=store,
        watermark_asset_id=watermark.asset_id,
    )


async def _render_one(fixture: XRevisionFixture, *, worker_id: str, minute: int) -> None:
    result = await run_derivative_cycle(
        fixture.context.database.sessions,
        fixture.store,
        worker_id=worker_id,
        lease_seconds=300,
        retry_base_seconds=10,
        retry_max_seconds=60,
        renderer=_trusted_renderer,
        now=PLAN_AT + timedelta(minutes=minute),
    )
    assert result.claimed_job is True
    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.SUCCEEDED, result.execution.error_code


async def _fail_one(fixture: XRevisionFixture, *, worker_id: str, minute: int) -> UUID:
    operation_at = PLAN_AT + timedelta(minutes=minute)
    async with fixture.context.database.sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=300,
            now=operation_at,
        )
        assert len(claims) == 1
        claim = claims[0]
        result = await fail_derivative_job(
            session,
            job_id=claim.job_id,
            worker_id=worker_id,
            expected_lock_version=claim.lock_version,
            error_code="test_failure",
            now=operation_at,
        )
        assert result.state == DerivativeJobState.FAILED
        return claim.job_id


async def _prepare(
    fixture: XRevisionFixture,
    *,
    key: str,
    positions: dict[UUID, WatermarkPosition],
    minute: int,
    require_active_revision: bool | None = None,
):
    async with fixture.context.database.sessions() as session:
        return await prepare_completed_review_x_teasers(
            session,
            review_task_id=fixture.context.review_task_id,
            actor_user_id=fixture.context.owner_id,
            idempotency_key=key,
            watermark_asset_id=fixture.watermark_asset_id,
            watermark_positions_by_asset_id=positions,
            require_active_revision=require_active_revision,
            now=PLAN_AT + timedelta(minutes=minute),
        )


@pytest.mark.asyncio
async def test_x_teaser_revisions_rerender_each_revision_and_switch_atomically(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    first_asset, second_asset = fixture.context.raw_asset_ids
    initial_positions = {
        first_asset: WatermarkPosition.TOP_LEFT,
        second_asset: WatermarkPosition.TOP_LEFT,
    }
    initial = await _prepare(
        fixture,
        key="x-revision-initial",
        positions=initial_positions,
        minute=1,
    )
    assert initial.jobs_created == initial.total_jobs == 2
    await _render_one(fixture, worker_id="x-revision-initial-a", minute=2)
    await _render_one(fixture, worker_id="x-revision-initial-b", minute=3)

    async with fixture.context.database.sessions() as session:
        status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        initial_output_ids = tuple(
            output.id
            for output in await active_x_teaser_outputs(
                session,
                review_task_id=fixture.context.review_task_id,
            )
        )
        assert status.active_revision_no == 1
        assert status.pending_revision_id is None
        assert status.current_positions_by_asset_id == {
            asset_id: position.value for asset_id, position in initial_positions.items()
        }
        assert len(initial_output_ids) == len(set(initial_output_ids)) == 2

    one_changed = {
        first_asset: WatermarkPosition.TOP_RIGHT,
        second_asset: WatermarkPosition.TOP_LEFT,
    }
    replacement = await _prepare(
        fixture,
        key="x-revision-change-one",
        positions=one_changed,
        minute=4,
    )
    assert replacement.jobs_created == replacement.total_jobs == 2

    async with fixture.context.database.sessions() as session:
        status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        pending_members = tuple(
            (
                await session.scalars(
                    select(XTeaserRevisionMember)
                    .where(XTeaserRevisionMember.revision_id == status.pending_revision_id)
                    .order_by(XTeaserRevisionMember.display_order)
                )
            ).all()
        )
        still_active_ids = tuple(
            output.id
            for output in await active_x_teaser_outputs(
                session,
                review_task_id=fixture.context.review_task_id,
            )
        )
        assert still_active_ids == initial_output_ids
        assert sum(member.derivative_job_id is not None for member in pending_members) == 2
        assert sum(member.derivative_output_id is not None for member in pending_members) == 0
        assert status.pending_total == 2
        assert status.pending_succeeded == 0

    await _render_one(fixture, worker_id="x-revision-one-change-a", minute=5)
    async with fixture.context.database.sessions() as session:
        midway = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert midway.active_revision_no == 1
        assert midway.pending_succeeded == 1
        assert (
            tuple(
                output.id
                for output in await active_x_teaser_outputs(
                    session,
                    review_task_id=fixture.context.review_task_id,
                )
            )
            == initial_output_ids
        )

    await _render_one(fixture, worker_id="x-revision-one-change-b", minute=6)
    async with fixture.context.database.sessions() as session:
        one_changed_status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        one_changed_ids = tuple(
            output.id
            for output in await active_x_teaser_outputs(
                session,
                review_task_id=fixture.context.review_task_id,
            )
        )
        assert one_changed_status.active_revision_no == 2
        assert one_changed_status.pending_revision_id is None
        assert one_changed_ids != initial_output_ids
        assert set(one_changed_ids).isdisjoint(initial_output_ids)
        assert len(one_changed_ids) == len(set(one_changed_ids)) == 2

    all_changed = {
        first_asset: WatermarkPosition.BOTTOM_LEFT,
        second_asset: WatermarkPosition.BOTTOM_RIGHT,
    }
    all_replacement = await _prepare(
        fixture,
        key="x-revision-change-all",
        positions=all_changed,
        minute=7,
    )
    assert all_replacement.jobs_created == all_replacement.total_jobs == 2
    await _render_one(fixture, worker_id="x-revision-all-a", minute=8)

    async with fixture.context.database.sessions() as session:
        midway = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        midway_ids = tuple(
            output.id
            for output in await active_x_teaser_outputs(
                session,
                review_task_id=fixture.context.review_task_id,
            )
        )
        assert midway.active_revision_no == 2
        assert midway.pending_succeeded == 1
        assert midway_ids == one_changed_ids

    await _render_one(fixture, worker_id="x-revision-all-b", minute=9)
    async with fixture.context.database.sessions() as session:
        final = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        final_outputs = await active_x_teaser_outputs(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        canonical_count = int(
            await session.scalar(
                select(func.count())
                .select_from(XTeaserRevisionMember)
                .where(XTeaserRevisionMember.revision_id == final.active_revision_id)
            )
            or 0
        )
        assert final.active_revision_no == 3
        assert final.pending_revision_id is None
        assert final.current_positions_by_asset_id == {
            asset_id: position.value for asset_id, position in all_changed.items()
        }
        assert len(final_outputs) == len({output.id for output in final_outputs}) == 2
        assert canonical_count == 2
        delivery = await load_operator_delivery(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert len(delivery.x_outputs) == 2
        assert len({output.output_id for output in delivery.x_outputs}) == 2


@pytest.mark.asyncio
async def test_first_x_revision_failure_clears_the_head_and_can_be_retried(
    derivative_approved_context: ApprovedContext,
) -> None:
    selected_asset_id = derivative_approved_context.raw_asset_ids[0]
    fixture = await _seed_x_revision_fixture(
        derivative_approved_context,
        selected_asset_ids=(selected_asset_id,),
    )
    positions = {
        selected_asset_id: WatermarkPosition.TOP_LEFT,
    }
    await _prepare(
        fixture,
        key="x-revision-first-failure",
        positions=positions,
        minute=1,
    )
    await _fail_one(fixture, worker_id="x-first-failure", minute=2)

    async with fixture.context.database.sessions() as session:
        failed_status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert failed_status.active_revision_id is None
        assert failed_status.pending_revision_id is None
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.READY_TO_PUBLISH

    retry = await _prepare(
        fixture,
        key="x-revision-first-failure-retry",
        positions=positions,
        minute=3,
    )
    assert retry.jobs_created == retry.total_jobs == 1
    async with fixture.context.database.sessions() as session:
        status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert status.pending_revision_id is not None
        revision = await session.get(XTeaserRevision, status.pending_revision_id)
        retry_job = await session.get(DerivativeJob, retry.job_ids[0])
        assert revision is not None
        assert retry_job is not None
        assert revision.revision_no == 2
        assert retry_job.gates_release is False

    await _render_one(fixture, worker_id="x-first-failure-retry-success", minute=4)
    async with fixture.context.database.sessions() as session:
        ready = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        outputs = await active_x_teaser_outputs(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        assert ready.active_revision_no == 2
        assert ready.pending_revision_id is None
        assert ready.current_positions_by_asset_id == {
            selected_asset_id: WatermarkPosition.TOP_LEFT.value,
        }
        assert len(outputs) == 1
        assert release.phase == ReleasePhase.READY_TO_PUBLISH


@pytest.mark.asyncio
async def test_initial_two_member_failure_then_success_reconciles_readiness_once(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    positions = {asset_id: WatermarkPosition.TOP_LEFT for asset_id in fixture.context.raw_asset_ids}
    await _prepare(
        fixture,
        key="x-initial-mixed-terminal",
        positions=positions,
        minute=1,
    )
    await _fail_one(fixture, worker_id="x-initial-mixed-failure", minute=2)

    async with fixture.context.database.sessions() as session:
        pending = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        assert pending.pending_revision_id is not None
        assert pending.pending_failed == 1
        assert release.phase == ReleasePhase.RENDERING

    await _render_one(fixture, worker_id="x-initial-mixed-success", minute=3)
    async with fixture.context.database.sessions() as session:
        reconciled = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        release = await session.get(Release, fixture.context.release_id)
        ready_events = int(
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.topic == "release.ready_to_publish",
                    OutboxEvent.dedupe_key
                    == f"release.ready_to_publish:{fixture.context.release_version_id}",
                )
            )
            or 0
        )
        assert release is not None
        assert reconciled.active_revision_id is None
        assert reconciled.pending_revision_id is None
        assert release.phase == ReleasePhase.READY_TO_PUBLISH
        assert ready_events == 1


@pytest.mark.asyncio
async def test_repeated_initial_failure_reuses_the_release_readiness_event(
    derivative_approved_context: ApprovedContext,
) -> None:
    selected_asset_id = derivative_approved_context.raw_asset_ids[0]
    fixture = await _seed_x_revision_fixture(
        derivative_approved_context,
        selected_asset_ids=(selected_asset_id,),
    )
    positions = {selected_asset_id: WatermarkPosition.TOP_LEFT}
    await _prepare(
        fixture,
        key="x-readiness-event-first",
        positions=positions,
        minute=1,
    )
    await _fail_one(fixture, worker_id="x-readiness-event-first", minute=2)
    await _prepare(
        fixture,
        key="x-readiness-event-retry",
        positions=positions,
        minute=3,
    )
    async with fixture.context.database.sessions() as session:
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        release.phase = ReleasePhase.RENDERING
        release.lock_version += 1
        await session.commit()

    await _fail_one(fixture, worker_id="x-readiness-event-retry", minute=4)
    async with fixture.context.database.sessions() as session:
        release = await session.get(Release, fixture.context.release_id)
        status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        ready_events = int(
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.topic == "release.ready_to_publish",
                    OutboxEvent.dedupe_key
                    == f"release.ready_to_publish:{fixture.context.release_version_id}",
                )
            )
            or 0
        )
        assert release is not None
        assert release.phase == ReleasePhase.READY_TO_PUBLISH
        assert status.active_revision_id is None
        assert status.pending_revision_id is None
        assert ready_events == 1


@pytest.mark.asyncio
async def test_first_x_revision_after_publication_is_nongating_and_keeps_phase(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    async with fixture.context.database.sessions() as session:
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        release.phase = ReleasePhase.PUBLISHED
        release.lock_version += 1
        await session.commit()

    positions = {
        asset_id: WatermarkPosition.BOTTOM_RIGHT for asset_id in fixture.context.raw_asset_ids
    }
    planned = await _prepare(
        fixture,
        key="x-first-after-published",
        positions=positions,
        minute=1,
        require_active_revision=False,
    )
    assert planned.jobs_created == planned.total_jobs == 2
    async with fixture.context.database.sessions() as session:
        jobs = tuple(
            (
                await session.scalars(
                    select(DerivativeJob).where(DerivativeJob.id.in_(planned.job_ids))
                )
            ).all()
        )
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        assert jobs and all(job.gates_release is False for job in jobs)
        assert release.phase == ReleasePhase.PUBLISHED

    await _render_one(fixture, worker_id="x-published-first-a", minute=2)
    await _render_one(fixture, worker_id="x-published-first-b", minute=3)
    async with fixture.context.database.sessions() as session:
        status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        assert status.active_revision_no == 1
        assert status.pending_revision_id is None
        assert release.phase == ReleasePhase.PUBLISHED


@pytest.mark.asyncio
async def test_matching_current_request_reserves_each_new_idempotency_key(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    positions = {asset_id: WatermarkPosition.TOP_LEFT for asset_id in fixture.context.raw_asset_ids}
    initial = await _prepare(
        fixture,
        key="repeat-a",
        positions=positions,
        minute=1,
    )
    replay = await _prepare(
        fixture,
        key="repeat-b",
        positions=positions,
        minute=2,
    )
    assert initial.replayed is False
    assert replay.replayed is True
    assert replay.jobs_created == 0

    async with fixture.context.database.sessions() as session:
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope
                == f"review-task:{fixture.context.review_task_id}:x-teaser-revision",
                IdempotencyRecord.idempotency_key == "repeat-b",
            )
        )
        assert record is not None

    changed = dict(positions)
    changed[fixture.context.raw_asset_ids[0]] = WatermarkPosition.BOTTOM_LEFT
    with pytest.raises(
        DerivativePipelineConflictError,
        match="idempotency key was already used for a different replacement",
    ):
        await _prepare(
            fixture,
            key="repeat-b",
            positions=changed,
            minute=3,
        )


@pytest.mark.asyncio
async def test_failed_replacement_then_other_success_keeps_old_active_and_unwedges_head(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    initial_positions = {
        asset_id: WatermarkPosition.TOP_LEFT for asset_id in fixture.context.raw_asset_ids
    }
    await _prepare(
        fixture,
        key="x-revision-failed-initial",
        positions=initial_positions,
        minute=1,
    )
    await _render_one(fixture, worker_id="x-failed-initial-a", minute=2)
    await _render_one(fixture, worker_id="x-failed-initial-b", minute=3)

    async with fixture.context.database.sessions() as session:
        old_ids = tuple(
            output.id
            for output in await active_x_teaser_outputs(
                session,
                review_task_id=fixture.context.review_task_id,
            )
        )

    failed_positions = {
        asset_id: WatermarkPosition.BOTTOM_RIGHT for asset_id in fixture.context.raw_asset_ids
    }
    await _prepare(
        fixture,
        key="x-revision-failed-replacement",
        positions=failed_positions,
        minute=4,
    )
    await _fail_one(fixture, worker_id="x-replacement-failure", minute=5)

    async with fixture.context.database.sessions() as session:
        failed_status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert failed_status.pending_state == "failed"
        assert failed_status.pending_failed == 1
        assert (
            tuple(
                output.id
                for output in await active_x_teaser_outputs(
                    session,
                    review_task_id=fixture.context.review_task_id,
                )
            )
            == old_ids
        )

    await _render_one(fixture, worker_id="x-replacement-other-success", minute=6)
    async with fixture.context.database.sessions() as session:
        recovered = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert recovered.pending_revision_id is None
        assert recovered.active_revision_no == 1
        assert recovered.can_replace is True
        assert (
            tuple(
                output.id
                for output in await active_x_teaser_outputs(
                    session,
                    review_task_id=fixture.context.review_task_id,
                )
            )
            == old_ids
        )


@pytest.mark.asyncio
async def test_x_publication_intent_blocks_replacement_without_moving_active_head(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    positions = {asset_id: WatermarkPosition.TOP_LEFT for asset_id in fixture.context.raw_asset_ids}
    await _prepare(fixture, key="x-race-initial", positions=positions, minute=1)
    await _render_one(fixture, worker_id="x-race-initial-a", minute=2)
    await _render_one(fixture, worker_id="x-race-initial-b", minute=3)

    async with fixture.context.database.sessions() as session:
        before = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        session.add(
            PublicationIntent(
                id=uuid7(),
                release_id=fixture.context.release_id,
                release_version_id=fixture.context.release_version_id,
                target=PublicationTarget.X,
                state=PublicationIntentState.AWAITING_APPROVAL,
                configuration={"text": "Frozen X post"},
                configuration_sha256="1" * 64,
                input_manifest_sha256="2" * 64,
                intent_digest="3" * 64,
                input_count=2,
                credential_reference="test://x/oauth",
                planned_by_user_id=fixture.context.owner_id,
                planned_at=PLAN_AT + timedelta(minutes=4),
                lock_version=1,
            )
        )
        await session.commit()

        with pytest.raises(
            DerivativePipelineConflictError,
            match="cannot be replaced after X delivery has been prepared",
        ):
            await prepare_completed_review_x_teasers(
                session,
                review_task_id=fixture.context.review_task_id,
                actor_user_id=fixture.context.owner_id,
                idempotency_key="x-race-blocked",
                watermark_asset_id=fixture.watermark_asset_id,
                watermark_positions_by_asset_id={
                    asset_id: WatermarkPosition.BOTTOM_RIGHT
                    for asset_id in fixture.context.raw_asset_ids
                },
                now=PLAN_AT + timedelta(minutes=5),
            )
        await session.rollback()

        after = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        revisions = int(
            await session.scalar(
                select(func.count())
                .select_from(XTeaserRevision)
                .where(XTeaserRevision.review_task_id == fixture.context.review_task_id)
            )
            or 0
        )
        outputs = int(
            await session.scalar(
                select(func.count())
                .select_from(DerivativeOutput)
                .where(DerivativeOutput.target == "x_teaser")
            )
            or 0
        )
        assert after.active_revision_id == before.active_revision_id
        assert after.pending_revision_id is None
        assert after.can_replace is False
        assert revisions == 1
        assert outputs == 2


@pytest.mark.asyncio
async def test_terminal_old_x_intent_cannot_be_reapproved_while_replacement_is_pending(
    derivative_approved_context: ApprovedContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    initial_positions = {
        asset_id: WatermarkPosition.TOP_LEFT for asset_id in fixture.context.raw_asset_ids
    }
    await _prepare(
        fixture,
        key="x-old-intent-initial",
        positions=initial_positions,
        minute=1,
    )
    await _render_one(fixture, worker_id="x-old-intent-initial-a", minute=2)
    await _render_one(fixture, worker_id="x-old-intent-initial-b", minute=3)

    async def allow_test_compliance(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        publication_service,
        "_require_current_compliance_approvals",
        allow_test_compliance,
    )
    async with fixture.context.database.sessions() as session:
        active_output_ids = tuple(
            output.id
            for output in await active_x_teaser_outputs(
                session,
                review_task_id=fixture.context.review_task_id,
            )
        )
        planned = await publication_service.plan_publication_intent(
            session,
            release_version_id=fixture.context.release_version_id,
            target=PublicationTarget.X,
            configuration={"text": "Frozen old teaser post", "adult_content": False},
            derivative_output_ids=active_output_ids,
            planned_by_user_id=fixture.context.owner_id,
            idempotency_key="x-old-intent-plan",
            credential_reference="test://x/oauth",
            now=PLAN_AT + timedelta(minutes=4),
        )
        old_intent = await session.get(PublicationIntent, planned.intent_id)
        assert old_intent is not None
        old_intent.state = PublicationIntentState.FAILED
        old_intent.completed_at = PLAN_AT + timedelta(minutes=5)
        old_intent.last_error_code = "test_terminal"
        old_intent.lock_version += 1
        await session.commit()
        old_lock_version = old_intent.lock_version

    replacement_positions = {
        asset_id: WatermarkPosition.BOTTOM_RIGHT for asset_id in fixture.context.raw_asset_ids
    }
    replacement = await _prepare(
        fixture,
        key="x-old-intent-pending-replacement",
        positions=replacement_positions,
        minute=6,
        require_active_revision=True,
    )
    assert replacement.jobs_created == replacement.total_jobs == 2

    async with fixture.context.database.sessions() as session:
        before = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        assert before.active_revision_id is not None
        assert before.pending_revision_id is not None
        assert (
            tuple(
                output.id
                for output in await active_x_teaser_outputs(
                    session,
                    review_task_id=fixture.context.review_task_id,
                )
            )
            == active_output_ids
        )

        with pytest.raises(
            PublicationConflictError,
            match="blocked while teaser replacement is pending",
        ):
            await publication_service.approve_publication_intent(
                session,
                intent_id=planned.intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=old_lock_version,
                actor_user_id=fixture.context.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
                approval_seconds=300,
                idempotency_key="x-old-intent-reapprove",
                now=PLAN_AT + timedelta(minutes=7),
            )
        await session.rollback()

        after = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        old_intent = await session.get(PublicationIntent, planned.intent_id)
        assert old_intent is not None
        assert old_intent.state == PublicationIntentState.FAILED
        assert after.active_revision_id == before.active_revision_id
        assert after.pending_revision_id == before.pending_revision_id


@pytest.mark.asyncio
async def test_failed_legacy_revision_null_x_job_does_not_block_current_revision_readiness(
    derivative_approved_context: ApprovedContext,
) -> None:
    fixture = await _seed_x_revision_fixture(derivative_approved_context)
    positions = {
        asset_id: WatermarkPosition.TOP_RIGHT for asset_id in fixture.context.raw_asset_ids
    }
    planned = await _prepare(
        fixture,
        key="x-current-with-failed-legacy-job",
        positions=positions,
        minute=1,
    )
    assert planned.jobs_created == planned.total_jobs == 2

    async with fixture.context.database.sessions() as session:
        current_job = await session.get(DerivativeJob, planned.job_ids[0])
        assert current_job is not None
        session.add(
            DerivativeJob(
                id=uuid7(),
                release_selection_id=current_job.release_selection_id,
                derivative_recipe_id=current_job.derivative_recipe_id,
                x_teaser_revision_id=None,
                gates_release=True,
                release_version_id=current_job.release_version_id,
                logical_key="d" * 64,
                request_payload=current_job.request_payload,
                request_sha256="e" * 64,
                expected_output_count=current_job.expected_output_count,
                state=DerivativeJobState.FAILED,
                priority=current_job.priority,
                attempt_count=1,
                max_attempts=current_job.max_attempts,
                lock_version=2,
                available_at=PLAN_AT,
                requested_at=PLAN_AT,
                claimed_at=PLAN_AT + timedelta(seconds=1),
                processing_started_at=PLAN_AT + timedelta(seconds=2),
                completed_at=PLAN_AT + timedelta(seconds=3),
                last_error_code="legacy_x_failure",
                last_error_detail="legacy revision-null X teaser job",
            )
        )
        await session.commit()

    await _render_one(fixture, worker_id="x-current-after-legacy-a", minute=2)
    await _render_one(fixture, worker_id="x-current-after-legacy-b", minute=3)
    async with fixture.context.database.sessions() as session:
        status = await x_teaser_revision_status(
            session,
            review_task_id=fixture.context.review_task_id,
        )
        release = await session.get(Release, fixture.context.release_id)
        assert release is not None
        assert status.active_revision_no == 1
        assert status.pending_revision_id is None
        assert release.phase == ReleasePhase.READY_TO_PUBLISH
