from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from types import TracebackType
from urllib.parse import quote

import httpx2
import pytest

from gen_automation.integrations.x.client import X_API_BASE_URL, XClient
from gen_automation.integrations.x.errors import (
    XAmbiguousTimeoutError,
    XAmbiguousTransportError,
    XRetryableTransportError,
    XTerminalAPIError,
)
from gen_automation.integrations.x.oauth1 import (
    XOAuth1Authorization,
    XOAuth1Credentials,
)
from gen_automation.services import x_oauth1
from gen_automation.services.publication_runtime import XCredentialUnavailableError

CONSUMER_KEY = "consumer-key"
CONSUMER_SECRET = "consumer secret"  # noqa: S105
ACCESS_TOKEN = "access/token"  # noqa: S105
ACCESS_TOKEN_SECRET = "token&secret"  # noqa: S105
NONCE = "nonce-123"
TIMESTAMP = 1_700_000_000
MEDIA_ID = "1880028106020515840"
POST_ID = "1880028106020515999"
IMAGE = b"\x89PNG\r\n\x1a\nmock-image"
REFERENCE = (
    "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
    "123456789012:secret:gen-automation-staging/x/oauth1-AbCdEf"
)
CREATOR_ID = "2244994945"


def _credentials() -> XOAuth1Credentials:
    return XOAuth1Credentials(
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )


def _authorization(
    *,
    nonce_factory: Callable[[], str] = lambda: NONCE,
    timestamp_factory: Callable[[], int] = lambda: TIMESTAMP,
) -> XOAuth1Authorization:
    return XOAuth1Authorization(
        _credentials(),
        nonce_factory=nonce_factory,
        timestamp_factory=timestamp_factory,
    )


def _oauth_header_fields(value: str) -> dict[str, str]:
    assert value.startswith("OAuth ")
    fields: dict[str, str] = {}
    for part in value.removeprefix("OAuth ").split(", "):
        key, encoded_value = part.split("=", maxsplit=1)
        assert encoded_value.startswith('"') and encoded_value.endswith('"')
        assert key not in fields
        fields[key] = encoded_value[1:-1]
    return fields


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
            str(value.url),
            repr(dict(value.headers)),
            value.content.decode("latin-1", errors="replace"),
        ]
    if isinstance(value, httpx2.Response):
        result = [repr(value), repr(dict(value.headers))]
        result.extend(_direct_security_surface(value.content))
        try:
            result.extend(_direct_security_surface(value.request))
        except RuntimeError:
            pass
        return result
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
        for attribute in ("doc", "request", "response"):
            result.extend(_direct_security_surface(getattr(value, attribute, None)))
        return result
    return [repr(value)]


def _exception_security_surface(error: BaseException) -> str:
    """Render recursive exception links plus direct production traceback locals."""

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
            module_name = frame.f_globals.get("__name__")
            if isinstance(module_name, str) and module_name.startswith("gen_automation"):
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


def test_oauth1_hmac_sha1_header_matches_fixed_x_api_vector() -> None:
    header = _authorization().authorization_header(
        method="POST",
        url=f"{X_API_BASE_URL}/2/media/upload",
    )
    frozen_rfc_value = "".join(("4iPambZKim4MoJJE", "AHADPlR090U%3D"))

    # Independently frozen vector. It exercises RFC 3986 encoding of both
    # signing secrets and the access token, plus the second encoding pass in
    # the RFC 5849 signature base string.
    assert header == (
        'OAuth oauth_consumer_key="consumer-key", '
        'oauth_nonce="nonce-123", '
        f'oauth_signature="{frozen_rfc_value}", '
        'oauth_signature_method="HMAC-SHA1", '
        'oauth_timestamp="1700000000", '
        'oauth_token="access%2Ftoken", '
        'oauth_version="1.0"'
    )


