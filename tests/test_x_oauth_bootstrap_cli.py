from __future__ import annotations

import base64
import hashlib
import io
import json
import warnings
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from gen_automation import x_oauth_bootstrap_cli as bootstrap

CLIENT_ID = "client-value-a"
CLIENT_SECRET = "-".join(("private", "value", "b"))
ACCESS_TOKEN = "-".join(("access", "value", "c"))
REFRESH_TOKEN = "-".join(("refresh", "value", "d"))
PORTAL_REFRESH_TOKEN = "-".join(("portal", "refresh", "value"))
ROTATED_REFRESH_TOKEN = "-".join(("rotated", "refresh", "value"))
OAUTH1_CONSUMER_KEY = "-".join(("oauth1", "consumer", "key"))
OAUTH1_CONSUMER_SECRET = "-".join(("oauth1", "consumer", "secret"))
OAUTH1_ACCESS_TOKEN = "-".join(("oauth1", "access", "token"))
OAUTH1_ACCESS_TOKEN_SECRET = "-".join(("oauth1", "access", "token", "secret"))
AUTHORIZATION_CODE = "authorization-value-e"
CREATOR_ID = "2244994945"
SECRET_ARN = (
    "arn:aws:secretsmanager:eu-central-1:861912887470:secret:gen-automation-staging/x/oauth-AbCd12"  # noqa: S105 - resource reference
)
OAUTH1_SECRET_ARN = (
    "arn:aws:secretsmanager:eu-central-1:861912887470:secret:gen-automation-staging/x/oauth1-AbCd12"  # noqa: S105 - resource reference
)


class _MissingSecretError(Exception):
    response: ClassVar[dict[str, object]] = {"Error": {"Code": "ResourceNotFoundException"}}


class _Sts:
    def __init__(
        self,
        account: str = bootstrap.AWS_ACCOUNT_ID,
        arn: object = (
            "arn:aws:sts::861912887470:assumed-role/"
            "AWSReservedSSO_GenAutomationStagingDeployer_0123456789abcdef/"
            "operator@example.com"
        ),
    ) -> None:
        self.account = account
        self.arn = arn
        self.closed = False

    def get_caller_identity(self) -> Mapping[str, object]:
        return {"Account": self.account, "Arn": self.arn}

    def close(self) -> None:
        self.closed = True


class _Secrets:
    def __init__(
        self,
        *,
        exists: bool = False,
        expected_secret_name: str = bootstrap.AWS_SECRET_NAME,
        secret_arn: str = SECRET_ARN,
    ) -> None:
        self.exists = exists
        self.expected_secret_name = expected_secret_name
        self.secret_arn = secret_arn
        self.created: dict[str, object] | None = None
        self.closed = False

    def describe_secret(self, **kwargs: object) -> Mapping[str, object]:
        assert kwargs == {"SecretId": self.expected_secret_name}
        if not self.exists:
            raise _MissingSecretError
        return {"ARN": self.secret_arn}

    def create_secret(self, **kwargs: object) -> Mapping[str, object]:
        self.created = dict(kwargs)
        return {"ARN": self.secret_arn}

    def close(self) -> None:
        self.closed = True


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


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


