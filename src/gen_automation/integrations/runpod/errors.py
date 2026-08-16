"""Redacted failures raised by the RunPod I2V adapter."""


class RunPodError(Exception):
    """Base RunPod integration error."""


class RunPodTransportError(RunPodError):
    """The provider outcome could not be determined."""


class RunPodTimeoutError(RunPodTransportError):
    """A bounded provider request timed out."""


class RunPodProtocolError(RunPodError):
    """RunPod returned data outside its documented contract."""


class RunPodAPIError(RunPodError):
    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class RunPodRateLimitError(RunPodAPIError):
    def __init__(self, *, message: str, retry_after_seconds: float | None) -> None:
        super().__init__(status_code=429, message=message)
        self.retry_after_seconds = retry_after_seconds
