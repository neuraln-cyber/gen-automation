import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from gen_automation.app import create_app
from gen_automation.config import Environment
from gen_automation.db.models import (
    Asset,
    Release,
    ReleaseVersion,
    ReviewDecision,
    ReviewTask,
    ReviewXSelection,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole, ReviewTaskState
from gen_automation.middleware import asset_connection_source, content_security_policy
from gen_automation.services.review import create_review_task
from gen_automation.storage.base import ObjectMetadata, PresignedUpload
from tests.test_review_api import (
    ORIGIN,
    ReviewApiContext,
    _login,
    _seed_review_api,
    _settings,
)

_FORM_KEY = re.compile(r"web-review-[0-9a-f]{64}")
_FORM_HEADERS = {
    "Origin": ORIGIN,
    "Sec-Fetch-Site": "same-origin",
}


@dataclass(frozen=True, slots=True)
class ParsedForm:
    action: str
    method: str
    fields: dict[str, str]


class FormCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[ParsedForm] = []
        self._action: str | None = None
        self._method = ""
        self._fields: dict[str, str] = {}
        self._textarea_name: str | None = None
        self._textarea_value: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._action = attributes.get("action", "")
            self._method = attributes.get("method", "get")
            self._fields = {}
        elif tag == "input" and self._action is not None:
            name = attributes.get("name")
            input_type = attributes.get("type", "text")
            if name is not None and input_type != "submit":
                self._fields[name] = attributes.get("value", "")
        elif tag == "textarea" and self._action is not None:
            self._textarea_name = attributes.get("name")
            self._textarea_value = []

    def handle_data(self, data: str) -> None:
        if self._textarea_name is not None:
            self._textarea_value.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._textarea_name is not None:
            self._fields[self._textarea_name] = "".join(self._textarea_value)
            self._textarea_name = None
            self._textarea_value = []
        elif tag == "form" and self._action is not None:
            self.forms.append(
                ParsedForm(
                    action=self._action,
                    method=self._method,
                    fields=dict(self._fields),
                )
            )
            self._action = None
            self._method = ""
            self._fields = {}


class SameOriginReviewStore:
    backend = "s3"
    bucket = "private-review-api-bucket"

    def __init__(self) -> None:
        self.download_calls = 0

    async def ping(self) -> None:
        return None

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        raise AssertionError("review dashboard must not request upload access")

    async def presign_download(
        self,
        *,
        key: str,
        expires_in: int,
        download_name: str | None = None,
        version_id: str | None = None,
    ) -> str:
        self.download_calls += 1
        kind = "download" if download_name is not None else "view"
        return f"{ORIGIN}/private-review/{kind}/{self.download_calls}"

    async def head(self, key: str) -> ObjectMetadata | None:
        raise AssertionError("review dashboard must use the frozen score snapshot")

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        raise AssertionError("review dashboard must not proxy raw bytes")

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        raise AssertionError("review dashboard is read-only")

    async def copy_if_absent(
        self,
        *,
        source_key: str,
        destination_key: str,
        content_type: str,
        metadata: dict[str, str],
        source_version_id: str | None = None,
        source_etag: str | None = None,
    ) -> ObjectMetadata:
        raise AssertionError("review dashboard is read-only")

    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        raise AssertionError("review dashboard is read-only")

    async def close(self) -> None:
        return None


def _forms(html: str, action: str) -> list[ParsedForm]:
    collector = FormCollector()
    collector.feed(html)
    return [form for form in collector.forms if form.action == action]


def _one_form(html: str, action: str) -> ParsedForm:
    matching = _forms(html, action)
    assert len(matching) == 1
    return matching[0]


async def _release_id(context: ReviewApiContext) -> UUID:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            release_id = await session.scalar(select(Release.id))
            assert release_id is not None
            return release_id
    finally:
        await database.dispose()


async def _create_task(
    context: ReviewApiContext,
    *,
    role: AdminRole = AdminRole.OWNER,
) -> UUID:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            await session.execute(
                update(Asset).values(available_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            result = await create_review_task(
                session,
                scoring_run_id=context.scoring_run_id,
                created_by_user_id=context.users[role].id,
                idempotency_key=f"dashboard-seed-{role.value}",
            )
            return result.task_id
    finally:
        await database.dispose()


async def _advance_release_without_a_ranking(context: ReviewApiContext) -> None:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            release = await session.scalar(select(Release))
            assert release is not None
            session.add(
                ReleaseVersion(
                    release_id=release.id,
                    version_no=2,
                    specification={"schema_version": 2},
                    specification_sha256="f" * 64,
                    created_by="test",
                    created_at=datetime.now(UTC),
                )
            )
            release.current_version_no = 2
            await session.commit()
    finally:
        await database.dispose()


async def _review_state_and_actor(
    context: ReviewApiContext,
    *,
    task_id: UUID,
    asset_id: UUID | None = None,
) -> tuple[ReviewTaskState, UUID | None]:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            task = await session.get(ReviewTask, task_id)
            assert task is not None
            actor = None
            if asset_id is not None:
                actor = await session.scalar(
                    select(ReviewDecision.decided_by_user_id)
                    .where(
                        ReviewDecision.review_task_id == task_id,
                        ReviewDecision.asset_id == asset_id,
                    )
                    .order_by(ReviewDecision.revision.desc())
                    .limit(1)
                )
            return task.state, actor
    finally:
        await database.dispose()


async def _single_task_identity(
    context: ReviewApiContext,
) -> tuple[int, UUID | None, UUID | None]:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            tasks = list((await session.scalars(select(ReviewTask))).all())
            if len(tasks) != 1:
                return len(tasks), None, None
            return (
                1,
                tasks[0].scoring_run_id,
                tasks[0].created_by_user_id,
            )
    finally:
        await database.dispose()


async def _x_selected_assets(
    context: ReviewApiContext,
    *,
    task_id: UUID,
) -> tuple[UUID, ...]:
    database = Database(context.settings.database_url)
    try:
        async with database.sessions() as session:
            return tuple(
                (
                    await session.scalars(
                        select(ReviewXSelection.asset_id)
                        .where(ReviewXSelection.review_task_id == task_id)
                        .order_by(ReviewXSelection.selected_at)
                    )
                ).all()
            )
    finally:
        await database.dispose()


def _assert_safe_error(body: str) -> None:
    assert "private-secret" not in body
    assert "private-review-api-bucket" not in body
    assert "private-version" not in body
    assert "do-not-reflect-this-value" not in body


def test_csp_allows_http_images_only_in_local_and_test() -> None:
    test_sources = (
        content_security_policy(Environment.TEST)
        .split("img-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    production_sources = (
        content_security_policy(Environment.PRODUCTION)
        .split("img-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    assert test_sources == ["'self'", "https:", "http:"]
    assert production_sources == ["'self'", "https:"]


def test_csp_allows_dashboard_to_fetch_clean_asset_copies() -> None:
    default_connect_sources = (
        content_security_policy(Environment.PRODUCTION)
        .split("connect-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    test_connect_sources = (
        content_security_policy(
            Environment.TEST,
            asset_connect_source="http://assets.test:9000",
        )
        .split("connect-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    production_connect_sources = (
        content_security_policy(
            Environment.PRODUCTION,
            asset_connect_source="https://private-assets.s3.eu-central-1.amazonaws.com",
        )
        .split("connect-src ", maxsplit=1)[1]
        .split(";", maxsplit=1)[0]
        .split()
    )
    assert default_connect_sources == ["'self'"]
    assert test_connect_sources == ["'self'", "http://assets.test:9000"]
    assert production_connect_sources == [
        "'self'",
        "https://private-assets.s3.eu-central-1.amazonaws.com",
    ]


def test_asset_connection_source_is_restricted_to_the_configured_origin(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "csp-origin.sqlite3").model_copy(
        update={
            "storage_enabled": True,
            "storage_bucket": "private-assets",
            "storage_region": "eu-central-1",
        }
    )
    assert asset_connection_source(settings) == (
        "https://private-assets.s3.eu-central-1.amazonaws.com"
    )

    custom_endpoint = settings.model_copy(
        update={"storage_endpoint_url": "https://objects.example.test:9443/private"}
    )
    assert asset_connection_source(custom_endpoint) == "https://objects.example.test:9443"


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (AdminRole.OWNER, True),
        (AdminRole.REVIEWER, True),
        (AdminRole.ADMIN, False),
        (AdminRole.PUBLISHER, False),
    ],
)
def test_browser_review_detail_is_owner_or_reviewer_only(
    tmp_path: Path,
    role: AdminRole,
    allowed: bool,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / f"browser-role-{role.value}.db")))
    task_id = asyncio.run(_create_task(context))
    app = create_app(context.settings)

    store = SameOriginReviewStore()
    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.90", 50000),
    ) as client:
        app.state.object_store = store
        _login(client, context.settings, context.users[role])
        detail = client.get(f"/dashboard/review-tasks/{task_id}")
        mutation = client.post(
            f"/dashboard/review-tasks/{task_id}:cancel",
            data={},
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )

    if allowed:
        assert detail.status_code == 200
        assert store.download_calls == 4
        assert mutation.status_code == 415
        assert "submitted form type is not supported" in mutation.text
    else:
        assert detail.status_code == 403
        assert detail.json() == {"detail": "permission denied"}
        assert mutation.status_code == 403
        assert mutation.json() == {"detail": "permission denied"}
        assert store.download_calls == 0
        assert asyncio.run(_review_state_and_actor(context, task_id=task_id)) == (
            ReviewTaskState.OPEN,
            None,
        )


def test_browser_review_forms_are_bounded_same_origin_and_cookie_csrf_bound(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-forms.db")))
    release_id = asyncio.run(_release_id(context))
    app = create_app(context.settings)
    action = f"/dashboard/releases/{release_id}/review-tasks"

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.91", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        csrf = _login(
            client,
            context.settings,
            context.users[AdminRole.REVIEWER],
        )
        first_page = client.get(f"/dashboard/releases/{release_id}")
        second_page = client.get(f"/dashboard/releases/{release_id}")
        first_form = _one_form(first_page.text, action)
        second_form = _one_form(second_page.text, action)

        assert first_form.fields["csrf_token"] == csrf
        assert first_form.fields["idempotency_key"] == second_form.fields["idempotency_key"]
        assert _FORM_KEY.fullmatch(first_form.fields["idempotency_key"])

        missing_origin = client.post(
            action,
            data=first_form.fields,
            follow_redirects=False,
        )
        wrong_hidden_csrf = client.post(
            action,
            data={**first_form.fields, "csrf_token": "wrong-hidden-token"},
            headers={**_FORM_HEADERS, "X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        malformed = client.post(
            action,
            content=(f"csrf_token=%ZZ&idempotency_key={first_form.fields['idempotency_key']}"),
            headers={
                **_FORM_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        duplicate_body = urlencode(
            [
                ("csrf_token", first_form.fields["csrf_token"]),
                ("csrf_token", first_form.fields["csrf_token"]),
                ("idempotency_key", first_form.fields["idempotency_key"]),
            ]
        )
        duplicate = client.post(
            action,
            content=duplicate_body,
            headers={
                **_FORM_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        oversized = client.post(
            action,
            content=b"x" * (64 * 1024 + 1),
            headers={
                **_FORM_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        unsupported = client.post(
            action,
            content=b"{}",
            headers={**_FORM_HEADERS, "Content-Type": "application/json"},
            follow_redirects=False,
        )
        created = client.post(
            action,
            data=first_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        replay = client.post(
            action,
            data=first_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )

    assert missing_origin.status_code == 403
    assert wrong_hidden_csrf.status_code == 403
    assert malformed.status_code == 400
    assert duplicate.status_code == 400
    assert oversized.status_code == 413
    assert unsupported.status_code == 415
    for response in (
        missing_origin,
        wrong_hidden_csrf,
        malformed,
        duplicate,
        oversized,
        unsupported,
    ):
        _assert_safe_error(response.text)
    assert created.status_code == 303
    assert replay.status_code == 303
    assert created.headers["location"] == replay.headers["location"]
    assert created.headers["location"].startswith("/dashboard/review-tasks/")
    assert asyncio.run(_single_task_identity(context)) == (
        1,
        context.scoring_run_id,
        context.users[AdminRole.REVIEWER].id,
    )


def test_review_detail_reads_its_exact_run_after_release_advances(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-exact-run.db")))
    task_id = asyncio.run(_create_task(context))
    asyncio.run(_advance_release_without_a_ranking(context))
    app = create_app(context.settings)

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.94", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(
            client,
            context.settings,
            context.users[AdminRole.REVIEWER],
        )
        page = client.get(f"/dashboard/review-tasks/{task_id}")

    assert page.status_code == 200
    assert "Frozen release version 1" in page.text
    assert str(context.asset_ids[0]) in page.text
    assert str(context.asset_ids[1]) in page.text


def test_owner_selects_and_removes_specific_x_image_from_review_dashboard(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-x-selection.db")))
    task_id = asyncio.run(_create_task(context))
    app = create_app(context.settings)
    detail_action = f"/dashboard/review-tasks/{task_id}"
    x_action = f"{detail_action}/x-selections"

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.95", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(client, context.settings, context.users[AdminRole.OWNER])
        page = client.get(detail_action)
        x_forms = _forms(page.text, x_action)
        assert len(x_forms) == 2
        assert {form.fields["selected"] for form in x_forms} == {"true"}

        selected = client.post(
            x_action,
            data=x_forms[0].fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        select_replay = client.post(
            x_action,
            data=x_forms[0].fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        updated = client.get(detail_action)
        remove_form = _forms(updated.text, x_action)[0]
        assert remove_form.fields["selected"] == "false"
        removed = client.post(
            x_action,
            data=remove_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        final_page = client.get(detail_action)

    assert selected.status_code == 303
    assert select_replay.status_code == 303
    assert "Selected for X</dt><dd>1 / 4" in updated.text
    assert removed.status_code == 303
    assert "Selected for X</dt><dd>0 / 4" in final_page.text
    assert asyncio.run(_x_selected_assets(context, task_id=task_id)) == ()


def test_review_dashboard_exposes_progressive_bulk_controls(tmp_path: Path) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-bulk-ui.db")))
    task_id = asyncio.run(_create_task(context))
    app = create_app(context.settings)
    detail_action = f"/dashboard/review-tasks/{task_id}"
    bulk_action = f"{detail_action}/bulk-actions"

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.96", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(client, context.settings, context.users[AdminRole.OWNER])
        page = client.get(detail_action)

    assert page.status_code == 200
    bulk_form = _one_form(page.text, bulk_action)
    assert _FORM_KEY.fullmatch(bulk_form.fields["idempotency_key"])
    assert bulk_form.fields["expected_lock_version"] == "1"
    assert page.text.count('form="bulk-action-form"') == len(context.asset_ids)
    assert "Accept selected" in page.text
    assert "Exclude selected" in page.text
    assert "Hold selected" in page.text
    assert "Add to X" in page.text
    assert "Remove from X" in page.text
    assert "data-review-asset" in page.text
    assert 'data-x-selected-count="0"' in page.text
    assert 'data-x-capacity="4"' in page.text
    assert "data-bulk-selection-status" in page.text


def test_browser_bulk_review_action_applies_repeated_selected_assets(tmp_path: Path) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-bulk-submit.db")))
    task_id = asyncio.run(_create_task(context))
    app = create_app(context.settings)
    detail_action = f"/dashboard/review-tasks/{task_id}"
    bulk_action = f"{detail_action}/bulk-actions"

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.97", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(client, context.settings, context.users[AdminRole.OWNER])
        page = client.get(detail_action)
        bulk_form = _one_form(page.text, bulk_action)
        form_data: dict[str, str | list[str]] = {
            **bulk_form.fields,
            "asset_id": [str(asset_id) for asset_id in context.asset_ids],
            "action": "reject",
            "reason_code": "manual_reject",
            "note": "Excluded from the set; immutable raw masters retained.",
        }
        rejected = client.post(
            bulk_action,
            data=form_data,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        replay = client.post(
            bulk_action,
            data=form_data,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        summary = client.get(f"/api/v1/review-tasks/{task_id}")

    assert rejected.status_code == 303
    assert replay.status_code == 303
    assert rejected.headers["location"] == detail_action
    assert summary.status_code == 200
    assert summary.json()["rejected_count"] == len(context.asset_ids)
    assert summary.json()["lock_version"] == 2


def test_browser_review_decisions_lock_replay_actor_and_exact_completion(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-workflow.db")))
    task_id = asyncio.run(_create_task(context))
    app = create_app(context.settings)
    detail_action = f"/dashboard/review-tasks/{task_id}"
    decision_action = f"{detail_action}/decisions"
    complete_action = f"{detail_action}:complete"

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.92", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(
            client,
            context.settings,
            context.users[AdminRole.REVIEWER],
        )
        page = client.get(detail_action)

        assert page.status_code == 200
        assert "form-action 'self'" in page.headers["content-security-policy"]
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert '<script src="/static/dashboard.js" defer></script>' in page.text
        assert '<link rel="stylesheet" href="/static/dashboard_ux.css">' in page.text
        assert '<script src="/static/asset_viewer.js" defer></script>' in page.text
        image_sources = re.findall(r'<img[^>]+src="([^"]+)"', page.text)
        assert image_sources
        assert all(urlsplit(source).netloc == "testserver" for source in image_sources)

        decision_forms = _forms(page.text, decision_action)
        assert len(decision_forms) == 2
        assert {form.fields["expected_lock_version"] for form in decision_forms} == {"1"}
        first = {
            **decision_forms[0].fields,
            "decision": "accept",
            "reason_code": "manual_qc_pass",
            "note": "Accepted after full-resolution review.",
        }
        second_stale = {
            **decision_forms[1].fields,
            "decision": "reject",
            "reason_code": "manual_reject",
            "note": "",
        }
        early_complete_form = _one_form(page.text, complete_action)
        early_complete = client.post(
            complete_action,
            data=early_complete_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        extra_actor = client.post(
            decision_action,
            data={
                **first,
                "decided_by_user_id": str(context.users[AdminRole.OWNER].id),
                "note": "do-not-reflect-this-value",
            },
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        invalid_reason = client.post(
            decision_action,
            data={
                **first,
                "reason_code": "NOT A MACHINE CODE",
                "note": "do-not-reflect-this-value",
            },
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        decision = client.post(
            decision_action,
            data=first,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        decision_replay = client.post(
            decision_action,
            data=first,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        stale = client.post(
            decision_action,
            data=second_stale,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        updated_page = client.get(detail_action)
        updated_first = _forms(updated_page.text, decision_action)[0]
        complete_form = _one_form(updated_page.text, complete_action)
        completed = client.post(
            complete_action,
            data=complete_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        completed_replay = client.post(
            complete_action,
            data=complete_form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        final_page = client.get(detail_action)

    assert early_complete.status_code == 409
    assert extra_actor.status_code == 400
    _assert_safe_error(extra_actor.text)
    assert invalid_reason.status_code == 422
    _assert_safe_error(invalid_reason.text)
    assert decision.status_code == 303
    assert decision_replay.status_code == 303
    assert decision.headers["location"] == detail_action
    assert decision_replay.headers["location"] == detail_action
    assert stale.status_code == 409
    assert "changed before the form was submitted" in stale.text
    _assert_safe_error(stale.text)
    assert updated_first.fields["expected_lock_version"] == "2"
    assert updated_first.fields["idempotency_key"] != decision_forms[0].fields["idempotency_key"]
    assert "Current decision:" in updated_page.text
    assert "accept" in updated_page.text
    assert completed.status_code == 303
    assert completed_replay.status_code == 303
    assert final_page.status_code == 200
    assert "task completed" in final_page.text
    assert _forms(final_page.text, decision_action) == []
    state, actor = asyncio.run(
        _review_state_and_actor(
            context,
            task_id=task_id,
            asset_id=context.asset_ids[0],
        )
    )
    assert state == ReviewTaskState.COMPLETED
    assert actor == context.users[AdminRole.REVIEWER].id


def test_browser_review_cancel_redirects_and_replays(
    tmp_path: Path,
) -> None:
    context = asyncio.run(_seed_review_api(_settings(tmp_path / "browser-cancel.db")))
    task_id = asyncio.run(_create_task(context))
    app = create_app(context.settings)
    detail_action = f"/dashboard/review-tasks/{task_id}"
    cancel_action = f"{detail_action}:cancel"

    with TestClient(
        app,
        base_url=ORIGIN,
        client=("192.0.2.93", 50000),
    ) as client:
        app.state.object_store = SameOriginReviewStore()
        _login(
            client,
            context.settings,
            context.users[AdminRole.OWNER],
        )
        page = client.get(detail_action)
        form = _one_form(page.text, cancel_action)
        cancelled = client.post(
            cancel_action,
            data=form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        replay = client.post(
            cancel_action,
            data=form.fields,
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        terminal = client.get(detail_action)

    assert cancelled.status_code == 303
    assert replay.status_code == 303
    assert cancelled.headers["location"] == detail_action
    assert replay.headers["location"] == detail_action
    assert "task cancelled" in terminal.text
    assert asyncio.run(_review_state_and_actor(context, task_id=task_id)) == (
        ReviewTaskState.CANCELLED,
        None,
    )
