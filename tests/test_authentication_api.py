import asyncio
import base64
import hashlib
import hmac
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.app import create_app
from gen_automation.auth.security import (
    PasswordManager,
    TotpSecretCipher,
    generate_totp_secret,
)
from gen_automation.config import Environment, Settings
from gen_automation.db.models import AdminSession, AdminUser
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole

SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
PASSWORD = "a long unique API test password"  # noqa: S105
ORIGIN = "http://testserver"


def _settings(
    database_path: Path,
    *,
    auto_create_schema: bool = False,
    trusted_proxy_cidrs: tuple[str, ...] = (),
) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=auto_create_schema,
        auth_enabled=True,
        auth_require_totp=True,
        session_secret=SESSION_KEY,
        auth_totp_active_key_id="key-1",
        auth_totp_encryption_keys={"key-1": TOTP_KEY},
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )


def _password_manager() -> PasswordManager:
    return PasswordManager(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


def _totp_code(secret: str, unix_time: int) -> str:
    counter = unix_time // 30
    digest = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


async def _prepare_owner(settings: Settings) -> str:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        secret = generate_totp_secret()
        now = datetime.now(UTC)
        async with database.sessions() as session:
            user = AdminUser(
                username_normalized="owner@example.test",
                display_name="Owner",
                password_hash=_password_manager().hash(PASSWORD),
                role=AdminRole.OWNER,
                is_active=True,
                failed_login_count=0,
                password_changed_at=now,
                credential_version=1,
                lock_version=1,
            )
            session.add(user)
            await session.flush()
            cipher = TotpSecretCipher({"key-1": TOTP_KEY}, active_key_id="key-1")
            user.totp_secret_ciphertext = cipher.encrypt(
                secret,
                subject=f"admin-user:{user.id}",
            )
            user.totp_confirmed_at = now
            await session.commit()
        return secret
    finally:
        await database.dispose()


async def _make_current_totp_counter_available(settings: Settings) -> None:
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            user = await session.scalar(select(AdminUser))
            assert user is not None
            user.last_totp_counter = int(datetime.now(UTC).timestamp()) // 30 - 1
            await session.commit()
    finally:
        await database.dispose()


async def _latest_client_context_hmac(settings: Settings) -> str:
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            value = await session.scalar(
                select(AdminSession.client_context_hmac).order_by(AdminSession.created_at.desc())
            )
            assert value is not None
            return value
    finally:
        await database.dispose()


def test_authenticated_browser_session_csrf_and_logout(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "authenticated.db")
    secret = asyncio.run(_prepare_owner(settings))

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.10", 50000),
    ) as client:
        unauthenticated = client.post(
            "/api/v1/projects",
            json={"slug": "blocked", "name": "Blocked"},
            headers={"Origin": ORIGIN},
        )
        assert unauthenticated.status_code == 401

        missing_origin = client.post(
            "/api/v1/auth/login",
            json={
                "username": "owner@example.test",
                "password": PASSWORD,
                "totp_code": _totp_code(secret, int(datetime.now(UTC).timestamp())),
            },
        )
        assert missing_origin.status_code == 403

        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": " OWNER@EXAMPLE.TEST ",
                "password": PASSWORD,
                "totp_code": _totp_code(secret, int(datetime.now(UTC).timestamp())),
            },
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert login.status_code == 200
        assert login.json()["role"] == AdminRole.OWNER.value
        cookies = login.headers.get_list("set-cookie")
        assert any("gen_session=" in value and "HttpOnly" in value for value in cookies)
        assert any("gen_csrf=" in value and "HttpOnly" not in value for value in cookies)
        assert all("SameSite=strict" in value for value in cookies)

        session_response = client.get("/api/v1/auth/session")
        assert session_response.status_code == 200
        assert session_response.json()["username"] == "owner@example.test"

        no_csrf = client.post(
            "/api/v1/projects",
            json={"slug": "blocked", "name": "Blocked"},
            headers={"Origin": ORIGIN},
        )
        assert no_csrf.status_code == 403

        csrf_token = client.cookies.get(settings.auth_csrf_cookie_name)
        assert csrf_token is not None
        missing_reauth_csrf = client.post(
            "/api/v1/auth/reauthenticate",
            json={
                "password": PASSWORD,
                "totp_code": _totp_code(secret, int(datetime.now(UTC).timestamp())),
            },
            headers={"Origin": ORIGIN},
        )
        assert missing_reauth_csrf.status_code == 403
        asyncio.run(_make_current_totp_counter_available(settings))
        reauthenticated = client.post(
            "/api/v1/auth/reauthenticate",
            json={
                "password": PASSWORD,
                "totp_code": _totp_code(secret, int(datetime.now(UTC).timestamp())),
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
        )
        assert reauthenticated.status_code == 200
        assert reauthenticated.json()["mfa_verified"] is True

        created = client.post(
            "/api/v1/projects",
            json={"slug": "main", "name": "Main"},
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": csrf_token,
            },
        )
        assert created.status_code == 201

        wrong_origin = client.post(
            "/api/v1/projects",
            json={"slug": "cross-site", "name": "Cross Site"},
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": csrf_token,
            },
        )
        assert wrong_origin.status_code == 403

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 204
        assert client.get("/api/v1/auth/session").status_code == 401


