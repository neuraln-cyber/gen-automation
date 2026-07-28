import base64
import io
import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx2
import pytest
from PIL import Image

from gen_automation.gpu_worker.comfy import (
    ComfyConfigurationError,
    ComfyExecutionError,
    ComfyExecutionTimeoutError,
    ComfyExecutor,
    ComfyOutputError,
    ComfyProtocolError,
)

PROMPT_ID = "57ecf4dd-a951-4e3b-a0e5-47ac72a783bf"


def _workflow() -> dict[str, object]:
    return {
        "10": {"class_type": "SaveImage", "inputs": {"images": ["5", 0]}},
        "2": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["5", 0]}},
        "5": {"class_type": "KSampler", "inputs": {"seed": 42}},
    }


def _image_bytes(media_type: str = "image/png") -> bytes:
    image = Image.new("RGB", (2, 2), color=(10, 20, 30))
    output = io.BytesIO()
    image.save(
        output,
        format={
            "image/png": "PNG",
            "image/jpeg": "JPEG",
            "image/webp": "WEBP",
        }[media_type],
    )
    return output.getvalue()


def _history(
    outputs: dict[str, object],
    *,
    status: str = "success",
    completed: bool = True,
    messages: list[object] | None = None,
) -> dict[str, object]:
    return {
        PROMPT_ID: {
            "status": {
                "status_str": status,
                "completed": completed,
                "messages": messages or [],
            },
            "outputs": outputs,
        }
    }


def _json_response(value: object, status_code: int = 200) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        content=json.dumps(value, separators=(",", ":")).encode(),
        headers={"content-type": "application/json"},
    )


def _success_handler(
    images: dict[tuple[str, str], tuple[str, bytes]] | None = None,
) -> tuple[Callable[[httpx2.Request], httpx2.Response], list[httpx2.Request]]:
    resolved_images = images or {
        ("two.png", ""): ("image/png", _image_bytes()),
        ("ten.webp", "safe/nested"): ("image/webp", _image_bytes("image/webp")),
    }
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/system_stats":
            return _json_response({"system": {}, "devices": []})
        if request.url.path == "/prompt":
            submitted = json.loads(request.content)
            assert submitted["prompt"] == _workflow()
            return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
        if request.url.path == f"/history/{PROMPT_ID}":
            return _json_response(
                _history(
                    {
                        "10": {
                            "images": [
                                {
                                    "filename": "ten.webp",
                                    "subfolder": "safe/nested",
                                    "type": "output",
                                }
                            ]
                        },
                        "2": {
                            "images": [
                                {
                                    "filename": "two.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        },
                        "5": {
                            "images": [
                                {
                                    "filename": "ignored.png",
                                    "subfolder": "../../ignored",
                                    "type": "output",
                                }
                            ]
                        },
                    }
                )
            )
        if request.url.path == "/view":
            key = (
                request.url.params["filename"],
                request.url.params["subfolder"],
            )
            media_type, content = resolved_images[key]
            return httpx2.Response(200, content=content, headers={"content-type": media_type})
        raise AssertionError(f"unexpected path: {request.url.path}")

    return handler, requests


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:8188",
        "http://127.0.0.1.evil.test:8188",
        "http://localhost:8188",
        "http://127.0.0.1:8188/path",
        "http://user@127.0.0.1:8188",
        "http://127.0.0.1:8188?target=http://evil.test",
        "file:///etc/passwd",
        "http://2130706433:8188",
        "http://127.0.0.1:8188\\@evil.test",
    ],
)
def test_base_url_must_be_a_literal_loopback_origin(base_url: str) -> None:
    with pytest.raises(ComfyConfigurationError):
        ComfyExecutor(base_url=base_url)


def test_ipv4_and_ipv6_loopback_origins_are_supported() -> None:
    for base_url in ("http://127.0.0.2:8188", "https://[::1]:443"):
        executor = ComfyExecutor(
            base_url=base_url,
            transport=httpx2.MockTransport(
                lambda _request: _json_response({"system": {}, "devices": []})
            ),
        )
        try:
            assert executor.is_ready()
        finally:
            executor.close()


def test_readiness_requires_comfy_structure_and_does_not_follow_redirects() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307, headers={"location": "http://169.254.169.254/latest/meta-data"})

    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        assert not executor.is_ready()
    finally:
        executor.close()

    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert requests[0].url.path == "/system_stats"


