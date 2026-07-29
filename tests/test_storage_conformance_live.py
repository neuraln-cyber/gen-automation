import os

import pytest

from gen_automation.config import Settings
from gen_automation.services.storage_conformance import run_storage_conformance
from gen_automation.storage.s3 import build_object_store

_LIVE_OPT_IN = (
    os.environ.get("GEN_AUTOMATION_RUN_LIVE_STORAGE_CONFORMANCE")
    == "I_UNDERSTAND_THIS_WRITES_LIVE_OBJECTS"
)


@pytest.mark.skipif(
    not _LIVE_OPT_IN,
    reason="live S3 conformance writes require a separate explicit test opt-in",
)
@pytest.mark.asyncio
async def test_live_s3_compatible_storage_conformance() -> None:
    settings = Settings()
    store = build_object_store(settings)
    assert store is not None
    try:
        report = await run_storage_conformance(
            store,
            confirmed=True,
        )
    finally:
        await store.close()

    assert report.success, report.to_safe_dict()
