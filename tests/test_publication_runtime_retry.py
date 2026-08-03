import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from gen_automation.domain.enums import (
    PublicationAttemptState,
    PublicationIntentState,
    PublicationRetryClass,
    PublicationStepKind,
    PublicationStepState,
)
from gen_automation.integrations.patreon import (
    PatreonDriverOutcome,
    PatreonDriverRequest,
    PatreonDriverResult,
)
from gen_automation.integrations.x.errors import XProtocolError
from gen_automation.integrations.x.models import XPost
from gen_automation.services import publication_runtime


@pytest.mark.parametrize(("retry_count", "max_retries"), ((0, 0), (1, 1)))
async def test_exhausted_retry_fails_without_exceeding_database_limit(
    monkeypatch: pytest.MonkeyPatch,
    retry_count: int,
    max_retries: int,
) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    worker_id = "controller:test:publication"
    attempt = SimpleNamespace(
        id=uuid4(),
        state=PublicationAttemptState.PROCESSING,
        attempt_count=1,
        max_attempts=3,
        lease_owner=worker_id,
        lease_expires_at=now + timedelta(minutes=5),
        retry_at=None,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        lock_version=1,
    )
    intent = SimpleNamespace(
        id=uuid4(),
        state=PublicationIntentState.PROCESSING,
        last_error_code=None,
        last_error_detail=None,
    )
    step = SimpleNamespace(
        id=uuid4(),
        state=PublicationStepState.PROCESSING,
        retry_class=None,
        retry_count=retry_count,
        max_retries=max_retries,
        retry_at=None,
        last_error_code=None,
        last_error_detail=None,
        updated_at=now,
        lock_version=1,
    )
    session = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def sessions() -> object:
        yield session

    async def locked_rows(
        _session: object,
        *,
        attempt_id: object,
        step_id: object,
    ) -> tuple[object, object, object]:
        assert attempt_id == attempt.id
        assert step_id == step.id
        return attempt, intent, step

    monkeypatch.setattr(publication_runtime, "_lock_execution_rows", locked_rows)

    await publication_runtime._schedule_retry(
        sessions,  # type: ignore[arg-type]
        attempt_id=attempt.id,
        step_id=step.id,
        worker_id=worker_id,
        error_code="x_media_upload_retryable",
        retry_base_seconds=30,
        retry_max_seconds=900,
        now=now,
    )

    assert step.retry_count == retry_count
    assert step.retry_count <= step.max_retries
    assert step.state == PublicationStepState.FAILED
    assert step.retry_class == PublicationRetryClass.TERMINAL
    assert attempt.state == PublicationAttemptState.FAILED
    assert intent.state == PublicationIntentState.FAILED
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("failure_mode", ("protocol", "completion"))
async def test_x_post_ambiguity_is_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: Literal["protocol", "completion"],
) -> None:
    now = datetime(2026, 7, 28, 13, tzinfo=UTC)
    attempt_id = uuid4()
    intent_id = uuid4()
    step_id = uuid4()
    client_calls = 0
    claims = 0
    unknown_codes: list[str] = []

    class FakeClient:
        async def create_post(self, *, text: str, media_ids: object) -> XPost:
            nonlocal client_calls
            client_calls += 1
            assert text == "approved copy"
            assert media_ids == ("1001",)
            if failure_mode == "protocol":
                raise XProtocolError("HTTP 201 response could not prove the created post")
            return XPost(id="2002", text=text)

    class FakeLease:
        creator_user_id = "42"
        client = FakeClient()

    class FakeProvider:
        def open_for_effect(self, credential_reference: str) -> object:
            assert credential_reference == "test://x/creator"

            @asynccontextmanager
            async def lease() -> object:
                yield FakeLease()

            return lease()

    async def no_recovery(*_args: object, **_kwargs: object) -> bool:
        return False

    async def claim_once(*_args: object, **_kwargs: object) -> object:
        nonlocal claims
        claims += 1
        if claims > 1:
            return None
        return publication_runtime._ClaimedAttempt(
            attempt_id=attempt_id,
            worker_id="controller:test:publication",
        )

    async def snapshot(*_args: object, **_kwargs: object) -> object:
        return publication_runtime._EffectContext(
            attempt_id=attempt_id,
            intent_id=intent_id,
            approval_id=uuid4(),
            step_id=step_id,
            kind=PublicationStepKind.X_CREATE_POST,
            guard_epoch=1,
            credential_reference="test://x/creator",
            configuration={"text": "approved copy"},
        )

    async def media_ids(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("1001",)

    async def request_started(*_args: object, **_kwargs: object) -> int:
        return 1

    async def finish(*_args: object, **_kwargs: object) -> None:
        if failure_mode == "completion":
            raise publication_runtime.PublicationRuntimeContractError(
                "simulated durable completion failure"
            )
        raise AssertionError("protocol failure must not reach durable completion")

    async def mark_unknown(*_args: object, **kwargs: object) -> object:
        unknown_codes.append(str(kwargs["error_code"]))
        return publication_runtime.PublicationCycleResult(
            claimed_attempt=True,
            attempt_id=attempt_id,
            state=PublicationAttemptState.UNKNOWN,
            error_code=str(kwargs["error_code"]),
        )

    monkeypatch.setattr(publication_runtime, "_recover_one_expired_lease", no_recovery)
    monkeypatch.setattr(publication_runtime, "_claim_one_attempt", claim_once)
    monkeypatch.setattr(
        publication_runtime,
        "_patreon_package_part_count",
        lambda *_args, **_kwargs: _async_value(1),
    )
    monkeypatch.setattr(publication_runtime, "_next_step_snapshot", snapshot)
    monkeypatch.setattr(publication_runtime, "_load_x_media_ids", media_ids)
    monkeypatch.setattr(publication_runtime, "_begin_effect", snapshot)
    monkeypatch.setattr(
        publication_runtime,
        "_mark_provider_request_started",
        request_started,
    )
    monkeypatch.setattr(publication_runtime, "_finish_x_post", finish)
    monkeypatch.setattr(publication_runtime, "_mark_unknown_result", mark_unknown)

    first = await publication_runtime.run_publication_cycle(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        worker_id="controller:test:publication",
        x_oauth_provider=FakeProvider(),  # type: ignore[arg-type]
        expected_x_creator_user_id="42",
        lease_seconds=600,
        retry_base_seconds=30,
        retry_max_seconds=900,
        now=now,
    )
    second = await publication_runtime.run_publication_cycle(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        worker_id="controller:test:publication",
        x_oauth_provider=FakeProvider(),  # type: ignore[arg-type]
        expected_x_creator_user_id="42",
        lease_seconds=600,
        retry_base_seconds=30,
        retry_max_seconds=900,
        now=now,
    )

    assert first.state == PublicationAttemptState.UNKNOWN
    assert unknown_codes == [
        (
            "x_create_post_protocol_unknown"
            if failure_mode == "protocol"
            else "x_create_post_completion_unknown"
        )
    ]
    assert second.did_work is False
    assert client_calls == 1


@pytest.mark.parametrize(
    ("outcome", "expected_state", "marker"),
    (
        (PatreonDriverOutcome.PUBLISHED, PublicationAttemptState.SUCCEEDED, "finish"),
        (
            PatreonDriverOutcome.NEEDS_OPERATOR,
            PublicationAttemptState.AWAITING_HUMAN,
            "operator",
        ),
        (PatreonDriverOutcome.UNKNOWN, PublicationAttemptState.UNKNOWN, "unknown"),
        (
            PatreonDriverOutcome.FAILED,
            PublicationAttemptState.AWAITING_HUMAN,
            "operator",
        ),
    ),
)
async def test_patreon_browser_outcomes_are_terminal_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    outcome: PatreonDriverOutcome,
    expected_state: PublicationAttemptState,
    marker: str,
) -> None:
    now = datetime(2026, 7, 29, 13, tzinfo=UTC)
    attempt_id = uuid4()
    intent_id = uuid4()
    step_id = uuid4()
    package_id = uuid4()
    driver_calls = 0
    claims = 0
    marked: list[str] = []

    class FakeDriver:
        async def publish(self, request: PatreonDriverRequest) -> PatreonDriverResult:
            nonlocal driver_calls
            driver_calls += 1
            assert await asyncio.to_thread(request.package_path.read_bytes) == b"frozen-zip"
            return PatreonDriverResult(
                outcome=outcome,
                remote_identifier="12345" if outcome == PatreonDriverOutcome.PUBLISHED else None,
                remote_url=(
                    "https://www.patreon.com/posts/fixture-12345"
                    if outcome == PatreonDriverOutcome.PUBLISHED
                    else None
                ),
                detail_code="fixture_result",
            )

    snapshot = publication_runtime._EffectContext(
        attempt_id=attempt_id,
        intent_id=intent_id,
        approval_id=uuid4(),
        step_id=step_id,
        kind=PublicationStepKind.PATREON_HANDOFF,
        guard_epoch=1,
        credential_reference=None,
        configuration={},
    )

    async def no_recovery(*_args: object, **_kwargs: object) -> bool:
        return False

    async def claim_once(*_args: object, **_kwargs: object) -> object:
        nonlocal claims
        claims += 1
        if claims > 1:
            return None
        return publication_runtime._ClaimedAttempt(
            attempt_id=attempt_id,
            worker_id="controller:test:publication",
        )

    async def load_package(*_args: object, **_kwargs: object) -> object:
        return publication_runtime._PatreonDriverPackage(
            intent_id=intent_id,
            intent_digest="a" * 64,
            package_id=package_id,
            package_sha256=hashlib.sha256(b"frozen-zip").hexdigest(),
            body=b"frozen-zip",
        )

    async def record(name: str, **kwargs: object) -> object:
        marked.append(name)
        state = {
            "finish": PublicationAttemptState.SUCCEEDED,
            "operator": PublicationAttemptState.AWAITING_HUMAN,
            "unknown": PublicationAttemptState.UNKNOWN,
            "failed": PublicationAttemptState.FAILED,
        }[name]
        if name == "finish":
            return None
        return publication_runtime.PublicationCycleResult(
            claimed_attempt=True,
            attempt_id=attempt_id,
            state=state,
            error_code=str(kwargs.get("error_code") or ""),
        )

    monkeypatch.setattr(publication_runtime, "_recover_one_expired_lease", no_recovery)
    monkeypatch.setattr(publication_runtime, "_claim_one_attempt", claim_once)
    monkeypatch.setattr(
        publication_runtime,
        "_patreon_package_part_count",
        lambda *_args, **_kwargs: _async_value(1),
    )
    monkeypatch.setattr(
        publication_runtime,
        "_next_step_snapshot",
        lambda *_args, **_kwargs: _async_value(snapshot),
    )
    monkeypatch.setattr(publication_runtime, "_load_patreon_driver_package", load_package)
    monkeypatch.setattr(
        publication_runtime,
        "_begin_effect",
        lambda *_args, **_kwargs: _async_value(snapshot),
    )
    monkeypatch.setattr(
        publication_runtime,
        "_mark_provider_request_started",
        lambda *_args, **_kwargs: _async_value(1),
    )
    monkeypatch.setattr(
        publication_runtime,
        "_finish_patreon_post",
        lambda *_args, **kwargs: record("finish", **kwargs),
    )
    monkeypatch.setattr(
        publication_runtime,
        "_mark_patreon_needs_operator_result",
        lambda *_args, **kwargs: record("operator", **kwargs),
    )
    monkeypatch.setattr(
        publication_runtime,
        "_mark_unknown_result",
        lambda *_args, **kwargs: record("unknown", **kwargs),
    )
    monkeypatch.setattr(
        publication_runtime,
        "_mark_failed_result",
        lambda *_args, **kwargs: record("failed", **kwargs),
    )

    first = await publication_runtime.run_publication_cycle(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        worker_id="controller:test:publication",
        x_oauth_provider=None,
        expected_x_creator_user_id=None,
        lease_seconds=600,
        retry_base_seconds=30,
        retry_max_seconds=900,
        patreon_driver=FakeDriver(),
        patreon_browser_profile_reference="creator-main",
        now=now,
    )
    second = await publication_runtime.run_publication_cycle(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        worker_id="controller:test:publication",
        x_oauth_provider=None,
        expected_x_creator_user_id=None,
        lease_seconds=600,
        retry_base_seconds=30,
        retry_max_seconds=900,
        patreon_driver=FakeDriver(),
        patreon_browser_profile_reference="creator-main",
        now=now,
    )

    assert first.state == expected_state
    assert second.did_work is False
    assert marked == [marker]
    assert driver_calls == 1


