"""Add the bounded SmoothMix A14B Animation Studio contracts.

Revision ID: 20260811_0036
Revises: 20260811_0035
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0036"
down_revision: str | None = "20260811_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_PROFILE_ID = "wan2.2-ti2v-5b-comfy-v1"
_HQ_PROFILE_ID = "wan2.2-ti2v-5b-comfy-hq-v1"
_A14B_PROFILE_ID = "wan2.2-smoothmix-i2v-a14b-q3-v1"
_A14B_ADULT_PROFILE_ID = "wan2.2-smoothmix-i2v-a14b-q3-adult-v1"

_A14B_JOB_CONTRACT_SHA256 = "3c31ee0e82b15c582dfccfb5cc83bde80b55dd835f4afd217f3ce342accc6e99"
_A14B_ADULT_JOB_CONTRACT_SHA256 = "014fc4502c334bdcd6ec9a8acad08b7599316dd0ddf3788e2101f40588d3e859"

_A14B_OUTPUT_CONTRACT = (
    "width BETWEEN 2 AND 2048 AND height BETWEEN 2 AND 2048 "
    "AND width % 2 = 0 AND height % 2 = 0 "
    "AND CAST(width AS BIGINT) * height <= 4194304 "
    "AND width = source_width + (source_width % 2) "
    "AND height = source_height + (source_height % 2) "
    "AND 13 * CAST(source_width AS BIGINT) >= 4 * source_height "
    "AND 4 * CAST(source_width AS BIGINT) <= 13 * source_height"
)

_PROFILE_CONTRACT = (
    f"(profile_key = '{_V1_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-v1' "
    "AND profile_sha256 = "
    "'a83c946f9a61bac7cf3794fc9aa4debacc2fc676c13957deaed42ecf82c7e2e4' "
    "AND frame_count IN (73, 121) AND fps = 24 AND loop_mode = 'ping_pong' "
    "AND max_attempts = 3 "
    "AND ((width = 832 AND height = 480) OR (width = 480 AND height = 832))) "
    f"OR (profile_key = '{_HQ_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-hq-v1' "
    "AND profile_sha256 = "
    "'00fb341e491f295b2db16a32626a6383d83c6cda88978b29479caf245c817387' "
    "AND frame_count = 73 AND fps = 24 AND loop_mode = 'ping_pong' "
    "AND max_attempts = 1 "
    "AND ((width = 1472 AND height = 1152) OR (width = 1152 AND height = 1472))) "
    f"OR (profile_key = '{_A14B_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-smoothmix-i2v-a14b-q3-v1' "
    f"AND profile_sha256 = '{_A14B_JOB_CONTRACT_SHA256}' "
    "AND frame_count = 81 AND fps = 16 AND loop_mode = 'forward' "
    "AND max_attempts = 1 AND content_rating = 'sfw' "
    f"AND ({_A14B_OUTPUT_CONTRACT})) "
    f"OR (profile_key = '{_A14B_ADULT_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-smoothmix-i2v-a14b-q3-adult-v1' "
    f"AND profile_sha256 = '{_A14B_ADULT_JOB_CONTRACT_SHA256}' "
    "AND frame_count = 81 AND fps = 16 AND loop_mode = 'forward' "
    "AND max_attempts = 1 AND content_rating IN ('nsfw', 'explicit') "
    f"AND ({_A14B_OUTPUT_CONTRACT}))"
)

_LEGACY_PROFILE_CONTRACT = (
    f"(profile_key = '{_V1_PROFILE_ID}' "
    "AND profile_version = 'video-worker-adapter-v1' "
    "AND profile_sha256 = "
    "'a83c946f9a61bac7cf3794fc9aa4debacc2fc676c13957deaed42ecf82c7e2e4' "
    "AND frame_count IN (73, 121) AND max_attempts = 3 "
    "AND ((width = 832 AND height = 480) OR (width = 480 AND height = 832))) "
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
            op.f("ck_video_generation_jobs_supported_frame_count"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_video_generation_jobs_fixed_fps"), type_="check")
        batch_op.drop_constraint(
            op.f("ck_video_generation_jobs_supported_profile_contract"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_video_generation_jobs_ping_pong_loop"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_frame_count"),
            "frame_count IN (73, 81, 121)",
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_fps"), "fps IN (16, 24)"
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_profile_contract"), _PROFILE_CONTRACT
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_loop_mode"),
            "loop_mode IN ('forward', 'ping_pong')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    a14b_rows = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM video_generation_jobs "
            "WHERE profile_key IN (:base_profile_id, :adult_profile_id)"
        ).bindparams(
            base_profile_id=_A14B_PROFILE_ID,
            adult_profile_id=_A14B_ADULT_PROFILE_ID,
        )
    )
    if a14b_rows:
        raise RuntimeError("cannot downgrade while SmoothMix A14B video jobs exist")
    with op.batch_alter_table("video_generation_jobs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_video_generation_jobs_supported_frame_count"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_video_generation_jobs_supported_fps"), type_="check")
        batch_op.drop_constraint(
            op.f("ck_video_generation_jobs_supported_profile_contract"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_video_generation_jobs_supported_loop_mode"), type_="check"
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_frame_count"),
            "frame_count IN (73, 121)",
        )
        batch_op.create_check_constraint(op.f("ck_video_generation_jobs_fixed_fps"), "fps = 24")
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_supported_profile_contract"),
            _LEGACY_PROFILE_CONTRACT,
        )
        batch_op.create_check_constraint(
            op.f("ck_video_generation_jobs_ping_pong_loop"), "loop_mode = 'ping_pong'"
        )
