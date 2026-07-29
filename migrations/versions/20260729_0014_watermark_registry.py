"""Allow immutable registered watermarks to be reused across releases.

Revision ID: 20260729_0014
Revises: 20260728_0013
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_0014"
down_revision: str | None = "20260728_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS derivative_recipes_guard_insert"))
        op.execute(sa.text(_SQLITE_GLOBAL_WATERMARK_TRIGGER))
        return
    op.execute(sa.text(_POSTGRES_GLOBAL_WATERMARK_FUNCTION))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS derivative_recipes_guard_insert"))
        op.execute(sa.text(_SQLITE_RELEASE_WATERMARK_TRIGGER))
        return
    op.execute(sa.text(_POSTGRES_RELEASE_WATERMARK_FUNCTION))


_SQLITE_GLOBAL_WATERMARK_TRIGGER = """
CREATE TRIGGER derivative_recipes_guard_insert
BEFORE INSERT ON derivative_recipes
WHEN NEW.watermark_asset_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM assets AS asset
        WHERE asset.id = NEW.watermark_asset_id
          AND asset.kind = 'derivative'
          AND asset.state = 'available'
          AND asset.content_type = 'image/png'
          AND asset.image_format = 'PNG'
          AND json_extract(asset.metadata, '$.purpose') = 'watermark'
          AND asset.storage_backend = NEW.watermark_storage_backend
          AND asset.storage_bucket = NEW.watermark_storage_bucket
          AND asset.object_key = NEW.watermark_object_key
          AND asset.object_version_id = NEW.watermark_object_version_id
          AND asset.sha256 = NEW.watermark_sha256
          AND asset.content_type = NEW.watermark_content_type
          AND asset.image_format = NEW.watermark_image_format
          AND asset.width = NEW.watermark_width
          AND asset.height = NEW.watermark_height
          AND asset.byte_size = NEW.watermark_byte_size
    ) THEN RAISE(
        ABORT,
        'derivative recipe watermark snapshot is invalid'
    ) END;
END
"""

_POSTGRES_GLOBAL_WATERMARK_FUNCTION = """
CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_recipe_mutation()
RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'derivative recipes are immutable';
    END IF;
    IF NEW.watermark_asset_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM assets AS asset
           WHERE asset.id = NEW.watermark_asset_id
             AND asset.kind = 'derivative'
             AND asset.state = 'available'
             AND asset.content_type = 'image/png'
             AND asset.image_format = 'PNG'
             AND asset.metadata ->> 'purpose' = 'watermark'
             AND asset.storage_backend = NEW.watermark_storage_backend
             AND asset.storage_bucket = NEW.watermark_storage_bucket
             AND asset.object_key = NEW.watermark_object_key
             AND asset.object_version_id = NEW.watermark_object_version_id
             AND asset.sha256 = NEW.watermark_sha256
             AND asset.content_type = NEW.watermark_content_type
             AND asset.image_format = NEW.watermark_image_format
             AND asset.width = NEW.watermark_width
             AND asset.height = NEW.watermark_height
             AND asset.byte_size = NEW.watermark_byte_size
       ) THEN
        RAISE EXCEPTION 'derivative recipe watermark snapshot is invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_SQLITE_RELEASE_WATERMARK_TRIGGER = """
CREATE TRIGGER derivative_recipes_guard_insert
BEFORE INSERT ON derivative_recipes
WHEN NEW.watermark_asset_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM assets AS asset
        JOIN release_versions AS version
          ON version.id = NEW.release_version_id
        WHERE asset.id = NEW.watermark_asset_id
          AND asset.release_id = version.release_id
          AND asset.state = 'available'
          AND asset.storage_backend = NEW.watermark_storage_backend
          AND asset.storage_bucket = NEW.watermark_storage_bucket
          AND asset.object_key = NEW.watermark_object_key
          AND asset.object_version_id = NEW.watermark_object_version_id
          AND asset.sha256 = NEW.watermark_sha256
          AND asset.content_type = NEW.watermark_content_type
          AND asset.image_format = NEW.watermark_image_format
          AND asset.width = NEW.watermark_width
          AND asset.height = NEW.watermark_height
          AND asset.byte_size = NEW.watermark_byte_size
    ) THEN RAISE(
        ABORT,
        'derivative recipe watermark snapshot is invalid'
    ) END;
END
"""

_POSTGRES_RELEASE_WATERMARK_FUNCTION = """
CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_recipe_mutation()
RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'derivative recipes are immutable';
    END IF;
    IF NEW.watermark_asset_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM assets AS asset
           JOIN release_versions AS version
             ON version.id = NEW.release_version_id
           WHERE asset.id = NEW.watermark_asset_id
             AND asset.release_id = version.release_id
             AND asset.state = 'available'
             AND asset.storage_backend = NEW.watermark_storage_backend
             AND asset.storage_bucket = NEW.watermark_storage_bucket
             AND asset.object_key = NEW.watermark_object_key
             AND asset.object_version_id = NEW.watermark_object_version_id
             AND asset.sha256 = NEW.watermark_sha256
             AND asset.content_type = NEW.watermark_content_type
             AND asset.image_format = NEW.watermark_image_format
             AND asset.width = NEW.watermark_width
             AND asset.height = NEW.watermark_height
             AND asset.byte_size = NEW.watermark_byte_size
       ) THEN
        RAISE EXCEPTION 'derivative recipe watermark snapshot is invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
