from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_STORED, ZipFile

import httpx2
import pytest
from PIL import Image
from pydantic import ValidationError

from gen_automation.config import Environment, Settings
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.deliverability import PATREON_MAX_ARCHIVE_BYTES
from gen_automation.integrations.patreon import (
    PatreonDriverOutcome,
    PatreonDriverRequest,
    PatreonDriverResult,
    PatreonPackageImage,
    PatreonSidecarDriver,
    PublicPreviewSafetyAttestation,
    build_patreon_handoff_package,
)
from gen_automation.integrations.patreon.sidecar import (
    PATREON_BROWSER_SIGNATURE_HEADER,
    calculate_patreon_browser_signature,
    patreon_browser_idempotency_key,
)
from gen_automation.patreon_browser import PatreonBrowserSettings, create_app
from gen_automation.patreon_browser.bootstrap import (
    _prepare_display_socket_directory,
    _profile_path,
)
from gen_automation.patreon_browser.package import (
    PatreonBrowserPackage,
    PatreonBrowserPackageError,
    load_patreon_browser_package,
)
from gen_automation.patreon_browser.publisher import (
    _configure_schedule,
    _visible_locator,
    _wait_until_enabled,
)

_PROFILE_REFERENCE = "creator-main"
_SHARED_SECRET = "test-only-patreon-browser-shared-secret"  # noqa: S105


def _png(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def _handoff() -> bytes:
    return build_patreon_handoff_package(
        approved_derivatives=(PatreonPackageImage("image.png", _png((1, 2, 3))),),
        public_preview=PatreonPackageImage("preview.png", _png((4, 5, 6))),
        title="Fixture set",
        body="Fixture body",
        tier="Paid members",
        tags=("fixture",),
        scheduled_at=None,
        public_preview_attestation=PublicPreviewSafetyAttestation(
            safe_for_public=True,
            attested_by="owner",
            attested_at=datetime(2026, 7, 29, tzinfo=UTC),
        ),
    ).archive_bytes


def _settings(tmp_path: Path) -> PatreonBrowserSettings:
    return PatreonBrowserSettings.model_validate(
        {
            "profile_root": tmp_path / "profiles",
            "spool_root": tmp_path / "spool",
            "state_path": tmp_path / "state" / "idempotency.sqlite3",
            "profile_reference": _PROFILE_REFERENCE,
            "shared_secret": _SHARED_SECRET,
        }
    )


class _DelayedLocator:
    def __init__(self, *, available_after: int = 0, enabled_after: int = 0) -> None:
        self.available_after = available_after
        self.enabled_after = enabled_after
        self.count_calls = 0
        self.enabled_calls = 0
        self.first = self

    async def count(self) -> int:
        self.count_calls += 1
        return int(self.count_calls > self.available_after)

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        self.enabled_calls += 1
        return self.enabled_calls > self.enabled_after


class _DelayedPage:
    def __init__(self, locator: _DelayedLocator) -> None:
        self._locator = locator

    def locator(self, _selector: str) -> _DelayedLocator:
        return self._locator


class _ScheduleLocator:
    def __init__(
        self,
        *,
        present: bool = True,
        input_type: str | None = None,
        checked: bool = False,
    ) -> None:
        self.present = present
        self.input_type = input_type
        self.checked = checked
        self.clicks = 0
        self.filled: list[str] = []
        self.pressed: list[str] = []
        self.first = self

    async def count(self) -> int:
        return int(self.present)

    async def is_visible(self) -> bool:
        return self.present

    async def get_attribute(self, name: str) -> str | None:
        if name == "aria-checked":
            return str(self.checked).lower()
        if name == "type":
            return self.input_type
        return None

    async def is_checked(self) -> bool:
        return self.checked

    async def click(self) -> None:
        self.clicks += 1
        self.checked = True

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class _SchedulePage:
    def __init__(self) -> None:
        self.toggle = _ScheduleLocator(checked=False)
        self.date = _ScheduleLocator(input_type="date")
        self.time = _ScheduleLocator(input_type="time")
        self.missing = _ScheduleLocator(present=False)

    def get_by_role(self, role: str, *, name: re.Pattern[str]) -> _ScheduleLocator:
        if role == "switch" and name.fullmatch("Set publish date"):
            return self.toggle
        return self.missing

    def get_by_label(self, name: re.Pattern[str]) -> _ScheduleLocator:
        if name.fullmatch("Set publish date"):
            return self.toggle
        if name.fullmatch("Publish date"):
            return self.date
        if name.fullmatch("Publish time"):
            return self.time
        return self.missing

    def locator(self, selector: str) -> _ScheduleLocator:
        if selector == 'input[type="date"]':
            return self.date
        if selector == 'input[type="time"]':
            return self.time
        return self.missing


async def test_browser_locator_waits_for_delayed_react_control() -> None:
    locator = _DelayedLocator(available_after=1)

    resolved = await _visible_locator(
        _DelayedPage(locator),
        (("css", "#editor", None),),
        wait_timeout_seconds=0.3,
    )

    assert resolved is locator
    assert locator.count_calls == 2


async def test_browser_waits_for_uploads_to_enable_publish_control() -> None:
    locator = _DelayedLocator(enabled_after=1)

    assert await _wait_until_enabled(locator, timeout_seconds=0.3)
    assert locator.enabled_calls == 2


async def test_browser_enables_set_publish_date_before_filling_utc_schedule() -> None:
    page = _SchedulePage()

    error = await _configure_schedule(
        page,
        "2026-08-15T14:30:00Z",
        wait_timeout_seconds=0.3,
    )

    assert error is None
    assert page.toggle.clicks == 1
    assert page.date.filled == ["2026-08-15"]
    assert page.time.filled == ["14:30"]
    assert page.time.pressed == ["Tab"]


def test_cloud_bootstrap_creates_only_the_named_profile_under_its_mount(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()

    assert _profile_path(profile_root, "creator-main") == (profile_root / "creator-main").resolve()
    with pytest.raises(ValueError, match="safe"):
        _profile_path(profile_root, "../escape")


def test_cloud_bootstrap_prepares_non_root_x11_socket_directory(tmp_path: Path) -> None:
    display_tmp = tmp_path / "tmp"
    display_tmp.mkdir()
    socket_directory = display_tmp / ".X11-unix"

    _prepare_display_socket_directory(socket_directory)

    assert socket_directory.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(socket_directory.stat().st_mode) == 0o1777


@pytest.mark.parametrize("offset", (-1, 1))
def test_browser_sidecar_capacity_is_exactly_the_internal_archive_cap(
    tmp_path: Path,
    offset: int,
) -> None:
    with pytest.raises(ValidationError):
        PatreonBrowserSettings.model_validate(
            {
                "profile_root": tmp_path / "profiles",
                "spool_root": tmp_path / "spool",
                "state_path": tmp_path / "state" / "idempotency.sqlite3",
                "profile_reference": _PROFILE_REFERENCE,
                "shared_secret": _SHARED_SECRET,
                "max_package_bytes": PATREON_MAX_ARCHIVE_BYTES + offset,
            }
        )


def _request_headers(
    *,
    intent_id: UUID,
    intent_digest: str,
    package_id: UUID,
    package_sha256: str,
    profile_reference: str = _PROFILE_REFERENCE,
    include_signature: bool = True,
) -> dict[str, str]:
    idempotency_key = patreon_browser_idempotency_key(
        intent_id=intent_id,
        intent_digest=intent_digest,
        package_id=package_id,
        package_sha256=package_sha256,
        profile_reference=profile_reference,
    )
    headers = {
        "Content-Type": "application/zip",
        "Idempotency-Key": idempotency_key,
        "X-Gen-Automation-Intent-Id": str(intent_id),
        "X-Gen-Automation-Intent-Digest": intent_digest,
        "X-Gen-Automation-Package-Id": str(package_id),
        "X-Gen-Automation-Package-Sha256": package_sha256,
        "X-Gen-Automation-Browser-Profile": profile_reference,
    }
    if include_signature:
        headers[PATREON_BROWSER_SIGNATURE_HEADER] = calculate_patreon_browser_signature(
            shared_secret=_SHARED_SECRET,
            intent_id=intent_id,
            intent_digest=intent_digest,
            package_id=package_id,
            package_sha256=package_sha256,
            profile_reference=profile_reference,
            idempotency_key=idempotency_key,
        )
    return headers


def _rewritten_handoff(package_bytes: bytes, kind: str) -> bytes:
    with ZipFile(BytesIO(package_bytes)) as source:
        entries = {info.filename: source.read(info) for info in source.infolist()}
    manifest = json.loads(entries["manifest.json"])
    if kind == "attestation":
        del manifest["public_preview"]["human_safety_attestation"]["statement"]
    elif kind == "metadata":
        manifest["post"]["title"] = "x" * 513
    elif kind == "image":
        record = manifest["approved_derivatives"][0]
        body = b"not-an-image"
        entries[record["path"]] = body
        record["sha256"] = hashlib.sha256(body).hexdigest()
        record["byte_size"] = len(body)
    else:
        raise AssertionError(f"unsupported fixture mutation: {kind}")
    entries["manifest.json"] = canonical_json_bytes(manifest)
    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_STORED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return output.getvalue()


@pytest.mark.asyncio
async def test_controller_to_browser_sidecar_contract_uses_only_frozen_package(
    tmp_path: Path,
) -> None:
    package_bytes = _handoff()
    package_path = tmp_path / "handoff.zip"
    package_path.write_bytes(package_bytes)
    publisher_calls = 0

    class FakePublisher:
        async def publish(
            self,
            package: PatreonBrowserPackage,
            *,
            profile_reference: str,
        ) -> PatreonDriverResult:
            nonlocal publisher_calls
            publisher_calls += 1
            assert profile_reference == _PROFILE_REFERENCE
            assert package.title == "Fixture set"
            assert package.tier == "Paid members"
            assert await asyncio.to_thread(package.content_paths[0].read_bytes) == _png((1, 2, 3))
            assert await asyncio.to_thread(package.public_preview_path.read_bytes) == _png(
                (4, 5, 6)
            )
            return PatreonDriverResult(
                outcome=PatreonDriverOutcome.PUBLISHED,
                remote_identifier="12345",
                remote_url="https://www.patreon.com/posts/fixture-set-12345",
            )

    settings = _settings(tmp_path)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=create_app(settings, publisher=FakePublisher())),
            base_url="http://patreon-browser.internal",
        ) as sidecar_http,
    ):
        driver = PatreonSidecarDriver(
            http_client=sidecar_http,
            endpoint_url="http://patreon-browser.internal/v1/publish",
            timeout_seconds=30,
            max_package_bytes=1024 * 1024,
            shared_secret=_SHARED_SECRET,
        )
        result = await driver.publish(
            PatreonDriverRequest(
                intent_id=uuid4(),
                intent_digest="a" * 64,
                package_id=uuid4(),
                package_path=package_path,
                package_sha256=hashlib.sha256(package_bytes).hexdigest(),
                browser_profile_reference=_PROFILE_REFERENCE,
            )
        )

    assert result.outcome == PatreonDriverOutcome.PUBLISHED
    assert result.remote_identifier == "12345"
    assert publisher_calls == 1


