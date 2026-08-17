from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runpod_i2v_seed_volume.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runpod_i2v_seed_volume", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _objects(tmp_path: Path) -> tuple[Path, dict[tuple[str, str, str], bytes]]:
    roles = (
        ("diffusion_model_high", "models/diffusion_models/high.safetensors", b"high"),
        ("diffusion_model_low", "models/diffusion_models/low.safetensors", b"low"),
        ("text_encoder", "models/text_encoders/text.safetensors", b"text"),
        ("vae", "models/vae/Wan/vae.safetensors", b"vae"),
    )
    manifest: list[dict[str, object]] = []
    sources: dict[tuple[str, str, str], bytes] = {}
    for role, install_path, body in roles:
        digest = hashlib.sha256(body).hexdigest()
        key = f"worker/i2v/sha256/{digest}"
        version = f"version-{role}"
        manifest.append(
            {
                "role": role,
                "bucket": "private-models",
                "key": key,
                "version_id": version,
                "byte_size": len(body),
                "sha256": digest,
                "install_path": install_path,
            }
        )
        sources[("private-models", key, version)] = body
    path = tmp_path / "objects.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, sources


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "404", "Message": "not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        operation,
    )


class _S3:
    def __init__(self, *, sources: dict[tuple[str, str, str], bytes] | None = None) -> None:
        self.sources = sources or {}
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.uploaded_parts = 0

    def head_object(self, **kwargs: object) -> dict[str, Any]:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        try:
            body = self.objects[(bucket, key)]
        except KeyError:
            raise _not_found("HeadObject") from None
        return {"ContentLength": len(body)}

    def get_object(self, **kwargs: object) -> dict[str, Any]:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        version_id = kwargs.get("VersionId")
        if version_id is None:
            body = self.objects[(bucket, key)]
        else:
            body = self.sources[(bucket, key, str(version_id))]
        return {"ContentLength": len(body), "Body": io.BytesIO(body)}

    def put_object(self, **kwargs: object) -> dict[str, Any]:
        assert kwargs["ContentType"] == "application/json"
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(
            kwargs["Body"]  # type: ignore[arg-type]
        )
        return {"ETag": "marker"}

    def create_multipart_upload(self, **kwargs: object) -> dict[str, Any]:
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = {
            "bucket": str(kwargs["Bucket"]),
            "key": str(kwargs["Key"]),
            "parts": {},
        }
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs: object) -> dict[str, Any]:
        upload = self.uploads[str(kwargs["UploadId"])]
        assert (str(kwargs["Bucket"]), str(kwargs["Key"])) == (
            upload["bucket"],
            upload["key"],
        )
        part_number = int(str(kwargs["PartNumber"]))
        upload["parts"][part_number] = bytes(
            kwargs["Body"]  # type: ignore[arg-type]
        )
        self.uploaded_parts += 1
        return {"ETag": f"etag-{part_number}"}

    def complete_multipart_upload(self, **kwargs: object) -> dict[str, Any]:
        upload = self.uploads.pop(str(kwargs["UploadId"]))
        multipart = kwargs["MultipartUpload"]
        assert isinstance(multipart, dict)
        expected = [part["PartNumber"] for part in multipart["Parts"]]
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = b"".join(
            upload["parts"][number] for number in expected
        )
        return {"ETag": "complete"}

    def abort_multipart_upload(self, **kwargs: object) -> dict[str, Any]:
        upload = self.uploads.pop(str(kwargs["UploadId"]))
        assert (str(kwargs["Bucket"]), str(kwargs["Key"])) == (
            upload["bucket"],
            upload["key"],
        )
        return {}