async def test_patreon_handoff_can_durably_start_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 29, 14, tzinfo=UTC)
    worker_id = "controller:test:publication"
    attempt = SimpleNamespace(
        id=uuid4(),
        intent_id=uuid4(),
        approval_id=uuid4(),
        state=PublicationAttemptState.PROCESSING,
        lease_owner=worker_id,
        lease_expires_at=now + timedelta(minutes=5),
        lock_version=1,
    )
    intent = SimpleNamespace(id=attempt.intent_id)
    step = SimpleNamespace(
        id=uuid4(),
        attempt_id=attempt.id,
        state=PublicationStepState.PROCESSING,
        kind=PublicationStepKind.PATREON_HANDOFF,
        guard_epoch=1,
        updated_at=now,
        lock_version=1,
    )
    guard = SimpleNamespace(epoch=2)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=(attempt, intent, step, 0)),
        add=Mock(),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def sessions() -> object:
        yield session

    async def authorize(*_args: object, **_kwargs: object) -> tuple[object, object]:
        return object(), guard

    monkeypatch.setattr(publication_runtime, "require_effect_authorization", authorize)

    request_no = await publication_runtime._mark_provider_request_started(
        sessions,  # type: ignore[arg-type]
        attempt_id=attempt.id,
        step_id=step.id,
        worker_id=worker_id,
        lease_seconds=600,
        now=now,
    )

    assert request_no == 1
    assert attempt.lease_expires_at == now + timedelta(seconds=600)
    assert step.guard_epoch == 2
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