@pytest.mark.parametrize(
    ("method", "url"),
    (
        ("POST", f"{X_API_BASE_URL}/2/tweets?expansion=author_id"),
        ("POST", f"{X_API_BASE_URL}/2/media/upload?media_category=tweet_image"),
        ("GET", f"{X_API_BASE_URL}/2/media/upload"),
        ("POST", f"{X_API_BASE_URL}/2/users/me"),
        ("GET", f"{X_API_BASE_URL}/2/tweets"),
        ("POST", f"{X_API_BASE_URL}/2/tweets/"),
        ("POST", f"{X_API_BASE_URL}/1.1/media/upload.json"),
    ),
)
def test_oauth1_signer_rejects_queries_and_wrong_endpoint_method_pairs(
    method: str,
    url: str,
) -> None:
    with pytest.raises(ValueError, match="target is not allowed"):
        _authorization().authorization_header(method=method, url=url)


def test_oauth1_signer_rejects_non_get_or_post_methods() -> None:
    with pytest.raises(ValueError, match="method is not allowed"):
        _authorization().authorization_header(
            method="DELETE",
            url=f"{X_API_BASE_URL}/2/tweets",
        )


@pytest.mark.parametrize(
    "url",
    (
        "http://api.x.com/2/tweets",
        "https://attacker.example/2/tweets",
        "https://api.x.com.evil.example/2/tweets",
        "https://user:password@api.x.com/2/tweets",
        "https://api.x.com:444/2/tweets",
        "https://api.x.com/2/tweets#fragment",
    ),
)
def test_oauth1_signer_rejects_every_noncanonical_request_target(url: str) -> None:
    with pytest.raises(ValueError, match="not approved"):
        _authorization().authorization_header(method="POST", url=url)


@pytest.mark.parametrize(
    ("nonce", "timestamp"),
    (
        ("", TIMESTAMP),
        ("unsafe\r\nnonce", TIMESTAMP),
        ("n" * 257, TIMESTAMP),
        (NONCE, 0),
        (NONCE, -1),
        (NONCE, True),
    ),
)
def test_oauth1_signer_rejects_invalid_nonce_and_timestamp(
    nonce: str,
    timestamp: int,
) -> None:
    authorization = _authorization(
        nonce_factory=lambda: nonce,
        timestamp_factory=lambda: timestamp,
    )
    with pytest.raises(ValueError, match=r"nonce|timestamp"):
        authorization.authorization_header(method="GET", url=f"{X_API_BASE_URL}/2/users/me")


@pytest.mark.parametrize(
    "field",
    ("consumer_key", "consumer_secret", "access_token", "access_token_secret"),
)
@pytest.mark.parametrize("invalid", ("", "   ", "unsafe\r\nvalue"))
def test_oauth1_credentials_fail_closed_on_invalid_secret_text(
    field: str,
    invalid: str,
) -> None:
    values = {
        "consumer_key": CONSUMER_KEY,
        "consumer_secret": CONSUMER_SECRET,
        "access_token": ACCESS_TOKEN,
        "access_token_secret": ACCESS_TOKEN_SECRET,
    }
    values[field] = invalid
    with pytest.raises(ValueError, match="is invalid"):
        XOAuth1Credentials(**values)


def test_oauth1_credentials_authorizer_and_redaction_never_reveal_values() -> None:
    credentials = _credentials()
    authorization = _authorization()
    rendered = f"{credentials!r} {authorization!r}"
    for value in (CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET):
        assert value not in rendered

    provider_text = " ".join((CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET))
    redacted = authorization.redact(provider_text)
    assert redacted.count("[redacted]") == 4
    for value in (CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET):
        assert value not in redacted


def test_oauth1_authorization_clear_is_irreversible() -> None:
    authorization = _authorization()
    authorization.clear()

    with pytest.raises(ValueError, match="cleared"):
        authorization.authorization_header(
            method="GET",
            url=f"{X_API_BASE_URL}/2/users/me",
        )
    rendered = repr(authorization)
    for secret in (CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET):
        assert secret not in rendered


