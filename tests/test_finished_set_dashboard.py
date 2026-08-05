from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from gen_automation.api.browser_delivery_forms import delivery_csrf_token
from gen_automation.api.routes import delivery_dashboard as delivery_routes
from gen_automation.config import Environment, Settings
from gen_automation.domain.enums import AdminRole, ReleasePhase, ReviewTaskState
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    DeliveryPackagePart,
    DerivativeProgress,
    DestinationState,
    OperatorDeliverySnapshot,
)
from gen_automation.services.publication import (
    FinishedSetPackageDownloadResult,
    PublicationConflictError,
)


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
        session_secret="finished-set-dashboard-test-secret-long-enough",  # noqa: S106
    )


def _owner() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="finished-set-owner",
        display_name="Finished Set Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.object_store = object()
    return app


def _download_request(
    app: FastAPI,
    *,
    review_task_id: UUID,
    intent_id: UUID,
    fields: dict[str, str],
) -> Request:
    body = urlencode(fields).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": (
                f"/dashboard/review-tasks/{review_task_id}/delivery/"
                f"finished-set/{intent_id}:download"
            ),
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
            "app": app,
        },
        receive,
    )


def _download_fields(
    settings: Settings,
    principal: AuthenticatedPrincipal,
    *,
    digest: str,
    lock_version: int,
    part_number: int,
) -> dict[str, str]:
    return {
        "csrf_token": delivery_csrf_token(settings, session_id=principal.session_id),
        "expected_intent_digest": digest,
        "expected_lock_version": str(lock_version),
        "part_number": str(part_number),
    }


@pytest.mark.asyncio
async def test_finished_set_route_forwards_exact_identity_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = _app(settings)
    principal = _owner()
    review_task_id = uuid4()
    intent_id = uuid4()
    digest = "b" * 64
    lock_version = 17
    part_number = 3
    session = object()
    store = app.state.object_store
    service_call: dict[str, object] = {}

    async def require_owner(
        request: Request,
        received_session: object,
        *,
        csrf_header: str,
    ) -> AuthenticatedPrincipal:
        assert request.app is app
        assert received_session is session
        assert csrf_header == delivery_csrf_token(
            settings,
            session_id=principal.session_id,
        )
        return principal

    async def presign(
        received_session: object,
        received_store: object,
        **kwargs: object,
    ) -> FinishedSetPackageDownloadResult:
        assert received_session is session
        assert received_store is store
        service_call.update(kwargs)
        return FinishedSetPackageDownloadResult(
            review_task_id=review_task_id,
            intent_id=intent_id,
            package_id=uuid4(),
            url="https://private-download.example/ranked-part-003.zip",
            filename="finished-ranked-set-part-003.zip",
            sha256="c" * 64,
            manifest_sha256="d" * 64,
            byte_size=1234,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            part_number=part_number,
            part_count=4,
            first_ordinal=201,
            last_ordinal=300,
        )

    monkeypatch.setattr(delivery_routes, "require_publication_mutation_owner", require_owner)
    monkeypatch.setattr(delivery_routes, "presign_finished_set_package_download", presign)

    response = await delivery_routes.dashboard_download_finished_set_package(
        review_task_id,
        intent_id,
        _download_request(
            app,
            review_task_id=review_task_id,
            intent_id=intent_id,
            fields=_download_fields(
                settings,
                principal,
                digest=digest,
                lock_version=lock_version,
                part_number=part_number,
            ),
        ),
        session,  # type: ignore[arg-type]
        principal,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://private-download.example/ranked-part-003.zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert service_call == {
        "review_task_id": review_task_id,
        "intent_id": intent_id,
        "expected_intent_digest": digest,
        "expected_lock_version": lock_version,
        "actor_user_id": principal.user_id,
        "actor_role": AdminRole.OWNER,
        "expires_in_seconds": 300,
        "part_number": part_number,
    }


