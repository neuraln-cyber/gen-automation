from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient


class _InputCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            self.inputs.append(dict(attrs))


def test_i2v_dashboard_exposes_focused_generation_controls(client: TestClient) -> None:
    response = client.get("/dashboard/animations")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "data-i2v-studio" in response.text
    assert "Choose the first frame" in response.text
    assert 'data-source-tab="asset-id"' in response.text
    assert "Generated image asset UUID" in response.text
    assert 'name="asset_id"' in response.text
    assert 'role="status" aria-live="polite" data-asset-id-status' in response.text
    assert "Positive prompt" in response.text
    assert "Negative prompt" in response.text
    assert "Reviewed motion LoRAs" in response.text
    assert 'data-lora-profile-enabled="false"' in response.text
    assert "data-lora-effective" in response.text
    assert "Worker state has not been loaded yet" in response.text
    assert "Recent videos" in response.text
    assert 'name="width" value="768"' in response.text
    assert 'name="height" value="992"' in response.text
    assert '<option value="source" selected>Match source image</option>' in response.text
    assert 'name="loop_count" value="2" min="1" max="2"' in response.text
    assert "Looped delivery is capped at 25 seconds" in response.text
    assert 'name="match_source_aspect" checked' in response.text
    assert 'name="loop"> Smooth loop by playing forward then backward' in response.text
    assert 'data-hires-profile-enabled="true"' in response.text
    assert "/static/i2v.js" in response.text
    assert "/static/i2v.css" in response.text


def test_i2v_dashboard_navigation_has_a_single_clear_entry(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.text.count('href="/dashboard/animations"') == 1


def test_i2v_dashboard_pauses_enqueue_until_matching_worker_is_enabled(
    client: TestClient,
) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"i2v_hires_profile_enabled": False}
    )

    response = client.get("/dashboard/animations")

    assert response.status_code == 200
    assert 'data-hires-profile-enabled="false"' in response.text
    assert "Higher-resolution submissions are paused" in response.text
    assert ">Animate</a>" in response.text


def test_i2v_dashboard_dynamically_enforces_loop_duration() -> None:
    script = (Path(__file__).parents[1] / "src/gen_automation/static/i2v.js").read_text(
        encoding="utf-8"
    )

    assert "const maxLoopDurationSeconds = 25" in script
    assert 'const hiresProfileEnabled = root.dataset.hiresProfileEnabled === "true"' in script
    assert "enqueueButton.disabled = !hiresProfileEnabled" in script
    assert "Math.floor((maxLoopDurationSeconds * fps) / cycleFrames)" in script
    assert "loopField.max = String(Math.max(1, allowedCycles))" in script
    assert "loopField.value = String(allowedCycles)" in script
    assert "One ping-pong cycle exceeds the 25-second limit." in script


def test_i2v_dashboard_default_submit_controls_are_constraint_valid(
    client: TestClient,
) -> None:
    response = client.get("/dashboard/animations")
    collector = _InputCollector()
    collector.feed(response.text)

    cfg = next(item for item in collector.inputs if item.get("name") == "cfg")
    cfg_steps = (float(cfg["value"]) - float(cfg["min"])) / float(cfg["step"])
    assert cfg["value"] == "1.0"
    assert cfg_steps.is_integer()

    preset_name = next(item for item in collector.inputs if "data-preset-name" in item)
    assert "required" in preset_name
    assert "disabled" in preset_name
    assert 'value="cancel" formnovalidate' in response.text

    script = (Path(__file__).parents[1] / "src/gen_automation/static/i2v.js").read_text(
        encoding="utf-8"
    )
    assert "presetName.disabled = false" in script
    assert "presetName.disabled = true" in script
    assert 'presetDialog.addEventListener("close"' in script


def test_i2v_dashboard_uses_only_the_backend_reviewed_lora_catalog() -> None:
    root = Path(__file__).parents[1]
    template = (root / "src/gen_automation/templates/dashboard/i2v.html").read_text(
        encoding="utf-8"
    )
    script = (root / "src/gen_automation/static/i2v.js").read_text(encoding="utf-8")

    assert 'api("/loras")' in script
    assert "entry.catalog_id" in script
    assert "entry.recommended_initial_strength" in script
    assert "entry.trigger_words" in script
    assert "entry.creator_name" in script
    assert "entry.canonical_source_url" in script
    assert "entry.canonical_version_urls" in script
    assert "catalog_id: catalogId" in script
    assert "lora_high" not in script
    assert "lora_low" not in script
    assert ".safetensors" not in template
    assert "Your written prompt stays unchanged" in template
    assert "Where an automatic trigger exists" in template
    assert "selectedManualPromptConflicts" in script
    assert "choose exactly one" in script
    assert "catalogâ" not in template


def test_i2v_dashboard_fails_closed_without_silently_dropping_saved_loras() -> None:
    script = (Path(__file__).parents[1] / "src/gen_automation/static/i2v.js").read_text(
        encoding="utf-8"
    )

    assert "loraCatalogLoaded: false" in script
    assert "loraCatalogError: false" in script
    assert "if (loraWriteBlocked()) throw new Error(loraBlockMessage())" in script
    assert "settings.loras = [...state.loraSelections]" in script
    assert "state.loraCatalogError = true" in script
    assert "No settings will be silently removed" in script
    assert "Saved reviewed LoRA settings cannot be reused on the current profile" in script
    assert "Clear them before queueing or saving" in script


def test_i2v_dashboard_can_register_an_older_generation_by_exact_asset_id() -> None:
    root = Path(__file__).parents[1]
    template = (root / "src/gen_automation/templates/dashboard/i2v.html").read_text(
        encoding="utf-8"
    )
    script = (root / "src/gen_automation/static/i2v.js").read_text(encoding="utf-8")
    styles = (root / "src/gen_automation/static/i2v.css").read_text(encoding="utf-8")

    assert 'data-source-view="asset-id"' in template
    assert 'pattern="[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}' in template
    assert 'aria-describedby="i2v-asset-id-help i2v-asset-id-status"' in template
    assert 'assetIdForm.setAttribute("aria-busy", "true")' in script
    assert "assetIdInput.setCustomValidity(message)" in script
    assert 'api("/inputs/from-asset"' in script
    assert "inputId: input.input_id" in script
    assert "sourceWidth: input.width" in script
    assert "sourceHeight: input.height" in script
    assert "form.elements.match_source_aspect.checked = true" in script
    assert 'assetIdButton.textContent = "Selecting…"' in script
    assert ".i2v-asset-id-picker p.error" in styles
