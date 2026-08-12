import ast
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "mirror-i2v-models.py"
SOURCES = REPOSITORY_ROOT / "i2v-models" / "dasiwa-wan22-i2v-v1.sources.json"


def test_model_sources_are_exact_and_content_addressed() -> None:
    value = json.loads(SOURCES.read_text(encoding="utf-8"))
    assert value["schema"] == "gen-automation/i2v-model-sources/v1"
    sources = value["sources"]
    assert len(sources) == 6
    assert len({source["sha256"] for source in sources}) == 6
    baseline = [source for source in sources if not source["optional"]]
    assert sum(source["expected_bytes"] for source in baseline) == 36_047_286_759
    assert {source["role"] for source in baseline} == {
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    }


def test_mirror_never_materializes_model_bytes_on_disk() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    ast.parse(source)
    assert "create_multipart_upload" in source
    assert "upload_part" in source
    assert "complete_multipart_upload" in source
    assert "abort_multipart_upload" in source
    assert "PART_BYTES = 64 * 1024 * 1024" in source
    assert ".write_bytes(" not in source
    assert 'open("wb")' not in source
    assert "tempfile" not in source


def test_credentials_are_only_attached_to_exact_civitai_host() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'host == "civitai.com"' in source
    assert '"Authorization": f"Bearer {token}"' in source
    assert "_next_url(current, location)" in source
    assert "follow_redirects=False" in source
