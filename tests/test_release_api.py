from copy import deepcopy

from fastapi.testclient import TestClient

from tests.factories import valid_release_payload


def test_release_creation_is_idempotent(client: TestClient) -> None:
    project_response = client.post(
        "/api/v1/projects",
        json={"slug": "main", "name": "Main"},
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]
    url = f"/api/v1/projects/{project_id}/releases"
    headers = {"Idempotency-Key": "create-release-one"}

    first = client.post(url, json=valid_release_payload(), headers=headers)
    replay = client.post(url, json=valid_release_payload(), headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json() == replay.json()
    assert first.headers["idempotency-replayed"] == "false"
    assert replay.headers["idempotency-replayed"] == "true"


def test_idempotency_key_reuse_with_changed_request_conflicts(client: TestClient) -> None:
    project = client.post(
        "/api/v1/projects",
        json={"slug": "main", "name": "Main"},
    ).json()
    url = f"/api/v1/projects/{project['id']}/releases"
    headers = {"Idempotency-Key": "same-logical-command"}
    original = valid_release_payload()
    changed = deepcopy(original)
    changed["title"] = "Changed title"

    assert client.post(url, json=original, headers=headers).status_code == 201
    conflict = client.post(url, json=changed, headers=headers)

    assert conflict.status_code == 409
    assert "idempotency key" in conflict.json()["detail"]


def test_release_rejects_minor_subject(client: TestClient) -> None:
    project = client.post(
        "/api/v1/projects",
        json={"slug": "main", "name": "Main"},
    ).json()
    payload = valid_release_payload()
    payload["specification"]["subjects"][0]["canonical_age"] = 17  # type: ignore[index]

    response = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=payload,
        headers={"Idempotency-Key": "invalid-minor-subject"},
    )

    assert response.status_code == 422


def test_release_can_be_read(client: TestClient) -> None:
    project = client.post(
        "/api/v1/projects",
        json={"slug": "main", "name": "Main"},
    ).json()
    created = client.post(
        f"/api/v1/projects/{project['id']}/releases",
        json=valid_release_payload(),
        headers={"Idempotency-Key": "read-back-release"},
    )

    fetched = client.get(f"/api/v1/releases/{created.json()['id']}")

    assert fetched.status_code == 200
    assert fetched.json() == created.json()
