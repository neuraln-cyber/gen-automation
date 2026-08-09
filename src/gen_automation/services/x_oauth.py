"""Just-in-time X OAuth leases backed by AWS Secrets Manager.

The adapter deliberately keeps OAuth material out of application tables.  A
short PostgreSQL advisory transaction lock serializes refresh-token rotation
across controller replicas, while an explicit Secrets Manager staging-label
compare-and-swap makes interrupted rotations recoverable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Protocol, cast
from uuid import uuid4

import boto3
import httpx2
from botocore.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from gen_automation.integrations.x.client import (
    X_API_BASE_URL,
    XClient,
    XStaticImageMediaType,
)
from gen_automation.integrations.x.models import XPost, XUploadedMedia
from gen_automation.services.publication_runtime import (
    XCredentialUnavailableError,
    XOAuthEffectLease,
    XOAuthRotationError,
    XPublicationClient,
)

AWS_SECRETS_MANAGER_REFERENCE_PREFIX = "aws-secrets-manager://"
X_OAUTH_PENDING_STAGE = "GEN_AUTOMATION_PENDING"
X_OAUTH_SECRET_SCHEMA = "gen-automation/x-oauth/v1"  # noqa: S105
X_OAUTH_TOKEN_URL = f"{X_API_BASE_URL}/2/oauth2/token"
X_OAUTH_CREATOR_URL = f"{X_API_BASE_URL}/2/users/me"

_FULL_SECRET_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):secretsmanager:"
    r"([a-z0-9-]{3,32}):([0-9]{12}):secret:"
    r"([A-Za-z0-9._/-]{1,400}-[A-Za-z0-9]{6})$"
)
_VERSION_ID = re.compile(r"^[A-Za-z0-9-]{32,64}$")
_CREATOR_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_REQUIRED_X_SCOPES = frozenset(
    {
        "media.write",
        "offline.access",
        "tweet.read",
        "tweet.write",
        "users.read",
    }
)
_SECRET_MAX_BYTES = 64 * 1024
_RESPONSE_MAX_BYTES = 64 * 1024
_LOCK_POLL_SECONDS = 0.1
logger = logging.getLogger(__name__)


class SecretsManagerClient(Protocol):
    """The small synchronous boto3 surface used by this adapter."""

    def get_secret_value(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_secret_value(self, **kwargs: object) -> Mapping[str, object]: ...

    def update_secret_version_stage(self, **kwargs: object) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class XOAuthCredentialLock(Protocol):
    """Cross-replica serialization for one credential reference."""

    def hold(self, credential_reference: str) -> AbstractAsyncContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class ParsedSecretsManagerReference:
    reference: str
    secret_arn: str
    region: str


@dataclass(frozen=True, slots=True, repr=False)
class _StoredCredential:
    client_id: str
    client_secret: str
    refresh_token: str
    access_token: str | None
    access_token_expires_at: datetime | None
    rotated_from_version_id: str | None

    def __repr__(self) -> str:
        return "_StoredCredential(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _SecretVersion:
    version_id: str
    credential: _StoredCredential

    def __repr__(self) -> str:
        return f"_SecretVersion(version_id={self.version_id!r}, credential=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _TokenResult:
    access_token: str
    expires_at: datetime
    replacement_refresh_token: str | None

    def __repr__(self) -> str:
        return "_TokenResult(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _EffectLease:
    client: XPublicationClient
    creator_user_id: str

    def __repr__(self) -> str:
        return f"_EffectLease(creator_user_id={self.creator_user_id!r}, client=<redacted>)"


class _XPublicationClientAdapter:
    """Widen the validated X client media type to the runtime protocol."""

    def __init__(self, client: XClient) -> None:
        self._client = client

    def __repr__(self) -> str:
        return "_XPublicationClientAdapter(client=<redacted>)"

    async def upload_image(
        self,
        *,
        image: bytes,
        media_type: str,
        adult_content: bool = True,
    ) -> XUploadedMedia:
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("X static image media type must be JPEG, PNG, or WEBP")
        return await self._client.upload_image(
            image=image,
            media_type=cast(XStaticImageMediaType, media_type),
            adult_content=adult_content,
        )

    async def create_post(
        self,
        *,
        text: str,
        media_ids: Sequence[str],
    ) -> XPost:
        return await self._client.create_post(text=text, media_ids=media_ids)

    def clear_authorization(self) -> None:
        self._client.clear_authorization()


class PostgresXOAuthCredentialLock:
    """A bounded transaction-scoped advisory lock.

    The transaction contains only credential resolution and, when necessary,
    token rotation.  It is released before ``GET /2/users/me`` and before the
    publication effect itself.
    """

    def __init__(self, engine: AsyncEngine, *, acquisition_timeout_seconds: float) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("X OAuth credential locking requires PostgreSQL")
        if not 0 < acquisition_timeout_seconds <= 60:
            raise ValueError("X OAuth lock timeout must be between 0 and 60 seconds")
        self._engine = engine
        self._acquisition_timeout_seconds = acquisition_timeout_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"acquisition_timeout_seconds={self._acquisition_timeout_seconds!r})"
        )

    @asynccontextmanager
    async def hold(self, credential_reference: str) -> AsyncIterator[None]:
        lock_key = int.from_bytes(
            hashlib.sha256(credential_reference.encode("utf-8")).digest()[:8],
            "big",
            signed=True,
        )
        try:
            async with self._engine.begin() as connection:
                await self._acquire(connection, lock_key)
                yield
        except XCredentialUnavailableError:
            raise
        except Exception:
            raise XCredentialUnavailableError(
                "X OAuth credential serialization is unavailable"
            ) from None

    async def _acquire(self, connection: AsyncConnection, lock_key: int) -> None:
        deadline = monotonic() + self._acquisition_timeout_seconds
        while True:
            try:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )
            except Exception:
                raise XCredentialUnavailableError(
                    "X OAuth credential serialization is unavailable"
                ) from None
            if acquired is True:
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise XCredentialUnavailableError("X OAuth credential is busy")
            await asyncio.sleep(min(_LOCK_POLL_SECONDS, remaining))


class AwsSecretsManagerXOAuthProvider:
    """Issue bounded X clients from one exact Secrets Manager credential."""

    def __init__(
        self,
        *,
        configured_reference: str,
        expected_creator_user_id: str,
        secrets_client: SecretsManagerClient,
        http_client: httpx2.AsyncClient,
        credential_lock: XOAuthCredentialLock,
        request_timeout_seconds: float,
        refresh_margin_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = parse_aws_secrets_manager_reference(configured_reference)
        if _CREATOR_ID.fullmatch(expected_creator_user_id) is None:
            raise ValueError("expected X creator user ID must contain 1 to 19 digits")
        if not 0 < request_timeout_seconds <= 60:
            raise ValueError("X OAuth request timeout must be between 0 and 60 seconds")
        if not 30 <= refresh_margin_seconds <= 1_800:
            raise ValueError("X OAuth refresh margin must be between 30 and 1800 seconds")
        self._reference = parsed.reference
        self._secret_arn = parsed.secret_arn
        self._expected_creator_user_id = expected_creator_user_id
        self._secrets = secrets_client
        self._http_client = http_client
        self._credential_lock = credential_lock
        self._request_timeout = httpx2.Timeout(request_timeout_seconds)
        self._refresh_margin = timedelta(seconds=refresh_margin_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            "credential_reference=<configured>, "
            f"expected_creator_user_id={self._expected_creator_user_id!r})"
        )

    def open_for_effect(
        self,
        credential_reference: str,
    ) -> AbstractAsyncContextManager[XOAuthEffectLease]:
        return self._open_for_effect(credential_reference)

    @asynccontextmanager
    async def _open_for_effect(
        self,
        credential_reference: str,
    ) -> AsyncIterator[XOAuthEffectLease]:
        if self._closed:
            raise XCredentialUnavailableError("X OAuth provider is closed")
        if not hmac.compare_digest(credential_reference, self._reference):
            raise XCredentialUnavailableError("X OAuth credential reference is not approved")

        current: _SecretVersion | None = None
        token: _TokenResult | None = None
        access_token: str | None = None
        try:
            async with self._credential_lock.hold(self._reference):
                current = await self._read_current()
                current = await self._recover_pending(current)
                now = self._now()
                access_token = current.credential.access_token
                expires_at = current.credential.access_token_expires_at
                if (
                    access_token is None
                    or expires_at is None
                    or expires_at <= now + self._refresh_margin
                ):
                    token = await self._refresh(current.credential, now=now)
                    current = await self._persist_refreshed_token(current, token)
                    access_token = current.credential.access_token
                if access_token is None:
                    raise XCredentialUnavailableError("X OAuth access token is unavailable")
        except (XCredentialUnavailableError, XOAuthRotationError):
            current = None
            token = None
            access_token = None
            raise
        except Exception:
            current = None
            token = None
            access_token = None
            raise XCredentialUnavailableError("X OAuth credential is unavailable") from None

        current = None
        token = None
        try:
            creator_user_id = await self._load_creator_user_id(access_token)
        except XCredentialUnavailableError:
            access_token = None
            raise
        if not hmac.compare_digest(creator_user_id, self._expected_creator_user_id):
            access_token = None
            raise XCredentialUnavailableError("X credential account binding failed")
        x_client = XClient(
            http_client=self._http_client,
            bearer_token=access_token,
            timeout=self._request_timeout,
        )
        access_token = None
        adapter = _XPublicationClientAdapter(x_client)
        lease = _EffectLease(
            client=adapter,
            creator_user_id=creator_user_id,
        )
        try:
            yield lease
        finally:
            adapter.clear_authorization()
            del lease
            del adapter
            del x_client

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._http_client.aclose()
        finally:
            try:
                await asyncio.to_thread(self._secrets.close)
            except Exception:
                # Shutdown cannot reveal SDK exception text or keep the process alive.
                logger.warning("x_oauth_secret_client_close_failed")

    async def _recover_pending(self, current: _SecretVersion) -> _SecretVersion:
        pending = await self._read_stage(X_OAUTH_PENDING_STAGE, allow_missing=True)
        if pending is None:
            return current
        if pending.version_id == current.version_id:
            await self._remove_pending_stage(pending.version_id)
            return current
        if pending.credential.rotated_from_version_id != current.version_id:
            raise XOAuthRotationError("X OAuth credential rotation requires reconciliation")
        promoted = await self._promote_pending(
            pending=pending,
            previous_version_id=current.version_id,
        )
        await self._remove_pending_stage(promoted.version_id)
        return promoted

    async def _persist_refreshed_token(
        self,
        current: _SecretVersion,
        token: _TokenResult,
    ) -> _SecretVersion:
        refreshed = _StoredCredential(
            client_id=current.credential.client_id,
            client_secret=current.credential.client_secret,
            refresh_token=(token.replacement_refresh_token or current.credential.refresh_token),
            access_token=token.access_token,
            access_token_expires_at=token.expires_at,
            rotated_from_version_id=current.version_id,
        )
        serialized = _serialize_credential(refreshed)
        version_id = str(uuid4())
        try:
            result = await asyncio.to_thread(
                self._secrets.put_secret_value,
                SecretId=self._secret_arn,
                SecretString=serialized,
                ClientRequestToken=version_id,
                VersionStages=[X_OAUTH_PENDING_STAGE],
            )
            returned_version = _response_version_id(result)
            if returned_version != version_id:
                raise XOAuthRotationError("X OAuth credential rotation could not be verified")
            pending = _SecretVersion(version_id=version_id, credential=refreshed)
        except XOAuthRotationError:
            raise
        except Exception:
            try:
                recovered_pending = await self._read_stage(
                    X_OAUTH_PENDING_STAGE,
                    allow_missing=True,
                )
            except XCredentialUnavailableError:
                raise XOAuthRotationError(
                    "X OAuth credential rotation outcome is unavailable"
                ) from None
            if (
                recovered_pending is None
                or recovered_pending.version_id != version_id
                or recovered_pending.credential != refreshed
            ):
                raise XOAuthRotationError(
                    "X OAuth credential rotation could not be persisted"
                ) from None
            pending = recovered_pending

        promoted = await self._promote_pending(
            pending=pending,
            previous_version_id=current.version_id,
        )
        await self._remove_pending_stage(promoted.version_id)
        return promoted

    async def _promote_pending(
        self,
        *,
        pending: _SecretVersion,
        previous_version_id: str,
    ) -> _SecretVersion:
        try:
            await asyncio.to_thread(
                self._secrets.update_secret_version_stage,
                SecretId=self._secret_arn,
                VersionStage="AWSCURRENT",
                MoveToVersionId=pending.version_id,
                RemoveFromVersionId=previous_version_id,
            )
        except Exception:
            # A response can be lost after Secrets Manager applied the CAS.
            try:
                observed = await self._read_current()
            except XCredentialUnavailableError:
                raise XOAuthRotationError(
                    "X OAuth credential rotation outcome is unavailable"
                ) from None
            if observed.version_id != pending.version_id:
                raise XOAuthRotationError(
                    "X OAuth credential rotation could not be promoted"
                ) from None
            return observed

        try:
            observed = await self._read_current()
        except XCredentialUnavailableError:
            raise XOAuthRotationError("X OAuth credential rotation could not be verified") from None
        if observed.version_id != pending.version_id:
            raise XOAuthRotationError("X OAuth credential rotation could not be verified")
        return observed

    async def _remove_pending_stage(self, version_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._secrets.update_secret_version_stage,
                SecretId=self._secret_arn,
                VersionStage=X_OAUTH_PENDING_STAGE,
                RemoveFromVersionId=version_id,
            )
        except Exception:
            # The fixed label is harmless on AWSCURRENT and is moved on the next
            # pending write.  Do not discard a verified token over label cleanup.
            return

    async def _read_current(self) -> _SecretVersion:
        version = await self._read_stage("AWSCURRENT", allow_missing=False)
        if version is None:  # pragma: no cover - guarded by allow_missing=False
            raise XCredentialUnavailableError("X OAuth credential is unavailable")
        return version

    async def _read_stage(
        self,
        stage: str,
        *,
        allow_missing: bool,
    ) -> _SecretVersion | None:
        read_failed = False
        missing = False
        response: Mapping[str, object] | None = None
        try:
            response = await asyncio.to_thread(
                self._secrets.get_secret_value,
                SecretId=self._secret_arn,
                VersionStage=stage,
            )
        except Exception as error:
            if allow_missing and _aws_error_code(error) == "ResourceNotFoundException":
                missing = True
            else:
                read_failed = True
        if missing:
            return None
        if read_failed or response is None:
            raise XCredentialUnavailableError("X OAuth credential secret store is unavailable")
        invalid = False
        raw: object = None
        version_id: object = None
        credential: _StoredCredential | None = None
        try:
            raw = response.get("SecretString")
            version_id = response.get("VersionId")
            if not isinstance(raw, str) or not isinstance(version_id, str):
                raise ValueError
            if len(raw.encode("utf-8")) > _SECRET_MAX_BYTES:
                raise ValueError
            if _VERSION_ID.fullmatch(version_id) is None:
                raise ValueError
            credential = _parse_credential(raw)
        except (TypeError, ValueError):
            invalid = True
        if invalid or not isinstance(version_id, str) or credential is None:
            raw = None
            version_id = None
            credential = None
            response = None
            raise XCredentialUnavailableError("X OAuth credential secret is invalid")
        return _SecretVersion(version_id=version_id, credential=credential)

    async def _refresh(
        self,
        credential: _StoredCredential,
        *,
        now: datetime,
    ) -> _TokenResult:
        request_failed = False
        response: httpx2.Response | None = None
        try:
            response = await self._http_client.request(
                "POST",
                X_OAUTH_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": credential.refresh_token,
                },
                auth=httpx2.BasicAuth(
                    username=credential.client_id,
                    password=credential.client_secret,
                ),
                follow_redirects=False,
                timeout=self._request_timeout,
            )
        except httpx2.RequestError:
            request_failed = True
        credential = cast(_StoredCredential, None)
        if request_failed or response is None:
            raise XCredentialUnavailableError("X OAuth token endpoint is unavailable")
        if response.status_code != 200 or len(response.content) > _RESPONSE_MAX_BYTES:
            _scrub_response_request(response)
            del response
            raise XCredentialUnavailableError("X OAuth token refresh was rejected")
        invalid_response = False
        payload: dict[str, object] | None = None
        access_token: str | None = None
        replacement: str | None = None
        expires_in: object = None
        try:
            payload = _json_object(response.content)
            access_token = _secret_text(
                payload.get("access_token"),
                "access token",
                maximum=16_384,
            )
            token_type = payload.get("token_type")
            expires_in = payload.get("expires_in")
            if not isinstance(token_type, str) or token_type.casefold() != "bearer":
                raise ValueError
            if (
                not isinstance(expires_in, int)
                or isinstance(expires_in, bool)
                or not 30 <= expires_in <= 86_400
            ):
                raise ValueError
            replacement_value = payload.get("refresh_token")
            replacement = (
                None
                if replacement_value is None
                else _secret_text(
                    replacement_value,
                    "replacement refresh token",
                    maximum=16_384,
                )
            )
            scope_value = payload.get("scope")
            if scope_value is not None:
                if not isinstance(scope_value, str):
                    raise ValueError
                scopes = frozenset(scope_value.split())
                if not _REQUIRED_X_SCOPES.issubset(scopes):
                    raise ValueError
        except (TypeError, ValueError):
            invalid_response = True
        if (
            invalid_response
            or access_token is None
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
        ):
            _scrub_response_request(response)
            del response
            payload = None
            access_token = None
            replacement = None
            expires_in = None
            raise XCredentialUnavailableError("X OAuth token response is invalid")
        return _TokenResult(
            access_token=access_token,
            expires_at=now + timedelta(seconds=expires_in),
            replacement_refresh_token=replacement,
        )

    async def _load_creator_user_id(self, access_token: str) -> str:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
        request_failed = False
        response: httpx2.Response | None = None
        try:
            response = await self._http_client.request(
                "GET",
                X_OAUTH_CREATOR_URL,
                headers=headers,
                follow_redirects=False,
                timeout=self._request_timeout,
            )
        except httpx2.RequestError:
            request_failed = True
        access_token = ""
        headers["Authorization"] = "[redacted]"
        if request_failed or response is None:
            raise XCredentialUnavailableError("X account binding is unavailable")
        if response.status_code != 200 or len(response.content) > _RESPONSE_MAX_BYTES:
            _scrub_response_request(response)
            del response
            raise XCredentialUnavailableError("X account binding failed")
        invalid_response = False
        payload: dict[str, object] | None = None
        data_value: object = None
        creator_id: object = None
        try:
            payload = _json_object(response.content)
            data_value = payload.get("data")
            if not isinstance(data_value, Mapping):
                raise ValueError
            creator_id = data_value.get("id")
            if not isinstance(creator_id, str) or _CREATOR_ID.fullmatch(creator_id) is None:
                raise ValueError
        except (TypeError, ValueError):
            invalid_response = True
        if invalid_response or not isinstance(creator_id, str):
            _scrub_response_request(response)
            del response
            payload = None
            data_value = None
            creator_id = None
            raise XCredentialUnavailableError("X account binding response is invalid")
        return creator_id

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise XCredentialUnavailableError("X OAuth clock is invalid")
        return value.astimezone(UTC)


def parse_aws_secrets_manager_reference(value: str) -> ParsedSecretsManagerReference:
    if (
        not isinstance(value, str)
        or not value.startswith(AWS_SECRETS_MANAGER_REFERENCE_PREFIX)
        or value != value.strip()
        or len(value) > 500
    ):
        raise ValueError("X OAuth credential reference must be a full Secrets Manager ARN")
    arn = value.removeprefix(AWS_SECRETS_MANAGER_REFERENCE_PREFIX)
    match = _FULL_SECRET_ARN.fullmatch(arn)
    if match is None:
        raise ValueError("X OAuth credential reference must be a full Secrets Manager ARN")
    return ParsedSecretsManagerReference(
        reference=value,
        secret_arn=arn,
        region=match.group(2),
    )


def build_aws_secrets_manager_x_oauth_provider(
    *,
    engine: AsyncEngine,
    configured_reference: str,
    expected_creator_user_id: str,
    request_timeout_seconds: float,
    lock_timeout_seconds: float,
    refresh_margin_seconds: int,
) -> AwsSecretsManagerXOAuthProvider:
    """Build the concrete provider using only boto3's ambient credential chain."""

    parsed = parse_aws_secrets_manager_reference(configured_reference)
    boto_config = Config(
        connect_timeout=min(request_timeout_seconds, 5),
        read_timeout=request_timeout_seconds,
        retries={"mode": "standard", "max_attempts": 3},
        max_pool_connections=4,
        ignore_configured_endpoint_urls=True,
        proxies={},
        user_agent_extra="gen-automation-x-oauth/1",
    )
    secrets_client = cast(
        SecretsManagerClient,
        boto3.client(
            "secretsmanager",
            region_name=parsed.region,
            config=boto_config,
            verify=True,
        ),
    )
    http_client = httpx2.AsyncClient(
        follow_redirects=False,
        trust_env=False,
    )
    return AwsSecretsManagerXOAuthProvider(
        configured_reference=configured_reference,
        expected_creator_user_id=expected_creator_user_id,
        secrets_client=secrets_client,
        http_client=http_client,
        credential_lock=PostgresXOAuthCredentialLock(
            engine,
            acquisition_timeout_seconds=lock_timeout_seconds,
        ),
        request_timeout_seconds=request_timeout_seconds,
        refresh_margin_seconds=refresh_margin_seconds,
    )


