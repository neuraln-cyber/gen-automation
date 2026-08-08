import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "delivery.html"
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"


def _form_tag(template: str, action_suffix: str) -> str:
    pattern = rf'<form\b[^>]*action="[^"]*{re.escape(action_suffix)}"[^>]*>'
    match = re.search(pattern, template, flags=re.DOTALL)
    assert match is not None
    return match.group(0)


def test_delivery_marks_only_external_effect_actions_for_recent_authentication() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "data-requires-recent-auth" in _form_tag(template, ":publication-guard")
    assert "guard_target_enabled" in _form_tag(template, ":publication-guard")
    assert "data-requires-recent-auth" in _form_tag(template, ":confirm-present")
    assert "data-requires-recent-auth" in _form_tag(template, ":confirm-absent")
    assert "data-requires-recent-auth" in _form_tag(template, ":prepare-patreon")
    assert "data-requires-recent-auth" in _form_tag(template, ":prepare-x")

    for low_risk_action in (
        ":prepare-outputs",
        ":prepare-archive",
        ":prepare-mega",
        ":upload-watermark",
        ":download",
    ):
        assert "data-requires-recent-auth" not in _form_tag(template, low_risk_action)


def test_delivery_reauthentication_is_inline_and_does_not_persist_credentials() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    flow = script.split("function initializeDeliveryReauthentication()", maxsplit=1)[1]
    flow = flow.split("function initializeAutomationPresets()", maxsplit=1)[0]

    assert "data-delivery-reauth-dialog" in template
    assert 'autocomplete="current-password"' in template
    assert 'autocomplete="one-time-code"' in template
    assert 'pattern="[0-9]{6}"' in template
    assert 'fetch("/api/v1/auth/reauthenticate"' in flow
    assert '"X-CSRF-Token": csrfToken' in flow
    assert "clearCredentials()" in flow
    assert 'password.value = ""' in flow
    assert 'totp.value = ""' in flow
    assert "localStorage" not in flow
    assert "sessionStorage" not in flow
    assert "pendingForm = form" in flow
    assert "window.location.assign(response.url)" in flow
