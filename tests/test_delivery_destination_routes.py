from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from gen_automation.api.browser_delivery_forms import delivery_csrf_token, delivery_form_key
from gen_automation.api.routes import delivery_dashboard as delivery_routes
from gen_automation.config import Environment, Settings
from gen_automation.domain.enums import AdminRole
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineInputError,
    DerivativePipelineNotFoundError,
)
from gen_automation.services.derivatives import WatermarkPosition


def _settings() -> Settings:
    settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
        session_secret="independent-destination-route-secret-long-enough",  # noqa: S106
    )
    settings.publishing_enabled = True
    settings.x_oauth_secret_reference = "test://x/oauth"  # noqa: S105
    settings.mega_delivery_enabled = True
    settings.mega_remote_root = "/Future"
    return settings


def _owner() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="delivery-owner",
        display_name="Delivery Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _request(app: FastAPI, *, path: str, fields: dict[str, str]) -> Request:
    body = urlencode(fields).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
            "app": app,
        },
        receive,
    )


def _signed_fields(
    settings: Settings,
    owner: AuthenticatedPrincipal,
    *,
    review_task_id: UUID,
    action: str,
) -> dict[str, str]:
    submission_id = uuid4()
    return {
        "csrf_token": delivery_csrf_token(settings, session_id=owner.session_id),
        "idempotency_key": delivery_form_key(
            settings,
            session_id=owner.session_id,
            action=action,
            parts=(str(review_task_id), str(submission_id)),
        ),
        "submission_id": str(submission_id),
    }


