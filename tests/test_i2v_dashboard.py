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
    assert "/static/i2v.js" in response.text
    assert "/static/i2v.css" in response.text


def test_i2v_dashboard_navigation_has_a_single_clear_entry(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.text.count('href="/dashboard/animations"') == 1
    assert ">Animate</a>" in response.text