@asynccontextmanager
async def _oauth1_client(
    handler: Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]],
    *,
    nonce_factory: Callable[[], str],
) -> AsyncIterator[XClient]:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        yield XClient(
            http_client=http_client,
            authorization=_authorization(nonce_factory=nonce_factory),
            timeout=30,
        )


@pytest.mark.asyncio
async def test_oauth1_client_signs_v2_upload_metadata_and_ai_labelled_post() -> None:
    nonces = iter(("upload-nonce", "metadata-nonce", "post-nonce"))
    calls: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        header = request.headers["Authorization"]
        fields = _oauth_header_fields(header)
        assert fields["oauth_consumer_key"] == CONSUMER_KEY
        assert fields["oauth_token"] == "access%2Ftoken"  # noqa: S105
        assert fields["oauth_signature_method"] == "HMAC-SHA1"
        assert fields["oauth_timestamp"] == str(TIMESTAMP)
        assert (
            fields["oauth_nonce"]
            == {
                "/2/media/upload": "upload-nonce",
                "/2/media/metadata": "metadata-nonce",
                "/2/tweets": "post-nonce",
            }[request.url.path]
        )
        assert "Bearer " not in header

        body = json.loads(request.content)
        if request.url.path == "/2/media/upload":
            assert body["media_category"] == "tweet_image"
            assert body["media_type"] == "image/png"
            assert body["shared"] is False
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "id": MEDIA_ID,
                        "media_key": f"3_{MEDIA_ID}",
                        "expires_after_secs": 86_400,
                        "size": len(IMAGE),
                    }
                },
            )
        if request.url.path == "/2/media/metadata":
            assert body == {
                "id": MEDIA_ID,
                "metadata": {"sensitive_media_warning": {"adult_content": True}},
            }
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "id": MEDIA_ID,
                        "associated_metadata": {"sensitive_media_warning": {"adult_content": True}},
                    }
                },
            )
        assert request.url.path == "/2/tweets"
        assert body == {
            "text": "New preview",
            "made_with_ai": True,
            "media": {"media_ids": [MEDIA_ID]},
        }
        return httpx2.Response(
            201,
            json={"data": {"id": POST_ID, "text": "New preview"}},
        )

    async with _oauth1_client(handler, nonce_factory=lambda: next(nonces)) as client:
        uploaded = await client.upload_image(image=IMAGE, media_type="image/png")
        post = await client.create_post(text="New preview", media_ids=[uploaded.id])

    assert calls == ["/2/media/upload", "/2/media/metadata", "/2/tweets"]
    assert uploaded.id == MEDIA_ID
    assert post.id == POST_ID


@pytest.mark.asyncio
async def test_oauth1_post_timeout_is_ambiguous_and_never_retried_internally() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise httpx2.ReadTimeout("response timed out", request=request)

    async with _oauth1_client(handler, nonce_factory=lambda: NONCE) as client:
        with pytest.raises(XAmbiguousTimeoutError, match="outcome is unknown"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert request_count == 1


@pytest.mark.asyncio
async def test_oauth1_proven_pre_send_failure_is_retryable_but_attempted_once() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise httpx2.ConnectTimeout("connect timed out", request=request)

    async with _oauth1_client(handler, nonce_factory=lambda: NONCE) as client:
        with pytest.raises(XRetryableTransportError, match="before request bytes"):
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    assert request_count == 1


@pytest.mark.asyncio
async def test_oauth1_transport_error_detaches_authorization_from_chain_and_tracebacks() -> None:
    authorization_headers: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        authorization_headers.append(request.headers["Authorization"])
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    async with _oauth1_client(handler, nonce_factory=lambda: NONCE) as client:
        with pytest.raises(XAmbiguousTransportError) as captured:
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])
        with pytest.raises(ValueError, match="cleared"):
            await client.create_post(text="Preview again", media_ids=[MEDIA_ID])

    error = captured.value
    assert authorization_headers and authorization_headers[0].startswith("OAuth ")
    assert not any(isinstance(item, httpx2.RequestError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    for sensitive in (
        CONSUMER_KEY,
        CONSUMER_SECRET,
        ACCESS_TOKEN,
        ACCESS_TOKEN_SECRET,
        authorization_headers[0],
    ):
        assert sensitive not in surface


@pytest.mark.asyncio
async def test_oauth1_provider_error_redacts_all_four_credential_values() -> None:
    echoed_headers: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        authorization_header = request.headers["Authorization"]
        echoed_headers.append(authorization_header)
        return httpx2.Response(
            400,
            json={
                "detail": " ".join(
                    (
                        CONSUMER_KEY,
                        CONSUMER_SECRET,
                        ACCESS_TOKEN,
                        ACCESS_TOKEN_SECRET,
                        "access%2Ftoken",
                        "access&#x2F;token",
                        authorization_header,
                        quote(authorization_header, safe=""),
                    )
                )
            },
            request=request,
        )

    async with _oauth1_client(handler, nonce_factory=lambda: NONCE) as client:
        with pytest.raises(XTerminalAPIError) as captured:
            await client.create_post(text="Preview", media_ids=[MEDIA_ID])

    rendered = f"{captured.value!r} {captured.value}"
    assert echoed_headers
    rendered += f" {captured.value.response_body}"
    for value in (
        CONSUMER_KEY,
        CONSUMER_SECRET,
        ACCESS_TOKEN,
        ACCESS_TOKEN_SECRET,
        "access%2Ftoken",
        "access&#x2F;token",
        echoed_headers[0],
        quote(echoed_headers[0], safe=""),
    ):
        assert value not in rendered


def test_x_client_requires_exactly_one_oauth_authorization_mode() -> None:
    http_client = httpx2.AsyncClient()
    try:
        with pytest.raises(ValueError, match="exactly one"):
            XClient(http_client=http_client)
        with pytest.raises(ValueError, match="exactly one"):
            XClient(
                http_client=http_client,
                bearer_token="oauth2-token",  # noqa: S106
                authorization=_authorization(),
            )
    finally:
        import asyncio

        asyncio.run(http_client.aclose())


def _credential_json(**updates: object) -> str:
    payload: dict[str, object] = {
        "schema": x_oauth1.X_OAUTH1_SECRET_SCHEMA,
        "consumer_key": CONSUMER_KEY,
        "consumer_secret": CONSUMER_SECRET,
        "access_token": ACCESS_TOKEN,
        "access_token_secret": ACCESS_TOKEN_SECRET,
    }
    payload.update(updates)
    return json.dumps(payload)


class _SecretsManager:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.reads: list[dict[str, object]] = []
        self.closed = False

    def get_secret_value(self, **kwargs: object) -> Mapping[str, object]:
        self.reads.append(dict(kwargs))
        return {"SecretString": self.secret, "VersionId": "a" * 32}

    def put_secret_value(self, **_kwargs: object) -> Mapping[str, object]:
        raise AssertionError("static OAuth1 credentials must never be rotated")

    def update_secret_version_stage(self, **_kwargs: object) -> Mapping[str, object]:
        raise AssertionError("static OAuth1 credentials must never mutate staging labels")

    def close(self) -> None:
        self.closed = True


def _provider(
    *,
    secrets: _SecretsManager,
    handler: Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]],
) -> x_oauth1.AwsSecretsManagerXOAuth1Provider:
    return x_oauth1.AwsSecretsManagerXOAuth1Provider(
        configured_reference=REFERENCE,
        expected_creator_user_id=CREATOR_ID,
        secrets_client=secrets,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        request_timeout_seconds=30,
    )


