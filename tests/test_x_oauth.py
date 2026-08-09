import base64
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import TracebackType
from typing import ClassVar
from urllib.parse import parse_qs

import httpx2
import pytest

from gen_automation.services.publication_runtime import XCredentialUnavailableError
from gen_automation.services.x_oauth import (
    X_OAUTH_PENDING_STAGE,
    AwsSecretsManagerXOAuthProvider,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
REFERENCE = (
    "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
    "123456789012:secret:gen-automation/x/creator-AbCdEf"
)
CREATOR_ID = "2244994945"
CLIENT_ID = "confidential-client-id"
CLIENT_SECRET = "confidential-client-secret"  # noqa: S105
REFRESH_TOKEN = "creator-refresh-token"  # noqa: S105
REPLACEMENT_REFRESH_TOKEN = "replacement-refresh-token"  # noqa: S105
ACCESS_TOKEN = "creator-access-token"  # noqa: S105
OLD_VERSION = "a" * 32
PENDING_VERSION = "b" * 32


def _direct_security_surface(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode("latin-1", errors="replace")]
    if isinstance(value, httpx2.Request):
        return [
            repr(value),
            repr(dict(value.headers)),
            value.content.decode("latin-1", errors="replace"),
        ]
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, item in value.items():
            result.extend(_direct_security_surface(key))
            result.extend(_direct_security_surface(item))
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = []
        for item in value:
            result.extend(_direct_security_surface(item))
        return result
    if isinstance(value, BaseException):
        result = [repr(value), str(value)]
        result.extend(_direct_security_surface(getattr(value, "request", None)))
        return result
    return [repr(value)]


def _exception_security_surface(error: BaseException) -> str:
    rendered: list[str] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend(_direct_security_surface(current))
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
        traceback: TracebackType | None = current.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_globals.get("__name__") == "gen_automation.services.x_oauth":
                for name, value in frame.f_locals.items():
                    rendered.append(name)
                    rendered.extend(_direct_security_surface(value))
            traceback = traceback.tb_next
    return "\n".join(rendered)


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    found: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return tuple(found)


class _StageMissingError(Exception):
    response: ClassVar[dict[str, object]] = {"Error": {"Code": "ResourceNotFoundException"}}


class _CredentialLock:
    def __init__(self) -> None:
        self.active = False
        self.entries = 0

    @asynccontextmanager
    async def hold(self, credential_reference: str) -> AsyncIterator[None]:
        assert credential_reference == REFERENCE
        assert not self.active
        self.active = True
        self.entries += 1
        try:
            yield
        finally:
            self.active = False


class _SecretsManager:
    def __init__(
        self,
        *,
        lock: _CredentialLock,
        current: tuple[str, str],
        pending: tuple[str, str] | None = None,
    ) -> None:
        self.lock = lock
        self.versions = {current[0]: current[1]}
        self.stages = {"AWSCURRENT": current[0]}
        if pending is not None:
            self.versions[pending[0]] = pending[1]
            self.stages[X_OAUTH_PENDING_STAGE] = pending[0]
        self.put_count = 0
        self.promote_count = 0
        self.closed = False

    def get_secret_value(self, **kwargs: object) -> Mapping[str, object]:
        assert self.lock.active
        assert kwargs["SecretId"] == REFERENCE.removeprefix("aws-secrets-manager://")
        stage = kwargs["VersionStage"]
        assert isinstance(stage, str)
        version_id = self.stages.get(stage)
        if version_id is None:
            raise _StageMissingError
        return {
            "SecretString": self.versions[version_id],
            "VersionId": version_id,
        }

    def put_secret_value(self, **kwargs: object) -> Mapping[str, object]:
        assert self.lock.active
        version_id = kwargs["ClientRequestToken"]
        secret_string = kwargs["SecretString"]
        stages = kwargs["VersionStages"]
        assert isinstance(version_id, str)
        assert isinstance(secret_string, str)
        assert stages == [X_OAUTH_PENDING_STAGE]
        self.put_count += 1
        self.versions[version_id] = secret_string
        self.stages[X_OAUTH_PENDING_STAGE] = version_id
        return {"VersionId": version_id}

    def update_secret_version_stage(self, **kwargs: object) -> Mapping[str, object]:
        assert self.lock.active
        stage = kwargs["VersionStage"]
        move_to = kwargs.get("MoveToVersionId")
        remove_from = kwargs.get("RemoveFromVersionId")
        assert isinstance(stage, str)
        if move_to is not None:
            assert stage == "AWSCURRENT"
            assert self.stages["AWSCURRENT"] == remove_from
            assert isinstance(move_to, str)
            self.stages["AWSCURRENT"] = move_to
            self.promote_count += 1
        elif self.stages.get(stage) == remove_from:
            del self.stages[stage]
        return {}

    def close(self) -> None:
        self.closed = True


def _initial_secret() -> str:
    return json.dumps(
        {
            "schema": "gen-automation/x-oauth/v1",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
        }
    )


def _pending_secret() -> str:
    return json.dumps(
        {
            "schema": "gen-automation/x-oauth/v1",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REPLACEMENT_REFRESH_TOKEN,
            "access_token": ACCESS_TOKEN,
            "access_token_expires_at": "2026-07-28T14:00:00Z",
            "rotated_from_version_id": OLD_VERSION,
        }
    )


def _provider(
    *,
    secrets: _SecretsManager,
    lock: _CredentialLock,
    handler: Callable[[httpx2.Request], object],
) -> AwsSecretsManagerXOAuthProvider:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AwsSecretsManagerXOAuthProvider(
        configured_reference=REFERENCE,
        expected_creator_user_id=CREATOR_ID,
        secrets_client=secrets,
        http_client=http_client,
        credential_lock=lock,
        request_timeout_seconds=30,
        refresh_margin_seconds=300,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_rotation_is_durable_before_yield_and_cached_token_is_reused() -> None:
    lock = _CredentialLock()
    secrets = _SecretsManager(
        lock=lock,
        current=(OLD_VERSION, _initial_secret()),
    )
    token_requests = 0
    binding_requests = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal token_requests, binding_requests
        if str(request.url).endswith("/2/oauth2/token"):
            token_requests += 1
            assert lock.active
            expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            assert request.headers["Authorization"] == f"Basic {expected_basic}"
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["refresh_token"],
                "refresh_token": [REFRESH_TOKEN],
            }
            return httpx2.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "expires_in": 7200,
                    "refresh_token": REPLACEMENT_REFRESH_TOKEN,
                    "scope": ("tweet.read tweet.write users.read media.write offline.access"),
                    "token_type": "bearer",
                },
            )
        binding_requests += 1
        assert str(request.url).endswith("/2/users/me")
        assert not lock.active
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        return httpx2.Response(200, json={"data": {"id": CREATOR_ID, "name": "ignored"}})

    provider = _provider(secrets=secrets, lock=lock, handler=handler)
    try:
        async with provider.open_for_effect(REFERENCE) as first:
            assert secrets.put_count == 1
            assert secrets.promote_count == 1
            assert X_OAUTH_PENDING_STAGE not in secrets.stages
            stored = json.loads(secrets.versions[secrets.stages["AWSCURRENT"]])
            assert stored["refresh_token"] == REPLACEMENT_REFRESH_TOKEN
            assert stored["access_token"] == ACCESS_TOKEN
            assert first.creator_user_id == CREATOR_ID
            assert ACCESS_TOKEN not in repr(first)

        async with provider.open_for_effect(REFERENCE) as second:
            assert second.creator_user_id == CREATOR_ID

        assert token_requests == 1
        assert binding_requests == 2
        assert secrets.put_count == 1
        assert lock.entries == 2
    finally:
        await provider.aclose()
    assert secrets.closed


