# ruff: noqa: F811

import asyncio
import hashlib
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import PIL
import pytest
from PIL import Image
from sqlalchemy import func, select

from gen_automation.db.models import (
    Asset,
    AssetLineage,
    DerivativeJob,
    DerivativeOutput,
    Release,
    ReleaseSelection,
    ReviewXSelection,
)
from gen_automation.domain.deliverability import patreon_full_output_byte_budget
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    DerivativeJobState,
    ReleasePhase,
)
from gen_automation.services.derivative_isolation import (
    DerivativeIsolationCrashError,
    DerivativeIsolationPolicy,
    DerivativeIsolationProtocolError,
    DerivativeIsolationTimeoutError,
)
from gen_automation.services.derivative_pipeline import (
    ClaimedDerivativeJob,
    DerivativePlanResult,
    claim_derivative_jobs,
    create_derivative_recipe_and_plan,
)
from gen_automation.services.derivative_runtime import (
    DerivativeCycleResult,
    DerivativeExecutionResult,
    derivative_recipe_configuration,
    process_claimed_derivative_job,
    run_derivative_cycle,
)
from gen_automation.services.derivatives import (
    DERIVATIVE_RENDERER_VERSION,
    LEGACY_DERIVATIVE_RENDERER_VERSION,
    DerivativeBundle,
    DerivativeRecipe,
    DerivativeSafetyLimits,
    WatermarkSpec,
    render_platform_derivatives,
)
from gen_automation.storage.base import ObjectMetadata, ObjectStoreError
from gen_automation.storage.memory import MemoryObjectStore, StoredObject
from tests.test_derivative_pipeline import PLAN_AT, ApprovedContext
from tests.test_derivative_pipeline import (  # noqa: F401
    approved_context as derivative_approved_context,
)

RUN_AT = PLAN_AT + timedelta(minutes=1)


class TrackingObjectStore(MemoryObjectStore):
    backend = "s3"

    def __init__(self) -> None:
        super().__init__(bucket="derivative-test")
        self.read_requests: list[tuple[str, int, str | None, str | None]] = []
        self.write_attempts: list[str] = []

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        self.read_requests.append((key, max_bytes, version_id, etag))
        return await super().read_bytes(
            key,
            max_bytes=max_bytes,
            version_id=version_id,
            etag=etag,
        )

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        self.write_attempts.append(key)
        return await super().write_bytes_if_absent(
            key=key,
            body=body,
            content_type=content_type,
            metadata=metadata,
            max_bytes=max_bytes,
        )


