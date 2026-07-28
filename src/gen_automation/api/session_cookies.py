from fastapi import Response

from gen_automation.config import Environment, Settings


def _secure_cookies(settings: Settings) -> bool:
    return settings.environment in {Environment.STAGING, Environment.PRODUCTION}


def set_session_cookies(
    response: Response,
    *,
    settings: Settings,
    session_token: str,
    csrf_token: str,
) -> None:
    secure = _secure_cookies(settings)
    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=session_token,
        max_age=settings.auth_session_absolute_seconds,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        key=settings.auth_csrf_cookie_name,
        value=csrf_token,
        max_age=settings.auth_session_absolute_seconds,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    secure = _secure_cookies(settings)
    response.delete_cookie(
        settings.auth_session_cookie_name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        settings.auth_csrf_cookie_name,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )
