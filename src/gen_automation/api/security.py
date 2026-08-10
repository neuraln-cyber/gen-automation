from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Environment, Settings, TrustedProxyNetwork
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.services.authentication import (
    AuthenticatedPrincipal,
    AuthenticationService,
    CsrfValidationError,
    RecentAuthenticationRequiredError,
    SessionUnauthorizedError,
)


class Permission(StrEnum):
    READ = "read"
    VIEW_RAW_MASTERS = "view_raw_masters"
    MANAGE_RELEASES = "manage_releases"
    MANAGE_COMPLIANCE = "manage_compliance"
    REVIEW = "review"
    PUBLISH = "publish"
    MANAGE_USERS = "manage_users"


_ROLE_PERMISSIONS: dict[AdminRole, frozenset[Permission]] = {
    AdminRole.OWNER: frozenset(Permission),
    AdminRole.ADMIN: frozenset(
        {
            Permission.READ,
            Permission.MANAGE_RELEASES,
            Permission.MANAGE_COMPLIANCE,
        }
    ),
    AdminRole.REVIEWER: frozenset(
        {
            Permission.READ,
            Permission.VIEW_RAW_MASTERS,
            Permission.REVIEW,
        }
    ),
    AdminRole.PUBLISHER: frozenset({Permission.READ, Permission.PUBLISH}),
}

Session = Annotated[AsyncSession, Depends(get_session)]
CsrfHeader = Annotated[
    str | None,
    Header(
        alias="X-CSRF-Token",
        min_length=1,
        max_length=100,
    ),
]
type ClientIPAddress = IPv4Address | IPv6Address

_MAX_FORWARDED_FOR_BYTES = 4096
_MAX_FORWARDED_FOR_HOPS = 32


class ClientAddressResolutionError(Exception):
    """The socket peer or a trusted proxy's forwarding chain is malformed."""


def _parse_client_ip(value: str) -> ClientIPAddress:
    if (
        not value
        or value != value.strip()
        or len(value) > 64
        or "%" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ClientAddressResolutionError("client address is invalid")
    try:
        address = ip_address(value)
    except ValueError:
        raise ClientAddressResolutionError("client address is invalid") from None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_trusted_proxy(
    address: ClientIPAddress,
    networks: tuple[TrustedProxyNetwork, ...],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def resolve_client_ip(request: Request) -> str:
    """Resolve a login client IP without trusting attacker-supplied forwarding data."""

    if request.client is None:
        raise ClientAddressResolutionError("client address is unavailable")
    direct_address = _parse_client_ip(request.client.host)
    settings: Settings = request.app.state.settings
    try:
        trusted_networks = settings.trusted_proxy_networks
    except ValueError:
        raise ClientAddressResolutionError("trusted proxy configuration is invalid") from None
    if not _is_trusted_proxy(direct_address, trusted_networks):
        return str(direct_address)

    forwarded_values = request.headers.getlist("x-forwarded-for")
    if not forwarded_values:
        raise ClientAddressResolutionError("forwarded client chain is required")
    if (
        len(forwarded_values) > _MAX_FORWARDED_FOR_HOPS
        or sum(len(value) for value in forwarded_values) + max(0, len(forwarded_values) - 1)
        > _MAX_FORWARDED_FOR_BYTES
    ):
        raise ClientAddressResolutionError("forwarded client chain is invalid")

    forwarded_addresses: list[ClientIPAddress] = []
    for value in forwarded_values:
        for item in value.split(","):
            if len(forwarded_addresses) >= _MAX_FORWARDED_FOR_HOPS:
                raise ClientAddressResolutionError("forwarded client chain is invalid")
            forwarded_addresses.append(_parse_client_ip(item.strip()))
    if not forwarded_addresses:
        raise ClientAddressResolutionError("forwarded client chain is invalid")

    for address in reversed(forwarded_addresses):
        if not _is_trusted_proxy(address, trusted_networks):
            return str(address)
    return str(forwarded_addresses[0])


def authentication_service(request: Request) -> AuthenticationService:
    settings: Settings = request.app.state.settings
    service: AuthenticationService | None = request.app.state.authentication_service
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="authentication is disabled",
        )
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service is unavailable",
        )
    return service


def require_same_origin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    expected_url = urlsplit(str(settings.public_base_url))
    expected_origin = f"{expected_url.scheme}://{expected_url.netloc}"
    supplied_origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if supplied_origin != expected_origin or (
        fetch_site is not None and fetch_site not in {"same-origin", "none"}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="request origin is not allowed",
        )


def _development_principal() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=UUID(int=0),
        user_id=UUID(int=0),
        username="local-developer",
        display_name="Local Developer",
        role=AdminRole.OWNER,
        csrf_sha256="0" * 64,
        expires_at=now + timedelta(days=1),
        idle_expires_at=now + timedelta(days=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


async def require_authenticated_principal(
    request: Request,
    session: Session,
) -> AuthenticatedPrincipal:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        if not settings.auth_development_bypass_enabled or settings.environment not in {
            Environment.LOCAL,
            Environment.TEST,
        }:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication is required",
            )
        return _development_principal()
    service = authentication_service(request)
    session_token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        return await service.resolve_session(
            session,
            session_token=session_token,
        )
    except SessionUnauthorizedError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        ) from error


CurrentPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_authenticated_principal),
]


def _require_permission(
    principal: AuthenticatedPrincipal,
    permission: Permission,
) -> None:
    if permission not in _ROLE_PERMISSIONS[principal.role]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="permission denied",
        )