def test_preseed_streams_exact_versioned_objects_once_and_reconciles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load()
    objects_file, source_objects = _objects(tmp_path)
    models, objects_sha = module._read_models(objects_file)
    source = _S3(sources=source_objects)
    destination = _S3()
    monkeypatch.setattr(module, "_source_client", lambda _profile: source)
    monkeypatch.setattr(
        module,
        "_runpod_client",
        lambda **_kwargs: destination,
    )
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_S3_ACCESS_KEY_ID", "user_test")
    monkeypatch.setenv("RUNPOD_S3_SECRET_ACCESS_KEY", "rps_test")
    state_file = tmp_path / "preseed-state.json"

    first = module.apply(
        models=models,
        model_objects_sha256=objects_sha,
        volume_id="volume-test",
        state_file=state_file,
        aws_profile=None,
        endpoint=module.DEFAULT_ENDPOINT,
        datacenter=module.DEFAULT_DATACENTER,
        part_bytes=64 * 1024 * 1024,
    )
    first_part_count = destination.uploaded_parts
    second = module.apply(
        models=models,
        model_objects_sha256=objects_sha,
        volume_id="volume-test",
        state_file=state_file,
        aws_profile=None,
        endpoint=module.DEFAULT_ENDPOINT,
        datacenter=module.DEFAULT_DATACENTER,
        part_bytes=64 * 1024 * 1024,
    )

    assert first == second
    assert first == {
        "ready": True,
        "network_volume_id": "volume-test",
        "artifact_identity_sha256": module._artifact_identity(models),
        "object_count": 4,
        "total_bytes": 14,
    }
    assert first_part_count == 4
    assert destination.uploaded_parts == first_part_count
    assert json.loads(state_file.read_text(encoding="utf-8"))["status"] == "ready"
    for model in models:
        assert (
            destination.objects[
                ("volume-test", module._object_key(module._artifact_identity(models), model))
            ]
            == source_objects[(model.bucket, model.key, model.version_id)]
        )


def test_preseed_adopts_exact_ready_volume_without_opening_a_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load()
    objects_file, source_objects = _objects(tmp_path)
    models, objects_sha = module._read_models(objects_file)
    destination = _S3()
    volume_id = "volume-ready"
    identity = module._artifact_identity(models)
    marker = module._marker_payload(
        volume_id=volume_id,
        artifact_identity=identity,
        model_objects_sha256=objects_sha,
        models=models,
    )
    destination.put_object(
        Bucket=volume_id,
        Key=module._marker_key(identity),
        Body=module._canonical_json(marker),
        ContentType="application/json",
    )
    for model in models:
        destination.objects[(volume_id, module._object_key(identity, model))] = source_objects[
            (model.bucket, model.key, model.version_id)
        ]

    monkeypatch.setattr(module, "_runpod_client", lambda **_kwargs: destination)
    monkeypatch.setattr(
        module,
        "_source_client",
        lambda _profile: pytest.fail("a ready volume must not open the source"),
    )
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    state_file = tmp_path / "adopted-state.json"

    result = module.apply(
        models=models,
        model_objects_sha256=objects_sha,
        volume_id=volume_id,
        state_file=state_file,
        aws_profile=None,
        endpoint=module.DEFAULT_ENDPOINT,
        datacenter=module.DEFAULT_DATACENTER,
        part_bytes=64 * 1024 * 1024,
        adopt_ready_only=True,
    )

    assert result["ready"] is True
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert all(item["status"] == "completed" for item in state["objects"].values())
    assert destination.uploaded_parts == 0


def test_preseed_adopt_only_rejects_mismatch_before_source_or_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load()
    objects_file, _source_objects = _objects(tmp_path)
    models, objects_sha = module._read_models(objects_file)
    destination = _S3()
    state_file = tmp_path / "adopt-mismatch-state.json"

    monkeypatch.setattr(module, "_runpod_client", lambda **_kwargs: destination)
    monkeypatch.setattr(
        module,
        "_source_client",
        lambda _profile: pytest.fail("adopt-only must not open the source"),
    )
    monkeypatch.setattr(
        module,
        "_load_state",
        lambda *_args, **_kwargs: pytest.fail("adopt-only mismatch must not load state"),
    )
    monkeypatch.setenv(module.SPEND_SWITCH, "true")

    with pytest.raises(RuntimeError, match="not exactly ready for adoption"):
        module.apply(
            models=models,
            model_objects_sha256=objects_sha,
            volume_id="volume-mismatch",
            state_file=state_file,
            aws_profile=None,
            endpoint=module.DEFAULT_ENDPOINT,
            datacenter=module.DEFAULT_DATACENTER,
            part_bytes=64 * 1024 * 1024,
            adopt_ready_only=True,
        )

    assert not state_file.exists()
    assert destination.objects == {}
    assert destination.uploads == {}
    assert destination.uploaded_parts == 0