@pytest.mark.asyncio
async def test_signed_patreon_route_calls_only_patreon_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="prepare-patreon",
        ),
        "patreon_title": "Patreon only",
        "patreon_body": "No other destination.",
        "patreon_tier": "Paid members",
        "patreon_tags": "independent",
        "public_preview_output_id": str(uuid4()),
        "public_preview_attested_at": datetime.now(UTC).isoformat(),
        "public_preview_safe": "true",
    }
    patreon_calls: list[dict[str, object]] = []
    x_calls: list[dict[str, object]] = []
    mega_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_patreon(_session: object, **kwargs: object) -> object:
        assert _session is session
        patreon_calls.append(kwargs)
        return object()

    async def prepare_x(_session: object, **kwargs: object) -> object:
        x_calls.append(kwargs)
        return object()

    async def prepare_mega(_session: object, **kwargs: object) -> object:
        mega_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_owner", verify_owner)
    monkeypatch.setattr(
        delivery_routes,
        "prepare_operator_patreon_destination",
        prepare_patreon,
    )
    monkeypatch.setattr(delivery_routes, "prepare_operator_x_destination", prepare_x)
    monkeypatch.setattr(delivery_routes, "request_mega_set_delivery", prepare_mega)

    response = await delivery_routes.dashboard_prepare_patreon_destination(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:prepare-patreon",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 303
    assert len(patreon_calls) == 1
    assert patreon_calls[0]["review_task_id"] == review_task_id
    assert patreon_calls[0]["patreon_title"] == "Patreon only"
    assert x_calls == []
    assert mega_calls == []


@pytest.mark.asyncio
async def test_signed_x_route_calls_only_x_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="prepare-x",
        ),
        "x_text": "X only",
        "x_adult_content": "false",
        "x_scheduled_local": "2026-08-10T18:30",
        "x_timezone": "Europe/Sofia",
    }
    patreon_calls: list[dict[str, object]] = []
    x_calls: list[dict[str, object]] = []
    mega_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_patreon(_session: object, **kwargs: object) -> object:
        patreon_calls.append(kwargs)
        return object()

    async def prepare_x(_session: object, **kwargs: object) -> object:
        assert _session is session
        x_calls.append(kwargs)
        return object()

    async def prepare_mega(_session: object, **kwargs: object) -> object:
        mega_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_owner", verify_owner)
    monkeypatch.setattr(
        delivery_routes,
        "prepare_operator_patreon_destination",
        prepare_patreon,
    )
    monkeypatch.setattr(delivery_routes, "prepare_operator_x_destination", prepare_x)
    monkeypatch.setattr(delivery_routes, "request_mega_set_delivery", prepare_mega)

    response = await delivery_routes.dashboard_prepare_x_destination(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:prepare-x",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 303
    assert len(x_calls) == 1
    assert x_calls[0]["review_task_id"] == review_task_id
    assert x_calls[0]["x_text"] == "X only"
    assert x_calls[0]["x_adult_content"] is False
    assert x_calls[0]["scheduled_at"] == datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
    assert patreon_calls == []
    assert mega_calls == []


@pytest.mark.asyncio
async def test_signed_mega_route_calls_only_mega_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = _signed_fields(
        settings,
        owner,
        review_task_id=review_task_id,
        action="prepare-mega",
    )
    patreon_calls: list[dict[str, object]] = []
    x_calls: list[dict[str, object]] = []
    mega_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_patreon(_session: object, **kwargs: object) -> object:
        patreon_calls.append(kwargs)
        return object()

    async def prepare_x(_session: object, **kwargs: object) -> object:
        x_calls.append(kwargs)
        return object()

    async def prepare_mega(_session: object, **kwargs: object) -> object:
        assert _session is session
        mega_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(
        delivery_routes,
        "prepare_operator_patreon_destination",
        prepare_patreon,
    )
    monkeypatch.setattr(delivery_routes, "prepare_operator_x_destination", prepare_x)
    monkeypatch.setattr(delivery_routes, "request_mega_set_delivery", prepare_mega)

    response = await delivery_routes.dashboard_prepare_mega(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:prepare-mega",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 303
    assert len(mega_calls) == 1
    assert mega_calls[0]["review_task_id"] == review_task_id
    assert mega_calls[0]["remote_root"] == "/Future"
    assert patreon_calls == []
    assert x_calls == []


@pytest.mark.asyncio
async def test_clean_full_output_route_never_plans_x_teasers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="prepare-outputs",
        ),
        "watermark_asset_id": "",
        "watermark_position": "",
    }
    full_calls: list[dict[str, object]] = []
    x_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_full(_session: object, **kwargs: object) -> object:
        assert _session is session
        full_calls.append(kwargs)
        return object()

    async def prepare_x(_session: object, **kwargs: object) -> object:
        x_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_full_outputs", prepare_full)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_x_teasers", prepare_x)

    response = await delivery_routes.dashboard_prepare_outputs(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:prepare-outputs",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 303
    assert len(full_calls) == 1
    assert full_calls[0]["review_task_id"] == review_task_id
    assert x_calls == []


@pytest.mark.asyncio
async def test_signed_retry_route_calls_only_full_output_retry_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = _signed_fields(
        settings,
        owner,
        review_task_id=review_task_id,
        action="retry-full-outputs",
    )
    retry_calls: list[dict[str, object]] = []
    prepare_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def retry_full(_session: object, **kwargs: object) -> object:
        assert _session is session
        retry_calls.append(kwargs)
        return object()

    async def prepare_full(_session: object, **kwargs: object) -> object:
        prepare_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(
        delivery_routes,
        "retry_failed_completed_review_full_outputs",
        retry_full,
    )
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_full_outputs", prepare_full)

    response = await delivery_routes.dashboard_retry_full_outputs(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:retry-full-outputs",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 303
    assert retry_calls == [
        {
            "review_task_id": review_task_id,
            "actor_user_id": owner.user_id,
            "idempotency_key": fields["idempotency_key"],
        }
    ]
    assert prepare_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_error", "expected_status"),
    [
        (delivery_routes.DerivativePipelineInputError("invalid retry"), 422),
        (delivery_routes.DerivativePipelineNotFoundError("missing review"), 404),
        (delivery_routes.DerivativePipelineConflictError("stale retry"), 409),
    ],
)
async def test_retry_route_maps_typed_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    service_error: Exception,
    expected_status: int,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = _signed_fields(
        settings,
        owner,
        review_task_id=review_task_id,
        action="retry-full-outputs",
    )

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def retry_full(*_args: object, **_kwargs: object) -> object:
        raise service_error

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(
        delivery_routes,
        "retry_failed_completed_review_full_outputs",
        retry_full,
    )

    response = await delivery_routes.dashboard_retry_full_outputs(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:retry-full-outputs",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == expected_status


@pytest.mark.asyncio
async def test_x_teaser_output_route_never_plans_clean_full_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    watermark_asset_id = uuid4()
    selected_asset_id = uuid4()
    session = object()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="prepare-x-outputs",
        ),
        "watermark_asset_id": str(watermark_asset_id),
        "watermark_position": "top_left",
        "watermark_placements": f'{{"{selected_asset_id}":"bottom_right"}}',
    }
    full_calls: list[dict[str, object]] = []
    x_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_full(_session: object, **kwargs: object) -> object:
        full_calls.append(kwargs)
        return object()

    async def prepare_x(_session: object, **kwargs: object) -> object:
        assert _session is session
        x_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_full_outputs", prepare_full)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_x_teasers", prepare_x)

    response = await delivery_routes.dashboard_prepare_x_outputs(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:prepare-x-outputs",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 303
    assert len(x_calls) == 1
    assert x_calls[0]["review_task_id"] == review_task_id
    assert x_calls[0]["watermark_asset_id"] == watermark_asset_id
    assert x_calls[0]["watermark_position"] == WatermarkPosition.TOP_LEFT
    assert x_calls[0]["watermark_positions_by_asset_id"] == {
        selected_asset_id: WatermarkPosition.BOTTOM_RIGHT,
    }
    assert x_calls[0]["require_active_revision"] is False
    assert full_calls == []


@pytest.mark.asyncio
async def test_x_teaser_replacement_route_propagates_exact_placements_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    watermark_asset_id = uuid4()
    asset_ids = tuple(uuid4() for _ in range(4))
    placements = dict(
        zip(
            asset_ids,
            ("top_left", "top_right", "bottom_left", "bottom_right"),
            strict=True,
        )
    )
    session = object()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="replace-x-outputs",
        ),
        "watermark_asset_id": str(watermark_asset_id),
        "watermark_position": "top_left",
        "watermark_placements": "{"
        + ",".join(f'"{asset_id}":"{position}"' for asset_id, position in placements.items())
        + "}",
    }
    calls: list[dict[str, object]] = []

    async def verify_owner(
        _request: Request,
        _session: object,
        principal: AuthenticatedPrincipal,
        csrf_token: str,
    ) -> AuthenticatedPrincipal:
        assert principal == owner
        assert _session is session
        assert csrf_token == delivery_csrf_token(settings, session_id=owner.session_id)
        return owner

    async def prepare_x(_session: object, **kwargs: object) -> object:
        assert _session is session
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_x_teasers", prepare_x)

    for _attempt in range(2):
        response = await delivery_routes.dashboard_replace_x_outputs(
            review_task_id,
            _request(
                app,
                path=f"/dashboard/review-tasks/{review_task_id}/delivery:replace-x-outputs",
                fields=fields,
            ),
            session,  # type: ignore[arg-type]
            owner,
        )
        assert response.status_code == 303
        assert response.headers["location"] == (
            f"/dashboard/review-tasks/{review_task_id}/delivery"
        )

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]["review_task_id"] == review_task_id
    assert calls[0]["actor_user_id"] == owner.user_id
    assert calls[0]["idempotency_key"] == fields["idempotency_key"]
    assert calls[0]["watermark_asset_id"] == watermark_asset_id
    assert calls[0]["watermark_position"] == WatermarkPosition.TOP_LEFT
    assert calls[0]["watermark_positions_by_asset_id"] == {
        asset_id: WatermarkPosition(position) for asset_id, position in placements.items()
    }
    assert calls[0]["require_active_revision"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_error",
    (
        DerivativePipelineInputError("bad placements"),
        DerivativePipelineNotFoundError("missing review"),
        DerivativePipelineConflictError("X delivery is already prepared"),
    ),
)
async def test_x_teaser_replacement_route_maps_service_failures_without_hiding_reason(
    monkeypatch: pytest.MonkeyPatch,
    service_error: Exception,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="replace-x-outputs",
        ),
        "watermark_asset_id": str(uuid4()),
        "watermark_position": "top_left",
        "watermark_placements": f'{{"{uuid4()}":"top_left"}}',
    }

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_x(*_args: object, **_kwargs: object) -> object:
        raise service_error

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_x_teasers", prepare_x)
    response = await delivery_routes.dashboard_replace_x_outputs(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:replace-x-outputs",
            fields=fields,
        ),
        object(),  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 409
    assert str(service_error).encode() in response.body


