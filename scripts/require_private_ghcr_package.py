"""Fail closed unless a GHCR package is private or exactly absent before first push."""

from __future__ import annotations

import argparse
import json
from enum import StrEnum
from pathlib import Path
from typing import Any


class PackageCheckPhase(StrEnum):
    PRE_PUSH = "pre-push"
    POST_PUSH = "post-push"


class PackageStateError(ValueError):
    """The GitHub Packages response does not satisfy the publication gate."""


def require_private_ghcr_package(
    *,
    phase: PackageCheckPhase,
    http_status: str,
    response_body: Path,
) -> None:
    """Accept only private/200, plus exact 404 during the pre-push bootstrap."""

    if phase == PackageCheckPhase.PRE_PUSH and http_status == "404":
        return
    if http_status != "200":
        raise PackageStateError(f"unexpected GHCR package {phase.value} status: {http_status!r}")

    try:
        payload: Any = json.loads(response_body.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageStateError("GHCR package response is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("visibility") != "private":
        raise PackageStateError("GHCR package visibility is not private")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase", type=PackageCheckPhase, choices=tuple(PackageCheckPhase), required=True
    )
    parser.add_argument("--http-status", required=True)
    parser.add_argument("--response-file", type=Path, required=True)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    try:
        require_private_ghcr_package(
            phase=arguments.phase,
            http_status=arguments.http_status,
            response_body=arguments.response_file,
        )
    except PackageStateError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