@pytest.mark.asyncio
async def test_oauth1_account_binding_canary_performs_one_signed_get_only() -> None:
    secrets = _SecretsManager(_credential_json())
    requests: list[tuple[str, str, bytes]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.method, request.url.path, request.content))
        assert request.url.query == b""
        assert request.headers["Authorization"].startswith("OAuth ")
        assert "Bearer " not in request.headers["Authorization"]
        return httpx2.Response(
            200,
            json={
                "data": {
                    "id": CREATOR_ID,
                    "name": "Creator name is not needed",
                    "username": "creator_handle_is_not_needed",
                }
            },
        )

    provider = _provider(secrets=secrets, handler=handler)
    try:
        creator_user_id = await provider.verify_account_binding(REFERENCE)
    finally:
        await provider.aclose()

    assert creator_user_id == CREATOR_ID
    assert requests == [("GET", "/2/users/me", b"")]
    assert secrets.reads == [
        {
            "SecretId": REFERENCE.removeprefix("aws-secrets-manager://"),
            "VersionStage": "AWSCURRENT",
        }
    ]
    assert secrets.closed


@pytest.mark.parametrize(
    "raw",
    (
        "{}",
        json.dumps(
            {
                "schema": "gen-automation/x-oauth/v1",
                "client_id": "oauth2-client",
                "client_secret": "oauth2-secret",
                "refresh_token": "oauth2-refresh",
            }
        ),
        _credential_json(schema="gen-automation/x-oauth1/v2"),
        _credential_json(extra="not-allowed"),
        _credential_json(client_secret="mixed-oauth2-field"),  # noqa: S106
        _credential_json(access_token_secret="   "),  # noqa: S106
        _credential_json(consumer_secret="unsafe\r\nsecret"),  # noqa: S106
        (
            '{"schema":"gen-automation/x-oauth1/v1",'
            '"consumer_key":"first","consumer_key":"second",'
            '"consumer_secret":"secret","access_token":"token",'
            '"access_token_secret":"token-secret"}'
        ),
    ),
)
def test_oauth1_secret_parser_rejects_wrong_mixed_extra_and_duplicate_schemas(raw: str) -> None:
    with pytest.raises(ValueError):
        x_oauth1._parse_credential(raw)


def test_oauth1_secret_parser_accepts_only_the_exact_redacted_schema() -> None:
    credential = x_oauth1._parse_credential(_credential_json())

    assert credential == _credentials()
    rendered = repr(credential)
    assert rendered == "XOAuth1Credentials(<redacted>)"
    for secret in (CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_oauth1_provider_reloads_and_rebinds_before_each_effect_without_rotation() -> None:
    secrets = _SecretsManager(_credential_json())
    calls: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"].startswith("OAuth ")
        assert "Bearer " not in request.headers["Authorization"]
        if request.url.path == "/2/users/me":
            assert request.method == "GET"
            return httpx2.Response(200, json={"data": {"id": CREATOR_ID}})
        if request.url.path == "/2/media/upload":
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "id": MEDIA_ID,
                        "media_key": f"3_{MEDIA_ID}",
                        "expires_after_secs": 86_400,
                        "size": len(IMAGE),
                    }
                },
            )
        if request.url.path == "/2/media/metadata":
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "id": MEDIA_ID,
                        "associated_metadata": {"sensitive_media_warning": {"adult_content": True}},
                    }
                },
            )
        assert request.url.path == "/2/tweets"
        return httpx2.Response(
            201,
            json={"data": {"id": POST_ID, "text": "New preview"}},
        )

    provider = _provider(secrets=secrets, handler=handler)
    try:
        async with provider.open_for_effect(REFERENCE) as upload_lease:
            assert upload_lease.creator_user_id == CREATOR_ID
            uploaded = await upload_lease.client.upload_image(
                image=IMAGE,
                media_type="image/png",
            )
            expired_client = upload_lease.client
        with pytest.raises(ValueError, match="cleared"):
            await expired_client.create_post(text="Must not publish", media_ids=[uploaded.id])
        async with provider.open_for_effect(REFERENCE) as post_lease:
            assert post_lease.creator_user_id == CREATOR_ID
            post = await post_lease.client.create_post(
                text="New preview",
                media_ids=[uploaded.id],
            )
    finally:
        await provider.aclose()

    assert calls == [
        "/2/users/me",
        "/2/media/upload",
        "/2/media/metadata",
        "/2/users/me",
        "/2/tweets",
    ]
    assert secrets.reads == [
        {
            "SecretId": REFERENCE.removeprefix("aws-secrets-manager://"),
            "VersionStage": "AWSCURRENT",
        },
        {
            "SecretId": REFERENCE.removeprefix("aws-secrets-manager://"),
            "VersionStage": "AWSCURRENT",
        },
    ]
    assert post.id == POST_ID
    assert secrets.closed


