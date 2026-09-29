"""Author-baseline pins and inference boundaries, without network/model execution."""

import json
from pathlib import Path
from uuid import uuid4

from gen_automation.i2v_worker.comfy_h3_node import NODE_CLASS_MAPPINGS
from gen_automation.i2v_worker.settings import H3_CUSTOM_NODES, I2V_CUSTOM_NODES
from gen_automation.i2v_worker.supervisor import _comfy_command
from tests.test_h3_upscale import _workflow
from tests.test_i2v_h3_readiness import h3_settings
from tests.test_i2v_worker_contract import _settings

ROOT = Path(__file__).parents[1]


def test_h3_process_cannot_import_legacy_or_deleted_inference_nodes(tmp_path):
    settings = _settings(tmp_path).model_copy(update={"profile": "minimax_h3"})
    command = _comfy_command(settings)
    allowed = command[
        command.index("--whitelist-custom-nodes") + 1 : command.index("--disable-api-nodes")
    ]
    assert tuple(allowed) == H3_CUSTOM_NODES
    assert I2V_CUSTOM_NODES == ("ComfyUI-NAG",)
    assert not set(I2V_CUSTOM_NODES) & set(allowed)
    assert "--use-ck-attention" in command
    assert set(NODE_CLASS_MAPPINGS) == {"ManagedH3DiagnosticDecode"}
    assert not (ROOT / "src/gen_automation/i2v_worker/comfy_h3_upscale.py").exists()


def test_complete_author_packages_and_runtime_are_immutable():
    docker = (ROOT / "Dockerfile.i2v-worker").read_text()
    requirements = (ROOT / "requirements-i2v-worker.in").read_text()
    installer = (ROOT / "scripts/install-h3-author-nodes.sh").read_text()
    for pin in ("comfyui-frontend-package==1.53.6", "comfy-kitchen==0.2.35", "comfy-aimdo==0.5.5"):
        assert pin in requirements
        assert pin in (ROOT / "requirements-i2v-worker.lock").read_text()
    assert "73c9bad4d21e7addbe1d13bc92eee0f1431b017d" in docker
    for revision in (
        "9f5aef4a2748bba9486dda0a7efec7689462d7e0",
        "d3cfe21625e5170126ce06fbfcfe1d88108688c3",
        "fe6658f6d144066f14150d3526247b417683ff2b",
    ):
        assert revision in installer and revision in docker
    assert 'test "$(git -C "$target" rev-parse HEAD)" = "$revision"' in installer
    assert "git apply" not in installer
    assert "verify_h3_author_workflow.py" in docker
    assert "51a20a9e79fb9a505c0bada2e4828f39605738cd98aee327af7cd114f3015bef" in docker
    publication = (ROOT / ".github/workflows/publish-images.yml").read_text()
    inputs = publication.split("i2v_inputs=(", 1)[1].split(")", 1)[0]
    for script in ("install-h3-author-nodes.sh", "verify_h3_author_workflow.py"):
        assert f"scripts/{script}" in inputs


def test_encoder_is_the_exact_file_linked_in_author_workflow_not_a_namesake():
    sources = json.loads((ROOT / "i2v-models/dasiwa-minimax-h3-turbo-v2.sources.json").read_text())
    encoder = next(s for s in sources["sources"] if s["role"] == "text_encoder")
    assert encoder["sha256"] == "21fd2e2f06bc4fc422c6aa20893fe189edbbd9ab3068215f96e7a6cf2f6cb5bb"
    assert encoder["expected_bytes"] == 14952506709
    assert (
        "/Abiray/MiniMax-H3-GGUF/resolve/9fc3454d3ebe1be1bade862cd4a5011f325a22cb/"
        in encoder["url"]
    )
    assert encoder["target_filename"] == "qwen3vl_32b_minimax_h3_int4_convrot.safetensors"


def test_author_chain_keeps_one_stack_and_no_hidden_branch_strengths():
    selections = [
        {"artifact_id": str(uuid4()), "sha256": value * 64, "strength": strength}
        for value, strength in (("a", 1.0), ("b", -0.5))
    ]
    graph = _workflow(h3_settings(h3_loras=selections))
    assert graph["h3-attention"]["inputs"] == {
        "model": ["2", 0],
        "attention": "comfy kitchen attention",
    }
    assert graph["h3-torch-settings"]["inputs"] == {
        "model": ["h3-attention", 0],
        "enable_fp16_accumulation": True,
    }
    assert graph["1"]["inputs"]["mode"] == "I2VA"
    assert graph["1"]["inputs"]["fl2va_model"] == ["h3-torch-settings", 0]
    assert "ref2va_model" not in graph["1"]["inputs"]
    assert graph["h3-lora-stack"]["inputs"]["model"] == ["1", 5]
    stack = json.loads(graph["h3-lora-stack"]["inputs"]["stack_data"])
    assert [item["str"] for item in stack] == [1.0, -0.5]
    assert all(item["vs"] == item["as"] == 1 for item in stack)
    assert graph["11"]["inputs"]["model"] == ["h3-lora-stack", 0]
    assert graph["7"]["inputs"]["model"] == ["h3-lora-stack", 0]
    assert graph["6"]["inputs"]["clip"] == ["h3-lora-stack", 1]
    assert graph["12"]["inputs"]["noise"] == ["8", 1]
    assert json.loads(graph["8"]["inputs"]["seed_control_state"])["mode"] == "fixed"
    assert json.loads(graph["1"]["inputs"]["timeline_data"])["items"] == [
        {"type": "image", "slot": 0, "value": "prepared.png", "enabled": True}
    ]
