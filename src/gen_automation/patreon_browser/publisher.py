from __future__ import annotations

import asyncio
import importlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gen_automation.integrations.patreon.driver import (
    PatreonDriverOutcome,
    PatreonDriverResult,
)
from gen_automation.patreon_browser.package import PatreonBrowserPackage

_POST_URL = re.compile(r"^https://(?:www\.)?patreon\.com/posts/(?:[^/?#]+-)?([0-9]{1,20})/?$")


@dataclass(frozen=True, slots=True)
class PlaywrightPatreonPublisher:
    profile_root: Path
    editor_url: str
    headless: bool
    action_timeout_seconds: float

    async def publish(
        self,
        package: PatreonBrowserPackage,
        *,
        profile_reference: str,
    ) -> PatreonDriverResult:
        profile_path = (self.profile_root / profile_reference).resolve()
        try:
            profile_path.relative_to(self.profile_root.resolve())
        except ValueError:
            return _result(PatreonDriverOutcome.FAILED, "profile_reference_invalid")
        if not profile_path.is_dir() or not any(profile_path.iterdir()):
            return _result(PatreonDriverOutcome.NEEDS_OPERATOR, "profile_not_bootstrapped")
        try:
            playwright_api = importlib.import_module("playwright.async_api")
            async_playwright = playwright_api.async_playwright
        except ImportError:
            return _result(PatreonDriverOutcome.FAILED, "browser_runtime_unavailable")

        submitted = False
        try:
            async with asyncio.timeout(self.action_timeout_seconds):
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        str(profile_path),
                        headless=self.headless,
                        timezone_id="UTC",
                        args=("--disable-dev-shm-usage",),
                    )
                    try:
                        page = context.pages[0] if context.pages else await context.new_page()
                        page.set_default_timeout(self.action_timeout_seconds * 1_000)
                        await page.goto(self.editor_url, wait_until="domcontentloaded")
                        preflight = await _preflight(page)
                        if preflight is not None:
                            return _result(PatreonDriverOutcome.NEEDS_OPERATOR, preflight)
                        prepared = await _prepare_post(
                            page,
                            package,
                            wait_timeout_seconds=self.action_timeout_seconds,
                        )
                        if prepared is not None:
                            return _result(PatreonDriverOutcome.NEEDS_OPERATOR, prepared)
                        submit = await _visible_locator(
                            page,
                            (
                                (
                                    "role",
                                    "button",
                                    re.compile(r"^(publish(?: now)?|schedule)$", re.I),
                                ),
                                ("css", 'button[data-tag="publish-button"]', None),
                            ),
                            wait_timeout_seconds=self.action_timeout_seconds,
                        )
                        if submit is None or not await _wait_until_enabled(
                            submit,
                            timeout_seconds=self.action_timeout_seconds,
                        ):
                            return _result(
                                PatreonDriverOutcome.NEEDS_OPERATOR,
                                "publish_control_unavailable",
                            )
                        submitted = True
                        await submit.click()
                        await page.wait_for_url(re.compile(r"/posts/(?:[^/?#]+-)?[0-9]{1,20}/?$"))
                        match = _POST_URL.fullmatch(page.url)
                        if match is None:
                            return _result(
                                PatreonDriverOutcome.UNKNOWN,
                                "post_identity_unconfirmed",
                            )
                        return PatreonDriverResult(
                            outcome=PatreonDriverOutcome.PUBLISHED,
                            remote_identifier=match.group(1),
                            remote_url=page.url.rstrip("/"),
                        )
                    finally:
                        await context.close()
        except Exception:
            if submitted:
                return _result(PatreonDriverOutcome.UNKNOWN, "publish_confirmation_lost")
            return _result(PatreonDriverOutcome.NEEDS_OPERATOR, "browser_ui_unavailable")


async def _preflight(page: Any) -> str | None:
    if "login" in page.url.casefold():
        return "profile_login_required"
    login = await _visible_locator(
        page,
        (("role", "button", re.compile(r"^log in$", re.I)),),
    )
    if login is not None:
        return "profile_login_required"
    challenge = await _visible_locator(
        page,
        (
            ("css", 'iframe[src*="captcha"]', None),
            ("css", 'iframe[title*="challenge" i]', None),
        ),
    )
    if challenge is not None:
        return "interactive_challenge"
    return None