def test_outputs_are_selected_from_save_nodes_and_ordered_deterministically() -> None:
    handler, requests = _success_handler()
    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        assert executor.is_ready()
        raw_outputs = executor.execute(_workflow())
    finally:
        executor.close()

    assert isinstance(raw_outputs, list)
    assert [(item["output_index"], item["media_type"]) for item in raw_outputs] == [
        (0, "image/png"),
        (1, "image/webp"),
    ]
    assert base64.b64decode(raw_outputs[0]["data_base64"], validate=True) == _image_bytes()
    view_requests = [request for request in requests if request.url.path == "/view"]
    assert [request.url.params["filename"] for request in view_requests] == [
        "two.png",
        "ten.webp",
    ]


def test_download_media_type_is_identified_from_bytes_not_header_or_extension() -> None:
    images = {
        ("two.png", ""): ("text/plain", _image_bytes("image/jpeg")),
        ("ten.webp", "safe/nested"): ("image/jpeg", _image_bytes("image/webp")),
    }
    handler, _requests = _success_handler(images)
    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        outputs = executor.execute(_workflow())
    finally:
        executor.close()

    assert isinstance(outputs, list)
    assert [output["media_type"] for output in outputs] == ["image/jpeg", "image/webp"]


def test_redirect_from_view_is_rejected_without_following_location() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_hosts.append(request.url.host)
        if request.url.path == "/prompt":
            return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
        if request.url.path.startswith("/history/"):
            return _json_response(
                _history(
                    {
                        "2": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]},
                        "10": {
                            "images": [{"filename": "y.png", "subfolder": "", "type": "output"}]
                        },
                    }
                )
            )
        return httpx2.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        )

    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(ComfyProtocolError, match="invalid Comfy response"):
            executor.execute(_workflow())
    finally:
        executor.close()
    assert requested_hosts and set(requested_hosts) == {"127.0.0.1"}