@pytest.mark.asyncio
async def test_sidecar_rejects_tampered_archive_before_browser(
    tmp_path: Path,
) -> None:
    publisher_calls = 0

    class FakePublisher:
        async def publish(self, *_args: object, **_kwargs: object) -> PatreonDriverResult:
            nonlocal publisher_calls
            publisher_calls += 1
            return PatreonDriverResult(outcome=PatreonDriverOutcome.FAILED)

    intent_id = uuid4()
    intent_digest = "b" * 64
    package_id = uuid4()
    claimed_digest = "c" * 64
    idempotency_key = patreon_browser_idempotency_key(
        intent_id=intent_id,
        intent_digest=intent_digest,
        package_id=package_id,
        package_sha256=claimed_digest,
        profile_reference=_PROFILE_REFERENCE,
    )
    signature = calculate_patreon_browser_signature(
        shared_secret=_SHARED_SECRET,
        intent_id=intent_id,
        intent_digest=intent_digest,
        package_id=package_id,
        package_sha256=claimed_digest,
        profile_reference=_PROFILE_REFERENCE,
        idempotency_key=idempotency_key,
    )
    settings = _settings(tmp_path)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(settings, publisher=FakePublisher())),
        base_url="http://patreon-browser.internal",
    ) as client:
        response = await client.post(
            "/v1/publish",
            headers={
                "Content-Type": "application/zip",
                "Idempotency-Key": idempotency_key,
                "X-Gen-Automation-Intent-Id": str(intent_id),
                "X-Gen-Automation-Intent-Digest": intent_digest,
                "X-Gen-Automation-Package-Id": str(package_id),
                "X-Gen-Automation-Package-Sha256": claimed_digest,
                "X-Gen-Automation-Browser-Profile": _PROFILE_REFERENCE,
                PATREON_BROWSER_SIGNATURE_HEADER: signature,
            },
            content=b"not-the-claimed-package",
        )

    assert response.status_code == 422
    assert publisher_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_signature", "profile_reference"),
    (
        (False, _PROFILE_REFERENCE),
        (True, "other-profile"),
    ),
)
async def test_sidecar_rejects_unauthenticated_or_non_allowlisted_profile_before_browser(
    tmp_path: Path,
    *,
    include_signature: bool,
    profile_reference: str,
) -> None:
    publisher_calls = 0

    class FakePublisher:
        async def publish(self, *_args: object, **_kwargs: object) -> PatreonDriverResult:
            nonlocal publisher_calls
            publisher_calls += 1
            return PatreonDriverResult(outcome=PatreonDriverOutcome.FAILED)

    package = _handoff()
    headers = _request_headers(
        intent_id=uuid4(),
        intent_digest="d" * 64,
        package_id=uuid4(),
        package_sha256=hashlib.sha256(package).hexdigest(),
        profile_reference=profile_reference,
        include_signature=include_signature,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(
            app=create_app(_settings(tmp_path), publisher=FakePublisher())
        ),
        base_url="http://patreon-browser.internal",
    ) as client:
        response = await client.post("/v1/publish", headers=headers, content=package)

    assert response.status_code == 401
    assert publisher_calls == 0


