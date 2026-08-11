"""Add the immutable HQ Animation Studio profile contract.

Revision ID: 20260811_0035
Revises: 20260810_0034
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0035"
down_revision: str | None = "20260810_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_PROFILE_ID = "wan2.2-ti2v-5b-comfy-v1"
_HQ_PROFILE_ID = "wan2.2-ti2v-5b-comfy-hq-v1"
_V1_DIMENSIONS = "(width = 832 AND height = 480) OR (width = 480 AND height = 832)"
_PROFILE_CONTRACT = (
    f"(profile_key = '{_V1_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-v1' "
    "AND profile_sha256 = "
    "'a83c946f9a61bac7cf3794fc9aa4debacc2fc676c13957deaed42ecf82c7e2e4' "
    "AND frame_count IN (73, 121) AND max_attempts = 3 "
    f"AND ({_V1_DIMENSIONS})) "
    f"OR (profile_key = '{_HQ_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-hq-v1' "
    "AND profile_sha256 = "
    "'00fb341e491f295b2db16a32626a6383d83c6cda88978b29479caf245c817387' "
    "AND frame_count = 73 AND max_attempts = 1 "
    "AND ((width = 1472 AND height = 1152) OR (width = 1152 AND height = 1472)))"
)


def upgrade() -> None:
    with op.batch_alter_table("video_generation_jobs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_video_generation_jobs_supported_dimensions"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_profile_contract"),
            _PROFILE_CONTRACT,
        )


def downgrade() -> None:
    connection = op.get_bind()
    hq_rows = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM video_generation_jobs WHERE profile_key = :profile_id"
        ).bindparams(profile_id=_HQ_PROFILE_ID)
    )
    if hq_rows:
        raise RuntimeError("cannot downgrade while HQ video jobs exist")
    with op.batch_alter_table("video_generation_jobs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_video_generation_jobs_supported_profile_contract"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_dimensions"),
            _V1_DIMENSIONS,
        )
