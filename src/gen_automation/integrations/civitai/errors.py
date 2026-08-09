"""Safe errors for the Civitai metadata and download adapter."""


class CivitaiError(Exception):
    """Base error. Messages never include credentials or signed URLs."""


class CivitaiURLValidationError(CivitaiError, ValueError):
    """A user-supplied Civitai URL is not a supported canonical URL."""


class CivitaiTransportError(CivitaiError):
    """The provider request failed before a usable response arrived."""


class CivitaiProtocolError(CivitaiError):
    """Civitai returned malformed, unsafe, or unsupported metadata."""


class CivitaiAPIError(CivitaiError):
    """Civitai returned an unsuccessful HTTP status."""

    def __init__(self, *, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Civitai returned HTTP {status_code}: {message}")


class CivitaiRateLimitError(CivitaiAPIError):
    """Civitai rejected a request because its request budget was exhausted."""

    def __init__(self, *, message: str, retry_after_seconds: float | None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(status_code=429, message=message)


class CivitaiDownloadError(CivitaiError):
    """A bounded Civitai download failed validation or transfer."""


class CivitaiSourceSelectionError(CivitaiError):
    """No single safe LoRA file can be selected from the supplied source."""