@pytest.mark.asyncio
async def test_x_teaser_replacement_rejects_a_cross_action_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="prepare-x-outputs",
        ),
        "watermark_asset_id": str(uuid4()),
        "watermark_position": "top_left",
        "watermark_placements": f'{{"{uuid4()}":"top_left"}}',
    }
    calls = 0

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_x(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_mutation_owner", verify_owner)
    monkeypatch.setattr(delivery_routes, "prepare_completed_review_x_teasers", prepare_x)
    response = await delivery_routes.dashboard_replace_x_outputs(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:replace-x-outputs",
            fields=fields,
        ),
        object(),  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 400
    assert calls == 0


@pytest.mark.asyncio
async def test_stale_cross_target_signature_cannot_start_x(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    owner = _owner()
    review_task_id = uuid4()
    session = object()
    fields = {
        **_signed_fields(
            settings,
            owner,
            review_task_id=review_task_id,
            action="prepare-patreon",
        ),
        "x_text": "must not post",
        "x_adult_content": "true",
        "x_scheduled_local": "",
        "x_timezone": "Europe/Sofia",
    }
    x_calls: list[dict[str, object]] = []

    async def verify_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        return owner

    async def prepare_x(_session: object, **kwargs: object) -> object:
        x_calls.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_routes, "_verified_owner", verify_owner)
    monkeypatch.setattr(delivery_routes, "prepare_operator_x_destination", prepare_x)

    response = await delivery_routes.dashboard_prepare_x_destination(
        review_task_id,
        _request(
            app,
            path=f"/dashboard/review-tasks/{review_task_id}/delivery:prepare-x",
            fields=fields,
        ),
        session,  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 400
    assert x_calls == []