def _parse_credential(raw: str) -> _StoredCredential:
    payload = _json_object(raw.encode("utf-8"))
    allowed = {
        "schema",
        "client_id",
        "client_secret",
        "refresh_token",
        "access_token",
        "access_token_expires_at",
        "rotated_from_version_id",
    }
    if set(payload) - allowed or payload.get("schema") != X_OAUTH_SECRET_SCHEMA:
        raise ValueError("invalid X OAuth credential schema")
    client_id = _secret_text(payload.get("client_id"), "client ID", maximum=1_024)
    client_secret = _secret_text(
        payload.get("client_secret"),
        "client secret",
        maximum=8_192,
    )
    refresh_token = _secret_text(
        payload.get("refresh_token"),
        "refresh token",
        maximum=16_384,
    )
    access_value = payload.get("access_token")
    expiry_value = payload.get("access_token_expires_at")
    if (access_value is None) != (expiry_value is None):
        raise ValueError("access token and expiry must be provided together")
    access_token = (
        None if access_value is None else _secret_text(access_value, "access token", maximum=16_384)
    )
    expires_at = None if expiry_value is None else _parse_datetime(expiry_value)
    rotated_value = payload.get("rotated_from_version_id")
    if rotated_value is not None and (
        not isinstance(rotated_value, str) or _VERSION_ID.fullmatch(rotated_value) is None
    ):
        raise ValueError("invalid prior secret version")
    return _StoredCredential(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        access_token=access_token,
        access_token_expires_at=expires_at,
        rotated_from_version_id=rotated_value,
    )