def test_preseed_adopt_only_rejects_source_volume_before_opening_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load()
    objects_file, _source_objects = _objects(tmp_path)
    models, objects_sha = module._read_models(objects_file)
    monkeypatch.setattr(
        module,
        "_runpod_client",
        lambda **_kwargs: pytest.fail("invalid adopt-only input must not open a client"),
    )
    monkeypatch.setenv(module.SPEND_SWITCH, "true")

    with pytest.raises(RuntimeError, match="cannot use a source volume"):
        module.apply(
            models=models,
            model_objects_sha256=objects_sha,
            volume_id="volume-destination",
            state_file=tmp_path / "unused-state.json",
            aws_profile=None,
            endpoint=module.DEFAULT_ENDPOINT,
            datacenter=module.DEFAULT_DATACENTER,
            part_bytes=64 * 1024 * 1024,
            source_runpod_volume_id="volume-source",
            source_runpod_endpoint="https://s3api-eu-ro-1.runpod.io/",
            source_runpod_datacenter="EU-RO-1",
            adopt_ready_only=True,
        )


def test_preseed_refuses_unowned_existing_destination_object(tmp_path: Path) -> None:
    module = _load()
    objects_file, source_objects = _objects(tmp_path)
    models, objects_sha = module._read_models(objects_file)
    source = _S3(sources=source_objects)
    destination = _S3()
    identity = module._artifact_identity(models)
    destination.objects[("volume-test", module._object_key(identity, models[0]))] = b"bad!"
    state = module._new_state(
        volume_id="volume-test",
        artifact_identity=identity,
        model_objects_sha256=objects_sha,
        models=models,
    )
    state_file = tmp_path / "state.json"
    module._write_state(state_file, state)

    with pytest.raises(RuntimeError, match="unexpected object already exists"):
        module._upload_model(
            source=source,
            source_bucket=models[0].bucket,
            source_key=models[0].key,
            source_version_id=models[0].version_id,
            destination=destination,
            volume_id="volume-test",
            artifact_identity=identity,
            model=models[0],
            object_state=state["objects"][models[0].role],
            state=state,
            state_file=state_file,
            part_bytes=64 * 1024 * 1024,
        )


def test_preseed_plan_does_not_require_credentials(tmp_path: Path) -> None:
    module = _load()
    objects_file, _sources = _objects(tmp_path)

    assert module.main.__module__ == "runpod_i2v_seed_volume"
    models, _objects_sha = module._read_models(objects_file)
    assert module._marker_key(module._artifact_identity(models)).endswith("/preseed.json")


def test_preseed_can_copy_an_exact_runpod_volume_without_aws(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load()
    objects_file, _source_objects = _objects(tmp_path)
    models, objects_sha = module._read_models(objects_file)
    identity = module._artifact_identity(models)
    source = _S3()
    destination = _S3()
    source_volume = "volume-source"
    destination_volume = "volume-destination"
    source_marker = module._marker_payload(
        volume_id=source_volume,
        artifact_identity=identity,
        model_objects_sha256=objects_sha,
        models=models,
    )
    source.put_object(
        Bucket=source_volume,
        Key=module._marker_key(identity),
        Body=module._canonical_json(source_marker),
        ContentType="application/json",
    )
    expected: dict[str, bytes] = {}
    for model in models:
        body = model.role.encode("ascii")[: model.byte_size].ljust(model.byte_size, b"_")
        # The fixture's hashes bind different short bodies, so preserve those exact bytes.
        body = {
            "diffusion_model_high": b"high",
            "diffusion_model_low": b"low",
            "text_encoder": b"text",
            "vae": b"vae",
        }[model.role]
        key = module._object_key(identity, model)
        source.objects[(source_volume, key)] = body
        expected[key] = body

    def runpod_client(*, endpoint: str, datacenter: str) -> _S3:
        if endpoint == "https://s3api-eu-ro-1.runpod.io/" and datacenter == "EU-RO-1":
            return source
        assert endpoint == "https://s3api-us-ks-2.runpod.io/"
        assert datacenter == "US-KS-2"
        return destination

    monkeypatch.setattr(module, "_runpod_client", runpod_client)
    monkeypatch.setattr(
        module,
        "_source_client",
        lambda _profile: pytest.fail("AWS source must not be opened"),
    )
    monkeypatch.setenv(module.SPEND_SWITCH, "true")

    result = module.apply(
        models=models,
        model_objects_sha256=objects_sha,
        volume_id=destination_volume,
        state_file=tmp_path / "runpod-copy-state.json",
        aws_profile=None,
        endpoint="https://s3api-us-ks-2.runpod.io/",
        datacenter="US-KS-2",
        part_bytes=64 * 1024 * 1024,
        source_runpod_volume_id=source_volume,
        source_runpod_endpoint="https://s3api-eu-ro-1.runpod.io/",
        source_runpod_datacenter="EU-RO-1",
    )

    assert result["ready"] is True
    for key, body in expected.items():
        assert destination.objects[(destination_volume, key)] == body
