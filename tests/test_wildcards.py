from copy import deepcopy
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.db.models import (
    GenerationJob,
    ReleaseVersion,
    WildcardLibrary,
    WildcardLibraryVersion,
    WorkflowApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from tests.factories import seed_release_approvals, valid_release_payload


def test_operator_can_version_and_append_wildcard_entries(client: TestClient) -> None:
    created = client.post(
        "/api/v1/wildcards",
        json={"name": "poses", "entries": ["standing", "sitting"]},
    )
    assert created.status_code == 201
    assert created.json()["current_version_no"] == 1

    appended = client.post(
        "/api/v1/wildcards/poses/entries",
        json={"expected_version_no": 1, "entries": ["kneeling"]},
    )
    assert appended.status_code == 200
    assert appended.json()["current_version_no"] == 2
    assert appended.json()["entries"] == ["standing", "sitting", "kneeling"]

    database = client.app.state.database
    assert client.portal is not None

    async def read_versions() -> list[list[str]]:
        async with database.sessions() as session:
            library = await session.scalar(
                select(WildcardLibrary).where(WildcardLibrary.name == "poses")
            )
            assert library is not None
            versions = list(
                (
                    await session.scalars(
                        select(WildcardLibraryVersion)
                        .where(WildcardLibraryVersion.library_id == library.id)
                        .order_by(WildcardLibraryVersion.version_no)
                    )
                ).all()
            )
            return [version.entries for version in versions]

    assert client.portal.call(read_versions) == [
        ["standing", "sitting"],
        ["standing", "sitting", "kneeling"],
    ]
    dashboard = client.get("/dashboard/wildcards")
    assert dashboard.status_code == 200
    assert "__poses__" in dashboard.text
    assert "data-wildcard-search" in dashboard.text
    assert 'data-copy-text="__poses__"' in dashboard.text
    assert "data-wildcard-file-input" in dashboard.text
    assert "data-wildcard-download" in dashboard.text


def test_wildcard_api_lists_reads_replaces_and_rejects_a_stale_editor(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/wildcards",
        json={"name": "camera-angles", "entries": ["low angle", "high angle"]},
    )
    assert created.status_code == 201

    listed = client.get("/api/v1/wildcards")
    fetched = client.get("/api/v1/wildcards/camera-angles")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["camera-angles"]
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]
    assert fetched.json()["entries"] == ["low angle", "high angle"]
    assert fetched.json()["entries_sha256"] == created.json()["entries_sha256"]

    replaced = client.put(
        "/api/v1/wildcards/camera-angles",
        json={"expected_version_no": 1, "entries": ["profile view"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["current_version_no"] == 2
    assert replaced.json()["entries"] == ["profile view"]

    stale = client.put(
        "/api/v1/wildcards/camera-angles",
        json={"expected_version_no": 1, "entries": ["overhead view"]},
    )
    assert stale.status_code == 409
    assert "reload" in stale.json()["detail"]


def test_wildcard_dashboard_can_create_and_append_from_line_based_forms(
    client: TestClient,
) -> None:
    created = client.post(
        "/dashboard/wildcards",
        data={
            "csrf_token": "development",
            "action": "create",
            "name": "positions",
            "entries": "standing\nsitting",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/dashboard/wildcards"

    appended = client.post(
        "/dashboard/wildcards",
        data={
            "csrf_token": "development",
            "action": "append",
            "name": "positions",
            "expected_version_no": "1",
            "entries": "kneeling\nlying down",
        },
        follow_redirects=False,
    )
    assert appended.status_code == 303

    fetched = client.get("/api/v1/wildcards/positions")
    assert fetched.status_code == 200
    assert fetched.json()["entries"] == [
        "standing",
        "sitting",
        "kneeling",
        "lying down",
    ]


def test_wildcard_dashboard_replaces_entries_and_renders_a_stale_conflict(
    client: TestClient,
) -> None:
    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "expressions", "entries": ["smiling", "serious"]},
        ).status_code
        == 201
    )
    form = {
        "csrf_token": "development",
        "action": "replace",
        "name": "expressions",
        "expected_version_no": "1",
        "entries": "laughing\nsurprised",
    }
    replaced = client.post(
        "/dashboard/wildcards",
        data=form,
        follow_redirects=False,
    )
    assert replaced.status_code == 303
    assert client.get("/api/v1/wildcards/expressions").json()["entries"] == [
        "laughing",
        "surprised",
    ]

    stale = client.post("/dashboard/wildcards", data=form)
    assert stale.status_code == 409
    assert "Wildcard change was not saved" in stale.text
    assert "reload" in stale.text.lower()


def test_wildcard_api_maps_missing_duplicate_and_stale_writes(
    client: TestClient,
) -> None:
    missing = client.get("/api/v1/wildcards/not-created")
    assert missing.status_code == 404

    command = {"name": "lighting", "entries": ["soft light"]}
    assert client.post("/api/v1/wildcards", json=command).status_code == 201
    duplicate = client.post("/api/v1/wildcards", json=command)
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]

    assert (
        client.post(
            "/api/v1/wildcards/lighting/entries",
            json={"expected_version_no": 1, "entries": ["rim light"]},
        ).status_code
        == 200
    )
    stale = client.post(
        "/api/v1/wildcards/lighting/entries",
        json={"expected_version_no": 1, "entries": ["backlight"]},
    )
    assert stale.status_code == 409
    assert "reload" in stale.json()["detail"]


def test_generation_rejects_a_tampered_frozen_wildcard_digest(
    client: TestClient,
) -> None:
    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "backgrounds", "entries": ["studio backdrop"]},
        ).status_code
        == 201
    )
    project = client.post(
        "/api/v1/projects",
        json={"slug": "tampered-wildcard", "name": "Tampered wildcard"},
    ).json()
    payload = valid_release_payload()
    payload["specification"]["generation"]["prompt"] = "portrait, __backgrounds__"  # type: ignore[index]
    release = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=payload,
        headers={"Idempotency-Key": "create-tampered-wildcard-release"},
    )
    assert release.status_code == 201

    database = client.app.state.database
    assert client.portal is not None

    async def tamper_and_seed() -> None:
        async with database.sessions() as session:
            version = await session.scalar(
                select(ReleaseVersion).where(
                    ReleaseVersion.release_id == UUID(release.json()["id"])
                )
            )
            assert version is not None
            changed = deepcopy(version.specification)
            references = changed["wildcard_versions"]
            assert isinstance(references, list)
            references[0]["entries_sha256"] = "f" * 64
            version.specification = changed
            version.specification_sha256 = canonical_sha256(changed)
            await session.commit()
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)

    client.portal.call(tamper_and_seed)
    response = client.post(
        f"/api/v1/releases/{release.json()['id']}/generation-plan:approve",
        headers={"Idempotency-Key": "reject-tampered-wildcard-release"},
    )
    assert response.status_code == 409
    assert "integrity check" in response.json()["detail"]