async def _prepare_post(
    page: Any,
    package: PatreonBrowserPackage,
    *,
    wait_timeout_seconds: float,
) -> str | None:
    image_post = await _visible_locator(
        page,
        (("role", "button", re.compile(r"^image(?: post)?$", re.I)),),
        wait_timeout_seconds=min(wait_timeout_seconds, 5),
    )
    if image_post is not None:
        await image_post.click()

    title = await _visible_locator(
        page,
        (
            ("label", re.compile(r"^title$", re.I), None),
            ("css", 'textarea[data-tag="post-title"]', None),
            ("css", 'input[name="title"]', None),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    body = await _visible_locator(
        page,
        (
            ("label", re.compile(r"(post )?(body|description)", re.I), None),
            ("css", '[contenteditable="true"][role="textbox"]', None),
            ("css", 'textarea[name="body"]', None),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    content_upload = await _visible_locator(
        page,
        (
            ("css", 'input[type="file"][multiple]', None),
            ("css", 'input[type="file"][data-tag*="content" i]', None),
        ),
        visibility_required=False,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    if title is None or body is None or content_upload is None:
        return "editor_contract_changed"

    await title.fill(package.title)
    await body.fill(package.body)
    await content_upload.set_input_files([str(path) for path in package.content_paths])

    audience = await _visible_locator(
        page,
        (
            ("role", "button", re.compile(r"(audience|access|tier)", re.I)),
            ("label", re.compile(r"(audience|access|tier)", re.I), None),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    if audience is None:
        return "audience_control_missing"
    await audience.click()
    tier = await _visible_locator(
        page,
        (
            ("role", "option", re.compile(rf"^{re.escape(package.tier)}$", re.I)),
            ("text", package.tier, None),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    if tier is None:
        return "configured_tier_missing"
    await tier.click()

    if package.tags:
        tag_input = await _visible_locator(
            page,
            (
                ("label", re.compile(r"^tags?$", re.I), None),
                ("css", 'input[name*="tag" i]', None),
            ),
            wait_timeout_seconds=wait_timeout_seconds,
        )
        if tag_input is None:
            return "tag_control_missing"
        for tag in package.tags:
            await tag_input.fill(tag)
            await tag_input.press("Enter")

    if package.scheduled_at is not None:
        schedule_error = await _configure_schedule(
            page,
            package.scheduled_at,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        if schedule_error is not None:
            return schedule_error
    return None


async def _configure_schedule(
    page: Any,
    scheduled_at: str,
    *,
    wait_timeout_seconds: float,
) -> str | None:
    set_publish_date = await _visible_locator(
        page,
        (
            ("role", "switch", re.compile(r"^set publish date$", re.I)),
            ("role", "checkbox", re.compile(r"^set publish date$", re.I)),
            ("label", re.compile(r"^set publish date$", re.I), None),
            ("role", "button", re.compile(r"^set publish date$", re.I)),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    if set_publish_date is None:
        return "set_publish_date_control_missing"
    if not await _control_is_checked(set_publish_date):
        await set_publish_date.click()

    scheduled = _scheduled_utc(scheduled_at)
    date_or_datetime = await _visible_locator(
        page,
        (
            ("css", 'input[type="datetime-local"]', None),
            ("label", re.compile(r"^(publish )?date$", re.I), None),
            ("css", 'input[type="date"]', None),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    if date_or_datetime is None:
        return "schedule_date_input_missing"
    input_type = await date_or_datetime.get_attribute("type")
    if input_type == "datetime-local":
        await date_or_datetime.fill(scheduled.strftime("%Y-%m-%dT%H:%M"))
        await date_or_datetime.press("Tab")
        return None

    await date_or_datetime.fill(scheduled.strftime("%Y-%m-%d"))
    time_input = await _visible_locator(
        page,
        (
            ("label", re.compile(r"^(publish )?time$", re.I), None),
            ("css", 'input[type="time"]', None),
        ),
        wait_timeout_seconds=wait_timeout_seconds,
    )
    if time_input is None:
        return "schedule_time_input_missing"
    await time_input.fill(scheduled.strftime("%H:%M"))
    await time_input.press("Tab")
    return None


async def _control_is_checked(control: Any) -> bool:
    try:
        aria_checked = await control.get_attribute("aria-checked")
    except Exception:
        aria_checked = None
    if aria_checked == "true":
        return True
    try:
        return bool(await control.is_checked())
    except Exception:
        return False


def _scheduled_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid Patreon schedule") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid Patreon schedule")
    return parsed.astimezone(UTC)


async def _visible_locator(
    page: Any,
    specifications: tuple[tuple[Any, ...], ...],
    *,
    visibility_required: bool = True,
    wait_timeout_seconds: float = 0,
) -> Any | None:
    locators: list[Any] = []
    for specification in specifications:
        kind = specification[0]
        if kind == "role":
            locator = page.get_by_role(specification[1], name=specification[2]).first
        elif kind == "label":
            locator = page.get_by_label(specification[1]).first
        elif kind == "text":
            locator = page.get_by_text(specification[1], exact=True).first
        else:
            locator = page.locator(specification[1]).first
        locators.append(locator)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(wait_timeout_seconds, 0)
    while True:
        for locator in locators:
            try:
                if await locator.count() and (
                    not visibility_required or await locator.is_visible()
                ):
                    return locator
            except Exception:  # noqa: S112 - alternate selectors are best-effort probes
                continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.25, remaining))


async def _wait_until_enabled(locator: Any, *, timeout_seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0)
    while True:
        try:
            enabled = await locator.is_enabled()
        except Exception:
            enabled = False
        if enabled:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.25, remaining))


def _result(outcome: PatreonDriverOutcome, detail_code: str) -> PatreonDriverResult:
    return PatreonDriverResult(outcome=outcome, detail_code=detail_code)
