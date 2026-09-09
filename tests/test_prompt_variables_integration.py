import json
import re
from copy import deepcopy
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.db.models import GenerationJob, Release, ReleaseVersion
from tests.test_experiment_dashboard import _automation_form, _seed_options


def _database_snapshot(client: TestClient) -> tuple[list[str], list[dict], list[dict]]:
    assert client.portal is not None

    async def read() -> tuple[list[str], list[dict], list[dict]]:
        async with client.app.state.database.sessions() as session:
            releases = list((await session.scalars(select(Release).order_by(Release.slug))).all())
            versions = list((await session.scalars(select(ReleaseVersion))).all())
            jobs = list(
                (
                    await session.scalars(select(GenerationJob).order_by(GenerationJob.logical_key))
                ).all()
            )
            return (
                [release.slug for release in releases],
                [deepcopy(version.specification) for version in versions],
                [deepcopy(job.parameters) for job in jobs],
            )

    return client.portal.call(read)


def _textarea_value(page: str, name: str) -> str:
    match = re.search(rf'<textarea\b[^>]*\bname="{name}"[^>]*>(.*?)</textarea>', page, re.S)
    assert match is not None
    return unescape(match.group(1))


@pytest.mark.parametrize("experiment", [False, True], ids=["new-set", "experiment-lab"])
def test_variables_expand_every_batch_and_freeze_nested_wildcards(
    client: TestClient, experiment: bool
) -> None:
    options = _seed_options(client)
    assert (
        client.post(
            "/api/v1/wildcards", json={"name": "locations", "entries": ["beach", "rooftop"]}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/wildcards",
            json={"name": "backgrounds", "entries": ["daylight, __locations__"]},
        ).status_code
        == 201
    )
    get_url = "/dashboard/experiments/new" if experiment else "/dashboard/new-set"
    post_url = "/dashboard/new-set?mode=experiment" if experiment else "/dashboard/new-set"
    page = client.get(get_url)
    assert page.status_code == 200
    assert 'name="prompt_variables"' in page.text
    assert "data-prompt-variables" in page.text
    assert "Saved with presets" in page.text

    variables = {
        "character": "original character",
        "outfit": "blue jacket",
        "setting": "__backgrounds__",
        "quality": "blur",
        "face": "gentle smile",
        "face negative": "closed eyes",
    }
    batches = [
        {"name": "Inherited", "image_count": 3, "prompt": "*character*, *outfit*, *setting*"},
        {
            "name": "Overrides",
            "image_count": 2,
            "prompt": "*character*, seated, *setting*",
            "negative_prompt": "*quality*, harsh shadows",
            "detailer_prompt": "*face*, *setting*",
            "detailer_negative_prompt": "*face negative*, noise",
        },
    ]
    form = _automation_form(
        page.text, options, slug="variables-set", title="Variables set", batches=batches
    )
    form.update(
        prompt="*character*, *setting*",
        negative_prompt="*quality*",
        detailer_prompt="*face*",
        detailer_negative_prompt="*face negative*",
        prompt_variables=json.dumps(variables),
        seed="100",
    )
    original_form = deepcopy(form)
    response = client.post(post_url, data=form, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert form == original_form
    before = _database_snapshot(client)
    releases, specifications, jobs = before
    assert releases == ["variables-set"]
    assert len(specifications) == 1
    assert len(jobs) == 2
    specification = specifications[0]
    frozen = specification["wildcard_versions"]
    assert {entry["name"] for entry in frozen} == {"backgrounds", "locations"}
    assert {entry["version_no"] for entry in frozen} == {1}
    generations = [batch["generation"] for batch in specification["generation_batches"]]
    assert generations[0]["prompt"] == "original character, blue jacket, __backgrounds__"
    assert generations[0]["negative_prompt"] == "blur"
    assert generations[0]["detailer_prompt"] == "gentle smile"
    assert generations[0]["detailer_negative_prompt"] == "closed eyes"
    assert generations[1]["negative_prompt"] == "blur, harsh shadows"
    assert generations[1]["detailer_prompt"] == "gentle smile, __backgrounds__"
    assert generations[1]["detailer_negative_prompt"] == "closed eyes, noise"

    outputs = []
    for job in jobs:
        assert len(job["output_generations"]) == len(job["output_prompt_resolutions"])
        for generation, evidence in zip(
            job["output_generations"], job["output_prompt_resolutions"], strict=True
        ):
            outputs.append(generation)
            assert generation["outputs_per_job"] == 1
            assert "*character*" not in generation["prompt"]
            assert "__" not in generation["prompt"]
            assert "daylight" in generation["prompt"]
            assert any(location in generation["prompt"] for location in ("beach", "rooftop"))
            assert "*" not in evidence["source_prompt"]
            assert "__backgrounds__" in evidence["source_prompt"]
            assert {entry["name"] for entry in evidence["wildcard_versions"]} == {
                "locations",
                "backgrounds",
            }
    assert len(outputs) == 5
    assert sorted(generation["seed"] for generation in outputs) == [100, 101, 102, 103, 104]

    # Library edits and an exact browser retry cannot mutate the frozen job evidence.
    assert (
        client.put(
            "/api/v1/wildcards/locations",
            json={"expected_version_no": 1, "entries": ["snowy forest"]},
        ).status_code
        == 200
    )
    replay = client.post(post_url, data=form, follow_redirects=False)
    assert replay.status_code == 303, replay.text
    assert replay.headers["location"] == response.headers["location"]
    assert _database_snapshot(client) == before

    changed = deepcopy(form)
    changed["prompt_variables"] = json.dumps({**variables, "character": "different character"})
    conflict = client.post(post_url, data=changed, follow_redirects=False)
    assert conflict.status_code == 409
    assert _database_snapshot(client) == before


@pytest.mark.parametrize("experiment", [False, True], ids=["new-set", "experiment-lab"])
@pytest.mark.parametrize(
    ("variables", "prompt", "expected_status", "message"),
    [
        ({"character": "portrait"}, "*missing*", 409, "unknown prompt variable"),
        ({"character": "portrait"}, "*Character*", 409, "unknown prompt variable"),
        ({"a": "*b*", "b": "*a*"}, "*a*", 409, "prompt variable cycle"),
        ({"a": "x" * 10_001}, "*a*, *a*", 409, "expanded prompt exceeds"),
        ({"a": "x" * 20_001}, "*a*", 422, "exceeds 20000 characters"),
    ],
    ids=["undefined", "case-sensitive", "cycle", "expanded-too-large", "value-too-large"],
)
def test_invalid_variables_reject_atomically_and_redisplay_raw_templates(
    client: TestClient,
    experiment: bool,
    variables: dict[str, str],
    prompt: str,
    expected_status: int,
    message: str,
) -> None:
    options = _seed_options(client)
    get_url = "/dashboard/experiments/new" if experiment else "/dashboard/new-set"
    post_url = "/dashboard/new-set?mode=experiment" if experiment else "/dashboard/new-set"
    page = client.get(get_url)
    batches = [{"name": "Raw template", "image_count": 1, "prompt": prompt}]
    form = _automation_form(
        page.text, options, slug="invalid-variables", title="Invalid variables", batches=batches
    )
    form["prompt_variables"] = json.dumps(variables)
    form["prompt"] = prompt
    response = client.post(post_url, data=form, follow_redirects=False)
    assert response.status_code == expected_status, response.text
    assert message in unescape(response.text).lower()
    if prompt == "*Character*":
        assert "unknown prompt variable *Character*" in unescape(response.text)
    assert json.loads(_textarea_value(response.text, "prompt_variables")) == variables
    assert json.loads(_textarea_value(response.text, "batch_plan")) == batches
    assert _textarea_value(response.text, "prompt") == prompt
    assert _database_snapshot(client) == ([], [], [])


@pytest.mark.parametrize("experiment", [False, True], ids=["new-set", "experiment-lab"])
def test_legacy_form_without_variables_remains_idempotent(
    client: TestClient, experiment: bool
) -> None:
    options = _seed_options(client)
    get_url = "/dashboard/experiments/new" if experiment else "/dashboard/new-set"
    post_url = "/dashboard/new-set?mode=experiment" if experiment else "/dashboard/new-set"
    page = client.get(get_url)
    form = _automation_form(
        page.text,
        options,
        slug="legacy-no-variables",
        title="Legacy no variables",
        batches=[{"name": "Portrait", "image_count": 1, "prompt": "original portrait"}],
    )
    assert "prompt_variables" not in form
    first = client.post(post_url, data=form, follow_redirects=False)
    assert first.status_code == 303, first.text
    before = _database_snapshot(client)
    replay = client.post(post_url, data=form, follow_redirects=False)
    assert replay.status_code == 303, replay.text
    assert replay.headers["location"] == first.headers["location"]
    assert _database_snapshot(client) == before
    assert len(before[2]) == 1
    assert before[2][0]["generation"]["prompt"] == "original portrait"