@pytest.mark.asyncio
async def test_finished_set_route_returns_conflict_for_mismatched_review_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = _app(settings)
    principal = _owner()
    wrong_review_task_id = uuid4()
    intent_id = uuid4()
    session = object()

    async def require_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return principal

    async def reject_mismatch(*_args: object, **kwargs: object) -> None:
        assert kwargs["review_task_id"] == wrong_review_task_id
        assert kwargs["intent_id"] == intent_id
        raise PublicationConflictError(
            "finished-set package does not belong to the completed review"
        )

    monkeypatch.setattr(delivery_routes, "require_publication_mutation_owner", require_owner)
    monkeypatch.setattr(
        delivery_routes,
        "presign_finished_set_package_download",
        reject_mismatch,
    )

    response = await delivery_routes.dashboard_download_finished_set_package(
        wrong_review_task_id,
        intent_id,
        _download_request(
            app,
            review_task_id=wrong_review_task_id,
            intent_id=intent_id,
            fields=_download_fields(
                settings,
                principal,
                digest="e" * 64,
                lock_version=9,
                part_number=1,
            ),
        ),
        session,  # type: ignore[arg-type]
        principal,
    )

    assert response.status_code == 409
    assert b"finished ranked-set package is not currently available" in response.body


@pytest.mark.asyncio
async def test_published_finished_set_keeps_multipart_download_controls_when_guard_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = _app(settings)
    principal = _owner()
    review_task_id = uuid4()
    intent_id = uuid4()
    destination = DestinationState(
        key="patreon",
        label="Patreon",
        state="published",
        detail="Provider publication is confirmed.",
        intent_id=intent_id,
        intent_digest="f" * 64,
        intent_lock_version=23,
        package_id=uuid4(),
        package_parts=(
            DeliveryPackagePart(
                package_id=uuid4(),
                part_number=1,
                part_count=2,
                first_ordinal=1,
                last_ordinal=100,
            ),
            DeliveryPackagePart(
                package_id=uuid4(),
                part_number=2,
                part_count=2,
                first_ordinal=101,
                last_ordinal=125,
            ),
        ),
    )
    snapshot = OperatorDeliverySnapshot(
        review_task_id=review_task_id,
        review_state=ReviewTaskState.COMPLETED,
        release_id=uuid4(),
        release_version_id=uuid4(),
        release_title="Published ranked set",
        release_phase=ReleasePhase.PUBLISHED,
        x_selected_count=0,
        progress=DerivativeProgress(
            planned=True,
            total_jobs=125,
            requested=0,
            running=0,
            retrying=0,
            succeeded=125,
            failed=0,
            expected_full_outputs=125,
            ready_full_outputs=125,
            expected_x_teasers=0,
            ready_x_teasers=0,
            ready_for_destinations=False,
        ),
        full_outputs=(),
        x_outputs=(),
        publishing_guard_enabled=False,
        publishing_guard_epoch=7,
        publishing_guard_lock_version=11,
        publishing_guard_changed_at=datetime.now(UTC),
        destinations=(
            destination,
            DestinationState("mega", "MEGA", "succeeded", "Delivered."),
            DestinationState("x", "X", "not_prepared", "No teasers selected."),
        ),
    )

    async def load_snapshot(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load_snapshot)
    monkeypatch.setattr(delivery_routes, "list_registered_watermarks", no_watermarks)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/review-tasks/{review_task_id}/delivery",
            "headers": [],
            "app": app,
        }
    )

    response = await delivery_routes.dashboard_review_delivery(
        review_task_id,
        request,
        SimpleNamespace(),  # type: ignore[arg-type]
        principal,
    )

    assert response.status_code == 200
    html = " ".join(bytes(response.body).decode().split())
    assert "Download finished ranked set" in html
    assert "Download ZIP part 1 / 2 (images 1\u2013100)" in html
    assert "Download ZIP part 2 / 2 (images 101\u2013125)" in html
    assert html.count(f"finished-set/{intent_id}:download") == 2
    assert 'name="part_number" value="1"' in html
    assert 'name="part_number" value="2"' in html
    assert "Publication switch" in html
    assert "stopped" in html
    assert "status published" in html
