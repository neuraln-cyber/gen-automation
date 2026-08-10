from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "gen_automation" / "templates"
TEMPLATE = TEMPLATES / "dashboard" / "loras.html"
BASE = TEMPLATES / "dashboard" / "base.html"
NEW_SET = TEMPLATES / "dashboard" / "new_set.html"
EXPERIMENT = TEMPLATES / "dashboard" / "experiment_new.html"
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "lora_manager.js"
STYLES = ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css"


def _render_manager() -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html",)),
    )
    template = environment.get_template("dashboard/loras.html")
    principal = SimpleNamespace(
        user_id="owner-id",
        display_name="Owner",
        role=SimpleNamespace(value="owner"),
    )
    entry = {
        "id": "artifact-id",
        "name": "Style </h3><script>unsafe()</script>",
        "status": "approved",
        "readiness_status": "ready",
        "size_bytes": 1048576,
        "sha256": "a" * 64,
        "source_url": "https://civitai.com/models/123",
        "source_label": "Civitai",
        "version_name": "Version 1",
        "trigger_words": ["one", "two"],
        "updated_at": "2026-08-09T12:00:00Z",
        "can_retire": True,
        "can_restore": False,
    }
    operation = {
        "id": "import-id",
        "name": "Style import",
        "source_kind": "civitai",
        "status": "verifying",
        "bytes_transferred": 524288,
        "total_bytes": 1048576,
        "error": None,
        "retryable": False,
        "cancellable": True,
        "updated_at": "2026-08-09T12:00:00Z",
    }
    return template.render(
        request=SimpleNamespace(url=SimpleNamespace(path="/dashboard/loras")),
        page_title="LoRA manager",
        principal=principal,
        csrf_token=principal.user_id,
        can_manage=True,
        entries=[entry],
        imports=[operation],
    )


def test_lora_manager_renders_one_large_accessible_row_per_artifact() -> None:
    markup = _render_manager()

    assert markup.count("data-lora-entry\n") == 1
    assert 'data-lora-id="artifact-id"' in markup
    assert 'data-lora-status="ready"' in markup
    assert "Style &lt;/h3&gt;&lt;script&gt;unsafe()&lt;/script&gt;" in markup
    assert "<script>unsafe()</script>" not in markup
    assert 'type="file"' in markup
    assert 'accept=".safetensors,application/octet-stream"' in markup
    assert 'aria-label="LoRA upload progress"' in markup
    assert "<progress" in markup
    assert "data-lora-delete-dialog" in markup
    assert 'aria-describedby="lora-delete-description"' in markup
    assert "Delete the stored file when safe" in markup
    assert "Stays off during onboarding" in markup
    assert 'src="/static/lora_manager.js"' in markup


def test_lora_manager_template_keeps_rights_and_provider_secrets_server_side() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "commercial_use_attested" in source
    assert "adult_use_attested" in source
    assert "NSFW classification" in source
    assert "server-side Civitai credential" in source
    assert "download_url" not in source
    assert "signed URL" not in source
    assert 'rel="noreferrer noopener"' in source
    assert 'data-list-url="/api/v1/loras"' in source
    assert 'data-resolve-url="/api/v1/loras/civitai:resolve"' in source
    assert 'name="target_filename"' in source
    assert 'pattern="[A-Za-z0-9][A-Za-z0-9._ -]*\\.safetensors"' in source
    assert 'name="trigger_words"' in source
    assert "Nothing is selected automatically" in source
    assert "data-lora-version-select" in source
    assert 'pattern="https://(www\\.)?civitai\\.(com|red)/.*"' in source
    assert "Civitai.com and Civitai.red links are accepted" in source


def test_lora_manager_javascript_uploads_directly_and_polls_durable_progress() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    fields = "Object.entries(fields).forEach(([key, value]) => {"
    file_append = 'body.append("file", file, file.name);'
    assert fields in script
    assert file_append in script
    assert script.index(fields) < script.index(file_append)
    assert "new XMLHttpRequest()" in script
    assert "request.withCredentials = false" in script
    assert 'headers["X-CSRF-Token"] = csrfToken' in script
    assert 'headers["Idempotency-Key"] = idempotencyKey' in script
    assert 'const stableMutationKey = (owner, fingerprint, scope = "default") =>' in script
    assert '"manual-create"' in script
    assert '"manual-complete"' in script
    assert "pendingManualCompletions.set(form, completion)" in script
    assert "pendingManualCompletions.delete(form)" in script
    assert "crypto.randomUUID()" in script
    assert "commercial_image_allowed === true" in script
    assert "adult_use_requires_attestation === true" in script
    assert "commercial_use_attested" in script
    assert "adult_use_attested" in script
    assert "canonical_source_url" in script
    assert "target_filename" in script
    assert 'targetFilename.value = `${sanitizedStem || "uploaded-lora"}.safetensors`' in script
    assert "normalizedTriggerWords" in script
    assert "normalizeCivitaiVersions" in script
    assert "requestBody.version_id = Number(selectedVersionId)" in script
    assert "No version was selected for you" in script
    assert 'request.getResponseHeader("x-amz-version-id")' in script
    assert 'request.getResponseHeader("ETag")' in script
    assert "object_version_id: uploaded.objectVersionId" in script
    assert "object_etag: uploaded.objectEtag" in script
    assert "download_url" not in script
    assert ".innerHTML" not in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "Math.min(pollDelay * 2, 15000)" in script
    assert "navigator.clipboard.writeText" in script
    assert "dialog.showModal()" in script
    assert "returnFocus.focus()" in script
    assert '["civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red"]' in script
    assert 'detail === "recent authentication required"' in script
    assert "Your recent sign-in expired. Sign in again, then retry this change." in script
    assert "Your session expired. Sign in again, then retry." in script


def test_lora_manager_styles_are_scoped_and_collapse_without_horizontal_table() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert "/* LoRA manager: direct onboarding, durable progress, and safe retirement. */" in styles
    assert ".lora-library-card {" in styles
    assert "grid-template-columns: minmax(12rem, 1fr) minmax(24rem, 2fr)" in styles
    assert "@media (max-width: 850px)" in styles
    assert ".lora-library-card { grid-template-columns: minmax(0, 1fr);" in styles
    assert ".lora-manager [hidden] { display: none !important; }" in styles
    assert ".lora-delete-dialog::backdrop" in styles
    assert "min-height: 2.75rem" in styles


def test_dashboard_and_both_generation_surfaces_link_to_lora_manager() -> None:
    base = BASE.read_text(encoding="utf-8")
    new_set = NEW_SET.read_text(encoding="utf-8")
    experiment = EXPERIMENT.read_text(encoding="utf-8")

    assert 'href="/dashboard/loras"' in base
    assert 'request.url.path.startswith("/dashboard/loras")' in base
    assert "{% block page_scripts %}{% endblock %}" in base
    assert 'class="secondary-button lora-manage-link" href="/dashboard/loras"' in new_set
    assert "data-lora-readiness" in new_set
    assert 'class="secondary-button lora-manage-link" href="/dashboard/loras"' in experiment
    assert "worker restart is loaded into the idle warm worker before its next batch" in experiment