async def require_release_reader(
    principal: CurrentPrincipal,
) -> AuthenticatedPrincipal:
    _require_permission(principal, Permission.READ)
    return principal


async def require_raw_master_reader(
    principal: CurrentPrincipal,
) -> AuthenticatedPrincipal:
    _require_permission(principal, Permission.VIEW_RAW_MASTERS)
    return principal


async def require_review_reader(
    principal: CurrentPrincipal,
) -> AuthenticatedPrincipal:
    _require_permission(principal, Permission.REVIEW)
    return principal


async def require_compliance_reader(
    principal: CurrentPrincipal,
) -> AuthenticatedPrincipal:
    _require_permission(principal, Permission.MANAGE_COMPLIANCE)
    return principal


async def require_publication_reader(
    principal: CurrentPrincipal,
) -> AuthenticatedPrincipal:
    _require_permission(principal, Permission.PUBLISH)
    return principal


async def require_publication_mutation_principal(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    """Require an authenticated publisher and CSRF proof for a mutation."""

    principal = await require_authenticated_principal(request, session)
    _require_permission(principal, Permission.PUBLISH)
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        require_same_origin(request)
        service = authentication_service(request)
        try:
            service.validate_csrf(
                principal,
                cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
                header_token=csrf_header,
            )
        except CsrfValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            ) from error
    return principal


async def require_publication_principal(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    """Require a recently authenticated owner/publisher for an external effect."""

    principal = await require_publication_mutation_principal(
        request,
        session,
        csrf_header,
    )
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        service = authentication_service(request)
        try:
            service.require_recent_authentication(principal)
        except RecentAuthenticationRequiredError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="recent authentication required",
            ) from error
    return principal


async def require_publication_mutation_owner(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    principal = await require_publication_mutation_principal(
        request,
        session,
        csrf_header,
    )
    if principal.role != AdminRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner role required",
        )
    return principal


async def require_publication_owner(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    principal = await require_publication_principal(
        request,
        session,
        csrf_header,
    )
    if principal.role != AdminRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner role required",
        )
    return principal


async def require_review_principal(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    principal = await require_authenticated_principal(request, session)
    _require_permission(principal, Permission.REVIEW)
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        require_same_origin(request)
        service = authentication_service(request)
        try:
            service.validate_csrf(
                principal,
                cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
                header_token=csrf_header,
            )
        except CsrfValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            ) from error
    return principal


async def require_release_manager(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    principal = await require_authenticated_principal(request, session)
    _require_permission(principal, Permission.MANAGE_RELEASES)
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        require_same_origin(request)
        service = authentication_service(request)
        try:
            service.validate_csrf(
                principal,
                cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
                header_token=csrf_header,
            )
        except CsrfValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            ) from error
    return principal


async def require_recent_principal(
    request: Request,
    principal: CurrentPrincipal,
) -> AuthenticatedPrincipal:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return principal
    service = authentication_service(request)
    try:
        service.require_recent_authentication(principal)
    except RecentAuthenticationRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="recent authentication required",
        ) from error
    return principal


async def require_user_manager(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    principal = await require_authenticated_principal(request, session)
    _require_permission(principal, Permission.MANAGE_USERS)
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        require_same_origin(request)
        service = authentication_service(request)
        try:
            service.validate_csrf(
                principal,
                cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
                header_token=csrf_header,
            )
        except CsrfValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            ) from error
        try:
            service.require_recent_authentication(principal)
        except RecentAuthenticationRequiredError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="recent authentication required",
            ) from error
    return principal


async def require_compliance_mutation_principal(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    """Require an authenticated compliance administrator and CSRF proof."""

    principal = await require_authenticated_principal(request, session)
    _require_permission(principal, Permission.MANAGE_COMPLIANCE)
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        require_same_origin(request)
        service = authentication_service(request)
        try:
            service.validate_csrf(
                principal,
                cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
                header_token=csrf_header,
            )
        except CsrfValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            ) from error
    return principal


async def require_compliance_manager(
    request: Request,
    session: Session,
    csrf_header: CsrfHeader = None,
) -> AuthenticatedPrincipal:
    """Require a recently authenticated compliance administrator for mutations."""

    principal = await require_compliance_mutation_principal(
        request,
        session,
        csrf_header,
    )
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        service = authentication_service(request)
        try:
            service.require_recent_authentication(principal)
        except RecentAuthenticationRequiredError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="recent authentication required",
            ) from error
    return principal


ReleaseReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_release_reader),
]
RawMasterReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_raw_master_reader),
]
ReviewReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_review_reader),
]
ComplianceReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_compliance_reader),
]
PublicationReader = Annotated[
    AuthenticatedPrincipal,
    Depends(require_publication_reader),
]
ReviewPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_review_principal),
]
ReleaseManager = Annotated[
    AuthenticatedPrincipal,
    Depends(require_release_manager),
]
RecentPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_recent_principal),
]
UserManager = Annotated[
    AuthenticatedPrincipal,
    Depends(require_user_manager),
]
ComplianceManager = Annotated[
    AuthenticatedPrincipal,
    Depends(require_compliance_manager),
]
ComplianceMutationPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_compliance_mutation_principal),
]
PublicationMutationPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_publication_mutation_principal),
]
PublicationMutationOwner = Annotated[
    AuthenticatedPrincipal,
    Depends(require_publication_mutation_owner),
]
PublicationPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_publication_principal),
]
PublicationOwner = Annotated[
    AuthenticatedPrincipal,
    Depends(require_publication_owner),
]