def test_login_uses_bounded_trusted_proxy_client_ip(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "trusted-proxy.db",
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    secret = asyncio.run(_prepare_owner(settings))
    body = {
        "username": "owner@example.test",
        "password": PASSWORD,
        "totp_code": _totp_code(secret, int(datetime.now(UTC).timestamp())),
    }

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("10.8.0.3", 50000),
    ) as client:
        malformed = client.post(
            "/api/v1/auth/login",
            json=body,
            headers={
                "Origin": ORIGIN,
                "X-Forwarded-For": "198.51.100.44,,10.9.0.2",
            },
        )
        assert malformed.status_code == 400

        login = client.post(
            "/api/v1/auth/login",
            json=body,
            headers={
                "Origin": ORIGIN,
                "X-Forwarded-For": "198.51.100.44, 10.9.0.2",
            },
        )
        assert login.status_code == 200

    expected = hmac.new(
        SESSION_KEY.encode("ascii"),
        b"client\0" + b"198.51.100.44",
        hashlib.sha256,
    ).hexdigest()
    assert asyncio.run(_latest_client_context_hmac(settings)) == expected


def test_browser_login_form_redirects_with_existing_cookie_contract_and_opens_dashboard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path / "browser-login.db")
    secret = asyncio.run(_prepare_owner(settings))
    client_ip = "192.0.2.71"

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=(client_ip, 50000),
    ) as client:
        form = client.get("/login")
        assert form.status_code == 200
        assert form.headers["content-type"].startswith("text/html")
        assert "no-store" in form.headers["cache-control"]
        assert "script-src 'none'" in form.headers["content-security-policy"]
        assert '<form method="post" action="/login"' in form.text
        assert 'name="username"' in form.text
        assert 'autocomplete="username"' in form.text
        assert 'type="password"' in form.text
        assert 'autocomplete="current-password"' in form.text
        assert 'name="totp_code"' in form.text
        assert 'autocomplete="one-time-code"' in form.text
        assert 'inputmode="numeric"' in form.text
        assert 'pattern="[0-9]{6}"' in form.text
        assert "<script" not in form.text
        assert "https://" not in form.text
        assert f'value="{PASSWORD}"' not in form.text

        code = _totp_code(secret, int(datetime.now(UTC).timestamp()))
        login = client.post(
            "/login",
            data={
                "username": "owner@example.test",
                "password": PASSWORD,
                "totp_code": code,
            },
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        assert login.headers["location"] == "/dashboard"
        assert login.request.url.path == "/login"
        assert login.request.url.query == b""
        cookies = login.headers.get_list("set-cookie")
        assert any(
            "gen_session=" in value and "HttpOnly" in value and "Path=/" in value
            for value in cookies
        )
        assert any(
            "gen_csrf=" in value and "HttpOnly" not in value and "Path=/" in value
            for value in cookies
        )
        assert all("SameSite=strict" in value for value in cookies)
        session_token = client.cookies.get(settings.auth_session_cookie_name)
        csrf_token = client.cookies.get(settings.auth_csrf_cookie_name)
        assert session_token is not None
        assert csrf_token is not None
        assert session_token not in login.text
        assert csrf_token not in login.text
        assert PASSWORD not in login.text
        assert code not in login.text

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "Ranked releases" in dashboard.text

    expected_context = hmac.new(
        SESSION_KEY.encode("ascii"),
        b"client\0" + client_ip.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    assert asyncio.run(_latest_client_context_hmac(settings)) == expected_context
    captured = capsys.readouterr()
    captured_output = captured.out + captured.err
    assert PASSWORD not in captured_output
    assert code not in captured_output
    assert session_token not in captured_output
    assert csrf_token not in captured_output


def test_browser_login_failure_is_generic_and_never_reflects_credentials(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "browser-failure.db")
    secret = asyncio.run(_prepare_owner(settings))
    attempted_username = "owner@example.test"
    attempted_password = "a private incorrect browser password"  # noqa: S105
    attempted_totp = _totp_code(secret, int(datetime.now(UTC).timestamp()))

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.72", 50000),
    ) as client:
        failed = client.post(
            "/login",
            data={
                "username": attempted_username,
                "password": attempted_password,
                "totp_code": attempted_totp,
            },
            headers={"Origin": ORIGIN},
        )
        malformed = client.post(
            "/login",
            data={
                "username": attempted_username,
                "password": attempted_password,
                "totp_code": "12345",
            },
            headers={"Origin": ORIGIN},
        )

    assert failed.status_code == 401
    assert malformed.status_code == 401
    assert failed.text == malformed.text
    assert "Sign-in failed" in failed.text
    assert attempted_username not in failed.text
    assert attempted_password not in failed.text
    assert attempted_totp not in failed.text
    assert "set-cookie" not in failed.headers
    assert "location" not in failed.headers
    assert "no-store" in failed.headers["cache-control"]
    assert "script-src 'none'" in failed.headers["content-security-policy"]


def test_browser_login_enforces_origin_form_content_type_and_client_ip(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path / "browser-boundary.db",
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    secret = asyncio.run(_prepare_owner(settings))
    body = {
        "username": "owner@example.test",
        "password": PASSWORD,
        "totp_code": _totp_code(secret, int(datetime.now(UTC).timestamp())),
    }

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("10.8.0.3", 50000),
    ) as client:
        missing_origin = client.post("/login", data=body)
        wrong_type = client.post(
            "/login",
            json=body,
            headers={"Origin": ORIGIN},
        )
        malformed_forwarding = client.post(
            "/login",
            data=body,
            headers={
                "Origin": ORIGIN,
                "X-Forwarded-For": "198.51.100.44,,10.9.0.2",
            },
        )

    assert missing_origin.status_code == 403
    assert wrong_type.status_code == 415
    assert wrong_type.headers["content-type"].startswith("text/html")
    assert "Sign-in failed" in wrong_type.text
    assert PASSWORD not in wrong_type.text
    assert malformed_forwarding.status_code == 400
    assert malformed_forwarding.json() == {"detail": "client address chain is invalid"}


def test_browser_login_is_not_exposed_when_authentication_is_disabled(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        public_base_url=ORIGIN,
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / 'browser-disabled.db').as_posix()}"),
        auto_create_schema=True,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
        session_secret=SESSION_KEY,
    )
    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.73", 50000),
    ) as client:
        get_response = client.get("/login")
        post_response = client.post(
            "/login",
            data={
                "username": "must-not-be-accepted",
                "password": "must-not-be-accepted",
                "totp_code": "000000",
            },
            headers={"Origin": ORIGIN},
        )

    assert get_response.status_code == 404
    assert post_response.status_code == 404
    assert "must-not-be-accepted" not in get_response.text
    assert "must-not-be-accepted" not in post_response.text


def test_auth_enabled_startup_requires_enrolled_owner(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "missing-owner.db",
        auto_create_schema=True,
    )
    with pytest.raises(RuntimeError, match=r"gen_automation\.cli"):
        with TestClient(create_app(settings), base_url=ORIGIN):
            pass


@pytest.mark.parametrize(
    "credential_failure",
    ["ciphertext", "argon-cost", "future-totp-counter"],
)
def test_auth_startup_rejects_owner_with_unusable_credentials(
    tmp_path: Path,
    credential_failure: str,
) -> None:
    settings = _settings(tmp_path / f"unusable-{credential_failure}.db")
    asyncio.run(_prepare_owner(settings))

    async def corrupt_credentials() -> None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                user = await session.scalar(select(AdminUser))
                assert user is not None
                if credential_failure == "ciphertext":
                    user.totp_secret_ciphertext = "v1.key-1.invalid"  # noqa: S105
                elif credential_failure == "argon-cost":
                    user.password_hash = user.password_hash.replace(
                        "m=8192",
                        "m=1048576",
                        1,
                    )
                else:
                    user.last_totp_counter = int(datetime.now(UTC).timestamp()) // 30 + 100
                await session.commit()
        finally:
            await database.dispose()

    asyncio.run(corrupt_credentials())

    with pytest.raises(RuntimeError, match="recover-owner"):
        with TestClient(create_app(settings), base_url=ORIGIN):
            pass
