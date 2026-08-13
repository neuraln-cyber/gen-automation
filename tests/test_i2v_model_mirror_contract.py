import ast
import json
from pathlib import Path

from gen_automation.i2v_worker.lora_catalog import LORA_CATALOG

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "mirror-i2v-models.py"
SOURCES = REPOSITORY_ROOT / "i2v-models" / "dasiwa-wan22-i2v-v1.sources.json"
MANIFEST = REPOSITORY_ROOT / "i2v-models" / "dasiwa-wan22-i2v-v1.json"


def test_model_sources_are_exact_and_content_addressed() -> None:
    value = json.loads(SOURCES.read_text(encoding="utf-8"))
    assert value["schema"] == "gen-automation/i2v-model-sources/v1"
    sources = value["sources"]
    assert len(sources) == 14
    assert len({source["sha256"] for source in sources}) == 14
    baseline = [source for source in sources if not source["optional"]]
    assert sum(source["expected_bytes"] for source in baseline) == 36_047_286_759
    assert {source["role"] for source in baseline} == {
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    }
    assert {source["role"] for source in sources if source["optional"]} == {
        "lora_wan_general_nsfw_high",
        "lora_wan_general_nsfw_low",
        "lora_bouncing_boobs_high",
        "lora_bouncing_boobs_low",
        "lora_m4crom4sti4_high",
        "lora_m4crom4sti4_low",
        "lora_dr34ml4y_high",
        "lora_dr34ml4y_low",
        "lora_smoothmix_animations_high",
        "lora_smoothmix_animations_low",
    }


def test_reviewed_catalog_matches_acquisition_sources_and_provenance_manifest() -> None:
    sources = {
        source["role"]: source
        for source in json.loads(SOURCES.read_text(encoding="utf-8"))["sources"]
    }
    pairs = {
        pair["id"]: pair
        for pair in json.loads(MANIFEST.read_text(encoding="utf-8"))["optional_paired_loras"]
    }

    for catalog_id, entry in LORA_CATALOG.items():
        pair = pairs[catalog_id]
        assert pair["creator_name"] == entry.creator_name
        assert pair["canonical_source_url"] == entry.canonical_source_url
        for stage, artifact in (("high", entry.high), ("low", entry.low)):
            source = sources[artifact.role]
            manifest_artifact = pair[stage]
            assert source["role"] == manifest_artifact["role"] == artifact.role
            assert source["target_filename"] == manifest_artifact["filename"] == artifact.filename
            assert source["expected_bytes"] == manifest_artifact["bytes"] == artifact.byte_size
            assert source["sha256"] == manifest_artifact["sha256"] == artifact.sha256
            assert source["url"].endswith(f"/{artifact.civitai_version_id}")
            assert manifest_artifact["civitai"] == {
                "model_id": artifact.civitai_model_id,
                "version_id": artifact.civitai_version_id,
                "file_id": artifact.civitai_file_id,
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
