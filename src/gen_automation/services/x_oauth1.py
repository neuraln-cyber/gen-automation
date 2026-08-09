"""Static OAuth 1.0a user-context leases backed by AWS Secrets Manager."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import cast

import boto3
import httpx2
from botocore.config import Config

from gen_automation.integrations.x.client import X_API_BASE_URL, XClient
from gen_automation.integrations.x.oauth1 import XOAuth1Authorization, XOAuth1Credentials
from gen_automation.services.publication_runtime import (
    XCredentialUnavailableError,
    XOAuthEffectLease,
    XPublicationClient,
)
from gen_automation.services.x_oauth import (
    SecretsManagerClient,
    parse_aws_secrets_manager_reference,
)

X_OAUTH1_SECRET_SCHEMA = "gen-automation/x-oauth1/v1"  # noqa: S105
X_OAUTH1_CREATOR_URL = f"{X_API_BASE_URL}/2/users/me"

_CREATOR_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_SECRET_MAX_BYTES = 64 * 1024
_RESPONSE_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True, repr=False)
class _EffectLease:
    client: XPublicationClient
    creator_user_id: str

    def __repr__(self) -> str:
        return f"_EffectLease(creator_user_id={self.creator_user_id!r}, client=<redacted>)"


class AwsSecretsManagerXOAuth1Provider:
    """Read one exact static credential immediately before every X effect."""

    def __init__(
        self,
        *,
        configured_reference: str,
        expected_creator_user_id: str,
        secrets_client: SecretsManagerClient,
        http_client: httpx2.AsyncClient,
        request_timeout_seconds: float,
    ) -> None:
        parsed = parse_aws_secrets_manager_reference(configured_reference)
        if _CREATOR_ID.fullmatch(expected_creator_user_id) is None:
            raise ValueError("expected X creator user ID must contain 1 to 19 digits")
        if not 0 < request_timeout_seconds <= 60:
            raise ValueError("X OAuth request timeout must be between 0 and 60 seconds")
        self._reference = parsed.reference
        self._secret_arn = parsed.secret_arn
        self._expected_creator_user_id = expected_creator_user_id
        self._secrets = secrets_client
        self._http_client = http_client
        self._request_timeout = httpx2.Timeout(request_timeout_seconds)
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(credential_reference=<configured>, "
            f"expected_creator_user_id={self._expected_creator_user_id!r})"
        )

    def open_for_effect(
        self,
        credential_reference: str,
    ) -> AbstractAsyncContextManager[XOAuthEffectLease]:
        return self._open_for_effect(credential_reference)

    async def verify_account_binding(self, credential_reference: str) -> str:
        """Verify the exact owner with one signed GET and no mutation-capable client."""

        authorization, creator_user_id = await self._authorized_binding(credential_reference)
        authorization.clear()
        del authorization
        return creator_user_id

    @asynccontextmanager
    async def _open_for_effect(
        self,
        credential_reference: str,
    ) -> AsyncIterator[XOAuthEffectLease]:
        authorization, creator_user_id = await self._authorized_binding(credential_reference)
        x_client = XClient(
            http_client=self._http_client,
            authorization=authorization,
            timeout=self._request_timeout,
        )
        del authorization
        lease = _EffectLease(
            client=cast(
                XPublicationClient,
                x_client,
            ),
            creator_user_id=creator_user_id,
        )
        try:
            yield lease
        finally:
            x_client.clear_authorization()
            del lease
            del x_client

    async def _authorized_binding(
        self,
        credential_reference: str,
    ) -> tuple[XOAuth1Authorization, str]:
        if self._closed:
            raise XCredentialUnavailableError("X OAuth provider is closed")
        if not hmac.compare_digest(credential_reference, self._reference):
            raise XCredentialUnavailableError("X OAuth credential reference is not approved")
        credential = await self._read_credential()
        authorization = XOAuth1Authorization(credential)
        del credential
        try:
            creator_user_id = await self._load_creator_user_id(authorization)
        except XCredentialUnavailableError:
            authorization.clear()
            del authorization
            raise
        if not hmac.compare_digest(creator_user_id, self._expected_creator_user_id):
            authorization.clear()
            del authorization
            raise XCredentialUnavailableError("X credential account binding failed")
        return authorization, creator_user_id

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
                return

    async def _read_credential(self) -> XOAuth1Credentials:
        store_unavailable = False
        response: Mapping[str, object] | None = None
        try:
            response = await asyncio.to_thread(
                self._secrets.get_secret_value,
                SecretId=self._secret_arn,
                VersionStage="AWSCURRENT",
            )
        except Exception:
            store_unavailable = True
        if store_unavailable:
            raise XCredentialUnavailableError("X OAuth credential secret store is unavailable")
        invalid = False
        raw: object = None
        try:
            if response is None:
                raise ValueError
            raw = response.get("SecretString")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > _SECRET_MAX_BYTES:
                raise ValueError
            credential = _parse_credential(raw)
        except (TypeError, ValueError):
            invalid = True
        if invalid:
            raw = None
            response = None
            raise XCredentialUnavailableError("X OAuth credential secret is invalid")
        return credential

    async def _load_creator_user_id(self, authorization: XOAuth1Authorization) -> str:
        authorization_header = authorization.authorization_header(
            method="GET",
            url=X_OAUTH1_CREATOR_URL,
        )
        del authorization
        headers = {
            "Accept": "application/json",
            "Authorization": authorization_header,
        }
        request_failed = False
        response: httpx2.Response | None = None
        try:
            response = await self._http_client.request(
                "GET",
                X_OAUTH1_CREATOR_URL,
                headers=headers,
                follow_redirects=False,
                timeout=self._request_timeout,
            )
        except httpx2.RequestError:
            request_failed = True
        authorization_header = "[redacted]"
        headers["Authorization"] = "[redacted]"
        if request_failed:
            raise XCredentialUnavailableError("X account binding is unavailable")
        if response is None:  # pragma: no cover - all request outcomes handled above
            raise XCredentialUnavailableError("X account binding is unavailable")
        if response.status_code != 200 or len(response.content) > _RESPONSE_MAX_BYTES:
            _scrub_response_request(response)
            del response
            raise XCredentialUnavailableError("X account binding failed")
        invalid_response = False
        creator_id: object = None
        payload: dict[str, object] | None = None
        data_value: object = None
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
            response = None
            payload = None
            data_value = None
            creator_id = None
            raise XCredentialUnavailableError("X account binding response is invalid")
        return creator_id


def build_aws_secrets_manager_x_oauth1_provider(
    *,
    configured_reference: str,
    expected_creator_user_id: str,
    request_timeout_seconds: float,
) -> AwsSecretsManagerXOAuth1Provider:
    """Build the static provider using only boto3's ambient credential chain."""

    parsed = parse_aws_secrets_manager_reference(configured_reference)
    boto_config = Config(
        connect_timeout=min(request_timeout_seconds, 5),
        read_timeout=request_timeout_seconds,
        retries={"mode": "standard", "max_attempts": 3},
        max_pool_connections=4,
        ignore_configured_endpoint_urls=True,
        proxies={},
        user_agent_extra="gen-automation-x-oauth1/1",
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
    return AwsSecretsManagerXOAuth1Provider(
        configured_reference=configured_reference,
        expected_creator_user_id=expected_creator_user_id,
        secrets_client=secrets_client,
        http_client=httpx2.AsyncClient(follow_redirects=False, trust_env=False),
        request_timeout_seconds=request_timeout_seconds,
    )


def _parse_credential(raw: str) -> XOAuth1Credentials:
    payload = _json_object(raw.encode("utf-8"))
    if (
        set(payload)
        != {
            "schema",
            "consumer_key",
            "consumer_secret",
            "access_token",
            "access_token_secret",
        }
        or payload.get("schema") != X_OAUTH1_SECRET_SCHEMA
    ):
        raise ValueError("invalid X OAuth 1.0a credential schema")
    values: dict[str, str] = {}
    for key in ("consumer_key", "consumer_secret", "access_token", "access_token_secret"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise ValueError("invalid X OAuth 1.0a credential")
        values[key] = value
    return XOAuth1Credentials(**values)


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