def test_release_freezes_nested_wildcards_and_jobs_store_resolution_evidence(
    client: TestClient,
) -> None:
    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "poses", "entries": ["standing", "sitting"]},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/wildcards",
            json={
                "name": "locations",
                "entries": ["on a beach, __poses__", "in a studio, __poses__"],
            },
        ).status_code
        == 201
    )
    project = client.post(
        "/api/v1/projects",
        json={"slug": "wildcard-project", "name": "Wildcard project"},
    ).json()
    payload = valid_release_payload()
    subjects = payload["specification"]["subjects"]  # type: ignore[index]
    second_subject = deepcopy(subjects[0])
    second_subject.update(
        {
            "name": "Second Approved Adult Character",
            "canonical_source_url": "https://example.com/second-character",
        }
    )
    subjects.append(second_subject)
    payload["specification"]["generation"]["prompt"] = "portrait, __locations__"  # type: ignore[index]
    payload["specification"]["generation"].update(  # type: ignore[index]
        {
            "composition_mode": "duo",
            "character_a_prompt": "first adult character, __poses__",
            "character_b_prompt": "second adult character, __locations__",
        }
    )
    payload["specification"]["generation"]["detailer_prompt"] = "face, __poses__"  # type: ignore[index]
    payload["specification"]["generation"]["detailer_negative_prompt"] = (  # type: ignore[index]
        "avoid, __locations__"
    )
    created = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=payload,
        headers={"Idempotency-Key": "create-wildcard-release"},
    )
    assert created.status_code == 201

    assert (
        client.post(
            "/api/v1/wildcards/poses/entries",
            json={"expected_version_no": 1, "entries": ["lying down"]},
        ).status_code
        == 200
    )

    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> None:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)
            workflow = await session.scalar(select(WorkflowApproval))
            assert workflow is not None
            workflow.reviewed_node_classes = [
                *workflow.reviewed_node_classes,
                "ConditioningCombine",
                "ConditioningSetAreaPercentage",
            ]
            await session.commit()

    client.portal.call(seed)
    response = client.post(
        f"/api/v1/releases/{created.json()['id']}/generation-plan:approve",
        headers={"Idempotency-Key": "approve-wildcard-release"},
    )
    assert response.status_code == 200

    async def read_plan() -> tuple[dict[str, object], list[dict[str, object]]]:
        async with database.sessions() as session:
            version = await session.scalar(
                select(ReleaseVersion).where(
                    ReleaseVersion.release_id == UUID(created.json()["id"])
                )
            )
            assert version is not None
            jobs = list(
                (
                    await session.scalars(
                        select(GenerationJob)
                        .where(GenerationJob.release_version_id == version.id)
                        .order_by(GenerationJob.logical_key)
                    )
                ).all()
            )
            return version.specification, [job.parameters for job in jobs]

    specification, jobs = client.portal.call(read_plan)
    frozen_versions = specification["wildcard_versions"]
    assert isinstance(frozen_versions, list)
    assert {item["name"] for item in frozen_versions} == {"poses", "locations"}
    assert {item["version_no"] for item in frozen_versions} == {1}
    assert len(jobs) == 3
    for parameters in jobs:
        generation = parameters["generation"]
        evidence = parameters["prompt_resolution"]
        assert "__" not in generation["prompt"]
        assert "__" not in generation["character_a_prompt"]
        assert "__" not in generation["character_b_prompt"]
        assert "__" not in generation["detailer_prompt"]
        assert "__" not in generation["detailer_negative_prompt"]
        assert evidence["source_prompt"] == "portrait, __locations__"
        assert evidence["schema_version"] == 2
        assert evidence["source_character_a_prompt"] == "first adult character, __poses__"
        assert evidence["source_character_b_prompt"] == "second adult character, __locations__"
        assert evidence["source_detailer_prompt"] == "face, __poses__"
        assert evidence["source_detailer_negative_prompt"] == "avoid, __locations__"
        assert len(evidence["wildcard_versions"]) == 2
        assert {selection["name"] for selection in evidence["selections"]} == {
            "poses",
            "locations",
        }
        assert {selection["field"] for selection in evidence["selections"]} >= {
            "prompt",
            "character_a_prompt",
            "character_b_prompt",
            "detailer_prompt",
            "detailer_negative_prompt",
        }