def _serialize_credential(credential: _StoredCredential) -> str:
    payload: dict[str, object] = {
        "schema": X_OAUTH_SECRET_SCHEMA,
        "client_id": credential.client_id,
        "client_secret": credential.client_secret,
        "refresh_token": credential.refresh_token,
    }
    if credential.access_token is not None:
        if credential.access_token_expires_at is None:
            raise XOAuthRotationError("X OAuth credential rotation data is incomplete")
        payload["access_token"] = credential.access_token
        payload["access_token_expires_at"] = _canonical_datetime(credential.access_token_expires_at)
    if credential.rotated_from_version_id is not None:
        payload["rotated_from_version_id"] = credential.rotated_from_version_id
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(serialized.encode("utf-8")) > _SECRET_MAX_BYTES:
        raise XOAuthRotationError("X OAuth credential rotation data is too large")
    return serialized


def _json_object(raw: bytes) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value: object = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("JSON object required")
    return cast(dict[str, object], value)


def _secret_text(value: object, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(f"invalid {label}")
    return value


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid access token expiry")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid access token expiry") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid access token expiry")
    return parsed.astimezone(UTC)


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _response_version_id(response: Mapping[str, object]) -> str:
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or _VERSION_ID.fullmatch(version_id) is None:
        raise XOAuthRotationError("X OAuth credential rotation response is invalid")
    return version_id


def _aws_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None


def _scrub_response_request(response: httpx2.Response) -> None:
    try:
        request = response.request
        response.request = httpx2.Request(
            request.method,
            request.url,
            headers={"Authorization": "[redacted]"},
        )
    except RuntimeError:
        return
