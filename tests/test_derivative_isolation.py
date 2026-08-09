import asyncio
import base64
import sys
import time
from io import BytesIO

import pytest
from PIL import Image

from gen_automation.services import derivative_isolation
from gen_automation.services.derivative_isolation import (
    DerivativeIsolationCrashError,
    DerivativeIsolationPolicy,
    DerivativeIsolationProtocolError,
    DerivativeIsolationTimeoutError,
    DerivativeIsolationUnavailableError,
    _apply_linux_hard_limits,
    _bounded_base64_field,
    _cleanup_process,
    _decode_child_response,
    _error_message,
    _receive_child_message,
    _success_message,
    render_platform_derivatives_isolated,
)
from gen_automation.services.derivatives import (
    DEFAULT_DERIVATIVE_LIMITS,
    PREVIOUS_DERIVATIVE_RENDERER_VERSION,
    DerivativeRecipe,
    DerivativeTarget,
    XStaticImagePngTooLargeError,
    render_platform_derivatives,
)


def _master() -> bytes:
    image = Image.new("RGB", (16, 12), (20, 40, 80))
    output = BytesIO()
    try:
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def _large_master() -> bytes:
    width = 128
    height = 128
    pixels = bytes((index * 73 + index // 11) % 256 for index in range(width * height * 3))
    image = Image.frombytes("RGB", (width, height), pixels)
    output = BytesIO()
    try:
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


class _Process:
    def __init__(self, *, alive: bool, exitcode: int | None = None) -> None:
        self.alive = alive
        self.exitcode = exitcode
        self.started = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_calls = 0
        self.closed = False
        self.stubborn_after_terminate = False

    def start(self) -> None:
        self.started = True
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.join_calls += 1

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.stubborn_after_terminate:
            self.alive = False
            self.exitcode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.alive = False
        self.exitcode = -9

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(
        self,
        *,
        ready: bool,
        payload: bytes = b"",
        receive_error: BaseException | None = None,
        sleep_on_poll: bool = False,
    ) -> None:
        self.ready = ready
        self.payload = payload
        self.receive_error = receive_error
        self.sleep_on_poll = sleep_on_poll
        self.maximum_received: int | None = None
        self.closed = False

    def poll(self, timeout: float = 0.0) -> bool:
        if self.sleep_on_poll:
            time.sleep(timeout)
        return self.ready

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        self.maximum_received = maxlength
        if self.receive_error is not None:
            raise self.receive_error
        return self.payload

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_receive_child_message_enforces_hard_wall_timeout() -> None:
    process = _Process(alive=True)
    connection = _Connection(ready=False, sleep_on_poll=True)

    with pytest.raises(DerivativeIsolationTimeoutError, match="wall-time"):
        await _receive_child_message(
            process,
            connection,
            timeout_seconds=0.02,
            maximum_result_bytes=1024,
        )


@pytest.mark.asyncio
async def test_receive_child_message_detects_child_crash() -> None:
    process = _Process(alive=False, exitcode=137)
    connection = _Connection(ready=False)

    with pytest.raises(DerivativeIsolationCrashError, match="137"):
        await _receive_child_message(
            process,
            connection,
            timeout_seconds=1.0,
            maximum_result_bytes=1024,
        )


@pytest.mark.asyncio
async def test_receive_child_message_rejects_oversized_result() -> None:
    process = _Process(alive=True)
    connection = _Connection(ready=True, receive_error=OSError("bad message length"))

    with pytest.raises(DerivativeIsolationProtocolError, match="IPC byte limit"):
        await _receive_child_message(
            process,
            connection,
            timeout_seconds=1.0,
            maximum_result_bytes=4096,
        )

    assert connection.maximum_received == 4096


def test_cleanup_terminates_then_kills_a_stubborn_child() -> None:
    process = _Process(alive=True)
    process.stubborn_after_terminate = True

    _cleanup_process(
        process,
        0.01,
        terminate_immediately=True,
    )

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.join_calls == 2
    assert process.closed is True
    assert process.alive is False


class _Sender:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(
        self,
        process: _Process,
        receiver: _Connection,
        sender: _Sender,
    ) -> None:
        self.process = process
        self.receiver = receiver
        self.sender = sender

    def Pipe(self, *, duplex: bool) -> tuple[_Connection, _Sender]:  # noqa: N802
        assert duplex is False
        return self.receiver, self.sender

    def Process(self, **kwargs: object) -> _Process:  # noqa: N802
        assert kwargs["daemon"] is True
        return self.process


@pytest.mark.asyncio
async def test_cancellation_terminates_and_closes_one_shot_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(alive=False)
    receiver = _Connection(ready=False)
    sender = _Sender()
    context = _Context(process, receiver, sender)
    receive_started = asyncio.Event()

    async def wait_forever(*args: object, **kwargs: object) -> bytes:
        receive_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        derivative_isolation,
        "_assert_production_isolation_available",
        lambda: None,
    )
    monkeypatch.setattr(
        derivative_isolation.multiprocessing,
        "get_context",
        lambda method: context,
    )
    monkeypatch.setattr(
        derivative_isolation,
        "_receive_child_message",
        wait_forever,
    )

    task = asyncio.create_task(
        render_platform_derivatives_isolated(
            _master(),
            recipe=DerivativeRecipe(),
        )
    )
    await receive_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.started is True
    assert process.terminate_calls == 1
    assert process.closed is True
    assert process.alive is False
    assert receiver.closed is True
    assert sender.closed is True


@pytest.mark.parametrize(
    "child_error",
    [
        DerivativeIsolationTimeoutError("timed out"),
        DerivativeIsolationCrashError("crashed"),
        DerivativeIsolationProtocolError("oversized"),
    ],
    ids=["timeout", "crash", "oversized-result"],
)
@pytest.mark.asyncio
async def test_receive_failures_terminate_and_close_one_shot_child(
    monkeypatch: pytest.MonkeyPatch,
    child_error: Exception,
) -> None:
    process = _Process(alive=False)
    receiver = _Connection(ready=False)
    sender = _Sender()
    context = _Context(process, receiver, sender)

    async def fail_receive(*args: object, **kwargs: object) -> bytes:
        raise child_error

    monkeypatch.setattr(
        derivative_isolation,
        "_assert_production_isolation_available",
        lambda: None,
    )
    monkeypatch.setattr(
        derivative_isolation.multiprocessing,
        "get_context",
        lambda method: context,
    )
    monkeypatch.setattr(
        derivative_isolation,
        "_receive_child_message",
        fail_receive,
    )

    with pytest.raises(type(child_error)):
        await render_platform_derivatives_isolated(
            _master(),
            recipe=DerivativeRecipe(),
        )

    assert process.started is True
    assert process.terminate_calls == 1
    assert process.closed is True
    assert receiver.closed is True
    assert sender.closed is True


@pytest.mark.asyncio
async def test_hard_limit_must_cover_explicit_working_set_plus_process_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        derivative_isolation,
        "_assert_production_isolation_available",
        lambda: None,
    )

    with pytest.raises(DerivativeIsolationUnavailableError, match="does not cover"):
        await render_platform_derivatives_isolated(
            _master(),
            recipe=DerivativeRecipe(),
            policy=DerivativeIsolationPolicy(
                memory_limit_bytes=703 * 1024 * 1024,
            ),
        )


