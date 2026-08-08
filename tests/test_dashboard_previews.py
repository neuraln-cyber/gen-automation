import asyncio
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from typing import cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from gen_automation.api.security import require_authenticated_principal
from gen_automation.db.models import Asset, GenerationJob, Project, Release, ReleaseVersion
from gen_automation.domain.enums import AssetKind, AssetState
from gen_automation.services import dashboard_previews
from gen_automation.services.dashboard_previews import DashboardPreview
from gen_automation.services.outbound_image_privacy import require_metadata_free_image
from gen_automation.storage.base import ObjectStore
from gen_automation.storage.memory import MemoryObjectStore

ASSET_ID = UUID("00000000-0000-4000-8000-00000000d501")
RELEASE_ID = UUID("00000000-0000-4000-8000-00000000d502")
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _source_png() -> bytes:
    image = Image.new("RGB", (1150, 1487), (120, 42, 160))
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("parameters", "private prompt and generation settings")
    image.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


async def _seed_preview_asset(
    client: TestClient,
    store: MemoryObjectStore,
    source: bytes,
) -> str:
    source_sha256 = hashlib.sha256(source).hexdigest()
    key = f"raw/{ASSET_ID}.png"
    store.put_for_test(key, source, content_type="image/png")
    version_id = store.objects[key].version_id
    async with client.app.state.database.sessions() as session:
        project = Project(slug="preview-test", name="Preview Test")
        session.add(project)
        await session.flush()
        release = Release(
            id=RELEASE_ID,
            project_id=project.id,
            slug="preview-set",
            title="Preview Set",
            current_version_no=1,
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="a" * 64,
            created_by="test",
            created_at=NOW,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="b" * 64,
            parameters={"ordinal": 0},
            parameters_sha256="c" * 64,
            provider="salad",
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        session.add(
            Asset(
                id=ASSET_ID,
                release_id=release.id,
                generation_job_id=job.id,
                output_index=0,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.AVAILABLE,
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                object_key=key,
                object_version_id=version_id,
                sha256=source_sha256,
                content_type="image/png",
                image_format="PNG",
                width=1150,
                height=1487,
                byte_size=len(source),
                asset_metadata={"prompt": "private generation metadata"},
                available_at=NOW,
            )
        )
        await session.commit()
    return source_sha256


def test_preview_is_small_private_revalidated_and_raw_master_remains_exact(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="preview-private")
    client.app.state.object_store = store
    source = _source_png()
    assert client.portal is not None
    source_sha256 = client.portal.call(_seed_preview_asset, client, store, source)
    preview_url = (
        f"/dashboard/assets/{ASSET_ID}/previews/dashboard-preview-v1/{source_sha256[:16]}.jpg"
    )

    preview = client.get(preview_url)

    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/jpeg")
    assert preview.headers["cache-control"] == "private, no-cache, must-revalidate"
    assert preview.headers["vary"].lower() == "cookie"
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert len(preview.content) < len(source)
    require_metadata_free_image(preview.content, content_type="image/jpeg")
    with Image.open(BytesIO(preview.content)) as image:
        assert max(image.size) == 768
        assert image.info.keys() <= {"jfif", "jfif_version", "jfif_unit", "jfif_density"}

    preview_objects = [key for key in store.objects if key.startswith("dashboard-previews/")]
    assert len(preview_objects) == 1
    cached = client.get(preview_url, headers={"If-None-Match": preview.headers["etag"]})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["cache-control"] == "private, no-cache, must-revalidate"
    assert [key for key in store.objects if key.startswith("dashboard-previews/")] == (
        preview_objects
    )

    raw = client.get(f"/dashboard/assets/{ASSET_ID}/view", follow_redirects=False)
    assert raw.status_code == 307
    assert raw.headers["location"].startswith("memory://preview-private/raw/")
    assert store.objects[f"raw/{ASSET_ID}.png"].body == source
    assert "no-store" in raw.headers["cache-control"]


def test_stale_or_malformed_preview_urls_fail_closed_without_cache(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="preview-private")
    client.app.state.object_store = store
    source = _source_png()
    assert client.portal is not None
    source_sha256 = client.portal.call(_seed_preview_asset, client, store, source)

    stale = client.get(f"/dashboard/assets/{ASSET_ID}/previews/dashboard-preview-v1/{'0' * 16}.jpg")
    malformed = client.get(
        f"/dashboard/assets/{ASSET_ID}/previews/dashboard-preview-v1/{source_sha256[:15]}.jpg"
    )
    old_version = client.get(
        f"/dashboard/assets/{ASSET_ID}/previews/dashboard-preview-v0/{source_sha256[:16]}.jpg"
    )

    assert stale.status_code == 404
    assert malformed.status_code == 404
    assert old_version.status_code == 404
    assert "no-store" in stale.headers["cache-control"]
    assert "no-store" in malformed.headers["cache-control"]
    assert "no-store" in old_version.headers["cache-control"]
    assert not any(key.startswith("dashboard-previews/") for key in store.objects)


def test_unauthenticated_preview_request_is_never_cacheable(client: TestClient) -> None:
    async def reject_unauthenticated_request() -> None:
        raise HTTPException(status_code=401, detail="authentication required")

    url = f"/dashboard/assets/{ASSET_ID}/previews/dashboard-preview-v1/{'a' * 16}.jpg"
    client.app.dependency_overrides[require_authenticated_principal] = (
        reject_unauthenticated_request
    )
    try:
        response = client.get(url, follow_redirects=False)
    finally:
        client.app.dependency_overrides.pop(require_authenticated_principal, None)

    assert response.status_code in {303, 401}
    assert "no-store" in response.headers["cache-control"]
    assert "immutable" not in response.headers["cache-control"]


def test_preview_creation_limits_full_master_reads_before_rendering(monkeypatch) -> None:
    class TrackingStore(MemoryObjectStore):
        def __init__(self) -> None:
            super().__init__(bucket="bounded-preview-private")
            self.active_source_reads = 0
            self.maximum_source_reads = 0

        async def read_bytes(
            self,
            key: str,
            *,
            max_bytes: int,
            version_id: str | None = None,
            etag: str | None = None,
        ) -> bytes:
            if not key.startswith("raw/"):
                return await super().read_bytes(
                    key,
                    max_bytes=max_bytes,
                    version_id=version_id,
                    etag=etag,
                )
            self.active_source_reads += 1
            self.maximum_source_reads = max(
                self.maximum_source_reads,
                self.active_source_reads,
            )
            try:
                await asyncio.sleep(0.05)
                return await super().read_bytes(
                    key,
                    max_bytes=max_bytes,
                    version_id=version_id,
                    etag=etag,
                )
            finally:
                self.active_source_reads -= 1

    class AssetSession:
        def __init__(self, assets: dict[UUID, Asset]) -> None:
            self.assets = assets

        async def get(self, _model, asset_id: UUID) -> Asset | None:
            return self.assets.get(asset_id)

    store = TrackingStore()
    assets: dict[UUID, Asset] = {}
    for index in range(6):
        asset_id = uuid4()
        source = f"verified-source-{index}".encode()
        source_sha256 = hashlib.sha256(source).hexdigest()
        key = f"raw/{asset_id}.png"
        store.put_for_test(key, source, content_type="image/png")
        assets[asset_id] = Asset(
            id=asset_id,
            release_id=uuid4(),
            generation_job_id=uuid4(),
            output_index=0,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.AVAILABLE,
            storage_backend=store.backend,
            storage_bucket=store.bucket,
            object_key=key,
            object_version_id=store.objects[key].version_id,
            sha256=source_sha256,
            content_type="image/png",
            image_format="PNG",
            width=10,
            height=10,
            byte_size=len(source),
            asset_metadata={},
            available_at=NOW,
        )

    async def fake_render(source: bytes, *, timeout_seconds: float = 30.0) -> DashboardPreview:
        del timeout_seconds
        output = BytesIO()
        Image.new("RGB", (8, 8), (25, 50, 75)).save(output, format="JPEG")
        data = output.getvalue()
        return DashboardPreview(
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            width=8,
            height=8,
            source_sha256=hashlib.sha256(source).hexdigest(),
        )

    monkeypatch.setattr(
        dashboard_previews,
        "render_dashboard_preview_isolated",
        fake_render,
    )

    async def create_all() -> None:
        session = cast(object, AssetSession(assets))
        await asyncio.gather(
            *(
                dashboard_previews.load_or_create_dashboard_preview(
                    session,  # type: ignore[arg-type]
                    cast(ObjectStore, store),
                    asset_id=asset.id,
                    source_token=asset.sha256[:16] if asset.sha256 else "",
                    max_master_bytes=1024,
                )
                for asset in assets.values()
            )
        )

    asyncio.run(create_all())

    assert store.maximum_source_reads == 2
    assert len([key for key in store.objects if key.startswith("dashboard-previews/")]) == 6
