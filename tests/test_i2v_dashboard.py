from pathlib import Path

from fastapi.testclient import TestClient


def test_i2v_dashboard_exposes_focused_generation_controls(client: TestClient) -> None:
    response = client.get("/dashboard/animations")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "data-i2v-studio" in response.text
    assert "Choose the first frame" in response.text
    assert "Positive prompt" in response.text
    assert "Negative prompt" in response.text
    assert "Paired motion LoRAs" in response.text
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
