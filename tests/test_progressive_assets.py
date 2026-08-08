from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from gen_automation.db.models import (
    Asset,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.enums import AssetKind, AssetState
from gen_automation.storage.memory import MemoryObjectStore

NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
RELEASE_ID = UUID("00000000-0000-4000-8000-000000001001")
ASSET_IDS = (
    UUID("00000000-0000-4000-8000-000000001031"),
    UUID("00000000-0000-4000-8000-000000001032"),
    UUID("00000000-0000-4000-8000-000000001033"),
)
LATE_ASSET_ID = UUID("00000000-0000-4000-8000-000000001034")
EXPECTED_ASSET_ID = UUID("00000000-0000-4000-8000-000000001035")
PROXY_ASSET_ID = UUID("00000000-0000-4000-8000-000000001036")
OTHER_RELEASE_ID = UUID("00000000-0000-4000-8000-000000001002")
OTHER_RELEASE_ASSET_ID = UUID("00000000-0000-4000-8000-000000001037")
FIRST_JOB_ID = UUID("00000000-0000-4000-8000-000000001022")
SECOND_JOB_ID = UUID("00000000-0000-4000-8000-000000001021")


def _available_asset(
    *,
    asset_id: UUID,
    release_id: UUID,
    job_id: UUID,
    output_index: int,
    available_at: datetime,
    store: MemoryObjectStore,
    kind: AssetKind = AssetKind.RAW_MASTER,
    pin_object_version: bool = False,
) -> Asset:
    key = f"raw/{asset_id}.png"
    store.put_for_test(key, b"private-image-bytes")
    stored_version = store.objects[key].version_id
    return Asset(
        id=asset_id,
        release_id=release_id,
        generation_job_id=job_id,
        output_index=output_index,
        kind=kind,
        state=AssetState.AVAILABLE,
        storage_backend=store.backend,
        storage_bucket=store.bucket,
        object_key=key,
        object_version_id=stored_version if pin_object_version else None,
        sha256=str(asset_id).replace("-", "") * 2,
        content_type="image/png",
        image_format="PNG",
        width=1144,
        height=1480,
        byte_size=19,
        asset_metadata={},
        available_at=available_at,
    )


async def _seed_progressive_release(
    client: TestClient,
    store: MemoryObjectStore,
) -> None:
    database = client.app.state.database
    async with database.sessions() as session:
        project = Project(slug="progressive-assets", name="Progressive Assets")
        session.add(project)
        await session.flush()
        release = Release(
            id=RELEASE_ID,
            project_id=project.id,
            slug="ordered-set",
            title="Ordered Set",
            current_version_no=2,
            desired_accepted_count=3,
        )
        session.add(release)
        await session.flush()
        old_version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="1" * 64,
            created_by="test",
            created_at=NOW - timedelta(days=1),
        )
        current_version = ReleaseVersion(
            release_id=release.id,
            version_no=2,
            specification={"schema_version": 1},
            specification_sha256="2" * 64,
            created_by="test",
            created_at=NOW,
        )
        session.add_all([old_version, current_version])
        await session.flush()

        first_job = GenerationJob(
            id=FIRST_JOB_ID,
            release_version_id=current_version.id,
            logical_key="a" * 64,
            parameters={
                "ordinal": 0,
                "batch": {"index": 0, "name": "SFW", "image_offset": 0},
            },
            parameters_sha256="3" * 64,
            provider="salad",
            expected_output_count=2,
        )
        second_job = GenerationJob(
            id=SECOND_JOB_ID,
            release_version_id=current_version.id,
            logical_key="b" * 64,
            parameters={
                "ordinal": 1,
                "batch": {"index": 1, "name": "NNSFW", "image_offset": 0},
            },
            parameters_sha256="4" * 64,
            provider="salad",
            expected_output_count=3,
        )
        old_job = GenerationJob(
            release_version_id=old_version.id,
            logical_key="c" * 64,
            parameters={"ordinal": 0},
            parameters_sha256="5" * 64,
            provider="salad",
            expected_output_count=1,
        )
        session.add_all([first_job, second_job, old_job])
        await session.flush()
        session.add_all(
            [
                _available_asset(
                    asset_id=ASSET_IDS[0],
                    release_id=release.id,
                    job_id=second_job.id,
                    output_index=2,
                    available_at=NOW,
                    store=store,
                    pin_object_version=True,
                ),
                _available_asset(
                    asset_id=ASSET_IDS[1],
                    release_id=release.id,
                    job_id=first_job.id,
                    output_index=1,
                    available_at=NOW,
                    store=store,
                ),
                _available_asset(
                    asset_id=ASSET_IDS[2],
                    release_id=release.id,
                    job_id=second_job.id,
                    output_index=0,
                    available_at=NOW + timedelta(seconds=1),
                    store=store,
                ),
                _available_asset(
                    asset_id=UUID("00000000-0000-4000-8000-000000001099"),
                    release_id=release.id,
                    job_id=old_job.id,
                    output_index=0,
                    available_at=NOW - timedelta(seconds=1),
                    store=store,
                ),
                Asset(
                    id=EXPECTED_ASSET_ID,
                    release_id=release.id,
                    generation_job_id=second_job.id,
                    output_index=1,
                    kind=AssetKind.RAW_MASTER,
                    state=AssetState.EXPECTED,
                    storage_backend=store.backend,
                    storage_bucket=store.bucket,
                    asset_metadata={},
                ),
                _available_asset(
                    asset_id=PROXY_ASSET_ID,
                    release_id=release.id,
                    job_id=first_job.id,
                    output_index=0,
                    available_at=NOW,
                    store=store,
                    kind=AssetKind.REVIEW_PROXY,
                ),
            ]
        )

        other_release = Release(
            id=OTHER_RELEASE_ID,
            project_id=project.id,
            slug="other-ordered-set",
            title="Other Ordered Set",
            current_version_no=1,
            desired_accepted_count=1,
        )
        session.add(other_release)
        await session.flush()
        other_version = ReleaseVersion(
            release_id=other_release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="6" * 64,
            created_by="test",
            created_at=NOW,
        )
        session.add(other_version)
        await session.flush()
        other_job = GenerationJob(
            release_version_id=other_version.id,
            logical_key="d" * 64,
            parameters={"ordinal": 0},
            parameters_sha256="7" * 64,
            provider="salad",
            expected_output_count=1,
        )
        session.add(other_job)
        await session.flush()
        session.add(
            _available_asset(
                asset_id=OTHER_RELEASE_ASSET_ID,
                release_id=other_release.id,
                job_id=other_job.id,
                output_index=0,
                available_at=NOW,
                store=store,
            )
        )
        await session.commit()