@pytest.mark.asyncio
async def test_pending_rotation_is_recovered_without_duplicate_refresh() -> None:
    lock = _CredentialLock()
    secrets = _SecretsManager(
        lock=lock,
        current=(OLD_VERSION, _initial_secret()),
        pending=(PENDING_VERSION, _pending_secret()),
    )
    token_requests = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal token_requests
        if str(request.url).endswith("/2/oauth2/token"):
            token_requests += 1
            raise AssertionError("pending recovery must not refresh again")
        assert not lock.active
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        return httpx2.Response(200, json={"data": {"id": CREATOR_ID}})

    provider = _provider(secrets=secrets, lock=lock, handler=handler)
    try:
        async with provider.open_for_effect(REFERENCE) as lease:
            assert lease.creator_user_id == CREATOR_ID
            assert secrets.stages["AWSCURRENT"] == PENDING_VERSION
            assert X_OAUTH_PENDING_STAGE not in secrets.stages
        assert token_requests == 0
        assert secrets.put_count == 0
        assert secrets.promote_count == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_oauth2_runtime_refresh_error_detaches_basic_and_form_secrets() -> None:
    lock = _CredentialLock()
    secrets = _SecretsManager(
        lock=lock,
        current=(OLD_VERSION, _initial_secret()),
    )
    requests: list[tuple[str, bytes]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.headers["Authorization"], request.content))
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    provider = _provider(secrets=secrets, lock=lock, handler=handler)
    try:
        with pytest.raises(XCredentialUnavailableError) as captured:
            async with provider.open_for_effect(REFERENCE):
                pytest.fail("a failed refresh must not yield")
    finally:
        await provider.aclose()

    error = captured.value
    assert requests
    authorization_header, request_body = requests[0]
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert authorization_header == f"Basic {expected_basic}"
    assert REFRESH_TOKEN.encode() in request_body
    assert not any(isinstance(item, httpx2.RequestError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    for sensitive in (
        CLIENT_ID,
        CLIENT_SECRET,
        REFRESH_TOKEN,
        expected_basic,
        request_body.decode(),
    ):
        assert sensitive not in surface


@pytest.mark.parametrize("failure_mode", ("token_rejected", "wrong_account"))
@pytest.mark.asyncio
async def test_credentials_fail_closed_without_secret_disclosure(
    failure_mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lock = _CredentialLock()
    secrets = _SecretsManager(
        lock=lock,
        current=(OLD_VERSION, _initial_secret()),
    )

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url).endswith("/2/oauth2/token"):
            if failure_mode == "token_rejected":
                return httpx2.Response(
                    400,
                    text=(f"{CLIENT_SECRET} {REFRESH_TOKEN} {REPLACEMENT_REFRESH_TOKEN}"),
                )
            return httpx2.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "expires_in": 7200,
                    "refresh_token": REPLACEMENT_REFRESH_TOKEN,
                    "token_type": "bearer",
                },
            )
        return httpx2.Response(200, json={"data": {"id": "42"}})

    provider = _provider(secrets=secrets, lock=lock, handler=handler)
    yielded = False
    try:
        with pytest.raises(XCredentialUnavailableError) as captured:
            async with provider.open_for_effect(REFERENCE):
                yielded = True
        serialized = f"{captured.value!r} {captured.value} {provider!r} {caplog.text}"
        assert not yielded
        for secret in (
            CLIENT_SECRET,
            REFRESH_TOKEN,
            REPLACEMENT_REFRESH_TOKEN,
            ACCESS_TOKEN,
        ):
            assert secret not in serialized
    finally:
        await provider.aclose()
