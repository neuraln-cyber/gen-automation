import json

import pytest

from gen_automation.gpu_worker import comfy
from gen_automation.gpu_worker.diagnostics import log_generation_failure


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (comfy.ComfyExecutionTimeoutError, "comfy_execution_timeout"),
        (comfy.ComfyExecutionError, "comfy_execution_failed"),
        (comfy.ComfyOutputError, "comfy_output_invalid"),
        (comfy.ComfyProtocolError, "comfy_protocol_invalid"),
        (comfy.ComfyUnavailableError, "comfy_unavailable"),
        (comfy.ComfyConfigurationError, "comfy_configuration_invalid"),
        (RuntimeError, "unexpected_error"),
    ],
)
def test_executor_diagnostic_never_logs_exception_text_or_attempt_identifier(
    error_type: type[Exception],
    category: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_generation_failure(
        phase="execute",
        attempt_id="private-attempt",
        output_index=3,
        uploaded_count=3,
        error=error_type("secret-url-prompt"),
    )
    output = capsys.readouterr().out
    assert json.loads(output)["category"] == category
    assert "private-attempt" not in output and "secret-url-prompt" not in output


def test_diagnostic_io_failure_does_not_change_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_print(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError("private message")

    monkeypatch.setattr("builtins.print", broken_print)
    log_generation_failure(
        phase="execute",
        attempt_id="id",
        output_index=None,
        uploaded_count=0,
        error=RuntimeError("private"),
    )
