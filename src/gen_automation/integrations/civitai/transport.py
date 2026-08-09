"""Network guards for Civitai downloads and their storage redirects."""

import ipaddress
import socket
import ssl
from collections.abc import Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

import anyio
import httpcore2
import httpx2

from gen_automation.integrations.civitai.errors import (
    CivitaiTransportError,
    CivitaiURLValidationError,
)

type SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)

DEFAULT_DOWNLOAD_HOST_SUFFIXES = (
    "civitai.com",
    "r2.cloudflarestorage.com",
    "backblazeb2.com",
    "b2api.com",
    "amazonaws.com",
)


def is_public_ip(value: str) -> bool:
    """Return true only for globally routed unicast addresses."""

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_global


def validate_public_addresses(values: Iterable[str]) -> tuple[str, ...]:
    """Reject empty or mixed public/private DNS answers, not just the chosen address."""

    addresses = tuple(dict.fromkeys(values))
    if not addresses or any(not is_public_ip(address) for address in addresses):
        raise CivitaiTransportError("download destination resolved to a blocked address")
    return addresses


def is_civitai_credential_host(hostname: str) -> bool:
    hostname = hostname.rstrip(".").casefold()
    # The API and authenticated download entry point are both on this exact
    # host. CDN/storage subdomains receive only credential-free redirect URLs.
    return hostname == "civitai.com"


def sanitize_download_destination(
    value: str,
    *,
    allowed_host_suffixes: tuple[str, ...] = DEFAULT_DOWNLOAD_HOST_SUFFIXES,
) -> str:
    """Validate a redirect without ever interpolating it into an exception."""

    if not isinstance(value, str) or not value or len(value) > 8_192 or "\\" in value:
        raise CivitaiURLValidationError("download redirect is malformed")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname.rstrip(".").casefold() if parsed.hostname else None
        port = parsed.port
    except ValueError:
        raise CivitaiURLValidationError("download redirect is malformed") from None
    if (
        parsed.scheme.casefold() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise CivitaiURLValidationError("download redirect must use credential-free HTTPS")
    if not any(
        hostname == suffix.casefold() or hostname.endswith(f".{suffix.casefold()}")
        for suffix in allowed_host_suffixes
    ):
        raise CivitaiURLValidationError("download redirect host is not allowlisted")
    sanitized = SplitResult(
        scheme="https",
        netloc=hostname,
        path=parsed.path or "/",
        query=parsed.query,
        fragment="",
    )
    return urlunsplit(sanitized)


class PublicOnlyNetworkBackend(httpcore2.AsyncNetworkBackend):
    """Pin each connection to a validated DNS answer to close DNS-rebinding gaps."""

    def __init__(self, backend: httpcore2.AsyncNetworkBackend | None = None) -> None:
        self._backend = backend or httpcore2.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 -- required httpcore override
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        try:
            with anyio.fail_after(timeout if timeout is not None else 10.0):
                records = await anyio.getaddrinfo(
                    host,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
        except (OSError, TimeoutError) as error:
            raise CivitaiTransportError("download destination DNS lookup failed") from error
        addresses = validate_public_addresses(str(record[4][0]) for record in records)
        try:
            return await self._backend.connect_tcp(
                addresses[0],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        except Exception:
            raise CivitaiTransportError("download destination connection failed") from None

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 -- required httpcore override
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        del path, timeout, socket_options
        raise CivitaiTransportError("Unix sockets are disabled for Civitai requests")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PublicOnlyAsyncTransport(httpx2.AsyncHTTPTransport):
    """HTTP transport with no environment proxy and a public-IP-only dialer."""

    def __init__(self) -> None:
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PublicOnlyNetworkBackend(),
        )
