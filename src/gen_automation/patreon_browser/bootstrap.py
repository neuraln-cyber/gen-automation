from __future__ import annotations

import argparse
import importlib
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from typing import Any

_PROFILE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DISPLAY = ":99"
_DISPLAY_SOCKET = Path("/tmp/.X11-unix/X99")  # noqa: S108 - standard Xvfb socket
_NOVNC_ROOT = Path("/usr/share/novnc")
_CONTAINER_LISTEN_HOST = "0.0.0.0"  # noqa: S104 - host publish is loopback-only
_BROWSER_CHANNEL = "chrome"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open a one-time headed Patreon login session over noVNC.",
    )
    parser.add_argument(
        "--profile-reference",
        default=os.environ.get(
            "GEN_AUTOMATION_PATREON_BROWSER_PROFILE_REFERENCE",
            "creator-main",
        ),
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=Path(
            os.environ.get(
                "GEN_AUTOMATION_PATREON_BROWSER_PROFILE_ROOT",
                "/profiles",
            )
        ),
    )
    parser.add_argument(
        "--editor-url",
        default=os.environ.get(
            "GEN_AUTOMATION_PATREON_BROWSER_EDITOR_URL",
            "https://www.patreon.com/posts/new",
        ),
    )
    parser.add_argument("--listen-host", default=_CONTAINER_LISTEN_HOST)
    parser.add_argument("--listen-port", type=int, default=6080)
    return parser


def _profile_path(root: Path, reference: str) -> Path:
    if _PROFILE_REFERENCE.fullmatch(reference) is None:
        raise ValueError("profile reference must be a safe 1-64 character name")
    if root.is_symlink():
        raise ValueError("profile root must not be a symlink")
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root / reference
    if candidate.is_symlink():
        raise ValueError("profile directory must not be a symlink")
    profile = candidate.resolve()
    try:
        profile.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("profile path escapes the configured profile root") from exc
    profile.mkdir(mode=0o700, parents=False, exist_ok=True)
    profile.chmod(0o700)
    return profile


def _start_process(
    *command: str,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - commands are fixed image-owned binaries
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        start_new_session=True,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_display(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float = 10,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Xvfb exited before the display became ready")
        if _DISPLAY_SOCKET.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for the headed browser display")


def _prepare_display_socket_directory(path: Path = _DISPLAY_SOCKET.parent) -> None:
    if path.is_symlink():
        raise RuntimeError("X11 socket directory must not be a symlink")
    path.mkdir(mode=0o1777, parents=False, exist_ok=True)
    path.chmod(0o1777)


def _register_signals(stop: Event) -> None:
    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def run_bootstrap(args: argparse.Namespace) -> int:
    if not 1 <= args.listen_port <= 65535:
        raise ValueError("listen port must be between 1 and 65535")
    if args.listen_host not in {_CONTAINER_LISTEN_HOST, "127.0.0.1"}:
        raise ValueError("listen host must be 0.0.0.0 or 127.0.0.1")
    if not _NOVNC_ROOT.is_dir():
        raise RuntimeError("noVNC assets are unavailable in this image")

    profile = _profile_path(args.profile_root, args.profile_reference)
    stop = Event()
    _register_signals(stop)
    browser_env = {**os.environ, "DISPLAY": _DISPLAY}

    with ExitStack() as stack:
        _prepare_display_socket_directory()
        xvfb = _start_process(
            "Xvfb",
            _DISPLAY,
            "-screen",
            "0",
            "1440x900x24",
            "-nolisten",
            "tcp",
            "-ac",
        )
        stack.callback(_stop_process, xvfb)
        _wait_for_display(xvfb)

        vnc = _start_process(
            "x11vnc",
            "-display",
            _DISPLAY,
            "-forever",
            "-shared",
            "-localhost",
            "-nopw",
            "-rfbport",
            "5900",
        )
        stack.callback(_stop_process, vnc)
        novnc = _start_process(
            "websockify",
            f"--web={_NOVNC_ROOT}",
            "--heartbeat=30",
            f"{args.listen_host}:{args.listen_port}",
            "127.0.0.1:5900",
        )
        stack.callback(_stop_process, novnc)

        playwright_api = importlib.import_module("playwright.sync_api")
        sync_playwright = playwright_api.sync_playwright

        with sync_playwright() as playwright:
            with playwright.chromium.launch_persistent_context(
                str(profile),
                channel=_BROWSER_CHANNEL,
                headless=False,
                env=browser_env,
                args=("--disable-dev-shm-usage",),
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(args.editor_url, wait_until="domcontentloaded")
                print(
                    "Headed Patreon session ready. Complete login in the SSM-tunneled "
                    f"noVNC page on port {args.listen_port}, then press Ctrl+C here.",
                    flush=True,
                )
                while not stop.wait(0.5):
                    if (
                        xvfb.poll() is not None
                        or vnc.poll() is not None
                        or novnc.poll() is not None
                    ):
                        raise RuntimeError("the private browser display exited unexpectedly")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run_bootstrap(_parser().parse_args(argv))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Patreon profile bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