async def _make_late_first_queue_asset_available(
    client: TestClient,
    store: MemoryObjectStore,
) -> None:
    database = client.app.state.database
    async with database.sessions() as session:
        session.add(
            _available_asset(
                asset_id=LATE_ASSET_ID,
                release_id=RELEASE_ID,
                job_id=FIRST_JOB_ID,
                output_index=0,
                available_at=NOW + timedelta(seconds=2),
                store=store,
            )
        )
        await session.commit()


def test_generated_asset_feed_pages_without_duplicates_and_preserves_queue_order(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="progressive-private")
    client.app.state.object_store = store
    assert client.portal is not None
    client.portal.call(_seed_progressive_release, client, store)

    first = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"limit": 2},
    )
    assert first.status_code == 200
    assert "no-store" in first.headers["cache-control"]
    assert first.json()["schema_version"] == 1
    assert first.json()["has_more"] is True
    assert [item["asset_id"] for item in first.json()["assets"]] == [
        str(ASSET_IDS[0]),
        str(ASSET_IDS[1]),
    ]
    assert [item["queue_position"] for item in first.json()["assets"]] == [5, 2]
    assert first.json()["assets"][0]["batch_name"] == "NNSFW"
    assert first.json()["assets"][0]["ordinal"] == 1
    assert first.json()["assets"][0]["output_index"] == 2
    assert first.json()["assets"][0]["preview_url"] == (
        f"/dashboard/assets/{ASSET_IDS[0]}/previews/dashboard-preview-v1/"
        f"{str(ASSET_IDS[0]).replace('-', '')[:16]}.jpg"
    )
    assert first.json()["assets"][0]["view_url"] == (f"/dashboard/assets/{ASSET_IDS[0]}/view")

    second = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert second.json()["has_more"] is False
    assert [item["asset_id"] for item in second.json()["assets"]] == [str(ASSET_IDS[2])]
    assert second.json()["assets"][0]["queue_position"] == 3
    assert {item["asset_id"] for item in first.json()["assets"] + second.json()["assets"]} == {
        str(asset_id) for asset_id in ASSET_IDS
    }


