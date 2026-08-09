"""One-time, local X OAuth 2.0 bootstrap for the staging creator account.

This command is deliberately interactive.  X credentials are read from a TTY
without echo, validated against X, and written directly to the one staging AWS
Secrets Manager secret.  The default flow uses a fixed loopback PKCE callback;
an explicit non-secret mode flag imports a Developer Portal-generated refresh
token and immediately rotates it with the confidential client.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import re
import secrets
import sys
import warnings
import webbrowser
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from typing import Protocol, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import boto3
import httpx2
from botocore.config import Config

from gen_automation.integrations.x.oauth1 import XOAuth1Authorization, XOAuth1Credentials
from gen_automation.services.x_oauth import X_OAUTH_SECRET_SCHEMA
from gen_automation.services.x_oauth1 import X_OAUTH1_SECRET_SCHEMA

AWS_PROFILE = "gen-automation-staging"
AWS_REGION = "eu-central-1"
AWS_ACCOUNT_ID = "861912887470"
AWS_SECRET_NAME = "gen-automation-staging/x/oauth"  # noqa: S105 - resource name
AWS_OAUTH1_SECRET_NAME = "gen-automation-staging/x/oauth1"  # noqa: S105 - resource name

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
CALLBACK_URL = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"  # noqa: S105 - endpoint URL
X_CREATOR_URL = "https://api.x.com/2/users/me"
PORTAL_REFRESH_TOKEN_MODE = "--portal-refresh-token"  # noqa: S105 - non-secret CLI mode
OAUTH1_MODE = "--oauth1"
X_REQUIRED_SCOPES = (
    "tweet.read",
    "tweet.write",
    "users.read",
    "media.write",
    "offline.access",
)

_CALLBACK_TIMEOUT_SECONDS = 10 * 60
_REQUEST_TIMEOUT_SECONDS = 20.0
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_AUTHORIZATION_CODE_BYTES = 16 * 1024
_CREATOR_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_STAGING_OPERATOR_ARN = re.compile(
    rf"^arn:aws:sts::{AWS_ACCOUNT_ID}:assumed-role/"
    r"AWSReservedSSO_GenAutomationStagingDeployer_[A-Fa-f0-9]{16}/"
    r"[A-Za-z0-9+=,.@_-]{2,64}$"
)
_SECRET_ARN = re.compile(
    rf"^arn:aws:secretsmanager:{re.escape(AWS_REGION)}:{AWS_ACCOUNT_ID}:"
    rf"secret:{re.escape(AWS_SECRET_NAME)}-[A-Za-z0-9]{{6}}$"
)


def _secret_arn_pattern(secret_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^arn:aws:secretsmanager:{re.escape(AWS_REGION)}:{AWS_ACCOUNT_ID}:"
        rf"secret:{re.escape(secret_name)}-[A-Za-z0-9]{{6}}$"
    )


class XOAuthBootstrapError(RuntimeError):
    """A deliberately non-sensitive operator-facing bootstrap failure."""


class _StsClient(Protocol):
    def get_caller_identity(self) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class _SecretsManagerClient(Protocol):
    def describe_secret(self, **kwargs: object) -> Mapping[str, object]: ...

    def create_secret(self, **kwargs: object) -> Mapping[str, object]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class _OAuthTokens:
    access_token: str
    refresh_token: str

    def __repr__(self) -> str:
        return "_OAuthTokens(<redacted>)"


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    secret_arn: str
    creator_user_id: str


@dataclass(frozen=True, slots=True, repr=False)
class _CallbackResult:
    authorization_code: str | None
    authorization_denied: bool

    def __repr__(self) -> str:
        return "_CallbackResult(<redacted>)"


class _OAuthCallbackServer(HTTPServer):
    """Single-purpose loopback server with no request logging."""

    allow_reuse_address = False

    def __init__(self, state: str) -> None:
        self.expected_state = state
        self.callback_result: _CallbackResult | None = None
        super().__init__((CALLBACK_HOST, CALLBACK_PORT), _OAuthCallbackHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        # Request details can contain the authorization code.  Never let the
        # stdlib server print them in a traceback.
        del request, client_address


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: _OAuthCallbackServer

    def do_GET(self) -> None:
        result = _parse_callback_request(
            target=self.path,
            host=self.headers.get("Host"),
            expected_state=self.server.expected_state,
        )
        if result is None:
            self._respond(
                400,
                b"<!doctype html><title>Authorization not accepted</title>"
                b"<p>This callback was not accepted. Return to the original X consent tab.</p>",
            )
            return
        self.server.callback_result = result
        if result.authorization_denied:
            self._respond(
                400,
                b"<!doctype html><title>Authorization declined</title>"
                b"<p>X authorization was declined. You can close this tab.</p>",
            )
            return
        self._respond(
            200,
            b"<!doctype html><title>Authorization received</title>"
            b"<p>Authorization received. You can close this tab.</p>",
        )

    def do_POST(self) -> None:
        self._respond(405, b"")

    def log_message(self, format: str, *args: object) -> None:
        # The default implementation logs the request target, including code.
        del format, args

    def _respond(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError:
            # A closed browser tab must not cause request details to be logged.
            pass


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


def _parse_callback_request(
    *,
    target: str,
    host: str | None,
    expected_state: str,
) -> _CallbackResult | None:
    if host != f"{CALLBACK_HOST}:{CALLBACK_PORT}" or len(target) > 32 * 1024:
        return None
    parsed = urlsplit(target)
    if parsed.path != CALLBACK_PATH or parsed.fragment:
        return None
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=12,
        )
    except ValueError:
        return None
    states = query.get("state")
    if states is None or len(states) != 1 or not hmac.compare_digest(states[0], expected_state):
        return None
    codes = query.get("code")
    errors = query.get("error")
    if codes is not None and errors is not None:
        return None
    if errors is not None:
        if len(errors) != 1 or not errors[0] or len(errors[0]) > 1_024:
            return None
        return _CallbackResult(authorization_code=None, authorization_denied=True)
    if codes is None or len(codes) != 1:
        return None
    code = codes[0]
    if (
        not code
        or len(code.encode("utf-8")) > _MAX_AUTHORIZATION_CODE_BYTES
        or "\r" in code
        or "\n" in code
    ):
        return None
    return _CallbackResult(authorization_code=code, authorization_denied=False)


def _pkce_values() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _authorization_url(*, client_id: str, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK_URL,
            "scope": " ".join(X_REQUIRED_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        quote_via=quote,
    )
    return f"{X_AUTHORIZE_URL}?{query}"


def _receive_authorization_code(
    *,
    authorization_url: str,
    state: str,
    browser_opener: Callable[[str], bool] = webbrowser.open_new_tab,
) -> str:
    try:
        server = _OAuthCallbackServer(state)
    except OSError:
        raise XOAuthBootstrapError("the private callback listener could not be started") from None
    with server:
        try:
            opened = browser_opener(authorization_url)
        except Exception:
            opened = False
        if not opened:
            raise XOAuthBootstrapError("the X authorization page could not be opened")
        deadline = monotonic() + _CALLBACK_TIMEOUT_SECONDS
        while server.callback_result is None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise XOAuthBootstrapError("X authorization timed out")
            server.timeout = min(remaining, 1.0)
            server.handle_request()
        result = server.callback_result
    if result.authorization_denied:
        raise XOAuthBootstrapError("X authorization was declined")
    if result.authorization_code is None:
        raise XOAuthBootstrapError("X authorization response was invalid")
    return result.authorization_code


def _exchange_authorization_code(
    client: httpx2.Client,
    *,
    client_id: str,
    client_secret: str,
    authorization_code: str,
    code_verifier: str,
) -> _OAuthTokens:
    request_failed = False
    response: httpx2.Response | None = None
    try:
        response = client.post(
            X_TOKEN_URL,
            data={
                "code": authorization_code,
                "grant_type": "authorization_code",
                "redirect_uri": CALLBACK_URL,
                "code_verifier": code_verifier,
            },
            auth=httpx2.BasicAuth(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    except httpx2.RequestError:
        request_failed = True
    client_id = client_secret = authorization_code = code_verifier = ""
    if request_failed or response is None:
        raise XOAuthBootstrapError("the X token exchange was unavailable")
    try:
        tokens = _validated_tokens(
            response,
            rejected_message="the X token exchange was rejected",
        )
    except XOAuthBootstrapError:
        _scrub_response_request(response)
        response = None
        raise
    return tokens


def _exchange_refresh_token(
    client: httpx2.Client,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> _OAuthTokens:
    """Rotate one portal-issued refresh token without exposing it to argv."""

    request_failed = False
    response: httpx2.Response | None = None
    try:
        response = client.post(
            X_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=httpx2.BasicAuth(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    except httpx2.RequestError:
        request_failed = True
    client_id = client_secret = refresh_token = ""
    if request_failed or response is None:
        raise XOAuthBootstrapError("the X refresh-token exchange was unavailable")
    try:
        tokens = _validated_tokens(
            response,
            rejected_message="the X refresh-token exchange was rejected",
        )
    except XOAuthBootstrapError:
        _scrub_response_request(response)
        response = None
        raise
    return tokens


def _validated_tokens(
    response: httpx2.Response,
    *,
    rejected_message: str,
) -> _OAuthTokens:
    if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
        _scrub_response_request(response)
        del response
        raise XOAuthBootstrapError(rejected_message)
    failure_message: str | None = None
    payload: dict[str, object] | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    try:
        payload = _json_object(response.content)
        access_token = _secret_value(payload.get("access_token"), maximum=16_384)
        try:
            refresh_token = _secret_value(payload.get("refresh_token"), maximum=16_384)
        except ValueError:
            failure_message = "X did not return a replacement refresh token"
        token_type = payload.get("token_type")
        scope = payload.get("scope")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise ValueError
        if not isinstance(scope, str):
            failure_message = "X did not return verifiable OAuth scopes"
            raise ValueError
        granted_scopes = frozenset(scope.split())
        missing_scopes = sorted(frozenset(X_REQUIRED_SCOPES) - granted_scopes)
        if missing_scopes:
            failure_message = "X OAuth token is missing required scopes: " + ", ".join(
                missing_scopes
            )
            raise ValueError
    except (TypeError, ValueError):
        if failure_message is None:
            failure_message = "the X token response was invalid"
    if failure_message is not None or access_token is None or refresh_token is None:
        _scrub_response_request(response)
        del response
        payload = None
        access_token = None
        refresh_token = None
        raise XOAuthBootstrapError(failure_message or "the X token response was invalid")
    return _OAuthTokens(access_token=access_token, refresh_token=refresh_token)


def _resolve_creator_user_id(client: httpx2.Client, *, access_token: str) -> str:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    request_failed = False
    response: httpx2.Response | None = None
    try:
        response = client.get(
            X_CREATOR_URL,
            headers=headers,
        )
    except httpx2.RequestError:
        request_failed = True
    access_token = ""
    headers["Authorization"] = "[redacted]"
    if request_failed or response is None:
        raise XOAuthBootstrapError("the X creator account check was unavailable")
    if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
        _scrub_response_request(response)
        del response
        raise XOAuthBootstrapError("the X creator account check was rejected")
    invalid_response = False
    payload: dict[str, object] | None = None
    data_value: object = None
    creator_user_id: object = None
    try:
        payload = _json_object(response.content)
        data_value = payload.get("data")
        if not isinstance(data_value, Mapping):
            raise ValueError
        creator_user_id = data_value.get("id")
        if not isinstance(creator_user_id, str) or _CREATOR_ID.fullmatch(creator_user_id) is None:
            raise ValueError
    except (TypeError, ValueError):
        invalid_response = True
    if invalid_response or not isinstance(creator_user_id, str):
        _scrub_response_request(response)
        del response
        payload = None
        data_value = None
        creator_user_id = None
        raise XOAuthBootstrapError("the X creator account response was invalid")
    return creator_user_id


def _resolve_oauth1_creator_user_id(
    client: httpx2.Client,
    *,
    authorization: XOAuth1Authorization,
) -> str:
    authorization_header = authorization.authorization_header(
        method="GET",
        url=X_CREATOR_URL,
    )
    del authorization
    headers = {
        "Accept": "application/json",
        "Authorization": authorization_header,
    }
    request_failed = False
    response: httpx2.Response | None = None
    try:
        response = client.get(
            X_CREATOR_URL,
            headers=headers,
        )
    except httpx2.RequestError:
        request_failed = True
    authorization_header = "[redacted]"
    headers["Authorization"] = "[redacted]"
    if request_failed:
        raise XOAuthBootstrapError("the X creator account check was unavailable")
    if response is None:  # pragma: no cover - all request outcomes handled above
        raise XOAuthBootstrapError("the X creator account check was unavailable")
    if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
        _scrub_response_request(response)
        del response
        raise XOAuthBootstrapError("the X creator account check was rejected")
    invalid_response = False
    creator_user_id: object = None
    payload: dict[str, object] | None = None
    data_value: object = None
    try:
        payload = _json_object(response.content)
        data_value = payload.get("data")
        if not isinstance(data_value, Mapping):
            raise ValueError
        creator_user_id = data_value.get("id")
        if not isinstance(creator_user_id, str) or _CREATOR_ID.fullmatch(creator_user_id) is None:
            raise ValueError
    except (TypeError, ValueError):
        invalid_response = True
    if invalid_response or not isinstance(creator_user_id, str):
        _scrub_response_request(response)
        response = None
        payload = None
        data_value = None
        creator_user_id = None
        raise XOAuthBootstrapError("the X creator account response was invalid")
    return creator_user_id


def _credential_json(*, client_id: str, client_secret: str, refresh_token: str) -> str:
    serialized = json.dumps(
        {
            "schema": X_OAUTH_SECRET_SCHEMA,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(serialized.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise XOAuthBootstrapError("the X credential is too large")
    return serialized


def _oauth1_credential_json(credentials: XOAuth1Credentials) -> str:
    serialized = json.dumps(
        {
            "schema": X_OAUTH1_SECRET_SCHEMA,
            "consumer_key": credentials.consumer_key,
            "consumer_secret": credentials.consumer_secret,
            "access_token": credentials.access_token,
            "access_token_secret": credentials.access_token_secret,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(serialized.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise XOAuthBootstrapError("the X credential is too large")
    return serialized


def _verify_aws_identity(sts_client: _StsClient) -> None:
    try:
        identity = sts_client.get_caller_identity()
    except Exception:
        raise XOAuthBootstrapError("the staging AWS session is unavailable") from None
    if identity.get("Account") != AWS_ACCOUNT_ID:
        raise XOAuthBootstrapError("the staging AWS session is for the wrong account")
    arn = identity.get("Arn")
    if not isinstance(arn, str) or _STAGING_OPERATOR_ARN.fullmatch(arn) is None:
        raise XOAuthBootstrapError("the staging AWS session is not the approved operator role")


def _ensure_secret_absent(
    secrets_client: _SecretsManagerClient,
    *,
    secret_name: str = AWS_SECRET_NAME,
) -> None:
    try:
        secrets_client.describe_secret(SecretId=secret_name)
    except Exception as error:
        if _aws_error_code(error) == "ResourceNotFoundException":
            return
        raise XOAuthBootstrapError("the staging secret destination could not be checked") from None
    raise XOAuthBootstrapError("the staging X OAuth secret already exists")


def _store_credential(
    secrets_client: _SecretsManagerClient,
    *,
    credential_json: str,
    secret_name: str = AWS_SECRET_NAME,
    description: str = "Gen Automation staging X OAuth creator credential",
    purpose: str = "x-oauth",
) -> str:
    store_failed = False
    response: Mapping[str, object] | None = None
    try:
        response = secrets_client.create_secret(
            Name=secret_name,
            Description=description,
            SecretString=credential_json,
            Tags=[
                {"Key": "Application", "Value": "gen-automation"},
                {"Key": "Environment", "Value": "staging"},
                {"Key": "Purpose", "Value": purpose},
            ],
        )
    except Exception:
        store_failed = True
    if store_failed:
        credential_json = "[redacted]"
        raise XOAuthBootstrapError("the staging X OAuth secret could not be created")
    if response is None:  # pragma: no cover - all SDK outcomes handled above
        credential_json = "[redacted]"
        raise XOAuthBootstrapError("the staging X OAuth secret could not be created")
    credential_json = "[redacted]"
    arn = response.get("ARN")
    if not isinstance(arn, str) or _secret_arn_pattern(secret_name).fullmatch(arn) is None:
        raise XOAuthBootstrapError("AWS returned an invalid staging secret reference")
    return arn


def _bootstrap(
    *,
    client_id: str,
    client_secret: str,
    secrets_client: _SecretsManagerClient,
    http_client: httpx2.Client,
    callback_receiver: Callable[..., str] = _receive_authorization_code,
) -> BootstrapResult:
    code_verifier, code_challenge = _pkce_values()
    state = secrets.token_urlsafe(32)
    authorization_url = _authorization_url(
        client_id=client_id,
        state=state,
        code_challenge=code_challenge,
    )
    authorization_code = callback_receiver(
        authorization_url=authorization_url,
        state=state,
    )
    tokens = _exchange_authorization_code(
        http_client,
        client_id=client_id,
        client_secret=client_secret,
        authorization_code=authorization_code,
        code_verifier=code_verifier,
    )
    creator_user_id = _resolve_creator_user_id(
        http_client,
        access_token=tokens.access_token,
    )
    secret_arn = _store_credential(
        secrets_client,
        credential_json=_credential_json(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=tokens.refresh_token,
        ),
    )
    return BootstrapResult(secret_arn=secret_arn, creator_user_id=creator_user_id)


def _bootstrap_from_portal_refresh_token(
    *,
    client_id: str,
    client_secret: str,
    portal_refresh_token: str,
    secrets_client: _SecretsManagerClient,
    http_client: httpx2.Client,
) -> BootstrapResult:
    """Validate and rotate a Developer Portal-generated creator credential."""

    tokens = _exchange_refresh_token(
        http_client,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=portal_refresh_token,
    )
    creator_user_id = _resolve_creator_user_id(
        http_client,
        access_token=tokens.access_token,
    )
    secret_arn = _store_credential(
        secrets_client,
        credential_json=_credential_json(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=tokens.refresh_token,
        ),
    )
    return BootstrapResult(secret_arn=secret_arn, creator_user_id=creator_user_id)


def _bootstrap_oauth1(
    *,
    credentials: XOAuth1Credentials,
    secrets_client: _SecretsManagerClient,
    http_client: httpx2.Client,
) -> BootstrapResult:
    """Validate the owner-account OAuth 1.0a credential before storing it."""

    authorization = XOAuth1Authorization(credentials)
    creator_user_id = _resolve_oauth1_creator_user_id(
        http_client,
        authorization=authorization,
    )
    secret_arn = _store_credential(
        secrets_client,
        credential_json=_oauth1_credential_json(credentials),
        secret_name=AWS_OAUTH1_SECRET_NAME,
        description="Gen Automation staging X OAuth 1.0a creator credential",
        purpose="x-oauth1",
    )
    return BootstrapResult(secret_arn=secret_arn, creator_user_id=creator_user_id)


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


def _secret_value(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\r" in value
        or "\n" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError("invalid secret value")
    return value


def _secret_prompt(label: str, *, maximum: int) -> str:
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                # getpass otherwise warns and falls back to echoed stdin when it
                # cannot control terminal echo.  Credential entry must fail closed.
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(label)
        except getpass.GetPassWarning:
            raise XOAuthBootstrapError("hidden credential input is unavailable") from None
        try:
            return _secret_value(value, maximum=maximum)
        except ValueError:
            value = ""
            if attempt < 2:
                print(
                    "That field was empty or contained invalid whitespace; please try again.",
                    file=sys.stderr,
                )
    raise XOAuthBootstrapError("credential input was not accepted after three attempts")


def _aws_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None


def _aws_clients() -> tuple[_StsClient, _SecretsManagerClient]:
    boto_config = Config(
        connect_timeout=5,
        read_timeout=15,
        retries={"mode": "standard", "max_attempts": 3},
        ignore_configured_endpoint_urls=True,
        proxies={},
        user_agent_extra="gen-automation-x-oauth-bootstrap/1",
    )
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    sts_client = cast(
        _StsClient,
        session.client(
            "sts",
            region_name=AWS_REGION,
            config=boto_config,
            verify=True,
        ),
    )
    secrets_client = cast(
        _SecretsManagerClient,
        session.client(
            "secretsmanager",
            region_name=AWS_REGION,
            config=boto_config,
            verify=True,
        ),
    )
    return sts_client, secrets_client


def x_oauth_bootstrap_main() -> int:
    """Bootstrap the exact staging X creator credential without CLI secrets."""

    arguments = sys.argv[1:]
    if arguments not in ([], [PORTAL_REFRESH_TOKEN_MODE], [OAUTH1_MODE]):
        print("This command accepts only its documented non-secret mode flag.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty() or not sys.stderr.isatty():
        print(
            "X authorization bootstrap requires an interactive terminal.",
            file=sys.stderr,
        )
        return 2

    sts_client: _StsClient | None = None
    secrets_client: _SecretsManagerClient | None = None
    try:
        sts_client, secrets_client = _aws_clients()
        _verify_aws_identity(sts_client)
        oauth1_mode = arguments == [OAUTH1_MODE]
        _ensure_secret_absent(
            secrets_client,
            secret_name=AWS_OAUTH1_SECRET_NAME if oauth1_mode else AWS_SECRET_NAME,
        )
        with httpx2.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as http_client:
            if oauth1_mode:
                result = _bootstrap_oauth1(
                    credentials=XOAuth1Credentials(
                        consumer_key=_secret_prompt(
                            "X OAuth 1.0a Consumer/API Key: ", maximum=16_384
                        ),
                        consumer_secret=_secret_prompt(
                            "X OAuth 1.0a Consumer/API Secret: ", maximum=16_384
                        ),
                        access_token=_secret_prompt("X OAuth 1.0a Access Token: ", maximum=16_384),
                        access_token_secret=_secret_prompt(
                            "X OAuth 1.0a Access Token Secret: ", maximum=16_384
                        ),
                    ),
                    secrets_client=secrets_client,
                    http_client=http_client,
                )
            else:
                client_id = _secret_prompt("X OAuth client ID: ", maximum=1_024)
                client_secret = _secret_prompt("X OAuth client secret: ", maximum=8_192)
                portal_refresh_token = (
                    _secret_prompt("X Developer Portal refresh token: ", maximum=16_384)
                    if arguments == [PORTAL_REFRESH_TOKEN_MODE]
                    else None
                )
                if portal_refresh_token is None:
                    result = _bootstrap(
                        client_id=client_id,
                        client_secret=client_secret,
                        secrets_client=secrets_client,
                        http_client=http_client,
                    )
                else:
                    result = _bootstrap_from_portal_refresh_token(
                        client_id=client_id,
                        client_secret=client_secret,
                        portal_refresh_token=portal_refresh_token,
                        secrets_client=secrets_client,
                        http_client=http_client,
                    )
    except XOAuthBootstrapError as error:
        print(f"X authorization bootstrap failed: {error}.", file=sys.stderr)
        return 1
    except ValueError:
        print("X authorization bootstrap failed.", file=sys.stderr)
        return 1
    except Exception:
        print("X authorization bootstrap failed.", file=sys.stderr)
        return 1
    finally:
        if secrets_client is not None:
            with suppress(Exception):
                secrets_client.close()
        if sts_client is not None:
            with suppress(Exception):
                sts_client.close()

    # These are the only successful stdout values.  OAuth tokens, client
    # credentials, profile details, and response bodies are never printed.
    print(result.secret_arn)
    print(result.creator_user_id)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the entry point
    raise SystemExit(x_oauth_bootstrap_main())