def test_isolation_fails_closed_when_hard_limits_are_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(derivative_isolation.sys, "platform", "win32")

    with pytest.raises(DerivativeIsolationUnavailableError, match="only on Linux"):
        derivative_isolation._assert_production_isolation_available()
    with pytest.raises(DerivativeIsolationUnavailableError, match="only on Linux"):
        _apply_linux_hard_limits(
            512 * 1024 * 1024,
            resource_module=object(),
            platform="win32",
        )


class _FakeResource:
    RLIMIT_AS = 1
    RLIMIT_RSS = 2
    RLIMIT_CORE = 3
    RLIM_INFINITY = -1

    def __init__(self, *, fail_kind: int | None = None) -> None:
        self.fail_kind = fail_kind
        self.limits: dict[int, tuple[int, int]] = {
            self.RLIMIT_AS: (self.RLIM_INFINITY, self.RLIM_INFINITY),
            self.RLIMIT_RSS: (self.RLIM_INFINITY, self.RLIM_INFINITY),
            self.RLIMIT_CORE: (self.RLIM_INFINITY, self.RLIM_INFINITY),
        }

    def getrlimit(self, kind: int) -> tuple[int, int]:
        return self.limits[kind]

    def setrlimit(self, kind: int, limits: tuple[int, int]) -> None:
        if kind == self.fail_kind:
            raise OSError("unsupported")
        self.limits[kind] = limits


def test_child_installs_and_verifies_address_space_rss_and_core_limits() -> None:
    resource = _FakeResource()
    memory_limit = 512 * 1024 * 1024

    _apply_linux_hard_limits(
        memory_limit,
        resource_module=resource,
        platform="linux",
    )

    assert resource.limits[resource.RLIMIT_AS] == (memory_limit, memory_limit)
    assert resource.limits[resource.RLIMIT_RSS] == (memory_limit, memory_limit)
    assert resource.limits[resource.RLIMIT_CORE] == (0, 0)


def test_child_fails_closed_when_rss_limit_cannot_be_installed() -> None:
    resource = _FakeResource(fail_kind=_FakeResource.RLIMIT_RSS)

    with pytest.raises(DerivativeIsolationUnavailableError, match="could not be installed"):
        _apply_linux_hard_limits(
            512 * 1024 * 1024,
            resource_module=resource,
            platform="linux",
        )


def test_child_fails_closed_when_existing_hard_limit_is_too_low() -> None:
    resource = _FakeResource()
    resource.limits[resource.RLIMIT_AS] = (256 * 1024 * 1024, 256 * 1024 * 1024)

    with pytest.raises(DerivativeIsolationUnavailableError, match="could not be installed"):
        _apply_linux_hard_limits(
            512 * 1024 * 1024,
            resource_module=resource,
            platform="linux",
        )


