from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import AdminUser
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.i2v import (
    I2VAttemptState,
    I2VInputRegistration,
    I2VInputSource,
    I2VJobDraft,
    I2VJobState,
    I2VOutputRegistration,
    I2VPresetDraft,
    I2VWorkerDeploymentRegistration,
    I2VWorkerDeploymentState,
)
from gen_automation.services.i2v import (
    I2VConflictError,
    acknowledge_i2v_cancellation,
    claim_next_i2v_job,
    complete_i2v_job,
    create_i2v_job,
    create_i2v_preset,
    delete_i2v_preset,
    fail_i2v_attempt,
    get_i2v_worker_deployment,
    list_i2v_jobs,
    list_i2v_presets,
    list_recent_i2v_outputs,
    record_i2v_worker_deployment,
    recover_expired_i2v_jobs,
    register_i2v_input,
    reorder_i2v_queue,
    request_i2v_job_cancellation,
    retry_i2v_job,
    start_i2v_attempt,
    update_i2v_preset,
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
async def i2v_session(tmp_path: Path) -> AsyncIterator[tuple[AsyncSession, UUID]]:
    database_path = tmp_path / "i2v-service.db"
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="i2v-owner",
            display_name="I2V Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=_NOW,
            lock_version=1,
        )
        session.add(owner)
        await session.commit()
        yield session, owner.id
    await database.dispose()


def _input_registration(*, object_key: str = "i2v/input.png") -> I2VInputRegistration:
    return I2VInputRegistration(
        source=I2VInputSource.UPLOAD,
        display_name="Uploaded source",
        storage_backend="s3",
        storage_bucket="private-i2v",
        object_key=object_key,
        sha256="a" * 64,
        content_type="image/png",
        width=2048,
        height=1536,
        byte_size=8_000_000,
        metadata={"origin": "dashboard"},
    )


async def test_presets_and_jobs_freeze_snapshots_and_support_fifo_reordering(
    i2v_session: tuple[AsyncSession, UUID],
) -> None:
    session, owner_id = i2v_session
    source = await register_i2v_input(
        session,
        actor_user_id=owner_id,
        registration=_input_registration(),
        now=_NOW,
    )
    replay = await register_i2v_input(
        session,
        actor_user_id=owner_id,
        registration=_input_registration(),
        now=_NOW,
    )
    assert replay == source

    preset = await create_i2v_preset(
        session,
        actor_user_id=owner_id,
        draft=I2VPresetDraft(
            name="DaSiWa fidelity",
            positive_prompt="smooth hip motion",
            negative_prompt="jitter",
            settings={"steps": 4, "frame_count": 81, "custom": {"cfg": 1}},
        ),
        now=_NOW,
    )
    jobs = []
    for index in range(3):
        jobs.append(
            await create_i2v_job(
                session,
                actor_user_id=owner_id,
                draft=I2VJobDraft(
                    input_id=source.input_id,
                    preset_id=preset.preset_id,
                    positive_prompt=f"smooth motion take {index}",
                    settings={"seed": index, "arbitrary_large_value": 10**18},
                ),
                now=_NOW + timedelta(seconds=index),
            )
        )
    assert [job.queue_position for job in jobs] == [1, 2, 3]
    frozen_preset = jobs[0].preset_snapshot
    frozen_settings = jobs[0].settings_snapshot

    updated = await update_i2v_preset(
        session,
        actor_user_id=owner_id,
        preset_id=preset.preset_id,
        expected_lock_version=1,
        draft=I2VPresetDraft(
            name="DaSiWa fidelity",
            positive_prompt="new default",
            settings={"steps": 999_999},
        ),
        now=_NOW + timedelta(minutes=1),
    )
    assert updated.lock_version == 2
    persisted = await list_i2v_jobs(session, actor_user_id=owner_id)
    assert persisted[0].preset_snapshot == frozen_preset
    assert persisted[0].settings_snapshot == frozen_settings

    reordered = await reorder_i2v_queue(
        session,
        actor_user_id=owner_id,
        job_id=jobs[2].job_id,
        before_job_id=jobs[0].job_id,
        now=_NOW + timedelta(minutes=2),
    )
    assert [job.job_id for job in reordered] == [
        jobs[2].job_id,
        jobs[0].job_id,
        jobs[1].job_id,
    ]
    assert [job.queue_position for job in reordered] == [1, 2, 3]

    cancelled = await request_i2v_job_cancellation(
        session,
        actor_user_id=owner_id,
        job_id=jobs[0].job_id,
        now=_NOW + timedelta(minutes=3),
    )
    assert cancelled.state == I2VJobState.CANCELLED
    assert [
        job.queue_position for job in await list_i2v_jobs(session, states={I2VJobState.QUEUED})
    ] == [1, 2]

    retried = await retry_i2v_job(
        session,
        actor_user_id=owner_id,
        job_id=jobs[0].job_id,
        now=_NOW + timedelta(minutes=4),
    )
    assert retried.queue_position == 3
    assert retried.input_snapshot == jobs[0].input_snapshot
    assert len(await list_i2v_presets(session, actor_user_id=owner_id)) == 1

    with pytest.raises(I2VConflictError, match="changed by another request"):
        await update_i2v_preset(
            session,
            actor_user_id=owner_id,
            preset_id=preset.preset_id,
            expected_lock_version=1,
            draft=I2VPresetDraft(name="stale"),
        )

    await delete_i2v_preset(
        session,
        actor_user_id=owner_id,
        preset_id=preset.preset_id,
    )
    assert await list_i2v_presets(session, actor_user_id=owner_id) == ()
    after_delete = await list_i2v_jobs(session, actor_user_id=owner_id)
    assert all(item.preset_id is None for item in after_delete)
    assert all(item.preset_snapshot == frozen_preset for item in after_delete)