@pytest.mark.asyncio
async def test_terminal_idempotency_result_survives_restart_without_republishing(
    tmp_path: Path,
) -> None:
    package = _handoff()
    package_path = tmp_path / "handoff.zip"
    package_path.write_bytes(package)
    request = PatreonDriverRequest(
        intent_id=uuid4(),
        intent_digest="e" * 64,
        package_id=uuid4(),
        package_path=package_path,
        package_sha256=hashlib.sha256(package).hexdigest(),
        browser_profile_reference=_PROFILE_REFERENCE,
    )
    publisher_calls = 0

    class FakePublisher:
        async def publish(self, *_args: object, **_kwargs: object) -> PatreonDriverResult:
            nonlocal publisher_calls
            publisher_calls += 1
            return PatreonDriverResult(
                outcome=PatreonDriverOutcome.PUBLISHED,
                remote_identifier="98765",
                remote_url="https://www.patreon.com/posts/fixture-98765",
            )

    settings = _settings(tmp_path)
    first_app = create_app(settings, publisher=FakePublisher())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=first_app),
        base_url="http://patreon-browser.internal",
    ) as client:
        driver = PatreonSidecarDriver(
            http_client=client,
            endpoint_url="http://patreon-browser.internal/v1/publish",
            timeout_seconds=30,
            max_package_bytes=1024 * 1024,
            shared_secret=_SHARED_SECRET,
        )
        first = await driver.publish(request)
        duplicate = await driver.publish(request)

    restarted_app = create_app(settings, publisher=FakePublisher())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=restarted_app),
        base_url="http://patreon-browser.internal",
    ) as client:
        restarted_driver = PatreonSidecarDriver(
            http_client=client,
            endpoint_url="http://patreon-browser.internal/v1/publish",
            timeout_seconds=30,
            max_package_bytes=1024 * 1024,
            shared_secret=_SHARED_SECRET,
        )
        after_restart = await restarted_driver.publish(request)

    assert first == duplicate == after_restart
    assert first.outcome == PatreonDriverOutcome.PUBLISHED
    assert publisher_calls == 1