def test_safe_bounded_response_round_trips_a_rendered_bundle() -> None:
    master = _master()
    recipe = DerivativeRecipe()
    expected = render_platform_derivatives(master, recipe=recipe)

    response = _success_message(expected)
    decoded = _decode_child_response(
        response,
        DEFAULT_DERIVATIVE_LIMITS,
        expected_source_sha256=expected.source_sha256,
        expected_recipe=recipe,
        expected_target_values=tuple(artifact.target.value for artifact in expected.artifacts),
    )

    assert decoded == expected


def test_safe_bounded_response_accepts_the_expected_full_only_collection() -> None:
    master = _master()
    recipe = DerivativeRecipe()
    expected = render_platform_derivatives(
        master,
        recipe=recipe,
        targets=(DerivativeTarget.FULL_RESOLUTION,),
    )

    decoded = _decode_child_response(
        _success_message(expected),
        DEFAULT_DERIVATIVE_LIMITS,
        expected_source_sha256=expected.source_sha256,
        expected_recipe=recipe,
        expected_target_values=(DerivativeTarget.FULL_RESOLUTION.value,),
    )

    assert decoded == expected


def test_safe_bounded_response_accepts_frozen_previous_renderer_lineage() -> None:
    master = _master()
    recipe = DerivativeRecipe()
    expected = render_platform_derivatives(
        master,
        recipe=recipe,
        targets=(DerivativeTarget.FULL_RESOLUTION,),
        renderer_version=PREVIOUS_DERIVATIVE_RENDERER_VERSION,
    )

    decoded = _decode_child_response(
        _success_message(expected),
        DEFAULT_DERIVATIVE_LIMITS,
        expected_source_sha256=expected.source_sha256,
        expected_recipe=recipe,
        expected_target_values=(DerivativeTarget.FULL_RESOLUTION.value,),
        expected_renderer_version=PREVIOUS_DERIVATIVE_RENDERER_VERSION,
    )

    assert decoded == expected


def test_safe_bounded_response_accepts_artifact_base64_larger_than_text_metadata() -> None:
    recipe = DerivativeRecipe()
    expected = render_platform_derivatives(
        _large_master(),
        recipe=recipe,
        targets=(DerivativeTarget.FULL_RESOLUTION,),
    )
    assert len(base64.b64encode(expected.artifacts[0].data)) > 1024

    decoded = _decode_child_response(
        _success_message(expected),
        DEFAULT_DERIVATIVE_LIMITS,
        expected_source_sha256=expected.source_sha256,
        expected_recipe=recipe,
        expected_target_values=(DerivativeTarget.FULL_RESOLUTION.value,),
    )

    assert decoded == expected


def test_isolated_response_preserves_lossless_x_png_size_failure() -> None:
    with pytest.raises(
        XStaticImagePngTooLargeError,
        match="automatic JPEG conversion and downscaling are forbidden",
    ):
        _decode_child_response(
            _error_message(
                "x_lossless_png_too_large",
                "full-resolution lossless X PNG exceeds the static image byte limit; "
                "automatic JPEG conversion and downscaling are forbidden",
            ),
            DEFAULT_DERIVATIVE_LIMITS,
        )


def test_bounded_base64_field_accepts_exact_decoded_byte_limit() -> None:
    payload = b"boundary"

    assert (
        _bounded_base64_field(
            base64.b64encode(payload).decode("ascii"),
            maximum_decoded_bytes=len(payload),
        )
        == payload
    )


@pytest.mark.parametrize(
    "value",
    [None, b"YQ==", "", "\N{SNOWMAN}", "not base64"],
    ids=["non-string", "bytes", "empty", "non-ascii", "malformed"],
)
def test_bounded_base64_field_rejects_invalid_fields(value: object) -> None:
    with pytest.raises(DerivativeIsolationProtocolError, match="invalid artifact bytes"):
        _bounded_base64_field(value, maximum_decoded_bytes=16)


@pytest.mark.parametrize(
    "payload,maximum",
    [(b"1234567", 6), (b"123456", 5)],
    ids=["encoded-length-over-limit", "decoded-length-over-limit"],
)
def test_bounded_base64_field_rejects_oversized_payloads(
    payload: bytes,
    maximum: int,
) -> None:
    with pytest.raises(DerivativeIsolationProtocolError, match="outside the output limit"):
        _bounded_base64_field(
            base64.b64encode(payload).decode("ascii"),
            maximum_decoded_bytes=maximum,
        )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="production derivative process isolation is Linux-only",
)
@pytest.mark.asyncio
async def test_real_linux_spawn_installs_limits_and_round_trips_bundle() -> None:
    master = _master()
    recipe = DerivativeRecipe()
    expected = render_platform_derivatives(master, recipe=recipe)

    actual = await render_platform_derivatives_isolated(
        master,
        recipe=recipe,
        policy=DerivativeIsolationPolicy(
            wall_timeout_seconds=30,
            memory_limit_bytes=704 * 1024 * 1024,
        ),
    )

    assert actual == expected