async def test_worker_claim_attempt_output_and_recent_result(
    i2v_session: tuple[AsyncSession, UUID],
) -> None:
    session, owner_id = i2v_session
    source = await register_i2v_input(
        session,
        actor_user_id=owner_id,
        registration=_input_registration(),
    )
    job = await create_i2v_job(
        session,
        actor_user_id=owner_id,
        draft=I2VJobDraft(
            input_id=source.input_id,
            positive_prompt="gentle movement",
            negative_prompt="flicker",
            settings={"workflow": "dasiwa-c-aio-v89"},
        ),
    )
    deployment = await record_i2v_worker_deployment(
        session,
        registration=I2VWorkerDeploymentRegistration(
            provider="salad",
            provider_group_id="group-5090",
            provider_instance_id="instance-1",
            state=I2VWorkerDeploymentState.READY,
            gpu_class="RTX 5090",
            worker_image_digest="sha256:" + "c" * 64,
            last_heartbeat_at=_NOW,
            metadata={"warm": True},
        ),
        now=_NOW,
    )
    assert (await get_i2v_worker_deployment(session)) == deployment

    claim = await claim_next_i2v_job(
        session,
        worker_id="worker-5090",
        lease_duration=timedelta(hours=12),
        worker_deployment_id=deployment.deployment_id,
        worker_image_digest=deployment.worker_image_digest,
        now=_NOW,
    )
    assert claim is not None
    assert claim.job.job_id == job.job_id
    assert claim.job.state == I2VJobState.CLAIMED
    assert claim.attempt.state == I2VAttemptState.CREATED

    running = await start_i2v_attempt(
        session,
        job_id=job.job_id,
        attempt_id=claim.attempt.attempt_id,
        worker_id="worker-5090",
        provider_job_id="comfy-prompt-1",
        request_metadata={"workflow_sha256": "d" * 64},
        now=_NOW + timedelta(seconds=1),
    )
    assert running.job.state == I2VJobState.RUNNING
    result = await complete_i2v_job(
        session,
        job_id=job.job_id,
        attempt_id=claim.attempt.attempt_id,
        worker_id="worker-5090",
        output=I2VOutputRegistration(
            storage_backend="s3",
            storage_bucket="private-i2v",
            object_key="i2v/results/video.mp4",
            sha256="e" * 64,
            content_type="video/mp4",
            width=1280,
            height=720,
            frame_count=81,
            fps=24,
            duration_ms=3375,
            byte_size=42_000_000,
            metadata={"codec": "h264"},
        ),
        response_metadata={"peak_vram_gib": 28.1},
        now=_NOW + timedelta(minutes=1),
    )
    assert result.job_id == job.job_id
    assert result.metadata == {"codec": "h264"}
    assert await list_recent_i2v_outputs(session, actor_user_id=owner_id) == (result,)
    completed = await list_i2v_jobs(
        session,
        actor_user_id=owner_id,
        states={I2VJobState.SUCCEEDED},
    )
    assert completed[0].completed_at == _NOW + timedelta(minutes=1)