async def test_definite_patreon_failure_preserves_manual_package_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 29, 14, 30, tzinfo=UTC)
    worker_id = "controller:test:publication"
    attempt = SimpleNamespace(
        id=uuid4(),
        state=PublicationAttemptState.PROCESSING,
        lease_owner=worker_id,
        lease_expires_at=now + timedelta(minutes=5),
        retry_at=None,
        last_error_code=None,
        last_error_detail=None,
        lock_version=1,
    )
    intent = SimpleNamespace(
        id=uuid4(),
        state=PublicationIntentState.PROCESSING,
        last_error_code=None,
        last_error_detail=None,
    )
    step = SimpleNamespace(
        id=uuid4(),
        state=PublicationStepState.PROCESSING,
        retry_class=None,
        retry_at=None,
        effect_completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        updated_at=now,
        lock_version=1,
    )
    session = SimpleNamespace(add=Mock(), commit=AsyncMock())

    @asynccontextmanager
    async def sessions() -> object:
        yield session

    async def locked_rows(
        _session: object,
        *,
        attempt_id: object,
        step_id: object,
    ) -> tuple[object, object, object]:
        assert attempt_id == attempt.id
        assert step_id == step.id
        return attempt, intent, step

    append_completion = AsyncMock()
    monkeypatch.setattr(publication_runtime, "_lock_execution_rows", locked_rows)
    monkeypatch.setattr(
        publication_runtime,
        "_append_effect_completion",
        append_completion,
    )

    result = await publication_runtime._mark_patreon_needs_operator_result(
        sessions,  # type: ignore[arg-type]
        attempt_id=attempt.id,
        step_id=step.id,
        worker_id=worker_id,
        provider_request_no=1,
        detail_code="browser_runtime_unavailable",
        definite_pre_submit_failure=True,
        now=now,
    )

    assert result.state == PublicationAttemptState.AWAITING_HUMAN
    assert result.error_code == "patreon_browser_failed"
    assert step.state == PublicationStepState.AWAITING_HUMAN
    assert step.retry_class == PublicationRetryClass.TERMINAL
    assert attempt.state == PublicationAttemptState.AWAITING_HUMAN
    assert intent.state == PublicationIntentState.AWAITING_HUMAN
    append_completion.assert_awaited_once()
    assert append_completion.await_args.kwargs["event_type"] == "terminal"
    assert append_completion.await_args.kwargs["error_code"] == "patreon_browser_failed"
    audit = session.add.call_args.args[0]
    assert audit.action == "publication.patreon_browser_failed_manual_fallback"
    assert audit.detail["automatic_retry_allowed"] is False
    assert audit.detail["manual_package_available"] is True
    session.commit.assert_awaited_once()


async def _async_value(value: object) -> object:
    return value