def test_generation_plan_rejects_prompt_budget_exceeded_only_after_wildcard_expansion(
    client: TestClient,
) -> None:
    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "large", "entries": ["x" * 2_000]},
        ).status_code
        == 201
    )
    project = client.post(
        "/api/v1/projects",
        json={"slug": "expanded-budget", "name": "Expanded prompt budget"},
    ).json()
    payload = valid_release_payload()
    generation = payload["specification"]["generation"]  # type: ignore[index]
    wildcard_source = ", ".join(["__large__"] * 4)
    generation.update(  # type: ignore[union-attr]
        {
            "prompt": wildcard_source,
            "negative_prompt": wildcard_source,
            "detailer_prompt": wildcard_source,
            "detailer_negative_prompt": wildcard_source,
        }
    )
    created = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=payload,
        headers={"Idempotency-Key": "create-expanded-budget-release"},
    )
    assert created.status_code == 201

    database = client.app.state.database
    assert client.portal is not None

    async def seed() -> None:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)

    client.portal.call(seed)
    response = client.post(
        f"/api/v1/releases/{created.json()['id']}/generation-plan:approve",
        headers={"Idempotency-Key": "approve-expanded-budget-release"},
    )

    assert response.status_code == 409
    assert "expanded prompt text is too large" in response.json()["detail"]

    async def read_jobs() -> list[GenerationJob]:
        async with database.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(GenerationJob)
                        .join(ReleaseVersion)
                        .where(ReleaseVersion.release_id == UUID(created.json()["id"]))
                    )
                ).all()
            )

    assert client.portal.call(read_jobs) == []


def test_release_rejects_missing_and_cyclic_wildcards(client: TestClient) -> None:
    project = client.post(
        "/api/v1/projects",
        json={"slug": "invalid-wildcards", "name": "Invalid wildcards"},
    ).json()
    missing = valid_release_payload()
    missing["specification"]["generation"]["prompt"] = "__does_not_exist__"  # type: ignore[index]
    response = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=missing,
        headers={"Idempotency-Key": "missing-wildcard-release"},
    )
    assert response.status_code == 409
    assert "does_not_exist" in response.json()["detail"]

    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "cycle-a", "entries": ["__cycle-b__"]},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "cycle-b", "entries": ["__cycle-a__"]},
        ).status_code
        == 201
    )
    cyclic = deepcopy(valid_release_payload())
    cyclic["slug"] = "cyclic-release"
    cyclic["specification"]["generation"]["prompt"] = "__cycle-a__"  # type: ignore[index]
    response = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=cyclic,
        headers={"Idempotency-Key": "cyclic-wildcard-release"},
    )
    assert response.status_code == 409
    assert "cycle" in response.json()["detail"]
