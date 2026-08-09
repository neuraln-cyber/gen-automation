# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from gen_automation.db.models import (
    DerivativeJob,
    DerivativeOutput,
    ReleaseSelection,
)
from gen_automation.domain.enums import PublicationTarget
from gen_automation.integrations.patreon import (
    PatreonPackageImage,
    PublicPreviewSafetyAttestation,
    build_patreon_handoff_package,
)
from gen_automation.services import publication
from gen_automation.services.publication import (
    PublicationInputError,
    _approval_expires_at,
    _initial_attempt_available_at,
    _load_frozen_outputs,
)
from tests.image_privacy_assertions import (
    assert_delivery_metadata_absent,
    assert_private_master_metadata_present,
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
    assert _approval_expires_at(
        approved_at=approved_at,
        available_at=scheduled_at,
        approval_seconds=900,
    ) == scheduled_at + timedelta(minutes=15)


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


def test_x_schedule_horizon_rejects_distant_typographical_dates() -> None:
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)

    with pytest.raises(PublicationInputError, match="too far in the future"):
        publication._optional_future_datetime(
            now + timedelta(days=367),
            now=now,
            label="scheduled_at",
            maximum_future=publication.X_MAX_SCHEDULE_HORIZON,
        )


def test_x_configuration_freezes_independent_labels_and_defaults_legacy_true() -> None:
    assert publication._normalize_configuration(
        PublicationTarget.X,
        {"text": "Adult preview", "adult_content": False, "made_with_ai": False},
    ) == {"text": "Adult preview", "adult_content": False, "made_with_ai": False}
    assert publication._normalize_configuration(
        PublicationTarget.X,
        {"text": "Legacy preview"},
    ) == {"text": "Legacy preview", "adult_content": True, "made_with_ai": True}

    with pytest.raises(PublicationInputError, match="adult_content must be a boolean"):
        publication._normalize_configuration(
            PublicationTarget.X,
            {"text": "Invalid preview", "adult_content": 1},
        )
    with pytest.raises(PublicationInputError, match="made_with_ai must be a boolean"):
        publication._normalize_configuration(
            PublicationTarget.X,
            {"text": "Invalid preview", "made_with_ai": 1},
        )
    with pytest.raises(PublicationInputError, match="optional adult_content and made_with_ai"):
        publication._normalize_configuration(
            PublicationTarget.X,
            {"text": "Unexpected option", "unrecognized": False},
        )
    with pytest.raises(PublicationInputError, match="280 UTF-8 bytes"):
        publication._normalize_configuration(
            PublicationTarget.X,
            {"text": "界" * 94, "adult_content": False},
        )


@pytest.mark.parametrize(
    ("validator", "identifier", "remote_url", "message"),
    (
        (
            publication._validate_x_post_identity,
            "1",
            "https://x.com:bad/example/status/1",
            "exact HTTPS X URL",
        ),
        (
            publication._validate_x_post_identity,
            "1",
            "https://x.com:99999/example/status/1",
            "exact HTTPS X URL",
        ),
        (
            publication._validate_x_post_identity,
            "1",
            "https://[not-an-ipv6]/example/status/1",
            "exact HTTPS X URL",
        ),
        (
            publication.validate_patreon_post_identity,
            "1",
            "https://patreon.com:bad/posts/example-1",
            "exact HTTPS Patreon URL",
        ),
    ),
)
def test_post_reconciliation_rejects_malformed_urls_without_server_errors(
    validator,
    identifier: str,
    remote_url: str,
    message: str,
) -> None:
    with pytest.raises(PublicationInputError, match=message):
        validator(identifier, remote_url)


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
    for asset_id, source in zip(approved.raw_asset_ids, approved.raw_payloads, strict=True):
        assert_private_master_metadata_present(source)
        assert prepared.store.objects[f"raw/{asset_id}.png"].body == source

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
        x_payloads = tuple(
            prepared.store.objects[item.output.asset_object_key].body for item in x_inputs
        )
        for payload in x_payloads:
            assert_delivery_metadata_absent(payload)

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
        patreon_payloads = tuple(
            prepared.store.objects[item.output.asset_object_key].body for item in patreon_inputs
        )
        for payload in patreon_payloads:
            assert_delivery_metadata_absent(payload)

    package = build_patreon_handoff_package(
        approved_derivatives=tuple(
            PatreonPackageImage(f"content-{index}.jpg", payload)
            for index, payload in enumerate(patreon_payloads[:2], start=1)
        ),
        public_preview=PatreonPackageImage("preview.jpg", patreon_payloads[2]),
        title="Metadata boundary",
        body="",
        tier="Members",
        tags=(),
        scheduled_at=None,
        public_preview_attestation=PublicPreviewSafetyAttestation(
            safe_for_public=True,
            attested_by="test-owner",
            attested_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        ),
    )
    with ZipFile(BytesIO(package.archive_bytes)) as archive:
        packaged_payloads = tuple(
            archive.read(path) for path in ("content/001.jpg", "content/002.jpg")
        )
        assert packaged_payloads == patreon_payloads[:2]
        for payload in (*packaged_payloads, archive.read("public-preview/preview.jpg")):
            assert_delivery_metadata_absent(payload)
    for asset_id, source in zip(approved.raw_asset_ids, approved.raw_payloads, strict=True):
        assert prepared.store.objects[f"raw/{asset_id}.png"].body == source
        assert_private_master_metadata_present(source)


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