def test_fixed_authorization_contract_uses_loopback_pkce_and_minimum_scopes() -> None:
    verifier, challenge = bootstrap._pkce_values()

    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    url = bootstrap._authorization_url(
        client_id=CLIENT_ID,
        state="state-value",
        code_challenge=challenge,
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == bootstrap.X_AUTHORIZE_URL
    assert bootstrap.CALLBACK_URL == "http://127.0.0.1:8765/callback"
    assert query == {
        "response_type": ["code"],
        "client_id": [CLIENT_ID],
        "redirect_uri": [bootstrap.CALLBACK_URL],
        "scope": [" ".join(bootstrap.X_REQUIRED_SCOPES)],
        "state": ["state-value"],
        "code_challenge": [challenge],
        "code_challenge_method": ["S256"],
    }


@pytest.mark.parametrize(
    ("target", "host"),
    [
        ("/callback?state=wrong&code=ok", "127.0.0.1:8765"),
        ("/callback?state=right&state=right&code=ok", "127.0.0.1:8765"),
        ("/callback?state=right&code=ok", "localhost:8765"),
        ("/other?state=right&code=ok", "127.0.0.1:8765"),
        ("/callback?state=right&code=ok&error=denied", "127.0.0.1:8765"),
    ],
)
def test_callback_rejects_wrong_state_duplicate_state_host_path_and_ambiguity(
    target: str,
    host: str,
) -> None:
    assert (
        bootstrap._parse_callback_request(
            target=target,
            host=host,
            expected_state="right",
        )
        is None
    )


def test_callback_accepts_one_code_or_a_state_bound_denial() -> None:
    accepted = bootstrap._parse_callback_request(
        target="/callback?state=right&code=code-value",
        host="127.0.0.1:8765",
        expected_state="right",
    )
    denied = bootstrap._parse_callback_request(
        target="/callback?state=right&error=access_denied&error_description=private",
        host="127.0.0.1:8765",
        expected_state="right",
    )

    assert accepted is not None
    assert accepted.authorization_code == "code-value"
    assert not accepted.authorization_denied
    assert denied is not None
    assert denied.authorization_code is None
    assert denied.authorization_denied
    assert "private" not in repr(denied)


def test_bootstrap_uses_confidential_exchange_verifies_creator_and_stores_exact_schema() -> None:
    callback_query: dict[str, list[str]] = {}

    def callback_receiver(**kwargs: str) -> str:
        nonlocal callback_query
        callback_query = parse_qs(urlsplit(kwargs["authorization_url"]).query)
        assert callback_query["state"] == [kwargs["state"]]
        return AUTHORIZATION_CODE

    def x_transport(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == bootstrap.X_TOKEN_URL:
            assert request.method == "POST"
            form = parse_qs(request.content.decode("ascii"))
            assert form["code"] == [AUTHORIZATION_CODE]
            assert form["grant_type"] == ["authorization_code"]
            assert form["redirect_uri"] == [bootstrap.CALLBACK_URL]
            assert "client_id" not in form
            verifier = form["code_verifier"][0]
            expected_challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            assert callback_query["code_challenge"] == [expected_challenge]
            expected_basic = base64.b64encode(
                f"{CLIENT_ID}:{CLIENT_SECRET}".encode("ascii")
            ).decode("ascii")
            assert request.headers["Authorization"] == f"Basic {expected_basic}"
            return httpx2.Response(
                200,
                json={
                    "token_type": "bearer",
                    "expires_in": 7_200,
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": REFRESH_TOKEN,
                    "scope": " ".join(bootstrap.X_REQUIRED_SCOPES),
                },
            )
        assert str(request.url) == bootstrap.X_CREATOR_URL
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        return httpx2.Response(200, json={"data": {"id": CREATOR_ID}})

    secrets_client = _Secrets()
    with httpx2.Client(
        transport=httpx2.MockTransport(x_transport),
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        result = bootstrap._bootstrap(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            secrets_client=secrets_client,
            http_client=http_client,
            callback_receiver=callback_receiver,
        )

    assert result == bootstrap.BootstrapResult(
        secret_arn=SECRET_ARN,
        creator_user_id=CREATOR_ID,
    )
    assert secrets_client.created is not None
    assert secrets_client.created["Name"] == bootstrap.AWS_SECRET_NAME
    assert json.loads(str(secrets_client.created["SecretString"])) == {
        "schema": "gen-automation/x-oauth/v1",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }
    assert ACCESS_TOKEN not in str(secrets_client.created["SecretString"])


def test_exchange_fails_closed_when_x_does_not_grant_every_required_scope() -> None:
    calls: list[str] = []

    def x_transport(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return httpx2.Response(
            200,
            json={
                "token_type": "bearer",
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "scope": "tweet.read users.read offline.access",
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(x_transport)) as http_client:
        with pytest.raises(bootstrap.XOAuthBootstrapError, match="missing required scopes"):
            bootstrap._exchange_authorization_code(
                http_client,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                authorization_code=AUTHORIZATION_CODE,
                code_verifier="verifier-value",
            )
    assert calls == [bootstrap.X_TOKEN_URL]


def test_oauth2_authorization_code_request_error_detaches_basic_and_form_secrets() -> None:
    code_verifier = "pkce-verifier-must-not-survive"
    requests: list[tuple[str, bytes]] = []

    def x_transport(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.headers["Authorization"], request.content))
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    with httpx2.Client(transport=httpx2.MockTransport(x_transport)) as http_client:
        with pytest.raises(bootstrap.XOAuthBootstrapError) as captured:
            bootstrap._exchange_authorization_code(
                http_client,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                authorization_code=AUTHORIZATION_CODE,
                code_verifier=code_verifier,
            )

    error = captured.value
    assert requests
    authorization_header, request_body = requests[0]
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert authorization_header == f"Basic {expected_basic}"
    assert AUTHORIZATION_CODE.encode() in request_body
    assert code_verifier.encode() in request_body
    assert not any(isinstance(item, httpx2.RequestError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    for sensitive in (
        CLIENT_ID,
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        code_verifier,
        expected_basic,
        request_body.decode(),
    ):
        assert sensitive not in surface


def test_portal_refresh_import_rotates_validates_creator_and_stores_returned_token() -> None:
    def x_transport(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == bootstrap.X_TOKEN_URL:
            assert request.method == "POST"
            assert parse_qs(request.content.decode("ascii")) == {
                "grant_type": ["refresh_token"],
                "refresh_token": [PORTAL_REFRESH_TOKEN],
            }
            expected_basic = base64.b64encode(
                f"{CLIENT_ID}:{CLIENT_SECRET}".encode("ascii")
            ).decode("ascii")
            assert request.headers["Authorization"] == f"Basic {expected_basic}"
            return httpx2.Response(
                200,
                json={
                    "token_type": "bearer",
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": ROTATED_REFRESH_TOKEN,
                    "scope": " ".join(bootstrap.X_REQUIRED_SCOPES),
                },
            )
        assert str(request.url) == bootstrap.X_CREATOR_URL
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        return httpx2.Response(200, json={"data": {"id": CREATOR_ID}})

    secrets_client = _Secrets()
    with httpx2.Client(
        transport=httpx2.MockTransport(x_transport),
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        result = bootstrap._bootstrap_from_portal_refresh_token(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            portal_refresh_token=PORTAL_REFRESH_TOKEN,
            secrets_client=secrets_client,
            http_client=http_client,
        )

    assert result == bootstrap.BootstrapResult(SECRET_ARN, CREATOR_ID)
    assert secrets_client.created is not None
    serialized = str(secrets_client.created["SecretString"])
    assert json.loads(serialized) == {
        "schema": "gen-automation/x-oauth/v1",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": ROTATED_REFRESH_TOKEN,
    }
    assert PORTAL_REFRESH_TOKEN not in serialized
    assert ACCESS_TOKEN not in serialized


def test_oauth2_refresh_request_error_detaches_basic_and_form_secrets() -> None:
    requests: list[tuple[str, bytes]] = []

    def x_transport(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.headers["Authorization"], request.content))
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    with httpx2.Client(transport=httpx2.MockTransport(x_transport)) as http_client:
        with pytest.raises(bootstrap.XOAuthBootstrapError) as captured:
            bootstrap._exchange_refresh_token(
                http_client,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                refresh_token=PORTAL_REFRESH_TOKEN,
            )

    error = captured.value
    assert requests
    authorization_header, request_body = requests[0]
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert authorization_header == f"Basic {expected_basic}"
    assert PORTAL_REFRESH_TOKEN.encode() in request_body
    assert not any(isinstance(item, httpx2.RequestError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    for sensitive in (
        CLIENT_ID,
        CLIENT_SECRET,
        PORTAL_REFRESH_TOKEN,
        expected_basic,
        request_body.decode(),
    ):
        assert sensitive not in surface


def test_oauth1_bootstrap_verifies_signed_owner_before_storing_exact_schema() -> None:
    secrets_client = _Secrets(
        expected_secret_name=bootstrap.AWS_OAUTH1_SECRET_NAME,
        secret_arn=OAUTH1_SECRET_ARN,
    )
    credentials = bootstrap.XOAuth1Credentials(
        consumer_key=OAUTH1_CONSUMER_KEY,
        consumer_secret=OAUTH1_CONSUMER_SECRET,
        access_token=OAUTH1_ACCESS_TOKEN,
        access_token_secret=OAUTH1_ACCESS_TOKEN_SECRET,
    )
    requests = 0

    def x_transport(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        assert secrets_client.created is None
        assert request.method == "GET"
        assert str(request.url) == bootstrap.X_CREATOR_URL
        header = request.headers["Authorization"]
        assert header.startswith("OAuth ")
        assert 'oauth_signature_method="HMAC-SHA1"' in header
        assert "Bearer " not in header
        return httpx2.Response(200, json={"data": {"id": CREATOR_ID}})

    with httpx2.Client(
        transport=httpx2.MockTransport(x_transport),
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        result = bootstrap._bootstrap_oauth1(
            credentials=credentials,
            secrets_client=secrets_client,
            http_client=http_client,
        )

    assert result == bootstrap.BootstrapResult(OAUTH1_SECRET_ARN, CREATOR_ID)
    assert requests == 1
    assert secrets_client.created is not None
    assert secrets_client.created["Name"] == bootstrap.AWS_OAUTH1_SECRET_NAME
    assert secrets_client.created["Description"] == (
        "Gen Automation staging X OAuth 1.0a creator credential"
    )
    assert secrets_client.created["Tags"] == [
        {"Key": "Application", "Value": "gen-automation"},
        {"Key": "Environment", "Value": "staging"},
        {"Key": "Purpose", "Value": "x-oauth1"},
    ]
    assert json.loads(str(secrets_client.created["SecretString"])) == {
        "schema": "gen-automation/x-oauth1/v1",
        "consumer_key": OAUTH1_CONSUMER_KEY,
        "consumer_secret": OAUTH1_CONSUMER_SECRET,
        "access_token": OAUTH1_ACCESS_TOKEN,
        "access_token_secret": OAUTH1_ACCESS_TOKEN_SECRET,
    }


@pytest.mark.parametrize(
    "response",
    (
        httpx2.Response(401, text="rejected"),
        httpx2.Response(200, json={"data": {"id": 42}}),
        httpx2.Response(200, content=b"not-json"),
    ),
)
def test_oauth1_bootstrap_never_stores_an_unverifiable_owner(
    response: httpx2.Response,
) -> None:
    secrets_client = _Secrets(
        expected_secret_name=bootstrap.AWS_OAUTH1_SECRET_NAME,
        secret_arn=OAUTH1_SECRET_ARN,
    )
    credentials = bootstrap.XOAuth1Credentials(
        consumer_key=OAUTH1_CONSUMER_KEY,
        consumer_secret=OAUTH1_CONSUMER_SECRET,
        access_token=OAUTH1_ACCESS_TOKEN,
        access_token_secret=OAUTH1_ACCESS_TOKEN_SECRET,
    )

    with httpx2.Client(transport=httpx2.MockTransport(lambda _request: response)) as http_client:
        with pytest.raises(bootstrap.XOAuthBootstrapError, match="account"):
            bootstrap._bootstrap_oauth1(
                credentials=credentials,
                secrets_client=secrets_client,
                http_client=http_client,
            )

    assert secrets_client.created is None


def test_oauth1_bootstrap_transport_error_detaches_authorization_request() -> None:
    credentials = bootstrap.XOAuth1Credentials(
        consumer_key=OAUTH1_CONSUMER_KEY,
        consumer_secret=OAUTH1_CONSUMER_SECRET,
        access_token=OAUTH1_ACCESS_TOKEN,
        access_token_secret=OAUTH1_ACCESS_TOKEN_SECRET,
    )
    authorization = bootstrap.XOAuth1Authorization(credentials)
    authorization_headers: list[str] = []

    def x_transport(request: httpx2.Request) -> httpx2.Response:
        authorization_headers.append(request.headers["Authorization"])
        raise httpx2.RemoteProtocolError("connection closed", request=request)

    with httpx2.Client(transport=httpx2.MockTransport(x_transport)) as http_client:
        with pytest.raises(bootstrap.XOAuthBootstrapError) as captured:
            bootstrap._resolve_oauth1_creator_user_id(
                http_client,
                authorization=authorization,
            )

    error = captured.value
    assert authorization_headers and authorization_headers[0].startswith("OAuth ")
    assert not any(isinstance(item, httpx2.RequestError) for item in _exception_chain(error))
    surface = _exception_security_surface(error)
    for sensitive in (
        OAUTH1_CONSUMER_KEY,
        OAUTH1_CONSUMER_SECRET,
        OAUTH1_ACCESS_TOKEN,
        OAUTH1_ACCESS_TOKEN_SECRET,
        authorization_headers[0],
    ):
        assert sensitive not in surface


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (
            {
                "token_type": "mac",
                "access_token": ACCESS_TOKEN,
                "refresh_token": ROTATED_REFRESH_TOKEN,
                "scope": " ".join(bootstrap.X_REQUIRED_SCOPES),
            },
            "token response was invalid",
        ),
        (
            {
                "token_type": "bearer",
                "access_token": ACCESS_TOKEN,
                "scope": " ".join(bootstrap.X_REQUIRED_SCOPES),
            },
            "replacement refresh token",
        ),
        (
            {
                "token_type": "bearer",
                "access_token": ACCESS_TOKEN,
                "refresh_token": ROTATED_REFRESH_TOKEN,
                "scope": "tweet.read users.read offline.access",
            },
            "missing required scopes",
        ),
    ],
)
def test_portal_refresh_import_fails_closed_on_invalid_rotated_token(
    payload: dict[str, str],
    expected_error: str,
) -> None:
    def x_transport(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    with httpx2.Client(transport=httpx2.MockTransport(x_transport)) as http_client:
        with pytest.raises(bootstrap.XOAuthBootstrapError, match=expected_error):
            bootstrap._exchange_refresh_token(
                http_client,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                refresh_token=PORTAL_REFRESH_TOKEN,
            )


def test_aws_preflight_is_account_and_role_bound_and_refuses_overwrite() -> None:
    bootstrap._verify_aws_identity(_Sts())
    bootstrap._ensure_secret_absent(_Secrets())

    with pytest.raises(bootstrap.XOAuthBootstrapError, match="wrong account"):
        bootstrap._verify_aws_identity(_Sts(account="123456789012"))
    with pytest.raises(bootstrap.XOAuthBootstrapError, match="approved operator role"):
        bootstrap._verify_aws_identity(
            _Sts(
                arn=(
                    "arn:aws:sts::861912887470:assumed-role/"
                    "AWSReservedSSO_AdministratorAccess_0123456789abcdef/operator"
                )
            )
        )
    with pytest.raises(bootstrap.XOAuthBootstrapError, match="approved operator role"):
        bootstrap._verify_aws_identity(_Sts(arn="not-an-arn"))
    with pytest.raises(bootstrap.XOAuthBootstrapError, match="approved operator role"):
        bootstrap._verify_aws_identity(_Sts(arn=None))
    with pytest.raises(bootstrap.XOAuthBootstrapError, match="already exists"):
        bootstrap._ensure_secret_absent(_Secrets(exists=True))


def test_create_secret_sdk_failure_detaches_the_complete_credential_json() -> None:
    credentials = bootstrap.XOAuth1Credentials(
        consumer_key=OAUTH1_CONSUMER_KEY,
        consumer_secret=OAUTH1_CONSUMER_SECRET,
        access_token=OAUTH1_ACCESS_TOKEN,
        access_token_secret=OAUTH1_ACCESS_TOKEN_SECRET,
    )
    credential_json = bootstrap._oauth1_credential_json(credentials)

    class _FailingSecrets:
        def create_secret(self, **kwargs: object) -> Mapping[str, object]:
            raise RuntimeError(f"SDK retained {kwargs['SecretString']}")

    with pytest.raises(bootstrap.XOAuthBootstrapError) as captured:
        bootstrap._store_credential(
            _FailingSecrets(),  # type: ignore[arg-type]
            credential_json=credential_json,
            secret_name=bootstrap.AWS_OAUTH1_SECRET_NAME,
            description="OAuth1 test secret",
            purpose="x-oauth1",
        )

    error = captured.value
    surface = _exception_security_surface(error)
    assert credential_json not in surface
    for secret in (
        OAUTH1_CONSUMER_KEY,
        OAUTH1_CONSUMER_SECRET,
        OAUTH1_ACCESS_TOKEN,
        OAUTH1_ACCESS_TOKEN_SECRET,
    ):
        assert secret not in surface


def test_secret_prompt_uses_hidden_input_and_rejects_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []
    values = iter((f" {CLIENT_ID}", "", CLIENT_ID))

    def hidden_prompt(label: str) -> str:
        prompts.append(label)
        return next(values)

    monkeypatch.setattr(bootstrap.getpass, "getpass", hidden_prompt)
    assert bootstrap._secret_prompt("hidden: ", maximum=1_024) == CLIENT_ID
    assert prompts == ["hidden: ", "hidden: ", "hidden: "]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("please try again") == 2


def test_secret_prompt_stops_after_three_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda _label: "")

    with pytest.raises(
        bootstrap.XOAuthBootstrapError,
        match="credential input was not accepted after three attempts",
    ):
        bootstrap._secret_prompt("hidden: ", maximum=1_024)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("please try again") == 2


def test_secret_prompt_fails_closed_instead_of_using_echoed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_reached = False

    def unsafe_prompt(_label: str) -> str:
        nonlocal fallback_reached
        warnings.warn(
            "terminal echo could not be disabled",
            bootstrap.getpass.GetPassWarning,
            stacklevel=2,
        )
        fallback_reached = True
        return CLIENT_SECRET

    monkeypatch.setattr(bootstrap.getpass, "getpass", unsafe_prompt)

    with pytest.raises(bootstrap.XOAuthBootstrapError, match="hidden credential input"):
        bootstrap._secret_prompt("hidden: ", maximum=8_192)
    assert not fallback_reached


def test_successful_cli_stdout_contains_only_arn_and_creator_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _TtyBuffer()
    stdout = _TtyBuffer()
    stderr = _TtyBuffer()
    sts_client = _Sts()
    secrets_client = _Secrets()
    prompt_values = iter((CLIENT_ID, CLIENT_SECRET))
    events: list[str] = []

    class _HttpContext:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    def fake_prompt(label: str, *, maximum: int) -> str:
        del label, maximum
        events.append("prompt")
        return next(prompt_values)

    def fake_bootstrap(**kwargs: Any) -> bootstrap.BootstrapResult:
        assert kwargs["client_id"] == CLIENT_ID
        assert kwargs["client_secret"] == CLIENT_SECRET
        assert kwargs["secrets_client"] is secrets_client
        events.append("oauth")
        return bootstrap.BootstrapResult(SECRET_ARN, CREATOR_ID)

    monkeypatch.setattr(bootstrap.sys, "argv", ["gen-automation-x-auth"])
    monkeypatch.setattr(bootstrap.sys, "stdin", stdin)
    monkeypatch.setattr(bootstrap.sys, "stdout", stdout)
    monkeypatch.setattr(bootstrap.sys, "stderr", stderr)
    monkeypatch.setattr(bootstrap, "_aws_clients", lambda: (sts_client, secrets_client))
    monkeypatch.setattr(bootstrap, "_secret_prompt", fake_prompt)
    monkeypatch.setattr(bootstrap, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(bootstrap.httpx2, "Client", _HttpContext)

    assert bootstrap.x_oauth_bootstrap_main() == 0
    assert stdout.getvalue() == f"{SECRET_ARN}\n{CREATOR_ID}\n"
    assert stderr.getvalue() == ""
    assert CLIENT_ID not in stdout.getvalue()
    assert CLIENT_SECRET not in stdout.getvalue()
    assert events == ["prompt", "prompt", "oauth"]
    assert sts_client.closed
    assert secrets_client.closed


def test_portal_refresh_cli_prompts_three_hidden_values_and_never_prints_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _TtyBuffer()
    stdout = _TtyBuffer()
    stderr = _TtyBuffer()
    sts_client = _Sts()
    secrets_client = _Secrets()
    prompt_values = iter((CLIENT_ID, CLIENT_SECRET, PORTAL_REFRESH_TOKEN))
    prompts: list[str] = []

    class _HttpContext:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    def fake_prompt(label: str, *, maximum: int) -> str:
        del maximum
        prompts.append(label)
        return next(prompt_values)

    def fake_portal_bootstrap(**kwargs: Any) -> bootstrap.BootstrapResult:
        assert kwargs["client_id"] == CLIENT_ID
        assert kwargs["client_secret"] == CLIENT_SECRET
        assert kwargs["portal_refresh_token"] == PORTAL_REFRESH_TOKEN
        assert kwargs["secrets_client"] is secrets_client
        return bootstrap.BootstrapResult(SECRET_ARN, CREATOR_ID)

    monkeypatch.setattr(
        bootstrap.sys,
        "argv",
        ["gen-automation-x-auth", bootstrap.PORTAL_REFRESH_TOKEN_MODE],
    )
    monkeypatch.setattr(bootstrap.sys, "stdin", stdin)
    monkeypatch.setattr(bootstrap.sys, "stdout", stdout)
    monkeypatch.setattr(bootstrap.sys, "stderr", stderr)
    monkeypatch.setattr(bootstrap, "_aws_clients", lambda: (sts_client, secrets_client))
    monkeypatch.setattr(bootstrap, "_secret_prompt", fake_prompt)
    monkeypatch.setattr(bootstrap, "_bootstrap_from_portal_refresh_token", fake_portal_bootstrap)
    monkeypatch.setattr(
        bootstrap,
        "_bootstrap",
        lambda **_kwargs: pytest.fail("PKCE flow must not run in portal mode"),
    )
    monkeypatch.setattr(bootstrap.httpx2, "Client", _HttpContext)

    assert bootstrap.x_oauth_bootstrap_main() == 0
    assert stdout.getvalue() == f"{SECRET_ARN}\n{CREATOR_ID}\n"
    assert stderr.getvalue() == ""
    assert prompts == [
        "X OAuth client ID: ",
        "X OAuth client secret: ",
        "X Developer Portal refresh token: ",
    ]
    combined_output = stdout.getvalue() + stderr.getvalue()
    assert CLIENT_ID not in combined_output
    assert CLIENT_SECRET not in combined_output
    assert PORTAL_REFRESH_TOKEN not in combined_output
    assert sts_client.closed
    assert secrets_client.closed


def test_oauth1_cli_prompts_four_hidden_values_and_never_prints_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _TtyBuffer()
    stdout = _TtyBuffer()
    stderr = _TtyBuffer()
    sts_client = _Sts()
    secrets_client = _Secrets(
        expected_secret_name=bootstrap.AWS_OAUTH1_SECRET_NAME,
        secret_arn=OAUTH1_SECRET_ARN,
    )
    prompt_values = iter(
        (
            OAUTH1_CONSUMER_KEY,
            OAUTH1_CONSUMER_SECRET,
            OAUTH1_ACCESS_TOKEN,
            OAUTH1_ACCESS_TOKEN_SECRET,
        )
    )
    prompts: list[str] = []

    class _HttpContext:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    def fake_prompt(label: str, *, maximum: int) -> str:
        assert maximum == 16_384
        prompts.append(label)
        return next(prompt_values)

    def fake_oauth1_bootstrap(**kwargs: Any) -> bootstrap.BootstrapResult:
        credentials = kwargs["credentials"]
        assert credentials == bootstrap.XOAuth1Credentials(
            consumer_key=OAUTH1_CONSUMER_KEY,
            consumer_secret=OAUTH1_CONSUMER_SECRET,
            access_token=OAUTH1_ACCESS_TOKEN,
            access_token_secret=OAUTH1_ACCESS_TOKEN_SECRET,
        )
        assert kwargs["secrets_client"] is secrets_client
        return bootstrap.BootstrapResult(OAUTH1_SECRET_ARN, CREATOR_ID)

    monkeypatch.setattr(
        bootstrap.sys,
        "argv",
        ["gen-automation-x-auth", bootstrap.OAUTH1_MODE],
    )
    monkeypatch.setattr(bootstrap.sys, "stdin", stdin)
    monkeypatch.setattr(bootstrap.sys, "stdout", stdout)
    monkeypatch.setattr(bootstrap.sys, "stderr", stderr)
    monkeypatch.setattr(bootstrap, "_aws_clients", lambda: (sts_client, secrets_client))
    monkeypatch.setattr(bootstrap, "_secret_prompt", fake_prompt)
    monkeypatch.setattr(bootstrap, "_bootstrap_oauth1", fake_oauth1_bootstrap)
    monkeypatch.setattr(
        bootstrap,
        "_bootstrap",
        lambda **_kwargs: pytest.fail("OAuth2 PKCE flow must not run in OAuth1 mode"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_bootstrap_from_portal_refresh_token",
        lambda **_kwargs: pytest.fail("OAuth2 portal flow must not run in OAuth1 mode"),
    )
    monkeypatch.setattr(bootstrap.httpx2, "Client", _HttpContext)

    assert bootstrap.x_oauth_bootstrap_main() == 0
    assert stdout.getvalue() == f"{OAUTH1_SECRET_ARN}\n{CREATOR_ID}\n"
    assert stderr.getvalue() == ""
    assert prompts == [
        "X OAuth 1.0a Consumer/API Key: ",
        "X OAuth 1.0a Consumer/API Secret: ",
        "X OAuth 1.0a Access Token: ",
        "X OAuth 1.0a Access Token Secret: ",
    ]
    combined_output = stdout.getvalue() + stderr.getvalue()
    for secret in (
        OAUTH1_CONSUMER_KEY,
        OAUTH1_CONSUMER_SECRET,
        OAUTH1_ACCESS_TOKEN,
        OAUTH1_ACCESS_TOKEN_SECRET,
    ):
        assert secret not in combined_output
    assert sts_client.closed
    assert secrets_client.closed


def test_cli_rejects_undocumented_arguments_before_reading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = _TtyBuffer()
    monkeypatch.setattr(bootstrap.sys, "argv", ["gen-automation-x-auth", "value"])
    monkeypatch.setattr(bootstrap.sys, "stderr", stderr)
    monkeypatch.setattr(
        bootstrap,
        "_secret_prompt",
        lambda *_args, **_kwargs: pytest.fail("credential prompt must not run"),
    )

    assert bootstrap.x_oauth_bootstrap_main() == 2
    assert "documented non-secret mode flag" in stderr.getvalue()


def test_cli_rejects_combined_oauth_modes_before_reading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = _TtyBuffer()
    monkeypatch.setattr(
        bootstrap.sys,
        "argv",
        [
            "gen-automation-x-auth",
            bootstrap.OAUTH1_MODE,
            bootstrap.PORTAL_REFRESH_TOKEN_MODE,
        ],
    )
    monkeypatch.setattr(bootstrap.sys, "stderr", stderr)
    monkeypatch.setattr(
        bootstrap,
        "_secret_prompt",
        lambda *_args, **_kwargs: pytest.fail("credential prompt must not run"),
    )

    assert bootstrap.x_oauth_bootstrap_main() == 2
    assert "documented non-secret mode flag" in stderr.getvalue()


def test_aws_clients_ignore_endpoint_and_proxy_overrides_and_require_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls: list[tuple[str, dict[str, object]]] = []
    clients = {"sts": _Sts(), "secretsmanager": _Secrets()}

    class _Session:
        def __init__(self, *, profile_name: str, region_name: str) -> None:
            assert profile_name == bootstrap.AWS_PROFILE
            assert region_name == bootstrap.AWS_REGION

        def client(self, service_name: str, **kwargs: object) -> object:
            client_calls.append((service_name, dict(kwargs)))
            return clients[service_name]

    monkeypatch.setattr(bootstrap.boto3, "Session", _Session)

    assert bootstrap._aws_clients() == (clients["sts"], clients["secretsmanager"])
    assert [name for name, _kwargs in client_calls] == ["sts", "secretsmanager"]
    for _name, kwargs in client_calls:
        assert kwargs["region_name"] == bootstrap.AWS_REGION
        assert kwargs["verify"] is True
        config = kwargs["config"]
        assert config.ignore_configured_endpoint_urls is True
        assert config.proxies == {}


def test_windows_wrapper_passes_no_credentials_on_the_process_command_line() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "bootstrap-x-oauth.ps1").read_text(
        encoding="utf-8"
    )

    assert "[object[]]$RemainingArguments = @()" in source
    assert "$RemainingArguments.Count -ne 0" in source
    assert "$args" not in source
    assert "[switch]$PortalRefreshToken" in source
    assert "[switch]$OAuth1" in source
    assert "($PortalRefreshToken -and $OAuth1)" in source
    assert "--portal-refresh-token" in source
    assert "--oauth1" in source
    assert "Push-Location -LiteralPath $repositoryRoot" in source
    assert "finally" in source
    assert "Pop-Location" in source
    assert "-I -m gen_automation.x_oauth_bootstrap_cli" in source
    assert source.index("Push-Location") < source.index("-I -m") < source.index("Pop-Location")
    assert "client_id" not in source.casefold()
    assert "client_secret" not in source.casefold()
    assert "consumer_secret" not in source.casefold()
    assert "access_token_secret" not in source.casefold()
