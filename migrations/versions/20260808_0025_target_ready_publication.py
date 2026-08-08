"""Permit independently ready publication targets in active release phases.

Revision ID: 20260808_0025
Revises: 20260808_0024
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0025"
down_revision: str | None = "20260808_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASE_PLACEHOLDER = "__RELEASE_PHASE_PREDICATE__"
_READY_ONLY = "release.phase = 'ready_to_publish'"
_TARGET_READY = "release.phase IN ('rendering', 'ready_to_publish', 'publishing', 'published')"

_PUBLICATION_INPUT_GUARD = """
CREATE OR REPLACE FUNCTION gen_automation_guard_publication_input()
RETURNS trigger AS $$ BEGIN
IF TG_OP <> 'INSERT' THEN
RAISE EXCEPTION 'publication inputs are append-only'; END IF;
IF NOT EXISTS (
SELECT 1 FROM publication_intents AS intent
JOIN release_versions AS version ON version.id = intent.release_version_id
JOIN releases AS release ON release.id = intent.release_id
JOIN derivative_outputs AS output ON output.id = NEW.derivative_output_id
JOIN derivative_jobs AS job ON job.id = output.derivative_job_id
JOIN assets AS asset ON asset.id = output.asset_id
WHERE intent.id = NEW.intent_id
AND intent.state = 'awaiting_approval'
AND version.release_id = release.id
AND release.current_version_no = version.version_no
AND __RELEASE_PHASE_PREDICATE__
AND job.release_version_id = version.id
AND output.derivative_recipe_id = NEW.derivative_recipe_id
AND output.asset_id = NEW.asset_id
AND output.target = NEW.derivative_target
AND output.asset_storage_backend = NEW.asset_storage_backend
AND output.asset_storage_bucket = NEW.asset_storage_bucket
AND output.asset_object_key = NEW.asset_object_key
AND output.asset_object_version_id = NEW.asset_object_version_id
AND output.asset_sha256 = NEW.asset_sha256
AND output.asset_content_type = NEW.asset_content_type
AND output.asset_image_format = NEW.asset_image_format
AND output.asset_width = NEW.asset_width
AND output.asset_height = NEW.asset_height
AND output.asset_byte_size = NEW.asset_byte_size
AND asset.state = 'available' AND asset.kind = 'derivative'
) THEN RAISE EXCEPTION 'publication input snapshot is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql
"""

_PUBLICATION_APPROVAL_GUARD = """
CREATE OR REPLACE FUNCTION gen_automation_guard_publication_approval()
RETURNS trigger AS $$ BEGIN
IF TG_OP <> 'INSERT' THEN
RAISE EXCEPTION 'publication approvals are append-only'; END IF;
IF NOT EXISTS (
SELECT 1 FROM publication_intents AS intent
JOIN release_versions AS version ON version.id = intent.release_version_id
JOIN releases AS release ON release.id = intent.release_id
JOIN admin_users AS actor ON actor.id = NEW.actor_user_id
WHERE intent.id = NEW.intent_id
AND intent.intent_digest = NEW.intent_digest
AND NEW.intent_lock_version = intent.lock_version + 1
AND actor.is_active AND actor.role IN ('owner', 'publisher')
AND actor.role = NEW.actor_role
AND version.release_id = release.id
AND release.current_version_no = version.version_no
AND __RELEASE_PHASE_PREDICATE__
AND (SELECT count(*) FROM publication_inputs AS input
WHERE input.intent_id = intent.id) = intent.input_count
AND (
(intent.target = 'x'
AND NOT EXISTS (SELECT 1 FROM publication_inputs AS input
WHERE input.intent_id = intent.id AND input.role <> 'x_teaser'))
OR (intent.target = 'patreon'
AND (SELECT count(*) FROM publication_inputs AS input
WHERE input.intent_id = intent.id AND input.role = 'patreon_preview') = 1
AND EXISTS (SELECT 1 FROM publication_inputs AS input
WHERE input.intent_id = intent.id AND input.role = 'patreon_content'))
)
) THEN RAISE EXCEPTION 'publication approval snapshot is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    with op.batch_alter_table("publication_intents") as batch_op:
        batch_op.drop_constraint(
            "uq_publication_intents_version_target_config",
            type_="unique",
        )
    op.create_index(
        "uq_publication_intents_release_target_canonical",
        "publication_intents",
        ["release_id", "target"],
        unique=True,
        sqlite_where=sa.text("state NOT IN ('failed', 'cancelled')"),
        postgresql_where=sa.text("state NOT IN ('failed', 'cancelled')"),
    )
    _replace_guards(_TARGET_READY)


def downgrade() -> None:
    _replace_guards(_READY_ONLY)
    op.drop_index(
        "uq_publication_intents_release_target_canonical",
        table_name="publication_intents",
    )
    with op.batch_alter_table("publication_intents") as batch_op:
        batch_op.create_unique_constraint(
            "uq_publication_intents_version_target_config",
            ["release_version_id", "target", "configuration_sha256"],
        )


def _replace_guards(phase_predicate: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(_PUBLICATION_INPUT_GUARD.replace(_PHASE_PLACEHOLDER, phase_predicate)))
    op.execute(sa.text(_PUBLICATION_APPROVAL_GUARD.replace(_PHASE_PLACEHOLDER, phase_predicate)))
