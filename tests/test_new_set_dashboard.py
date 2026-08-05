import re
from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.db.models import GenerationJob, Project, Release, ReleaseVersion
from gen_automation.domain.enums import ReleasePhase
from gen_automation.domain.wildcards import WildcardCreate
from gen_automation.services.new_sets import list_new_set_options
from gen_automation.services.wildcards import create_wildcard_library
from tests.factories import seed_release_approvals, valid_release_payload


def _hidden_value(page: str, name: str) -> str:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]+)">', page)
    assert match is not None
    return match.group(1)


def test_dashboard_javascript_is_packaged_and_served(client: TestClient) -> None:
    response = client.get("/static/dashboard.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "initializeAutomationBuilder" in response.text
    assert "initializeLoraPicker" in response.text
    assert "syncCanonicalSlots" in response.text
    assert "gen-automation:generation-presets:v1" in response.text
    assert "initializeLiveGeneratedAssets" in response.text
    assert "data-live-assets-grid" in response.text
    assert "const assets = new Map()" in response.text
    assert 'url.searchParams.set("cursor", cursor)' in response.text
    assert 'image.loading = "lazy"' in response.text
    assert 'new CustomEvent("gen-automation:assets-updated"' in response.text
    assert "reorderCards" in response.text
    assert 'createNode("span", "live-generation-asset-batch"' in response.text
    assert "payload.has_more === true" in response.text
    assert "immediatePages < 20" in response.text
    assert "window.setTimeout(refresh, 0)" in response.text
    assert "url.origin === window.location.origin" in response.text
    assert "`/dashboard/assets/${assetId.toLowerCase()}/`" in response.text
    assert "const initializedGenerationDetails = new WeakSet()" in response.text
    assert "bindGenerationDetails(root || document)" in response.text


def test_new_set_builder_frontend_keeps_batch_edits_safe_and_actionable(
    client: TestClient,
) -> None:
    script = client.get("/static/dashboard.js")
    stylesheet = client.get("/static/dashboard_ux.css")

    assert script.status_code == 200
    assert "const maximumProviderJobs = 10_000" in script.text
    assert "const tooManyJobs = totalJobs > maximumProviderJobs" in script.text
    assert "|| tooManyJobs" in script.text
    assert 'form.addEventListener("invalid"' in script.text
    assert 'row.classList.remove("is-collapsed")' in script.text
    assert "row.contains(lastPrompt)" in script.text
    assert "const suffix = end < target.value.length" in script.text
    assert "`${prefix}${token}${suffix}`" in script.text
    assert "const wildcardPattern = /__([a-z0-9]+(?:[._/-][a-z0-9]+)*)__/g;" in script.text
    assert "const wildcardPattern = /__([a-z0-9]+(?:[._/-][a-z0-9]+)*)__/gi;" not in script.text
    assert "if (prompt.value === previousDefaultPrompt)" in script.text
    assert "AUTOMATION_DRAFT_STORAGE_KEY" in script.text
    assert "restoreAutomationDraft" in script.text
    assert "clearAutomationDraftAfterQueue" in script.text
    assert "knownWildcards" in script.text
    assert "Unknown wildcard:" in script.text
    assert "parseBatchSequence" in script.text
    assert "promptForSequenceWildcard" in script.text
    assert "lines.length > 50" in script.text
    assert "targetFollowsQueue" in script.text
    assert "pairedWorkflow" in script.text
    assert 'event.target.closest("details")' in script.text

    assert stylesheet.status_code == 200
    assert ".batch-card-actions button { min-width: 2.75rem; min-height: 2.75rem; }" in (
        stylesheet.text
    )


def test_new_set_form_freezes_and_queues_an_idempotent_plan(client: TestClient) -> None:
    payload = deepcopy(valid_release_payload())
    specification = payload["specification"]
    assert isinstance(specification, dict)
    specification["loras"] = [
        {
            "name": "Portrait Style",
            "source_url": "https://example.com/portrait-style",
            "storage_key": "models/portrait-style.safetensors",
            "sha256": "b" * 64,
            "license_url": "https://example.com/portrait-style-license",
            "commercial_use_approved": True,
            "adult_use_approved": True,
            "weight": 0.75,
        },
        {
            "name": "Texture Style",
            "source_url": "https://example.com/texture-style",
            "storage_key": "models/texture-style.safetensors",
            "sha256": "c" * 64,
            "license_url": "https://example.com/texture-style-license",
            "commercial_use_approved": True,
            "adult_use_approved": True,
            "weight": 0.4,
        },
    ]
    database = client.app.state.database
    assert client.portal is not None

    async def seed_and_read_options() -> object:
        async with database.sessions() as session:
            await seed_release_approvals(session, payload)
            await create_wildcard_library(
                session,
                command=WildcardCreate(
                    name="poses",
                    entries=["standing portrait", "seated portrait"],
                ),
                actor="fixture-owner",
            )
            return await list_new_set_options(session)

    options = client.portal.call(seed_and_read_options)
    assert len(options.subjects) == 1
    assert len(options.checkpoints) == 1
    assert len(options.loras) == 2
    assert len(options.workflows) == 1

    page = client.get("/dashboard/new-set")
    assert page.status_code == 200
    assert "Approved Adult Character" in page.text
    assert "Illustrious" in page.text
    assert "Portrait Style" in page.text
    assert "production-v1" in page.text
    assert "__poses__" in page.text
    assert 'name="width" value="1144"' in page.text
    assert 'name="height" value="1480"' in page.text
    assert 'name="cfg" value="6.0"' in page.text
    assert 'name="steps" value="30"' in page.text
    assert 'name="scheduler" value="karras"' in page.text
    assert 'name="clip_skip" value="2"' in page.text
    assert 'name="detailer_prompt"' in page.text
    assert ">sexy, expressive, </textarea>" in page.text
    assert 'name="detailer_negative_prompt"' in page.text
    assert ">closed eyes, </textarea>" in page.text
    assert 'name="detailer_denoise" value="0.4"' in page.text
    assert 'name="detailer_bbox_threshold" value="0.3"' in page.text
    assert 'name="detailer_bbox_dilation" value="4"' in page.text
    assert 'name="detailer_feather" value="4"' in page.text
    assert 'name="detailer_max_size" value="1536"' in page.text
    assert 'name="detailer_bbox_crop_factor" value="1.5"' in page.text
    assert 'name="outputs_per_job" value="4"' in page.text
    assert re.search(r'name="outputs_per_job"[^>]+max="25"', page.text)
    assert 'name="planned_job_count" value="1"' in page.text
    assert 'name="batch_plan"' in page.text
    assert 'id="batch-row-template"' in page.text
    batch_template = page.text.split('<template id="batch-row-template">', maxsplit=1)[1]
    overrides = batch_template.index('<details class="batch-overrides">')
    negative_editor = batch_template.index('data-batch-field="negative_prompt"')
    assert negative_editor < overrides
    assert 'aria-label="Positive prompt"' in batch_template
    assert 'aria-label="Negative prompt"' in batch_template
    assert 'data-batch-wildcard-target="prompt"' in batch_template
    assert 'data-batch-wildcard-target="negative_prompt"' in batch_template
    assert "Starts with the shared negative prompt" in batch_template
    assert "data-batch-sequence-input" in page.text
    assert "data-batch-sequence-apply" in page.text
    assert "50 sfw" in page.text
    assert "100 nnsfw" in page.text
    assert 'id="lora-selection-template"' in page.text
    assert "data-lora-picker" in page.text
    assert "data-lora-search" in page.text
    assert "data-lora-catalog" in page.text
    assert "data-lora-selected" in page.text
    assert "data-automation-presets" in page.text
    assert "data-automation-preset-select" in page.text
    assert "data-automation-preset-load" in page.text
    assert "data-automation-preset-save" in page.text
    assert "data-automation-preset-delete" in page.text
    assert "data-automation-preset-export" in page.text
    assert "data-automation-preset-import" in page.text
    assert "data-automation-draft-status" in page.text
    assert "data-automation-storage-scope" in page.text
    assert "data-match-queue-target" in page.text
    assert "data-random-seed" in page.text
    assert "Saved on this device" in page.text
    assert page.text.count("data-lora-option\n") == 2
    assert page.text.count("data-lora-native-slot>") == 8
    for slot in range(1, 9):
        assert page.text.count(f'name="lora_{slot}_id"') == 1
        assert page.text.count(f'name="lora_{slot}_weight"') == 1
    assert 'class="mobile-queue-dock"' in page.text
    assert re.search(r'name="desired_accepted_count"\s+value="4"', page.text)
    assert re.search(r'name="desired_accepted_count"[\s\S]{0,160}max="500"', page.text)
    form = {
        "csrf_token": _hidden_value(page.text, "csrf_token"),
        "submission_id": _hidden_value(page.text, "submission_id"),
        "idempotency_key": _hidden_value(page.text, "idempotency_key"),
        "slug": "character-july-set",
        "title": "Character July Set",
        "subject_id": str(options.subjects[0].approval_id),
        "checkpoint_id": str(options.checkpoints[0].approval_id),
        "workflow_id": str(options.workflows[0].approval_id),
        "lora_1_id": str(options.loras[0].approval_id),
        "lora_1_weight": "0.75",
        "lora_2_id": str(options.loras[1].approval_id),
        "lora_2_weight": "0.4",
        "lora_3_id": "",
        "lora_3_weight": "",
        "lora_4_id": "",
        "lora_4_weight": "",
        "lora_5_id": "",
        "lora_5_weight": "",
        "lora_6_id": "",
        "lora_6_weight": "",
        "lora_7_id": "",
        "lora_7_weight": "",
        "lora_8_id": "",
        "lora_8_weight": "",
        "prompt": "__poses__, detailed portrait",
        "negative_prompt": "low quality",
        "detailer_prompt": "expressive face",
        "detailer_negative_prompt": "closed eyes",
        "seed": "1234",
        "width": "1024",
        "height": "1024",
        "cfg": "6.5",
        "steps": "30",
        "sampler": "euler",
        "scheduler": "normal",
        "clip_skip": "2",
        "outputs_per_job": "4",
        "hires_scale": "1.75",
        "hires_denoise": "0.4",
        "hires_upscale_method": "bicubic",
        "detailer_guide_size": "768",
        "detailer_max_size": "1280",
        "detailer_denoise": "0.3",
        "detailer_bbox_threshold": "0.55",
        "detailer_bbox_dilation": "12",
        "detailer_bbox_crop_factor": "2.8",
        "detailer_feather": "4",
        "planned_job_count": "3",
        "desired_accepted_count": "10",
    }

    first = client.post("/dashboard/new-set", data=form, follow_redirects=False)
    replay = client.post("/dashboard/new-set", data=form, follow_redirects=False)

    assert first.status_code == 303
    assert replay.status_code == 303
    assert first.headers["location"] == replay.headers["location"]
    assert "/status?draft=" in first.headers["location"]

    async def read_created_state() -> tuple[
        list[Project], list[Release], ReleaseVersion, list[GenerationJob]
    ]:
        async with database.sessions() as session:
            projects = list((await session.scalars(select(Project))).all())
            releases = list((await session.scalars(select(Release))).all())
            version = await session.scalar(select(ReleaseVersion))
            assert version is not None
            jobs = list(
                (
                    await session.scalars(select(GenerationJob).order_by(GenerationJob.logical_key))
                ).all()
            )
            return projects, releases, version, jobs

    projects, releases, version, jobs = client.portal.call(read_created_state)
    assert [project.slug for project in projects] == ["main"]
    assert len(releases) == 1
    assert releases[0].phase == ReleasePhase.READY
    assert len(jobs) == 3
    assert version.specification["generation"]["cfg"] == 6.5
    assert version.specification["generation"]["hires_scale"] == 1.75
    assert version.specification["generation"]["hires_upscale_method"] == "bicubic"
    assert version.specification["generation"]["detailer_max_size"] == 1280
    assert version.specification["generation"]["detailer_prompt"] == "expressive face"
    assert version.specification["generation"]["detailer_negative_prompt"] == "closed eyes"
    assert version.specification["generation"]["clip_skip"] == 2
    assert version.specification["generation"]["detailer_feather"] == 4
    assert [(lora["name"], lora["weight"]) for lora in version.specification["loras"]] == [
        ("Portrait Style", 0.75),
        ("Texture Style", 0.4),
    ]
    assert version.specification["wildcard_versions"][0]["name"] == "poses"
    assert {job.parameters["generation"]["prompt"].split(",")[0] for job in jobs} <= {
        "standing portrait",
        "seated portrait",
    }

    status_page = client.get(first.headers["location"])
    assert status_page.status_code == 200
    assert "Character July Set" in status_page.text
    assert "Generation jobs" in status_page.text
    assert ">3<" in status_page.text
    assert "queued" in status_page.text
    assert "0 / 12 images" in status_page.text
    assert "data-generation-progress" in status_page.text
    assert f'data-submitted-draft-id="{form["submission_id"]}"' in status_page.text
    assert "data-progress-bar" in status_page.text
    assert "data-live-generated-assets" in status_page.text
    assert f'data-live-assets-url="/dashboard/releases/{releases[0].id}/generated-assets"' in (
        status_page.text
    )
    assert 'data-live-assets-expected="12"' in status_page.text
    assert "data-live-assets-grid" in status_page.text
    assert "Verified raw masters appear here" in status_page.text
    assert "automatically in their planned" in status_page.text
    assert "queue order" in status_page.text

    progress = client.get(first.headers["location"].replace("/status", "/progress"))
    assert progress.status_code == 200
    assert "no-store" in progress.headers["cache-control"]
    assert progress.json() == {
        "schema_version": 1,
        "release_id": str(releases[0].id),
        "phase": "ready",
        "health": "healthy",
        "stage": {
            "key": "queued",
            "step": 1,
            "step_count": 5,
            "label": "Queued",
            "detail": "Generation jobs are queued for cloud GPU capacity.",
        },
        "images": {"generated": 0, "expected": 12, "percent": 0.0},
        "jobs": {
            "completed": 0,
            "total": 3,
            "active": 0,
            "failed": 0,
            "states": {"queued": 3},
        },
        "scoring": None,
        "error": None,
        "ready_for_review": False,
        "next_url": None,
        "poll_after_ms": 3000,
    }
