class SaladCloudError(Exception):
    """Base error for SaladCloud adapter failures."""


class SaladTransportError(SaladCloudError):
    """The HTTP request failed before SaladCloud returned a response."""


class SaladTimeoutError(SaladTransportError):
    """The SaladCloud request exceeded its configured timeout."""


class SaladProtocolError(SaladCloudError):
    """SaladCloud returned a response that did not match its documented contract."""


class SaladAPIError(SaladCloudError):
    """SaladCloud returned a non-success HTTP status."""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        response_body: str,
        request_id: str | None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        self.request_id = request_id
        super().__init__(f"SaladCloud API returned HTTP {status_code}: {message}")


class SaladRateLimitError(SaladAPIError):
    """The per-API-key SaladCloud request budget was exhausted."""

    def __init__(
        self,
        *,
        message: str,
        response_body: str,
        request_id: str | None,
        retry_after_seconds: float | None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            status_code=429,
            message=message,
            response_body=response_body,
            request_id=request_id,
        )