@pytest.mark.parametrize(
    "binding_response",
    (
        httpx2.Response(200, json={"data": {"id": "42"}}),
        httpx2.Response(401, text="credential rejected"),
        httpx2.Response(200, json={"data": {"id": 42}}),
    ),
)
@pytest.mark.asyncio
async def test_oauth1_wrong_or_unverifiable_account_never_yields_an_effect_client(
    binding_response: httpx2.Response,
) -> None:
    secrets = _SecretsManager(_credential_json())
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/2/users/me"
        return binding_response

    provider = _provider(secrets=secrets, handler=handler)
    yielded = False
    try:
        with pytest.raises(XCredentialUnavailableError) as captured:
            async with provider.open_for_effect(REFERENCE):
                yielded = True
        rendered = f"{captured.value!r} {captured.value} {provider!r}"
        for secret in (CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET):
            assert secret not in rendered
    finally:
        await provider.aclose()

    assert not yielded
    assert calls == 1


@pytest.mark.asyncio
async def test_oauth1_account_transport_error_detaches_signed_request_and_secrets() -> None:
    secrets = _SecretsManager(_credential_json())
    authorization_headers: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        authorization_headers.append(request.headers["Authorization"])
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    provider = _provider(secrets=secrets, handler=handler)
    try:
        with pytest.raises(XCredentialUnavailableError) as captured:
            async with provider.open_for_effect(REFERENCE):
                pytest.fail("a failed account binding must not yield")
    finally:
        await provider.aclose()

    error = captured.value
    assert authorization_headers and authorization_headers[0].startswith("OAuth ")
    assert not any(isinstance(item, httpx2.RequestError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    for sensitive in (
        CONSUMER_KEY,
        CONSUMER_SECRET,
        ACCESS_TOKEN,
        ACCESS_TOKEN_SECRET,
        authorization_headers[0],
    ):
        assert sensitive not in surface


@pytest.mark.asyncio
async def test_oauth1_malformed_secret_detaches_json_error_document_from_traceback() -> None:
    secret_marker = "-".join(("malformed", "consumer", "secret", "must", "not", "survive"))
    malformed = (
        f'{{"schema":"gen-automation/x-oauth1/v1","consumer_secret":"{secret_marker}", invalid-json'
    )
    secrets = _SecretsManager(malformed)

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("malformed secret JSON must fail before X network I/O")

    provider = _provider(secrets=secrets, handler=handler)
    try:
        with pytest.raises(XCredentialUnavailableError) as captured:
            async with provider.open_for_effect(REFERENCE):
                pytest.fail("a malformed secret must not yield")
    finally:
        await provider.aclose()

    error = captured.value
    assert not any(isinstance(item, json.JSONDecodeError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    assert malformed not in surface
    assert secret_marker not in surface


@pytest.mark.asyncio
async def test_oauth1_unapproved_reference_fails_before_secret_or_network_access() -> None:
    secrets = _SecretsManager(_credential_json())

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("an unapproved secret reference must fail before network I/O")

    provider = _provider(secrets=secrets, handler=handler)
    try:
        with pytest.raises(XCredentialUnavailableError, match="not approved"):
            async with provider.open_for_effect(REFERENCE.replace("AbCdEf", "GhIjKl")):
                pytest.fail("an unapproved credential must not yield")
    finally:
        await provider.aclose()

    assert secrets.reads == []