async def test_fail_retry_has_no_attempt_ceiling_and_running_cancel_is_acknowledged(
    i2v_session: tuple[AsyncSession, UUID],
) -> None:
    session, owner_id = i2v_session
    source = await register_i2v_input(
        session,
        actor_user_id=owner_id,
        registration=_input_registration(),
    )
    job = await create_i2v_job(
        session,
        actor_user_id=owner_id,
        draft=I2VJobDraft(input_id=source.input_id, settings={"steps": 4}),
    )

    for attempt_no in range(1, 13):
        claim = await claim_next_i2v_job(
            session,
            worker_id="durable-worker",
            lease_duration=timedelta(days=2),
            now=_NOW + timedelta(minutes=attempt_no),
        )
        assert claim is not None
        assert claim.attempt.attempt_no == attempt_no
        await start_i2v_attempt(
            session,
            job_id=job.job_id,
            attempt_id=claim.attempt.attempt_id,
            worker_id="durable-worker",
            now=_NOW + timedelta(minutes=attempt_no, seconds=1),
        )
        failed = await fail_i2v_attempt(
            session,
            job_id=job.job_id,
            attempt_id=claim.attempt.attempt_id,
            worker_id="durable-worker",
            error_code="test_failure",
            error_detail="retry without an arbitrary ceiling",
            now=_NOW + timedelta(minutes=attempt_no, seconds=2),
        )
        assert failed.attempt_count == attempt_no
        await retry_i2v_job(
            session,
            actor_user_id=owner_id,
            job_id=job.job_id,
            now=_NOW + timedelta(minutes=attempt_no, seconds=3),
        )

    final_claim = await claim_next_i2v_job(
        session,
        worker_id="durable-worker",
        lease_duration=timedelta(days=2),
        now=_NOW + timedelta(hours=1),
    )
    assert final_claim is not None
    await start_i2v_attempt(
        session,
        job_id=job.job_id,
        attempt_id=final_claim.attempt.attempt_id,
        worker_id="durable-worker",
        now=_NOW + timedelta(hours=1, seconds=1),
    )
    requested = await request_i2v_job_cancellation(
        session,
        actor_user_id=owner_id,
        job_id=job.job_id,
        now=_NOW + timedelta(hours=1, seconds=2),
    )
    assert requested.state == I2VJobState.CANCEL_REQUESTED
    cancelled = await acknowledge_i2v_cancellation(
        session,
        job_id=job.job_id,
        attempt_id=final_claim.attempt.attempt_id,
        worker_id="durable-worker",
        now=_NOW + timedelta(hours=1, seconds=3),
    )
    assert cancelled.state == I2VJobState.CANCELLED
    assert cancelled.attempt_count == 13


async def test_expired_claim_is_requeued_and_old_attempt_cannot_complete(
    i2v_session: tuple[AsyncSession, UUID],
) -> None:
    session, owner_id = i2v_session
    source = await register_i2v_input(
        session,
        actor_user_id=owner_id,
        registration=_input_registration(),
    )
    job = await create_i2v_job(
        session,
        actor_user_id=owner_id,
        draft=I2VJobDraft(input_id=source.input_id),
    )
    old_claim = await claim_next_i2v_job(
        session,
        worker_id="same-worker-name",
        lease_duration=timedelta(seconds=1),
        now=_NOW,
    )
    assert old_claim is not None
    recovered = await recover_expired_i2v_jobs(session, now=_NOW + timedelta(seconds=2))
    assert recovered[0].state == I2VJobState.QUEUED
    assert recovered[0].queue_position == 1

    new_claim = await claim_next_i2v_job(
        session,
        worker_id="same-worker-name",
        lease_duration=timedelta(hours=1),
        now=_NOW + timedelta(seconds=3),
    )
    assert new_claim is not None
    assert new_claim.attempt.attempt_no == 2
    with pytest.raises(I2VConflictError, match="another worker"):
        await start_i2v_attempt(
            session,
            job_id=job.job_id,
            attempt_id=old_claim.attempt.attempt_id,
            worker_id="same-worker-name",
            now=_NOW + timedelta(seconds=4),
        )
