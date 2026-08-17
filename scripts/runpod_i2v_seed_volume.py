#!/usr/bin/env python3
"""Pre-seed exact I2V artifacts into a RunPod network volume without GPU compute."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from gen_automation.i2v_worker.models import ModelObject

PRESEED_SCHEMA = "gen-automation/i2v-runpod-preseed/v1"
STATE_SCHEMA = "gen-automation/i2v-runpod-preseed-state/v1"
SPEND_SWITCH = "GEN_AUTOMATION_RUNPOD_I2V_SEED_ALLOWED"
VOLUME_PREFIX = "gen-automation/i2v"
DEFAULT_DATACENTER = "EU-RO-1"
DEFAULT_ENDPOINT = "https://s3api-eu-ro-1.runpod.io/"
DEFAULT_PART_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class S3Client(Protocol):
    def head_object(self, **kwargs: object) -> dict[str, Any]: ...

    def get_object(self, **kwargs: object) -> dict[str, Any]: ...

    def put_object(self, **kwargs: object) -> dict[str, Any]: ...

    def create_multipart_upload(self, **kwargs: object) -> dict[str, Any]: ...

    def upload_part(self, **kwargs: object) -> dict[str, Any]: ...

    def complete_multipart_upload(self, **kwargs: object) -> dict[str, Any]: ...

    def abort_multipart_upload(self, **kwargs: object) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _read_models(path: Path) -> tuple[tuple[ModelObject, ...], str]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file() or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ValueError
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or len(raw) not in (4, 14):
            raise ValueError
        models = tuple(ModelObject.model_validate(item) for item in raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("model objects file is invalid") from None
    if len({model.role for model in models}) != len(models):
        raise RuntimeError("model objects file contains duplicate roles")
    canonical = _canonical_json([model.model_dump(mode="json") for model in models])
    return models, hashlib.sha256(canonical).hexdigest()


def _artifact_identity(models: tuple[ModelObject, ...]) -> str:
    identity = [
        {
            "role": model.role,
            "byte_size": model.byte_size,
            "sha256": model.sha256,
            "version_id": model.version_id,
        }
        for model in models
    ]
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _object_key(artifact_identity: str, model: ModelObject) -> str:
    return f"{VOLUME_PREFIX}/{artifact_identity}/{model.sha256}.safetensors"


def _marker_key(artifact_identity: str) -> str:
    return f"{VOLUME_PREFIX}/{artifact_identity}/preseed.json"


def _marker_payload(
    *,
    volume_id: str,
    artifact_identity: str,
    model_objects_sha256: str,
    models: tuple[ModelObject, ...],
) -> dict[str, object]:
    return {
        "schema": PRESEED_SCHEMA,
        "network_volume_id": volume_id,
        "artifact_identity_sha256": artifact_identity,
        "model_objects_sha256": model_objects_sha256,
        "objects": [
            {
                "role": model.role,
                "byte_size": model.byte_size,
                "sha256": model.sha256,
                "install_path": model.install_path,
                "volume_key": _object_key(artifact_identity, model),
            }
            for model in models
        ],
    }


def _new_state(
    *,
    volume_id: str,
    artifact_identity: str,
    model_objects_sha256: str,
    models: tuple[ModelObject, ...],
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "status": "planned",
        "created_at": _now(),
        "updated_at": _now(),
        "network_volume_id": volume_id,
        "artifact_identity_sha256": artifact_identity,
        "model_objects_sha256": model_objects_sha256,
        "objects": {
            model.role: {
                "volume_key": _object_key(artifact_identity, model),
                "status": "planned",
                "upload_id": None,
                "completion_attempted": False,
            }
            for model in models
        },
    }


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _load_state(
    path: Path,
    *,
    volume_id: str,
    artifact_identity: str,
    model_objects_sha256: str,
    models: tuple[ModelObject, ...],
) -> dict[str, Any]:
    if not path.exists():
        return _new_state(
            volume_id=volume_id,
            artifact_identity=artifact_identity,
            model_objects_sha256=model_objects_sha256,
            models=models,
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("preseed state file is invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("schema") != STATE_SCHEMA
        or value.get("network_volume_id") != volume_id
        or value.get("artifact_identity_sha256") != artifact_identity
        or value.get("model_objects_sha256") != model_objects_sha256
    ):
        raise RuntimeError("preseed state file belongs to another artifact or volume")
    object_states = value.get("objects")
    if not isinstance(object_states, dict) or set(object_states) != {
        model.role for model in models
    }:
        raise RuntimeError("preseed state file is invalid")
    return value


def _source_client(profile: str | None) -> S3Client:
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return cast(
        S3Client,
        session.client(
            "s3",
            config=Config(
                connect_timeout=30,
                read_timeout=300,
                retries={"mode": "standard", "max_attempts": 10},
            ),
        ),
    )


def _runpod_client(*, endpoint: str, datacenter: str) -> S3Client:
    access_id = os.environ.get("RUNPOD_S3_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("RUNPOD_S3_SECRET_ACCESS_KEY", "").strip()
    if not access_id or not secret:
        raise RuntimeError("RUNPOD_S3_ACCESS_KEY_ID and RUNPOD_S3_SECRET_ACCESS_KEY are required")
    return cast(
        S3Client,
        boto3.client(
            "s3",
            aws_access_key_id=access_id,
            aws_secret_access_key=secret,
            region_name=datacenter,
            endpoint_url=endpoint,
            config=Config(
                signature_version="s3v4",
                connect_timeout=30,
                read_timeout=7200,
                retries={"mode": "standard", "max_attempts": 10},
                s3={"addressing_style": "path"},
            ),
        ),
    )


def _head_size(client: S3Client, *, bucket: str, key: str) -> int | None:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise RuntimeError("RunPod volume object lookup failed") from None
    size = response.get("ContentLength")
    if not isinstance(size, int) or size < 0:
        raise RuntimeError("RunPod volume returned invalid object metadata")
    return size


def _read_marker(
    client: S3Client,
    *,
    volume_id: str,
    artifact_identity: str,
) -> dict[str, object] | None:
    key = _marker_key(artifact_identity)
    if _head_size(client, bucket=volume_id, key=key) is None:
        return None
    try:
        response = client.get_object(Bucket=volume_id, Key=key)
        body = response["Body"]
        raw = body.read(MAX_MANIFEST_BYTES + 1)
        body.close()
        value = json.loads(raw)
    except (BotoCoreError, ClientError, KeyError, OSError, ValueError, json.JSONDecodeError):
        raise RuntimeError("RunPod preseed marker is invalid") from None
    if len(raw) > MAX_MANIFEST_BYTES or not isinstance(value, dict):
        raise RuntimeError("RunPod preseed marker is invalid")
    return cast(dict[str, object], value)


def _remote_ready(
    client: S3Client,
    *,
    volume_id: str,
    marker: dict[str, object],
    artifact_identity: str,
    models: tuple[ModelObject, ...],
) -> bool:
    existing = _read_marker(
        client,
        volume_id=volume_id,
        artifact_identity=artifact_identity,
    )
    if existing is None:
        return False
    if existing != marker:
        raise RuntimeError("RunPod preseed marker drifted from the exact artifact")
    for model in models:
        if (
            _head_size(
                client,
                bucket=volume_id,
                key=_object_key(artifact_identity, model),
            )
            != model.byte_size
        ):
            raise RuntimeError("RunPod preseeded model object drifted")
    return True


def _abort_upload(
    client: S3Client,
    *,
    volume_id: str,
    key: str,
    upload_id: str,
) -> None:
    try:
        client.abort_multipart_upload(Bucket=volume_id, Key=key, UploadId=upload_id)
    except (BotoCoreError, ClientError):
        raise RuntimeError("RunPod multipart upload abort failed") from None


def _upload_model(
    *,
    source: S3Client,
    source_bucket: str,
    source_key: str,
    source_version_id: str | None,
    destination: S3Client,
    volume_id: str,
    artifact_identity: str,
    model: ModelObject,
    object_state: dict[str, Any],
    state: dict[str, Any],
    state_file: Path,
    part_bytes: int,
) -> None:
    key = _object_key(artifact_identity, model)
    print(f"preseed {model.role}: checking", file=sys.stderr, flush=True)
    existing_size = _head_size(destination, bucket=volume_id, key=key)
    if object_state.get("status") == "completed":
        if existing_size != model.byte_size:
            raise RuntimeError("completed RunPod volume object drifted")
        print(f"preseed {model.role}: already complete", file=sys.stderr, flush=True)
        return
    if existing_size is not None:
        if object_state.get("completion_attempted") and existing_size == model.byte_size:
            object_state.update({"status": "completed", "upload_id": None, "completed_at": _now()})
            _write_state(state_file, state)
            print(
                f"preseed {model.role}: reconciled completed upload",
                file=sys.stderr,
                flush=True,
            )
            return
        raise RuntimeError("unexpected object already exists on the RunPod volume")

    old_upload_id = object_state.get("upload_id")
    if isinstance(old_upload_id, str) and old_upload_id:
        _abort_upload(
            destination,
            volume_id=volume_id,
            key=key,
            upload_id=old_upload_id,
        )
        object_state["upload_id"] = None
        object_state["status"] = "planned"
        _write_state(state_file, state)

    try:
        created = destination.create_multipart_upload(Bucket=volume_id, Key=key)
        upload_id = created.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise RuntimeError("RunPod did not return a multipart upload ID")
        object_state.update({"status": "uploading", "upload_id": upload_id, "started_at": _now()})
        state["status"] = f"uploading_{model.role}"
        state["updated_at"] = _now()
        _write_state(state_file, state)
        print(
            f"preseed {model.role}: streaming {model.byte_size} bytes",
            file=sys.stderr,
            flush=True,
        )

        source_request: dict[str, object] = {
            "Bucket": source_bucket,
            "Key": source_key,
        }
        if source_version_id is not None:
            source_request["VersionId"] = source_version_id
        source_response = source.get_object(**source_request)
        if source_response.get("ContentLength") != model.byte_size:
            raise RuntimeError("source model object size drifted")
        body = source_response["Body"]
        digest = hashlib.sha256()
        byte_size = 0
        parts: list[dict[str, object]] = []
        try:
            part_number = 1
            while chunk := body.read(part_bytes):
                if not isinstance(chunk, bytes) or len(chunk) > part_bytes:
                    raise RuntimeError("source model object stream is invalid")
                byte_size += len(chunk)
                if byte_size > model.byte_size:
                    raise RuntimeError("source model object size drifted")
                digest.update(chunk)
                uploaded = destination.upload_part(
                    Bucket=volume_id,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                etag = uploaded.get("ETag")
                if not isinstance(etag, str) or not etag:
                    raise RuntimeError("RunPod did not return a multipart part ETag")
                parts.append({"ETag": etag, "PartNumber": part_number})
                part_number += 1
        finally:
            body.close()
        if byte_size != model.byte_size or digest.hexdigest() != model.sha256:
            raise RuntimeError("source model object identity drifted")
        object_state["completion_attempted"] = True
        object_state["source_verified_at"] = _now()
        _write_state(state_file, state)
        try:
            destination.complete_multipart_upload(
                Bucket=volume_id,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except (BotoCoreError, ClientError):
            if _head_size(destination, bucket=volume_id, key=key) != model.byte_size:
                raise RuntimeError("RunPod multipart completion outcome is unresolved") from None
        if _head_size(destination, bucket=volume_id, key=key) != model.byte_size:
            raise RuntimeError("RunPod completed object size is invalid")
        object_state.update({"status": "completed", "upload_id": None, "completed_at": _now()})
        state["updated_at"] = _now()
        _write_state(state_file, state)
        print(f"preseed {model.role}: complete", file=sys.stderr, flush=True)
    except Exception:
        current_upload_id = object_state.get("upload_id")
        if isinstance(current_upload_id, str) and current_upload_id:
            if _head_size(destination, bucket=volume_id, key=key) is None:
                _abort_upload(
                    destination,
                    volume_id=volume_id,
                    key=key,
                    upload_id=current_upload_id,
                )
                object_state["upload_id"] = None
                object_state["status"] = "planned"
                _write_state(state_file, state)
        raise


def apply(
    *,
    models: tuple[ModelObject, ...],
    model_objects_sha256: str,
    volume_id: str,
    state_file: Path,
    aws_profile: str | None,
    endpoint: str,
    datacenter: str,
    part_bytes: int,
    source_runpod_volume_id: str | None = None,
    source_runpod_endpoint: str | None = None,
    source_runpod_datacenter: str | None = None,
) -> dict[str, object]:
    if os.environ.get(SPEND_SWITCH, "").casefold() != "true":
        raise RuntimeError(f"volume upload requires {SPEND_SWITCH}=true")
    artifact_identity = _artifact_identity(models)
    marker = _marker_payload(
        volume_id=volume_id,
        artifact_identity=artifact_identity,
        model_objects_sha256=model_objects_sha256,
        models=models,
    )
    destination = _runpod_client(endpoint=endpoint, datacenter=datacenter)
    if _remote_ready(
        destination,
        volume_id=volume_id,
        marker=marker,
        artifact_identity=artifact_identity,
        models=models,
    ):
        state = _load_state(
            state_file,
            volume_id=volume_id,
            artifact_identity=artifact_identity,
            model_objects_sha256=model_objects_sha256,
            models=models,
        )
        completed_at = _now()
        object_states = cast(dict[str, dict[str, Any]], state["objects"])
        for model in models:
            object_states[model.role].update(
                {
                    "status": "completed",
                    "upload_id": None,
                    "completion_attempted": True,
                    "completed_at": completed_at,
                }
            )
        state.update({"status": "ready", "updated_at": completed_at, "completed_at": completed_at})
        _write_state(state_file, state)
        return _result(volume_id, artifact_identity, models, ready=True)
    state = _load_state(
        state_file,
        volume_id=volume_id,
        artifact_identity=artifact_identity,
        model_objects_sha256=model_objects_sha256,
        models=models,
    )
    _write_state(state_file, state)
    if source_runpod_volume_id is None:
        source = _source_client(aws_profile)
    else:
        if source_runpod_volume_id == volume_id:
            raise RuntimeError("source and destination RunPod volumes must differ")
        if source_runpod_endpoint is None or source_runpod_datacenter is None:
            raise RuntimeError("RunPod source volume endpoint is incomplete")
        source = _runpod_client(
            endpoint=source_runpod_endpoint,
            datacenter=source_runpod_datacenter,
        )
        source_marker = _marker_payload(
            volume_id=source_runpod_volume_id,
            artifact_identity=artifact_identity,
            model_objects_sha256=model_objects_sha256,
            models=models,
        )
        if not _remote_ready(
            source,
            volume_id=source_runpod_volume_id,
            marker=source_marker,
            artifact_identity=artifact_identity,
            models=models,
        ):
            raise RuntimeError("source RunPod volume is not exactly preseeded")
    object_states = cast(dict[str, dict[str, Any]], state["objects"])
    for model in models:
        source_bucket = source_runpod_volume_id or model.bucket
        source_key = (
            _object_key(artifact_identity, model)
            if source_runpod_volume_id is not None
            else model.key
        )
        _upload_model(
            source=source,
            source_bucket=source_bucket,
            source_key=source_key,
            source_version_id=(None if source_runpod_volume_id is not None else model.version_id),
            destination=destination,
            volume_id=volume_id,
            artifact_identity=artifact_identity,
            model=model,
            object_state=object_states[model.role],
            state=state,
            state_file=state_file,
            part_bytes=part_bytes,
        )
    marker_bytes = _canonical_json(marker)
    destination.put_object(
        Bucket=volume_id,
        Key=_marker_key(artifact_identity),
        Body=marker_bytes,
        ContentType="application/json",
    )
    if not _remote_ready(
        destination,
        volume_id=volume_id,
        marker=marker,
        artifact_identity=artifact_identity,
        models=models,
    ):
        raise RuntimeError("RunPod volume did not retain the completed preseed")
    state.update({"status": "ready", "updated_at": _now(), "completed_at": _now()})
    _write_state(state_file, state)
    return _result(volume_id, artifact_identity, models, ready=True)


def status(
    *,
    models: tuple[ModelObject, ...],
    model_objects_sha256: str,
    volume_id: str,
    endpoint: str,
    datacenter: str,
) -> dict[str, object]:
    artifact_identity = _artifact_identity(models)
    marker = _marker_payload(
        volume_id=volume_id,
        artifact_identity=artifact_identity,
        model_objects_sha256=model_objects_sha256,
        models=models,
    )
    destination = _runpod_client(endpoint=endpoint, datacenter=datacenter)
    ready = _remote_ready(
        destination,
        volume_id=volume_id,
        marker=marker,
        artifact_identity=artifact_identity,
        models=models,
    )
    return _result(volume_id, artifact_identity, models, ready=ready)


def _result(
    volume_id: str,
    artifact_identity: str,
    models: tuple[ModelObject, ...],
    *,
    ready: bool,
) -> dict[str, object]:
    return {
        "ready": ready,
        "network_volume_id": volume_id,
        "artifact_identity_sha256": artifact_identity,
        "object_count": len(models),
        "total_bytes": sum(model.byte_size for model in models),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply", "status"))
    parser.add_argument("--model-objects-file", type=Path, required=True)
    parser.add_argument("--network-volume-id", required=True)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--aws-profile")
    parser.add_argument("--source-runpod-volume-id")
    parser.add_argument("--source-runpod-datacenter")
    parser.add_argument("--source-runpod-endpoint")
    parser.add_argument("--datacenter", default=DEFAULT_DATACENTER)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument(
        "--part-bytes",
        type=int,
        default=DEFAULT_PART_BYTES,
        choices=(64 * 1024 * 1024, 128 * 1024 * 1024, 256 * 1024 * 1024),
    )
    parser.add_argument("--acknowledge-upload", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", args.network_volume_id):
        raise RuntimeError("network volume ID is invalid")
    if not re.fullmatch(r"[A-Z]{2,4}-[A-Z]{2,4}-[0-9]", args.datacenter):
        raise RuntimeError("RunPod datacenter is invalid")
    if args.endpoint != f"https://s3api-{args.datacenter.casefold()}.runpod.io/":
        raise RuntimeError("RunPod S3 endpoint does not match the datacenter")
    source_options = (
        args.source_runpod_volume_id,
        args.source_runpod_datacenter,
        args.source_runpod_endpoint,
    )
    if any(source_options) and not all(source_options):
        raise RuntimeError("RunPod source volume options must be provided together")
    if args.source_runpod_volume_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", args.source_runpod_volume_id):
            raise RuntimeError("source RunPod volume ID is invalid")
        if not re.fullmatch(r"[A-Z]{2,4}-[A-Z]{2,4}-[0-9]", args.source_runpod_datacenter):
            raise RuntimeError("source RunPod datacenter is invalid")
        if args.source_runpod_endpoint != (
            f"https://s3api-{args.source_runpod_datacenter.casefold()}.runpod.io/"
        ):
            raise RuntimeError("source RunPod S3 endpoint does not match the datacenter")
        if args.aws_profile is not None:
            raise RuntimeError("AWS and RunPod sources are mutually exclusive")
    models, model_objects_sha256 = _read_models(args.model_objects_file.resolve())
    artifact_identity = _artifact_identity(models)
    if args.action == "plan":
        result = {
            "mutates_runpod_volume": False,
            **_result(args.network_volume_id, artifact_identity, models, ready=False),
            "marker_key": _marker_key(artifact_identity),
        }
    elif args.action == "apply":
        if not args.acknowledge_upload or args.state_file is None:
            raise RuntimeError("apply requires --acknowledge-upload and --state-file")
        result = apply(
            models=models,
            model_objects_sha256=model_objects_sha256,
            volume_id=args.network_volume_id,
            state_file=args.state_file.resolve(),
            aws_profile=args.aws_profile,
            endpoint=args.endpoint,
            datacenter=args.datacenter,
            part_bytes=args.part_bytes,
            source_runpod_volume_id=args.source_runpod_volume_id,
            source_runpod_endpoint=args.source_runpod_endpoint,
            source_runpod_datacenter=args.source_runpod_datacenter,
        )
    else:
        result = status(
            models=models,
            model_objects_sha256=model_objects_sha256,
            volume_id=args.network_volume_id,
            endpoint=args.endpoint,
            datacenter=args.datacenter,
        )
    json.dump(result, sys.stdout, ensure_ascii=True, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
