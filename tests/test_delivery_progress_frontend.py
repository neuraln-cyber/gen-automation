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
    assert 'initialArchiveState !== "failed" && archiveState === "failed"' in handler
    assert "const fullOutputsReady = output.full_outputs_ready === true;" in handler
    assert 'archiveState === "not_started" && fullOutputsReady' not in handler
    assert "payload.mega.active === true" in handler
    assert "renderMega(payload.mega)" in handler
    assert "data-mega-progress" in handler
    assert "renderPublicationDestination" in handler
    assert "payload.patreon?.active === true" in handler
    assert "payload.x?.active === true" in handler
    assert 'if (state === "not_started")' in handler
    assert "Nothing is running until you choose Prepare clean full set." in handler
    assert "Automatic preparation will start" not in handler


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

    assert "Prepare clean full-set copies" in template
    assert "metadata-free copy for every accepted image" in template
    assert "does not watermark teasers, upload, post" in template
    assert "Creating ZIP parts in the background" in template
    assert "This ZIP is independent of publishing" in template
    assert "before or while" in template
    assert "independent of publishing and the extracted MEGA upload" in template
    assert "delivery:prepare-archive" in template
    assert "Prepare ZIP download" in template
    assert "Retry ZIP preparation" in template
    assert "publication runtime or switch is stopped" not in template
    assert "Prepare destinations once to create" not in template
    assert "data-delivery-output-progress" in template
    assert 'aria-live="polite"' in template
    assert '{% elif output_state == "failed" %}' in template
    assert '{% elif output_state == "stalled" %}' in template
    assert "data-delivery-auto-refresh" not in template
    assert 'data-delivery-progress-url="/dashboard/review-tasks/' in template
    assert "data-delivery-progress" in template
    assert "Prepare or download finished set" in review_template
    assert "ordinary full-resolution image files" in template
    assert "data-mega-progress" in template
    assert "data-mega-remote-path" in template


def test_delivery_template_exposes_four_independent_target_actions() -> None:
    template = DELIVERY_TEMPLATE.read_text(encoding="utf-8")

    assert template.count("delivery:prepare-archive") == 1
    assert template.count("delivery:prepare-mega") == 1
    assert template.count("delivery:prepare-patreon") == 1
    assert template.count('delivery:prepare-x"') == 1
    assert template.count('delivery:prepare-x-outputs"') == 1
    assert "Prepare watermarked X teasers" in template
    assert "This creates only the selected teaser copies. It does not post them." in template
    assert "delivery:prepare-destinations" not in template
    assert "Prepare Patreon and X" not in template
    assert 'data-delivery-card="zip"' in template
    assert 'data-delivery-card="{{ destination.key }}"' in template
    assert 'data-destination-state="{{ destination.state }}"' in template
    assert "This button does not start MEGA, X, or ZIP preparation." in template
    assert "This button does not start MEGA, Patreon, or ZIP preparation." in template


def test_ready_archive_is_excluded_from_delivery_polling_condition() -> None:
    template = DELIVERY_TEMPLATE.read_text(encoding="utf-8")
    handler = _delivery_progress_handler()

    assert "archive_state = delivery_progress.archive.state" in template
    assert "poll_delivery" not in template
    assert 'archiveState === "preparing"' in handler
    assert (
        'archiveState === "ready"'
        not in handler.split("polling:", maxsplit=1)[1].split("delay:", maxsplit=1)[0]
    )
