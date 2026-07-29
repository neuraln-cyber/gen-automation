# ruff: noqa: F811

import pytest
from sqlalchemy import select

from gen_automation.db.models import (
    DerivativeJob,
    DerivativeOutput,
    ReleaseSelection,
)
from gen_automation.domain.enums import PublicationTarget
from gen_automation.services.publication import (
    PublicationInputError,
    _load_frozen_outputs,
)
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _cycle, _prepare


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
