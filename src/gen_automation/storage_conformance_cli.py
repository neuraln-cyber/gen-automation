from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from gen_automation.config import Settings
from gen_automation.services.storage_conformance import (
    CONFORMANCE_OPT_IN_FLAG,
    StorageConformanceReport,
    report_with_client_close_failure,
    run_storage_conformance,
    storage_conformance_failure_report,
)
from gen_automation.storage.s3 import build_object_store


def storage_conformance_main(arguments: Sequence[str] | None = None) -> int:
    """Run an explicitly confirmed, redacted live-storage conformance probe."""

    selected_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if selected_arguments in (["--help"], ["-h"]):
        print(f"Usage: gen-automation-storage-conformance {CONFORMANCE_OPT_IN_FLAG}")
        return 0
    if selected_arguments != [CONFORMANCE_OPT_IN_FLAG]:
        _print_report(
            storage_conformance_failure_report(
                backend="unconfigured",
                code="explicit_opt_in_required",
            ),
            to_stderr=True,
        )
        return 2

    try:
        settings = Settings()
    except ValidationError:
        _print_report(
            storage_conformance_failure_report(
                backend="unconfigured",
                code="storage_configuration_invalid",
            ),
            to_stderr=True,
        )
        return 2

    try:
        report = asyncio.run(_run_configured_conformance(settings))
    except Exception:
        report = storage_conformance_failure_report(
            backend="unconfigured",
            code="conformance_command_failed",
        )
    _print_report(report, to_stderr=not report.success)
    if report.success:
        return 0
    if report.failure_code in {
        "explicit_opt_in_required",
        "storage_configuration_invalid",
        "storage_not_enabled",
        "s3_backend_required",
    }:
        return 2
    return 1


async def _run_configured_conformance(settings: Settings) -> StorageConformanceReport:
    try:
        store = build_object_store(settings)
    except Exception:
        return storage_conformance_failure_report(
            backend="unconfigured",
            code="storage_configuration_invalid",
        )
    if store is None:
        return storage_conformance_failure_report(
            backend="unconfigured",
            code="storage_not_enabled",
        )

    report: StorageConformanceReport
    try:
        report = await run_storage_conformance(
            store,
            confirmed=True,
        )
    except Exception:
        report = storage_conformance_failure_report(
            backend="s3",
            code="conformance_service_failed",
        )
    try:
        await store.close()
    except Exception:
        report = report_with_client_close_failure(report)
    return report


def _print_report(report: StorageConformanceReport, *, to_stderr: bool) -> None:
    print(
        json.dumps(
            report.to_safe_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr if to_stderr else sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(storage_conformance_main())
