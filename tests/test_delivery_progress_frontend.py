from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"
DELIVERY_TEMPLATE = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "delivery.html"
REVIEW_TEMPLATE = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"


class _BrowserSuccessfulControls(HTMLParser):
    """Collect names that a browser includes in a non-clicked form submission."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"input", "select", "textarea"}:
            return
        values = dict(attrs)
        name = values.get("name")
        if not name or "disabled" in values:
            return
        control_type = (values.get("type") or "text").lower()
        if control_type in {"button", "submit", "reset", "image", "file"}:
            return
        if control_type in {"radio", "checkbox"} and "checked" not in values:
            return
        self.names.append(name)


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
    assert 'initialOutputState !== "failed" && outputState === "failed"' in handler
    assert 'archiveState === "ready"' in handler
    assert 'initialArchiveState !== "failed" && archiveState === "failed"' in handler
    assert "const fullOutputsReady = output.full_outputs_ready === true;" in handler
    assert "const succeeded = count(output.full_succeeded);" in handler
    assert "const totalJobs = count(output.full_total_jobs);" in handler
    assert "const failed = count(output.full_failed);" in handler
    assert "const active = count(output.full_active_jobs);" in handler
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
    assert "delivery:retry-full-outputs" in template
    assert "{% if not delivery.progress.full_planned %}" in template
    assert "delivery.progress.full_succeeded" in template
    assert "delivery.progress.full_total_jobs" in template
    assert "delivery.progress.full_active_jobs" in template
    assert template.count("delivery:retry-full-outputs") == 1
    assert "retry_output_idempotency_key" in template
    assert "retry_output_submission_id" in template
    assert "Retry clean full-set copies" in template
    assert "operator repair is required" in template
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
    assert template.count('delivery:replace-x-outputs"') == 1
    assert "Prepare watermarked X teasers" in template
    assert "Render replacement teasers" in template
    assert "delivery.progress.x_action_message" in template
    assert "data-x-lossless-png-too-large" in template
    assert "This creates only the selected teaser copies. It does not post them." in template
    assert template.count('name="watermark_position"') == 2
    assert 'name="watermark_placements"' in template
    assert "data-watermark-corner" in template
    for position in ("top_left", "top_right", "bottom_left", "bottom_right"):
        assert f"'{position}'" in template
    assert 'name="x_adult_content" value="true" checked' in template
    assert 'name="x_adult_content" value="false"' in template
    assert 'name="x_made_with_ai" value="true" checked' in template
    assert 'name="x_made_with_ai" value="false"' in template
    assert 'name="x_scheduled_local"' in template
    assert 'name="x_timezone"' in template
    assert "This setting is independent from the AI-generated disclosure." in template
    assert "Controls X's AI-generated label independently." in template
    assert "Cancel scheduled X post" in template
    assert "You can then enter a new" in template
    assert "Resolve unknown X result" in template
    assert "These controls record what" in template
    assert ":confirm-present" in template
    assert ":confirm-absent" in template
    assert 'name="attestation" value="{{ x_cancel_attestation }}"' in template
    assert 'name="attestation" value="{{ x_present_attestation }}"' in template
    assert 'name="attestation" value="{{ x_absent_attestation }}"' in template
    assert "delivery:prepare-destinations" not in template
    assert "Prepare Patreon and X" not in template
    assert 'data-delivery-card="zip"' in template
    assert 'data-delivery-card="{{ destination.key }}"' in template
    assert 'data-destination-state="{{ destination.state }}"' in template
    assert "This button does not start MEGA, X, or ZIP preparation." in template
    assert "This button does not start MEGA, Patreon, or ZIP preparation." in template


def test_x_watermark_composer_previews_each_image_and_preserves_individual_corners() -> None:
    template = DELIVERY_TEMPLATE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeWatermarkComposers()", maxsplit=1)[1]
    handler = handler.split("function initializeXPublishingControls()", maxsplit=1)[0]

    assert "Upload once and it stays securely available for future sets." in template
    assert "data-watermark-composer" in template
    assert "Image 1 of {{ x_previews|length }}" in template
    assert "data-watermark-thumbnail" in template
    assert "data-watermark-overlay" in template
    assert "data-watermark-placements" in template
    assert (
        "{% if not watermarks or not x_previews or "
        "(replacing_x_teasers and not replacement_allowed) %}disabled{% endif %}" in template
    )
    assert "x_output_downloads" in template
    assert "Download any teaser now" in template
    assert "Download teaser {{ loop.index }}" in template
    assert "JSON.stringify(placements)" in handler
    assert "placements[slide.dataset.assetId]" in handler
    assert "candidate.checked = false" in handler
    assert "overlay.dataset.position = input.value" in handler
    assert 'event.key === "ArrowLeft"' in handler
    assert 'event.key === "ArrowRight"' in handler
    assert "trimTransparentCanvas(previewUrl)" in handler
    assert "HTMLCanvasElement" in handler
    assert "URL.createObjectURL" not in handler
    assert "Math.min(width, height) * watermarkMargin / relativeScale" in handler
    assert "imageWidth * watermarkWidth / relativeScale" in handler
    assert "targetHeight > availableHeight" in handler
    assert "prepared.width * targetHeight / prepared.height" in handler
    assert "slides.forEach((slide) => paintWatermarkOverlay(slide, prepared))" in handler
    assert "scopedStorageKey(X_WATERMARK_ASSET_STORAGE_KEY)" in handler
    assert "data-watermark-current-asset-id" in template
    assert "current_positions_by_asset_id.get(" in template
    assert "preview.asset_id," in template
    assert "preview.asset_id|string" in template
    assert "Change watermark corners" in template
    assert "Render replacement teasers" in template
    assert "data-x-revision-pending" in template
    assert "current watermarked teasers above remain available" in template
    assert "Nothing is overwritten until the complete replacement is ready." in template
    assert "x_teaser_revision.can_replace" in template
    assert "x_teaser_revision.blocked_reason" in template
    assert "const currentAssetId = assetSelect.dataset.watermarkCurrentAssetId" in handler
    assert handler.index("if (hasCurrentAsset)") < handler.index("window.localStorage.getItem")


def test_x_watermark_corner_controls_do_not_leak_strict_form_fields() -> None:
    template = DELIVERY_TEMPLATE.read_text(encoding="utf-8")
    action = template.index("delivery:prepare-x-outputs")
    form_start = template.rindex("<form", 0, action)
    form_end = template.index("</form>", action) + len("</form>")
    form = template[form_start:form_end]
    controls = _BrowserSuccessfulControls()
    controls.feed(form)

    assert set(controls.names) == {
        "csrf_token",
        "idempotency_key",
        "submission_id",
        "watermark_asset_id",
        "watermark_position",
        "watermark_placements",
    }
    assert "watermark_corner_" not in form
    assert 'name="watermark_placements"' in form
    assert 'name="watermark_position" value="bottom_right"' in form
    assert "x_replace_output_idempotency_key" in form
    assert "x_replace_output_submission_id" in form


def test_x_composer_tracks_caption_timezone_and_optional_schedule() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeXPublishingControls()", maxsplit=1)[1]
    handler = handler.split("const experimentFormPresent", maxsplit=1)[0]

    assert 'form[action$="delivery:prepare-x"]' in handler
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in handler
    assert "timezone.value = browserTimezone" in handler
    assert "new TextEncoder().encode(caption.value).length" in handler
    assert 'byteLength > 280 ? "Keep the X caption within 280 UTF-8 bytes."' in handler
    assert "Math.ceil((Date.now() + 60_000) / 60_000) * 60_000" in handler
    assert "schedule.min = localMinimum.toISOString().slice(0, 16)" in handler
    assert "schedule.max = localMaximum.toISOString().slice(0, 16)" in handler
    assert 'schedule.addEventListener("input", updateSchedule)' in handler
    assert '"Approve and schedule X post"' in handler


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
