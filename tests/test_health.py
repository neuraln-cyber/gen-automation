from fastapi.testclient import TestClient


def test_live_and_ready_health(client: TestClient) -> None:
    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert live.headers["cache-control"] == "no-store"
    assert live.headers["cross-origin-opener-policy"] == "same-origin"
    assert live.headers["cross-origin-resource-policy"] == "same-origin"
    assert "default-src 'self'" in live.headers["content-security-policy"]
    assert "script-src 'none'" in live.headers["content-security-policy"]
    assert live.headers["permissions-policy"] == (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    assert live.headers["x-content-type-options"] == "nosniff"
    assert live.headers["x-frame-options"] == "DENY"
    assert live.headers["referrer-policy"] == "no-referrer"
    assert live.headers["x-robots-tag"] == "noindex, nofollow, noimageindex"
    assert "strict-transport-security" not in live.headers