class LostWriteResponseStore(TrackingObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_write_response = True

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        result = await super().write_bytes_if_absent(
            key=key,
            body=body,
            content_type=content_type,
            metadata=metadata,
            max_bytes=max_bytes,
        )
        if self.lose_next_write_response:
            self.lose_next_write_response = False
            raise ObjectStoreError("simulated response loss with secret=https://signed.example")
        return result


class CancellableWriteStore(TrackingObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.first_write_completed = asyncio.Event()
        self._never_release = asyncio.Event()
        self._block_next_write = True

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        result = await super().write_bytes_if_absent(
            key=key,
            body=body,
            content_type=content_type,
            metadata=metadata,
            max_bytes=max_bytes,
        )
        if self._block_next_write:
            self._block_next_write = False
            self.first_write_completed.set()
            await self._never_release.wait()
        return result


@dataclass(frozen=True, slots=True)
class PreparedRuntime:
    approved: ApprovedContext
    store: TrackingObjectStore
    recipe: DerivativeRecipe
    plan: DerivativePlanResult
    watermark_key: str | None
    watermark_version: str | None


type TestRenderer = Callable[
    [
        bytes,
        DerivativeRecipe,
        bytes | None,
        tuple[str, ...],
        DerivativeSafetyLimits,
        DerivativeIsolationPolicy,
    ],
    Awaitable[DerivativeBundle],
]


def _watermark_png() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGBA", (32, 12), color=(255, 255, 255, 0))
    for x in range(4, 28):
        for y in range(3, 9):
            image.putpixel((x, y), (255, 255, 255, 220))
    image.save(output, format="PNG")
    return output.getvalue()


async def _trusted_renderer(
    source: bytes,
    recipe: DerivativeRecipe,
    watermark: bytes | None,
    targets: tuple[str, ...],
    limits: DerivativeSafetyLimits,
    policy: DerivativeIsolationPolicy,
) -> DerivativeBundle:
    assert policy.wall_timeout_seconds >= 1
    return render_platform_derivatives(
        source,
        recipe=recipe,
        watermark_png=watermark,
        targets=targets,
        limits=limits,
    )


async def _prepare(
    approved: ApprovedContext,
    *,
    store: TrackingObjectStore | None = None,
    with_watermark: bool = False,
    x_selected_asset_ids: tuple[UUID, ...] = (),
    max_attempts: int = 3,
    renderer_version: str = DERIVATIVE_RENDERER_VERSION,
) -> PreparedRuntime:
    selected_store = store or TrackingObjectStore()
    for index, (asset_id, payload) in enumerate(
        zip(approved.raw_asset_ids, approved.raw_payloads, strict=True)
    ):
        key = f"raw/{asset_id}.png"
        selected_store.objects[key] = StoredObject(
            body=payload,
            content_type="image/png",
            metadata={"sha256": hashlib.sha256(payload).hexdigest()},
            version_id=f"raw-version-{index}",
        )

    watermark_asset_id: UUID | None = None
    watermark_key: str | None = None
    watermark_version: str | None = None
    recipe = DerivativeRecipe(watermark=WatermarkSpec() if with_watermark else None)
    if with_watermark:
        watermark_asset_id = uuid4()
        watermark_key = f"watermarks/{watermark_asset_id}.png"
        watermark_version = "watermark-version-1"
        watermark = _watermark_png()
        selected_store.objects[watermark_key] = StoredObject(
            body=watermark,
            content_type="image/png",
            metadata={"sha256": hashlib.sha256(watermark).hexdigest()},
            version_id=watermark_version,
        )
        async with approved.database.sessions() as session:
            session.add(
                Asset(
                    id=watermark_asset_id,
                    release_id=approved.release_id,
                    generation_job_id=None,
                    output_index=None,
                    kind=AssetKind.DERIVATIVE,
                    state=AssetState.AVAILABLE,
                    storage_backend=selected_store.backend,
                    storage_bucket=selected_store.bucket,
                    object_key=watermark_key,
                    object_version_id=watermark_version,
                    sha256=hashlib.sha256(watermark).hexdigest(),
                    content_type="image/png",
                    image_format="PNG",
                    width=32,
                    height=12,
                    byte_size=len(watermark),
                    asset_metadata={"purpose": "watermark"},
                    available_at=PLAN_AT,
                )
            )
            await session.commit()

    if x_selected_asset_ids:
        async with approved.database.sessions() as session:
            session.add_all(
                [
                    ReviewXSelection(
                        id=uuid4(),
                        review_task_id=approved.review_task_id,
                        asset_id=asset_id,
                        selected_by_user_id=approved.owner_id,
                        selected_at=PLAN_AT,
                    )
                    for asset_id in x_selected_asset_ids
                ]
            )
            await session.commit()

    async with approved.database.sessions() as session:
        plan = await create_derivative_recipe_and_plan(
            session,
            review_task_id=approved.review_task_id,
            configuration=derivative_recipe_configuration(recipe),
            recipe_version=1,
            renderer_version=renderer_version,
            pillow_version=PIL.__version__,
            created_by_user_id=approved.owner_id,
            approved_by_user_id=approved.owner_id,
            idempotency_key="runtime-derivative-plan",
            output_targets=("full", "x_teaser"),
            watermark_asset_id=watermark_asset_id,
            max_attempts=max_attempts,
            now=PLAN_AT,
        )
    return PreparedRuntime(
        approved=approved,
        store=selected_store,
        recipe=recipe,
        plan=plan,
        watermark_key=watermark_key,
        watermark_version=watermark_version,
    )


async def _cycle(
    prepared: PreparedRuntime,
    *,
    worker_id: str,
    now: datetime = RUN_AT,
    renderer: TestRenderer = _trusted_renderer,
) -> DerivativeCycleResult:
    return await run_derivative_cycle(
        prepared.approved.database.sessions,
        prepared.store,
        worker_id=worker_id,
        lease_seconds=300,
        retry_base_seconds=10,
        retry_max_seconds=60,
        renderer=renderer,
        now=now,
    )


async def _claim(
    prepared: PreparedRuntime,
    *,
    worker_id: str,
    now: datetime = RUN_AT,
) -> ClaimedDerivativeJob:
    async with prepared.approved.database.sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=300,
            now=now,
        )
    assert len(claims) == 1
    return claims[0]


async def _process(
    prepared: PreparedRuntime,
    *,
    claim: ClaimedDerivativeJob,
    worker_id: str,
    now: datetime = RUN_AT,
    renderer: TestRenderer = _trusted_renderer,
) -> DerivativeExecutionResult:
    return await process_claimed_derivative_job(
        prepared.approved.database.sessions,
        prepared.store,
        claim=claim,
        worker_id=worker_id,
        retry_base_seconds=10,
        retry_max_seconds=60,
        renderer=renderer,
        now=now,
    )


@pytest.mark.asyncio
async def test_cycle_renders_only_clean_full_outputs_without_x_selection(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)

    first = await _cycle(prepared, worker_id="derivative-controller-a")
    second = await _cycle(prepared, worker_id="derivative-controller-a")

    assert first.execution is not None
    assert second.execution is not None
    assert first.execution.state == DerivativeJobState.SUCCEEDED
    assert second.execution.state == DerivativeJobState.SUCCEEDED
    assert first.execution.outputs_registered == 1
    assert second.execution.outputs_registered == 1
    async with approved_context.database.sessions() as session:
        release = await session.get(Release, approved_context.release_id)
        derivative_assets = list(
            (await session.scalars(select(Asset).where(Asset.kind == AssetKind.DERIVATIVE))).all()
        )
        output_count = await session.scalar(select(func.count()).select_from(DerivativeOutput))
        lineage_count = await session.scalar(select(func.count()).select_from(AssetLineage))
    assert release is not None
    assert release.phase == ReleasePhase.READY_TO_PUBLISH
    assert output_count == 2
    assert lineage_count == 2
    assert len(derivative_assets) == 2
    for asset in derivative_assets:
        assert asset.object_key is not None
        assert asset.sha256 is not None
        assert asset.object_key.startswith(
            f"derivatives/{approved_context.release_id}/{approved_context.release_version_id}/"
        )
        assert f"/{asset.sha256}." in asset.object_key
        assert asset.object_version_id


@pytest.mark.asyncio
async def test_cycle_executes_a_frozen_legacy_renderer_recipe(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=(approved.raw_asset_ids[0],),
        renderer_version=LEGACY_DERIVATIVE_RENDERER_VERSION,
    )

    async def legacy_renderer(
        source: bytes,
        recipe: DerivativeRecipe,
        watermark: bytes | None,
        targets: tuple[str, ...],
        limits: DerivativeSafetyLimits,
        policy: DerivativeIsolationPolicy,
    ) -> DerivativeBundle:
        assert policy.wall_timeout_seconds >= 1
        return render_platform_derivatives(
            source,
            recipe=recipe,
            watermark_png=watermark,
            targets=targets,
            limits=limits,
            renderer_version=LEGACY_DERIVATIVE_RENDERER_VERSION,
        )

    first = await _cycle(
        prepared,
        worker_id="derivative-controller-legacy-v4",
        renderer=legacy_renderer,
    )
    second = await _cycle(
        prepared,
        worker_id="derivative-controller-legacy-v4",
        renderer=legacy_renderer,
    )
    third = await _cycle(
        prepared,
        worker_id="derivative-controller-legacy-v4",
        renderer=legacy_renderer,
    )

    assert first.execution is not None
    assert second.execution is not None
    assert third.execution is not None
    assert first.execution.state == DerivativeJobState.SUCCEEDED
    assert second.execution.state == DerivativeJobState.SUCCEEDED
    assert third.execution.state == DerivativeJobState.SUCCEEDED
    assert (
        first.execution.outputs_registered
        + second.execution.outputs_registered
        + third.execution.outputs_registered
        == 3
    )


@pytest.mark.asyncio
async def test_cycle_applies_the_frozen_release_full_output_budget(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    seen_limits: list[DerivativeSafetyLimits] = []

    async def capturing_renderer(
        source: bytes,
        recipe: DerivativeRecipe,
        watermark: bytes | None,
        targets: tuple[str, ...],
        limits: DerivativeSafetyLimits,
        policy: DerivativeIsolationPolicy,
    ) -> DerivativeBundle:
        seen_limits.append(limits)
        return await _trusted_renderer(
            source,
            recipe,
            watermark,
            targets,
            limits,
            policy,
        )

    result = await _cycle(
        prepared,
        worker_id="derivative-controller-budget",
        renderer=capturing_renderer,
    )

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.SUCCEEDED
    assert len(seen_limits) == 1
    assert seen_limits[0].max_full_output_bytes == patreon_full_output_byte_budget(2)


@pytest.mark.asyncio
async def test_only_owner_selected_image_gets_watermarked_x_teaser(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    selected_asset_id = approved.raw_asset_ids[0]
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=(selected_asset_id,),
    )

    first = await _cycle(prepared, worker_id="derivative-controller")
    second = await _cycle(prepared, worker_id="derivative-controller")
    third = await _cycle(prepared, worker_id="derivative-controller")

    assert first.execution is not None
    assert second.execution is not None
    assert third.execution is not None
    assert first.execution.outputs_registered == 1
    assert second.execution.outputs_registered == 1
    assert third.execution.outputs_registered == 1
    async with approved.database.sessions() as session:
        rows = (
            await session.execute(
                select(ReleaseSelection.asset_id, DerivativeOutput.target)
                .join(
                    DerivativeJob,
                    DerivativeJob.release_selection_id == ReleaseSelection.id,
                )
                .join(
                    DerivativeOutput,
                    DerivativeOutput.derivative_job_id == DerivativeJob.id,
                )
            )
        ).all()
    targets_by_asset: dict[UUID, set[str]] = {}
    for asset_id, target in rows:
        targets_by_asset.setdefault(asset_id, set()).add(target)
    assert targets_by_asset[selected_asset_id] == {"full", "x_teaser"}
    assert targets_by_asset[approved.raw_asset_ids[1]] == {"full"}


@pytest.mark.asyncio
async def test_inputs_are_read_sequentially_at_frozen_versions(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(
        approved_context,
        with_watermark=True,
        x_selected_asset_ids=(approved_context.raw_asset_ids[0],),
    )

    await _cycle(prepared, worker_id="derivative-controller")
    await _cycle(prepared, worker_id="derivative-controller")
    prepared.store.read_requests.clear()
    result = await _cycle(prepared, worker_id="derivative-controller")

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.SUCCEEDED
    source_read, watermark_read = prepared.store.read_requests[:2]
    assert source_read[0].startswith("raw/")
    assert source_read[2] in {"raw-version-0", "raw-version-1"}
    assert source_read[3] is None
    assert watermark_read[0] == prepared.watermark_key
    assert watermark_read[2] == prepared.watermark_version
    assert watermark_read[3] is None


@pytest.mark.asyncio
async def test_missing_frozen_source_version_retries_without_decoding(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)
    for asset_id in approved_context.raw_asset_ids:
        key = f"raw/{asset_id}.png"
        original = prepared.store.objects[key]
        prepared.store.objects[key] = StoredObject(
            body=original.body,
            content_type=original.content_type,
            metadata=original.metadata,
            version_id="replacement-version",
        )

    result = await _cycle(prepared, worker_id="derivative-controller")

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.RETRY_WAIT
    assert result.execution.error_code == "object_version_unavailable"
    assert prepared.store.write_attempts == []


@pytest.mark.asyncio
async def test_mutated_bytes_at_frozen_version_fail_closed(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)
    for asset_id in approved_context.raw_asset_ids:
        key = f"raw/{asset_id}.png"
        original = prepared.store.objects[key]
        mutated = bytearray(original.body)
        mutated[-12] ^= 1
        prepared.store.objects[key] = StoredObject(
            body=bytes(mutated),
            content_type=original.content_type,
            metadata=original.metadata,
            version_id=original.version_id,
        )

    result = await _cycle(prepared, worker_id="derivative-controller")

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.FAILED
    assert result.execution.error_code == "derivative_contract_invalid"
    assert prepared.store.write_attempts == []


@pytest.mark.asyncio
async def test_mutated_watermark_at_frozen_version_fails_before_render(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(
        approved_context,
        with_watermark=True,
        x_selected_asset_ids=(approved_context.raw_asset_ids[0],),
    )
    assert prepared.watermark_key is not None
    original = prepared.store.objects[prepared.watermark_key]
    mutated = bytearray(original.body)
    mutated[-12] ^= 1
    prepared.store.objects[prepared.watermark_key] = StoredObject(
        body=bytes(mutated),
        content_type=original.content_type,
        metadata=original.metadata,
        version_id=original.version_id,
    )

    first_full = await _cycle(prepared, worker_id="derivative-controller")
    second_full = await _cycle(prepared, worker_id="derivative-controller")
    assert first_full.execution is not None
    assert second_full.execution is not None
    assert first_full.execution.state == DerivativeJobState.SUCCEEDED
    assert second_full.execution.state == DerivativeJobState.SUCCEEDED
    writes_before_x = len(prepared.store.write_attempts)
    prepared.store.read_requests.clear()

    result = await _cycle(prepared, worker_id="derivative-controller")

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.FAILED
    assert result.execution.error_code == "derivative_contract_invalid"
    source_read, watermark_read = prepared.store.read_requests[:2]
    assert source_read[0].startswith("raw/")
    assert watermark_read[0] == prepared.watermark_key
    assert len(prepared.store.write_attempts) == writes_before_x


@pytest.mark.asyncio
async def test_restart_adopts_exact_object_written_before_response_loss(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    store = LostWriteResponseStore()
    prepared = await _prepare(approved_context, store=store)
    first_claim = await _claim(prepared, worker_id="derivative-controller-a")

    first = await _process(
        prepared,
        claim=first_claim,
        worker_id="derivative-controller-a",
    )

    assert first.state == DerivativeJobState.RETRY_WAIT
    derivative_keys = [key for key in store.objects if key.startswith("derivatives/")]
    assert len(derivative_keys) == 1
    written_key = derivative_keys[0]
    written_version = store.objects[written_key].version_id

    retry_at = RUN_AT + timedelta(seconds=11)
    async with approved_context.database.sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id="derivative-controller-b",
            limit=100,
            lease_seconds=300,
            now=retry_at,
        )
    retry_claim = next(claim for claim in claims if claim.job_id == first_claim.job_id)
    second = await _process(
        prepared,
        claim=retry_claim,
        worker_id="derivative-controller-b",
        now=retry_at,
    )

    assert second.state == DerivativeJobState.SUCCEEDED
    assert second.outputs_written == 0
    assert second.outputs_registered == 1
    assert store.objects[written_key].version_id == written_version
    async with approved_context.database.sessions() as session:
        outputs = list(
            (
                await session.scalars(
                    select(DerivativeOutput).where(
                        DerivativeOutput.derivative_job_id == first_claim.job_id
                    )
                )
            ).all()
        )
    assert len(outputs) == 1


@pytest.mark.asyncio
async def test_restart_rejects_existing_object_with_different_bytes(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    store = LostWriteResponseStore()
    prepared = await _prepare(approved_context, store=store)
    first_claim = await _claim(prepared, worker_id="derivative-controller-a")
    first = await _process(
        prepared,
        claim=first_claim,
        worker_id="derivative-controller-a",
    )
    assert first.state == DerivativeJobState.RETRY_WAIT
    written_key = next(key for key in store.objects if key.startswith("derivatives/"))
    original = store.objects[written_key]
    mutated = bytearray(original.body)
    mutated[-2] ^= 1
    store.objects[written_key] = StoredObject(
        body=bytes(mutated),
        content_type=original.content_type,
        metadata=original.metadata,
        version_id=original.version_id,
    )

    retry_at = RUN_AT + timedelta(seconds=11)
    async with approved_context.database.sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id="derivative-controller-b",
            limit=100,
            lease_seconds=300,
            now=retry_at,
        )
    retry_claim = next(claim for claim in claims if claim.job_id == first_claim.job_id)
    second = await _process(
        prepared,
        claim=retry_claim,
        worker_id="derivative-controller-b",
        now=retry_at,
    )

    assert second.state == DerivativeJobState.FAILED
    assert second.error_code == "output_object_conflict"
    async with approved_context.database.sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(DerivativeOutput)
            .where(DerivativeOutput.derivative_job_id == first_claim.job_id)
        )
    assert count == 0


@pytest.mark.asyncio
async def test_cancellation_releases_lease_and_retry_adopts_written_object(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    store = CancellableWriteStore()
    prepared = await _prepare(approved_context, store=store)
    claim = await _claim(
        prepared,
        worker_id="derivative-controller-cancelled",
    )
    task = asyncio.create_task(
        _process(
            prepared,
            claim=claim,
            worker_id="derivative-controller-cancelled",
        )
    )
    await asyncio.wait_for(store.first_write_completed.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    derivative_keys = [key for key in store.objects if key.startswith("derivatives/")]
    assert len(derivative_keys) == 1
    preserved_version = store.objects[derivative_keys[0]].version_id
    async with approved_context.database.sessions() as session:
        cancelled_job = await session.get(DerivativeJob, claim.job_id)
        registered_count = await session.scalar(
            select(func.count())
            .select_from(DerivativeOutput)
            .where(DerivativeOutput.derivative_job_id == claim.job_id)
        )
    assert cancelled_job is not None
    assert cancelled_job.state == DerivativeJobState.RETRY_WAIT
    assert cancelled_job.last_error_code == "execution_cancelled"
    assert cancelled_job.last_error_detail == (
        "Derivative execution failed inside the bounded processing boundary."
    )
    assert cancelled_job.lease_owner is None
    assert cancelled_job.lease_expires_at is None
    assert registered_count == 0

    retry_at = RUN_AT + timedelta(seconds=11)
    async with approved_context.database.sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id="derivative-controller-retry",
            limit=100,
            lease_seconds=300,
            now=retry_at,
        )
    retry_claim = next(candidate for candidate in claims if candidate.job_id == claim.job_id)
    retried = await _process(
        prepared,
        claim=retry_claim,
        worker_id="derivative-controller-retry",
        now=retry_at,
    )

    assert retried.state == DerivativeJobState.SUCCEEDED
    assert retried.outputs_written == 0
    assert retried.outputs_registered == 1
    assert store.objects[derivative_keys[0]].version_id == preserved_version
    async with approved_context.database.sessions() as session:
        registered_count = await session.scalar(
            select(func.count())
            .select_from(DerivativeOutput)
            .where(DerivativeOutput.derivative_job_id == claim.job_id)
        )
    assert registered_count == 1


@pytest.mark.asyncio
async def test_completed_job_replay_does_not_render_or_rewrite(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)
    claim = await _claim(prepared, worker_id="derivative-controller")
    first = await _process(
        prepared,
        claim=claim,
        worker_id="derivative-controller",
    )
    write_count = len(prepared.store.write_attempts)

    async def forbidden_renderer(
        _source: bytes,
        _recipe: DerivativeRecipe,
        _watermark: bytes | None,
        _targets: tuple[str, ...],
        _limits: DerivativeSafetyLimits,
        _policy: DerivativeIsolationPolicy,
    ) -> DerivativeBundle:
        raise AssertionError("completed replay attempted to render")

    replay = await _process(
        prepared,
        claim=claim,
        worker_id="derivative-controller",
        renderer=forbidden_renderer,
    )

    assert first.state == DerivativeJobState.SUCCEEDED
    assert replay.state == DerivativeJobState.SUCCEEDED
    assert replay.replayed is True
    assert len(prepared.store.write_attempts) == write_count


@pytest.mark.asyncio
async def test_two_controllers_claim_distinct_jobs_without_duplicate_outputs(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)
    first_claim = await _claim(
        prepared,
        worker_id="derivative-controller-a",
    )
    second_claim = await _claim(
        prepared,
        worker_id="derivative-controller-b",
    )

    first, second = await asyncio.gather(
        _process(
            prepared,
            claim=first_claim,
            worker_id="derivative-controller-a",
        ),
        _process(
            prepared,
            claim=second_claim,
            worker_id="derivative-controller-b",
        ),
    )

    assert {first.job_id, second.job_id} == set(prepared.plan.job_ids)
    assert first.state == DerivativeJobState.SUCCEEDED
    assert second.state == DerivativeJobState.SUCCEEDED
    async with approved_context.database.sessions() as session:
        output_count = await session.scalar(select(func.count()).select_from(DerivativeOutput))
        release = await session.get(Release, approved_context.release_id)
    assert output_count == 2
    assert release is not None
    assert release.phase == ReleasePhase.READY_TO_PUBLISH
    assert len(set(prepared.store.write_attempts)) == 2


@pytest.mark.asyncio
async def test_expired_exhausted_lease_is_dead_lettered_before_new_claim(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context, max_attempts=1)
    claim = await _claim(prepared, worker_id="abandoned-controller")

    recovered = await run_derivative_cycle(
        approved_context.database.sessions,
        prepared.store,
        worker_id="recovery-controller",
        lease_seconds=300,
        retry_base_seconds=10,
        retry_max_seconds=60,
        renderer=_trusted_renderer,
        now=claim.lease_expires_at + timedelta(seconds=1),
    )

    assert recovered.recovered_expired_lease is True
    assert recovered.claimed_job is False
    async with approved_context.database.sessions() as session:
        job = await session.get(DerivativeJob, claim.job_id)
    assert job is not None
    assert job.state == DerivativeJobState.FAILED
    assert job.last_error_code == "execution_lease_expired"
    assert job.lease_owner is None
    assert job.lease_expires_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            DerivativeIsolationTimeoutError("secret=https://signed.example/timeout"),
            "renderer_timeout",
        ),
        (
            MemoryError("secret=https://signed.example/oom"),
            "renderer_memory_limit",
        ),
        (
            DerivativeIsolationCrashError("secret=https://signed.example/crash"),
            "renderer_crash",
        ),
        (
            DerivativeIsolationProtocolError("secret=https://signed.example/protocol"),
            "renderer_protocol_error",
        ),
    ],
)
async def test_renderer_failures_are_bounded_and_redacted(
    derivative_approved_context: ApprovedContext,
    error: Exception,
    expected_code: str,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context, max_attempts=1)

    async def broken_renderer(
        _source: bytes,
        _recipe: DerivativeRecipe,
        _watermark: bytes | None,
        _targets: tuple[str, ...],
        _limits: DerivativeSafetyLimits,
        _policy: DerivativeIsolationPolicy,
    ) -> DerivativeBundle:
        raise error

    result = await _cycle(
        prepared,
        worker_id="derivative-controller",
        renderer=broken_renderer,
    )

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.FAILED
    assert result.execution.error_code == expected_code
    async with approved_context.database.sessions() as session:
        job = await session.get(DerivativeJob, result.execution.job_id)
    assert job is not None
    assert job.last_error_code == expected_code
    assert job.last_error_detail == (
        "Derivative execution failed inside the bounded processing boundary."
    )
    assert "secret" not in job.last_error_detail
    assert "signed.example" not in job.last_error_detail


@pytest.mark.asyncio
async def test_stale_release_phase_is_rejected_before_object_reads(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)
    async with approved_context.database.sessions() as session:
        release = await session.get(Release, approved_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.APPROVED
        await session.commit()

    result = await _cycle(prepared, worker_id="derivative-controller")

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.FAILED
    assert result.execution.error_code == "derivative_contract_invalid"
    assert prepared.store.read_requests == []
    assert prepared.store.write_attempts == []


@pytest.mark.asyncio
async def test_noncurrent_release_version_is_rejected_before_object_reads(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved_context = derivative_approved_context
    prepared = await _prepare(approved_context)
    async with approved_context.database.sessions() as session:
        release = await session.get(Release, approved_context.release_id)
        assert release is not None
        release.current_version_no = 2
        await session.commit()

    result = await _cycle(prepared, worker_id="derivative-controller")

    assert result.execution is not None
    assert result.execution.state == DerivativeJobState.FAILED
    assert result.execution.error_code == "derivative_contract_invalid"
    assert prepared.store.read_requests == []
    assert prepared.store.write_attempts == []
