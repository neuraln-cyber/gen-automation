from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "aws-staging" / "deploy" / "seed-i2v-runpod-volume.sh"


def test_runpod_volume_seed_is_exact_scoped_and_non_gpu() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "/gen-automation-staging/runpod/s3-access-key-id" in script
    assert "/gen-automation-staging/runpod/s3-secret-access-key" in script
    assert "--env GEN_AUTOMATION_I2V_ENABLED=true" in script
    assert "--env GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED=true" in script
    assert "i2v_worker_model_objects(Settings())" in script
    assert "if len(models) != 14" in script
    assert "/app/scripts/runpod_i2v_seed_volume.py apply" in script
    assert "--network-volume-id" in script
    assert "--source-runpod-volume-id" in script
    assert "--source-runpod-datacenter" in script
    assert "--adopt-ready-only" in script
    assert "seed_args+=(--adopt-ready-only)" in script
    assert "adopt-ready-only cannot use a source volume" in script
    assert '"https://s3api-${datacenter,,}.runpod.io/"' in script
    assert "--acknowledge-upload" in script
    assert "--read-only --network host" in script
    assert "--cap-drop ALL" in script
    assert "--security-opt no-new-privileges:true" in script
    assert "--pull" not in script
    assert "salad" not in script.casefold()
    assert "3090" not in script


def test_runpod_volume_seed_never_prints_or_persists_credentials_in_state() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'chmod 0600 "$credentials_file"' in script
    assert 'rm -f -- "$credentials_file" "$model_objects_file"' in script
    assert "RUNPOD_S3_SECRET_ACCESS_KEY=%s" in script
    assert "preseed-state.json" in script
    assert "preseed-state-$volume_id.json" in script
    assert "printf '%s\\n' \"$RUNPOD_S3" not in script
