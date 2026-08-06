# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from gen_automation.db.models import (
    DerivativeJob,
    DerivativeOutput,
    ReleaseSelection,
)
from gen_automation.domain.enums import PublicationTarget
from gen_automation.services import publication
from gen_automation.services.publication import (
    PublicationInputError,
    _initial_attempt_available_at,
    _load_frozen_outputs,
)
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _cycle, _prepare


def test_scheduled_patreon_creation_is_due_at_approval_while_x_waits() -> None:
    approved_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    scheduled_at = approved_at + timedelta(days=14)

    assert (
        _initial_attempt_available_at(
            target=PublicationTarget.PATREON,
            scheduled_at=scheduled_at,
            approved_at=approved_at,
        )
        == approved_at
    )
    assert (
        _initial_attempt_available_at(
            target=PublicationTarget.X,
            scheduled_at=scheduled_at,
            approved_at=approved_at,
        )
        == scheduled_at
    )


def test_immediate_publication_attempts_remain_due_at_approval() -> None:
    approved_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    for target in PublicationTarget:
        assert (
            _initial_attempt_available_at(
                target=target,
                scheduled_at=None,
                approved_at=approved_at,
            )
            == approved_at
        )


@pytest.mark.asyncio
async def test_publication_uses_exact_selected_x_and_only_clean_patreon_inputs(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=approved.raw_asset_ids,
    )
    await _cycle(prepared, worker_id="derivative-controller")
    await _cycle(prepared, worker_id="derivative-controller")
    await _cycle(prepared, worker_id="derivative-controller")
    await _cycle(prepared, worker_id="derivative-controller")

    async with approved.database.sessions() as session:
        rows = (
            await session.execute(
                select(
                    ReleaseSelection.asset_id,
                    ReleaseSelection.display_order,
                    DerivativeOutput.id,
                    DerivativeOutput.target,
                )
                .join(
                    DerivativeJob,
                    DerivativeJob.release_selection_id == ReleaseSelection.id,
                )
                .join(
                    DerivativeOutput,
                    DerivativeOutput.derivative_job_id == DerivativeJob.id,
                )
                .order_by(ReleaseSelection.display_order, DerivativeOutput.target)
            )
        ).all()
        x_ids = tuple(output_id for _, _, output_id, target in rows if target == "x_teaser")
        full_ids = tuple(output_id for _, _, output_id, target in rows if target == "full")
        assert len(x_ids) == len(full_ids) == 2

        with pytest.raises(PublicationInputError, match="exactly match"):
            await _load_frozen_outputs(
                session,
                release_version_id=approved.release_version_id,
                target=PublicationTarget.X,
                derivative_output_ids=tuple(reversed(x_ids)),
                public_preview_output_id=None,
            )
        with pytest.raises(PublicationInputError, match="exactly match"):
            await _load_frozen_outputs(
                session,
                release_version_id=approved.release_version_id,
                target=PublicationTarget.X,
                derivative_output_ids=(x_ids[0], full_ids[1]),
                public_preview_output_id=None,
            )

        x_inputs = await _load_frozen_outputs(
            session,
            release_version_id=approved.release_version_id,
            target=PublicationTarget.X,
            derivative_output_ids=x_ids,
            public_preview_output_id=None,
        )
        assert [item.output.id for item in x_inputs] == list(x_ids)
        assert all(item.output.target == "x_teaser" for item in x_inputs)

        with pytest.raises(PublicationInputError, match="all accepted full outputs"):
            await _load_frozen_outputs(
                session,
                release_version_id=approved.release_version_id,
                target=PublicationTarget.PATREON,
                derivative_output_ids=(full_ids[0],),
                public_preview_output_id=full_ids[0],
            )

        patreon_inputs = await _load_frozen_outputs(
            session,
            release_version_id=approved.release_version_id,
            target=PublicationTarget.PATREON,
            derivative_output_ids=full_ids,
            public_preview_output_id=full_ids[0],
        )
        assert [item.role for item in patreon_inputs] == [
            "patreon_content",
            "patreon_content",
            "patreon_preview",
        ]
        assert all(item.output.target == "full" for item in patreon_inputs)


@pytest.mark.asyncio
async def test_patreon_inputs_reject_an_aggregate_that_cannot_be_packaged(
    derivative_approved_context: ApprovedContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="derivative-controller")
    await _cycle(prepared, worker_id="derivative-controller")

    async with approved.database.sessions() as session:
        full_outputs = tuple(
            (
                await session.scalars(
                    select(DerivativeOutput)
                    .join(
                        DerivativeJob,
                        DerivativeJob.id == DerivativeOutput.derivative_job_id,
                    )
                    .join(
                        ReleaseSelection,
                        ReleaseSelection.id == DerivativeJob.release_selection_id,
                    )
                    .where(DerivativeOutput.target == "full")
                    .order_by(ReleaseSelection.display_order)
                )
            ).all()
        )
        output_ids = tuple(output.id for output in full_outputs)
        aggregate_bytes = sum(output.asset_byte_size for output in full_outputs) + (
            full_outputs[0].asset_byte_size
        )
        monkeypatch.setattr(
            publication,
            "PATREON_MAX_TOTAL_IMAGE_BYTES",
            aggregate_bytes - 1,
        )

        with pytest.raises(PublicationInputError, match="combined Patreon content"):
            await _load_frozen_outputs(
                session,
                release_version_id=approved.release_version_id,
                target=PublicationTarget.PATREON,
                derivative_output_ids=output_ids,
                public_preview_output_id=output_ids[0],
            )
