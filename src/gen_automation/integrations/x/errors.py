class XError(Exception):
    """Base error for the isolated X API transport."""


class XRetryableError(XError):
    """A failure for which a caller may schedule a deliberate retry."""


class XTerminalError(XError):
    """A failure that should not be retried without changing the request."""


class XAmbiguousError(XError):
    """A failure where X may have accepted the mutation."""


class XProtocolError(XTerminalError):
    """X returned a success response that did not match the documented contract."""


class XAPIError(XError):
    """X returned a non-success HTTP status."""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        response_body: str,
        request_id: str | None,
        retry_after_seconds: float | None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"X API returned HTTP {status_code}: {message}")


class XRetryableAPIError(XAPIError, XRetryableError):
    """X rejected the request with a transient HTTP response."""


class XTerminalAPIError(XAPIError, XTerminalError):
    """X rejected the request with a terminal HTTP response."""


class XRetryableTransportError(XRetryableError):
    """The request failed before an HTTP mutation could be sent."""


class XAmbiguousTimeoutError(XAmbiguousError):
    """A timeout occurred after mutation bytes may have been sent."""


class XAmbiguousTransportError(XAmbiguousError):
    """A transport failure occurred after mutation bytes may have been sent."""