@pytest.mark.parametrize(
    "history",
    [
        {PROMPT_ID: []},
        {PROMPT_ID: {"status": [], "outputs": {}}},
        {
            PROMPT_ID: {
                "status": {"status_str": "success", "completed": False, "messages": []},
                "outputs": {},
            }
        },
        {
            PROMPT_ID: {
                "status": {"status_str": "success", "completed": True, "messages": "bad"},
                "outputs": {},
            }
        },
        {"different-prompt": {"status": {}, "outputs": {}}},
    ],
)
def test_malformed_history_is_rejected(history: dict[str, object]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/prompt":
            return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
        return _json_response(history)

    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(ComfyProtocolError):
            executor.execute(_workflow())
    finally:
        executor.close()


@pytest.mark.parametrize(
    ("status", "completed", "messages"),
    [
        ("error", False, []),
        ("failed", True, []),
        ("running", False, [["execution_error", {"exception": "private detail"}]]),
    ],
)
def test_execution_errors_are_redacted(
    status: str,
    completed: bool,
    messages: list[object],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/prompt":
            return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
        return _json_response(_history({}, status=status, completed=completed, messages=messages))

    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(ComfyExecutionError) as captured:
            executor.execute(_workflow())
    finally:
        executor.close()
    assert "private detail" not in str(captured.value)


@dataclass
class _FakeClock:
    current: float = 100.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


def test_timeout_best_effort_interrupts_and_remains_redacted() -> None:
    clock = _FakeClock()
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/prompt":
            return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
        if request.url.path.startswith("/history/"):
            return _json_response({})
        if request.url.path == "/interrupt":
            return httpx2.Response(204)
        raise AssertionError(request.url.path)

    executor = ComfyExecutor(
        transport=httpx2.MockTransport(handler),
        execution_timeout_seconds=2,
        poll_interval_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    try:
        with pytest.raises(ComfyExecutionTimeoutError) as captured:
            executor.execute(_workflow())
    finally:
        executor.close()

    assert paths.count(f"/history/{PROMPT_ID}") == 2
    assert paths[-1] == "/interrupt"
    assert str(captured.value) == "Comfy execution timed out"


def test_output_download_enforces_declared_and_streamed_byte_caps() -> None:
    image = _image_bytes()

    def make_handler(declared: bool) -> Callable[[httpx2.Request], httpx2.Response]:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/prompt":
                return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
            if request.url.path.startswith("/history/"):
                return _json_response(
                    _history(
                        {
                            "2": {
                                "images": [
                                    {
                                        "filename": "large.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            },
                            "10": {
                                "images": [
                                    {
                                        "filename": "other.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            },
                        }
                    )
                )
            headers = {"content-length": str(len(image))} if declared else {}
            return httpx2.Response(200, content=image, headers=headers)

        return handler

    for declared in (True, False):
        executor = ComfyExecutor(
            transport=httpx2.MockTransport(make_handler(declared)),
            max_output_bytes=len(image) - 1,
            max_total_output_bytes=len(image),
        )
        try:
            with pytest.raises(ComfyOutputError):
                executor.execute(_workflow())
        finally:
            executor.close()


@pytest.mark.parametrize(
    ("filename", "subfolder", "output_type"),
    [
        ("../secret.png", "", "output"),
        ("secret.png", "../../outside", "output"),
        ("secret.png", "safe\\..\\outside", "output"),
        ("secret.png", "/absolute", "output"),
        ("secret.png", "", "input"),
    ],
)
def test_traversal_like_output_metadata_is_rejected_before_view(
    filename: str,
    subfolder: str,
    output_type: str,
) -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/prompt":
            return _json_response({"prompt_id": PROMPT_ID, "node_errors": {}})
        return _json_response(
            _history(
                {
                    "2": {
                        "images": [
                            {
                                "filename": filename,
                                "subfolder": subfolder,
                                "type": output_type,
                            }
                        ]
                    },
                    "10": {"images": [{"filename": "safe.png", "subfolder": "", "type": "output"}]},
                }
            )
        )

    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(ComfyOutputError):
            executor.execute(_workflow())
    finally:
        executor.close()
    assert "/view" not in paths


def test_prompt_identifier_and_duplicate_json_keys_are_rejected() -> None:
    prompt_calls = 0

    def invalid_identifier_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal prompt_calls
        prompt_calls += 1
        return _json_response({"prompt_id": "../../escape", "node_errors": {}})

    executor = ComfyExecutor(transport=httpx2.MockTransport(invalid_identifier_handler))
    try:
        with pytest.raises(ComfyProtocolError):
            executor.execute(_workflow())
    finally:
        executor.close()
    assert prompt_calls == 1

    def duplicate_handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=b'{"prompt_id":"first","prompt_id":"second","node_errors":{}}',
        )

    executor = ComfyExecutor(transport=httpx2.MockTransport(duplicate_handler))
    try:
        with pytest.raises(ComfyProtocolError):
            executor.execute(_workflow())
    finally:
        executor.close()


def test_workflow_requires_api_nodes_and_at_least_one_save_node() -> None:
    executor = ComfyExecutor(
        transport=httpx2.MockTransport(lambda _request: pytest.fail("network called"))
    )
    try:
        with pytest.raises(ComfyProtocolError):
            executor.execute({"1": {"class_type": "KSampler", "inputs": {}}})
        with pytest.raises(ComfyProtocolError):
            executor.execute({"../1": {"class_type": "SaveImage", "inputs": {}}})
        with pytest.raises(ComfyProtocolError):
            executor.execute({1: {"class_type": "SaveImage", "inputs": {}}})  # type: ignore[dict-item]
        with pytest.raises(ComfyProtocolError):
            executor.execute({"1": {"class_type": "Save\ud800Image", "inputs": {}}})
    finally:
        executor.close()


def test_workflow_node_classes_are_default_deny_before_prompt_submission() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return pytest.fail("unapproved workflow reached Comfy")

    private_node_class = "ExecutePython_private_secret"
    workflow = _workflow()
    workflow["6"] = {"class_type": private_node_class, "inputs": {}}
    executor = ComfyExecutor(transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(ComfyProtocolError) as captured:
            executor.execute(workflow)
    finally:
        executor.close()

    assert str(captured.value) == "invalid Comfy workflow"
    assert private_node_class not in str(captured.value)
    assert requests == []


def test_configured_workflow_allowlist_is_copied_to_an_immutable_value() -> None:
    configured = {
        "ApprovedCustomNode",
        "KSampler",
        "SaveImage",
        "SaveImageWebsocket",
    }
    executor = ComfyExecutor(
        approved_node_classes=configured,
        transport=httpx2.MockTransport(lambda _request: pytest.fail("network called")),
    )
    configured.add("LateInjectedNode")
    try:
        assert isinstance(executor.approved_node_classes, frozenset)
        assert "ApprovedCustomNode" in executor.approved_node_classes
        assert "LateInjectedNode" not in executor.approved_node_classes

        workflow = _workflow()
        workflow["6"] = {"class_type": "LateInjectedNode", "inputs": {}}
        with pytest.raises(ComfyProtocolError):
            executor.execute(workflow)
    finally:
        executor.close()


@pytest.mark.parametrize(
    "approved",
    [
        set(),
        {"KSampler"},
        {"KSampler", "SaveImage", "bad node class"},
    ],
)
def test_invalid_workflow_allowlist_configuration_fails_closed(
    approved: set[str],
) -> None:
    with pytest.raises(ComfyConfigurationError):
        ComfyExecutor(approved_node_classes=approved)