@pytest.mark.asyncio
async def test_unresolved_started_request_returns_unknown_without_browser_reentry(
    tmp_path: Path,
) -> None:
    package = _handoff()
    package_path = tmp_path / "handoff.zip"
    package_path.write_bytes(package)
    request = PatreonDriverRequest(
        intent_id=uuid4(),
        intent_digest="f" * 64,
        package_id=uuid4(),
        package_path=package_path,
        package_sha256=hashlib.sha256(package).hexdigest(),
        browser_profile_reference=_PROFILE_REFERENCE,
    )

    class CrashingPublisher:
        async def publish(self, *_args: object, **_kwargs: object) -> PatreonDriverResult:
            raise RuntimeError("simulated process interruption")

    settings = _settings(tmp_path)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(settings, publisher=CrashingPublisher())),
        base_url="http://patreon-browser.internal",
    ) as client:
        driver = PatreonSidecarDriver(
            http_client=client,
            endpoint_url="http://patreon-browser.internal/v1/publish",
            timeout_seconds=30,
            max_package_bytes=1024 * 1024,
            shared_secret=_SHARED_SECRET,
        )
        with pytest.raises(RuntimeError, match="simulated process interruption"):
            await driver.publish(request)

    publisher_calls = 0

    class MustNotRunPublisher:
        async def publish(self, *_args: object, **_kwargs: object) -> PatreonDriverResult:
            nonlocal publisher_calls
            publisher_calls += 1
            return PatreonDriverResult(outcome=PatreonDriverOutcome.FAILED)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(settings, publisher=MustNotRunPublisher())),
        base_url="http://patreon-browser.internal",
    ) as client:
        driver = PatreonSidecarDriver(
            http_client=client,
            endpoint_url="http://patreon-browser.internal/v1/publish",
            timeout_seconds=30,
            max_package_bytes=1024 * 1024,
            shared_secret=_SHARED_SECRET,
        )
        result = await driver.publish(request)

    assert result.outcome == PatreonDriverOutcome.UNKNOWN
    assert result.detail_code == "idempotency_outcome_unresolved"
    assert publisher_calls == 0


@pytest.mark.parametrize("kind", ("attestation", "metadata", "image"))
def test_browser_package_rejects_noncanonical_or_unverified_inputs(
    tmp_path: Path,
    kind: str,
) -> None:
    archive_path = tmp_path / f"{kind}.zip"
    archive_path.write_bytes(_rewritten_handoff(_handoff(), kind))

    with pytest.raises(PatreonBrowserPackageError):
        load_patreon_browser_package(
            archive_path,
            tmp_path / f"extract-{kind}",
            max_package_bytes=1024 * 1024,
        )


def test_patreon_browser_controller_configuration_requires_shared_secret() -> None:
    values = {
        "environment": Environment.TEST,
        "background_runtime_enabled": True,
        "storage_enabled": True,
        "storage_bucket": "private-assets",
        "publishing_enabled": True,
        "patreon_browser_publishing_enabled": True,
        "patreon_browser_sidecar_url": "http://patreon-browser:8090/v1/publish",
        "patreon_browser_profile_reference": _PROFILE_REFERENCE,
    }
    with pytest.raises(ValidationError, match="32-4096 byte shared secret"):
        Settings(**values)  # type: ignore[arg-type]

    settings = Settings(
        **values,  # type: ignore[arg-type]
        patreon_browser_shared_secret=_SHARED_SECRET,
    )
    assert settings.patreon_browser_shared_secret is not None
    assert settings.patreon_browser_shared_secret.get_secret_value() == _SHARED_SECRET
