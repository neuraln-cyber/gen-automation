from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"
DELIVERY_TEMPLATE = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "delivery.html"
REVIEW_TEMPLATE = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"


def _delivery_progress_handler() -> str:
    script = SCRIPT.read_text(encoding="utf-8")
    return script.split("function initializeDeliveryProgress()", 1)[1].split(
        "function initializeDeliveryReauthentication()", 1
    )[0]


def test_delivery_progress_uses_json_polling_without_periodic_page_reloads() -> None:
    handler = _delivery_progress_handler()

    assert 'payload.schema !== "delivery-progress/v1"' in handler
    assert 'cache: "no-store"' in handler
    assert 'headers: { Accept: "application/json" }' in handler
    assert 'document.visibilityState === "hidden"' in handler
    assert "consecutiveFailures" in handler
    assert "poll_after_ms" in handler
    assert "data-delivery-auto-refresh" not in handler
    assert handler.count("window.location.reload()") == 1
    assert "reloadForNewControls" in handler
    assert 'outputState === "ready"' in handler
    assert 'archiveState === "ready"' in handler


def test_delivery_progress_stops_polling_when_the_session_expires() -> None:
    handler = _delivery_progress_handler()

    assert "response.redirected" in handler
    assert "response.status === 401" in handler
    assert "response.status === 403" in handler
    assert 'stopPolling("Session expired; sign in again or reload this page.")' in handler
    assert "Session expired; sign in again or reload this page." in handler


def test_delivery_progress_bounds_transient_failures_and_treats_not_found_as_terminal() -> None:
    handler = _delivery_progress_handler()

    assert "const maxConsecutiveFailures = 6;" in handler
    assert "response.status === 404" in handler
    assert "consecutiveFailures >= maxConsecutiveFailures" in handler
    assert handler.count("Live updates paused; reload this page to retry.") == 2
    assert "const backoff = Math.min(60000" in handler


def test_delivery_template_explains_copy_and_archive_work_truthfully() -> None:
    template = DELIVERY_TEMPLATE.read_text(encoding="utf-8")
    review_template = REVIEW_TEMPLATE.read_text(encoding="utf-8")

    assert "Preparing clean publishing copies" in template
    assert "Your AI images are already finished" in template
    assert "ZIP creation happens" in template
    assert "Creating ZIP parts in the background" in template
    assert "publication runtime or switch is stopped" in template
    assert "data-delivery-output-progress" in template
    assert 'aria-live="polite"' in template
    assert '{% elif output_state == "failed" %}' in template
    assert '{% elif output_state == "stalled" %}' in template
    assert "data-delivery-auto-refresh" not in template
    assert 'data-delivery-progress-url="/dashboard/review-tasks/' in template
    assert "poll_delivery" in template
    assert "Prepare or download finished set" in review_template


def test_ready_archive_is_excluded_from_delivery_polling_condition() -> None:
    template = DELIVERY_TEMPLATE.read_text(encoding="utf-8")

    assert "archive_state = delivery_progress.archive.state" in template
    assert "poll_delivery = delivery_progress.poll_after_ms is not none" in template
    assert "{% if poll_delivery %}" in template