def test_generated_asset_feed_scopes_to_current_release_version_and_available_raw_masters(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="progressive-private")
    client.app.state.object_store = store
    assert client.portal is not None
    client.portal.call(_seed_progressive_release, client, store)

    response = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"limit": 64},
    )

    assert response.status_code == 200
    returned_ids = {item["asset_id"] for item in response.json()["assets"]}
    assert returned_ids == {str(asset_id) for asset_id in ASSET_IDS}
    assert str(EXPECTED_ASSET_ID) not in returned_ids
    assert str(PROXY_ASSET_ID) not in returned_ids
    assert str(OTHER_RELEASE_ASSET_ID) not in returned_ids
    assert "00000000-0000-4000-8000-000000001099" not in returned_ids


def test_generated_asset_cursor_observes_late_lower_queue_position_without_replaying(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="progressive-private")
    client.app.state.object_store = store
    assert client.portal is not None
    client.portal.call(_seed_progressive_release, client, store)

    initial = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"limit": 64},
    )
    assert initial.status_code == 200
    assert initial.json()["has_more"] is False
    initial_cursor = initial.json()["next_cursor"]
    assert initial_cursor

    client.portal.call(_make_late_first_queue_asset_available, client, store)
    late = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"limit": 64, "cursor": initial_cursor},
    )
    assert late.status_code == 200
    assert [item["asset_id"] for item in late.json()["assets"]] == [str(LATE_ASSET_ID)]
    assert late.json()["assets"][0]["queue_position"] == 1
    assert late.json()["assets"][0]["batch_image_number"] == 1
    assert late.json()["next_cursor"] != initial_cursor

    replay = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"limit": 64, "cursor": late.json()["next_cursor"]},
    )
    assert replay.status_code == 200
    assert replay.json()["assets"] == []
    assert replay.json()["has_more"] is False


def test_generated_asset_feed_rejects_malformed_cursor(client: TestClient) -> None:
    store = MemoryObjectStore(bucket="progressive-private")
    client.app.state.object_store = store
    assert client.portal is not None
    client.portal.call(_seed_progressive_release, client, store)

    response = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"cursor": "not-a-valid-cursor"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "available-master cursor is invalid"}
    assert "no-store" in response.headers["cache-control"]


def test_generated_asset_access_uses_fresh_no_store_redirects(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="progressive-private")
    client.app.state.object_store = store
    assert client.portal is not None
    client.portal.call(_seed_progressive_release, client, store)

    view = client.get(
        f"/dashboard/assets/{ASSET_IDS[0]}/view",
        follow_redirects=False,
    )
    download = client.get(
        f"/dashboard/assets/{ASSET_IDS[0]}/download",
        follow_redirects=False,
    )
    invalid_cursor = client.get(
        f"/dashboard/releases/{RELEASE_ID}/generated-assets",
        params={"cursor": "not-a-valid-cursor"},
    )

    assert view.status_code == 307
    assert "no-store" in view.headers["cache-control"]
    assert view.headers["location"].startswith("memory://progressive-private/raw/")
    assert "name=" not in view.headers["location"]
    pinned_version = store.objects[f"raw/{ASSET_IDS[0]}.png"].version_id
    assert f"version={pinned_version}" in view.headers["location"]
    assert download.status_code == 307
    assert "no-store" in download.headers["cache-control"]
    assert f"name=raw-master-{ASSET_IDS[0]}.png" in download.headers["location"]
    assert f"version={pinned_version}" in download.headers["location"]
    assert invalid_cursor.status_code == 400
    assert "no-store" in invalid_cursor.headers["cache-control"]

    unversioned_view = client.get(
        f"/dashboard/assets/{ASSET_IDS[1]}/view",
        follow_redirects=False,
    )
    assert unversioned_view.status_code == 307
    assert "version=" not in unversioned_view.headers["location"]


def test_generated_asset_access_returns_safe_errors_for_missing_or_unavailable_raw_master(
    client: TestClient,
) -> None:
    store = MemoryObjectStore(bucket="progressive-private")
    client.app.state.object_store = store
    assert client.portal is not None
    client.portal.call(_seed_progressive_release, client, store)

    missing = client.get(
        "/dashboard/assets/00000000-0000-4000-8000-000000001098/view",
        follow_redirects=False,
    )
    expected = client.get(
        f"/dashboard/assets/{EXPECTED_ASSET_ID}/download",
        follow_redirects=False,
    )
    wrong_kind = client.get(
        f"/dashboard/assets/{PROXY_ASSET_ID}/view",
        follow_redirects=False,
    )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "raw master not found"}
    assert "no-store" in missing.headers["cache-control"]
    assert expected.status_code == 409
    assert expected.json() == {"detail": "raw master is not available"}
    assert "no-store" in expected.headers["cache-control"]
    assert wrong_kind.status_code == 409
    assert wrong_kind.json() == {"detail": "raw master is not available"}
    assert "no-store" in wrong_kind.headers["cache-control"]
