"""Stream the pinned DaSiWa I2V model set directly into private S3.

This operator is intended to run on the staging control-plane host.  It holds
one 64 MiB multipart part at a time, never writes model bytes to disk, and only
completes an S3 version after the full source byte count and SHA-256 match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import boto3
import httpx2
from botocore.exceptions import BotoCoreError, ClientError

PART_BYTES = 64 * 1024 * 1024
READ_BYTES = 1024 * 1024
MAX_REDIRECTS = 10
MAX_ATTEMPTS = 8
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_REDIRECT_SUFFIXES = (
    "civitai.com",
    "r2.cloudflarestorage.com",
    "backblazeb2.com",
    "b2api.com",
    "amazonaws.com",
    "huggingface.co",
    "hf.co",
    "hfusercontent.com",
)


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    role: str
    url: str
    expected_bytes: int
    sha256: str
    target_filename: str
    optional: bool

    @property
    def key(self) -> str:
        return f"worker/i2v/sha256/{self.sha256}"


class MirrorError(RuntimeError):
    pass


def _source(value: object) -> Source:
    if not isinstance(value, dict) or set(value) != {
        "expected_bytes",
        "name",
        "optional",
        "role",
        "sha256",
        "target_filename",
        "url",
    }:
        raise MirrorError("model source entry is invalid")
    name = value["name"]
    role = value["role"]
    url = value["url"]
    expected_bytes = value["expected_bytes"]
    sha256 = value["sha256"]
    target_filename = value["target_filename"]
    optional = value["optional"]
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(role, str)
        or not role
        or not isinstance(url, str)
        or urlsplit(url).scheme != "https"
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes <= 0
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or not isinstance(target_filename, str)
        or Path(target_filename).name != target_filename
        or not isinstance(optional, bool)
    ):
        raise MirrorError("model source entry is invalid")
    return Source(name, role, url, expected_bytes, sha256, target_filename, optional)


def _load_sources(path: Path, *, include_optional: bool) -> tuple[Source, ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MirrorError("model source document is unreadable") from error
    if not isinstance(document, dict) or set(document) != {"schema", "sources"}:
        raise MirrorError("model source document is invalid")
    if document["schema"] != "gen-automation/i2v-model-sources/v1":
        raise MirrorError("model source schema is unsupported")
    values = document["sources"]
    if not isinstance(values, list) or not values:
        raise MirrorError("model source list is empty")
    sources = tuple(_source(value) for value in values)
    selected = tuple(
        sorted(
            (source for source in sources if include_optional or not source.optional),
            key=lambda source: (source.optional, source.expected_bytes, source.name),
        )
    )
    if len({source.sha256 for source in selected}) != len(selected):
        raise MirrorError("selected model sources are not unique")
    return selected


def _token_from_secret(client: Any, secret_id: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_id)
        raw = response.get("SecretString")
        value = json.loads(raw) if isinstance(raw, str) else None
    except (BotoCoreError, ClientError, json.JSONDecodeError) as error:
        raise MirrorError("Civitai credential could not be loaded") from error
    token = value.get("api_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token or token != token.strip():
        raise MirrorError("Civitai credential is invalid")
    return token


def _credential_header(url: str, token: str) -> dict[str, str]:
    host = (urlsplit(url).hostname or "").casefold()
    return {"Authorization": f"Bearer {token}"} if host == "civitai.com" else {}


def _next_url(current: str, location: str) -> str:
    value = urljoin(current, location)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not any(
            host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_REDIRECT_SUFFIXES
        )
    ):
        raise MirrorError("download redirect is not allowed")
    return value


def _open_range(
    client: httpx2.Client,
    source: Source,
    *,
    token: str,
    start: int,
    end: int,
) -> Any:
    current = source.url
    for redirect in range(MAX_REDIRECTS + 1):
        headers = {
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
            "User-Agent": "gen-automation-i2v-mirror/1",
            **_credential_header(current, token),
        }
        request = client.build_request("GET", current, headers=headers)
        response = client.send(request, stream=True, follow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            response.close()
            if redirect >= MAX_REDIRECTS:
                raise MirrorError("download redirected too many times")
            location = response.headers.get("Location")
            if not location:
                raise MirrorError("download redirect is missing a location")
            current = _next_url(current, location)
            continue
        if response.status_code not in {200, 206}:
            response.close()
            raise MirrorError(f"download returned HTTP {response.status_code}")
        if start > 0 and response.status_code != 206:
            response.close()
            raise MirrorError("download source does not support byte ranges")
        return response
    raise MirrorError("download redirected too many times")


def _read_part(response: Any, *, expected_bytes: int) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=READ_BYTES):
        if chunk:
            body.extend(chunk)
            if len(body) > expected_bytes:
                raise MirrorError("download part exceeded the expected size")
    if len(body) != expected_bytes:
        raise MirrorError("download part was truncated")
    return bytes(body)


def _head_matches(s3: Any, bucket: str, source: Source) -> dict[str, Any] | None:
    try:
        result = s3.head_object(Bucket=bucket, Key=source.key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise MirrorError("private model object could not be inspected") from error
    metadata = result.get("Metadata")
    if (
        result.get("ContentLength") != source.expected_bytes
        or not isinstance(metadata, dict)
        or metadata.get("sha256") != source.sha256
        or metadata.get("target-filename") != source.target_filename
    ):
        raise MirrorError("content-addressed model object has conflicting metadata")
    return result


def _mirror_one(
    *,
    s3: Any,
    http: httpx2.Client,
    bucket: str,
    source: Source,
    token: str,
) -> dict[str, object]:
    existing = _head_matches(s3, bucket, source)
    if existing is not None:
        print(f"{source.name}: already mirrored", flush=True)
        return _result(source, existing)
    try:
        created = s3.create_multipart_upload(
            Bucket=bucket,
            Key=source.key,
            ContentType="application/octet-stream",
            Metadata={
                "sha256": source.sha256,
                "target-filename": source.target_filename,
                "role": source.role,
                "source": "official-upstream",
            },
            StorageClass="INTELLIGENT_TIERING",
        )
        upload_id = created["UploadId"]
    except (BotoCoreError, ClientError, KeyError) as error:
        raise MirrorError("private multipart upload could not be created") from error
    parts: list[dict[str, object]] = []
    digest = hashlib.sha256()
    completed = 0
    try:
        for part_no, start in enumerate(range(0, source.expected_bytes, PART_BYTES), 1):
            end = min(start + PART_BYTES, source.expected_bytes) - 1
            expected = end - start + 1
            last_error: Exception | None = None
            for attempt in range(MAX_ATTEMPTS):
                try:
                    response = _open_range(http, source, token=token, start=start, end=end)
                    try:
                        body = _read_part(response, expected_bytes=expected)
                    finally:
                        response.close()
                    uploaded = s3.upload_part(
                        Bucket=bucket,
                        Key=source.key,
                        UploadId=upload_id,
                        PartNumber=part_no,
                        Body=body,
                    )
                    etag = uploaded["ETag"]
                    break
                except (BotoCoreError, ClientError, httpx2.HTTPError, MirrorError) as error:
                    last_error = error
                    if attempt + 1 == MAX_ATTEMPTS:
                        raise MirrorError("model part exhausted its retries") from error
                    time.sleep(min(60, 2**attempt))
            else:
                raise MirrorError("model part could not be transferred") from last_error
            digest.update(body)
            completed += len(body)
            parts.append({"ETag": etag, "PartNumber": part_no})
            del body
            print(
                f"{source.name}: {completed}/{source.expected_bytes} bytes "
                f"({completed * 100 // source.expected_bytes}%)",
                flush=True,
            )
        if completed != source.expected_bytes or digest.hexdigest() != source.sha256:
            raise MirrorError("complete model SHA-256 or size did not match")
        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=source.key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except BaseException:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=source.key, UploadId=upload_id)
        except (BotoCoreError, ClientError):
            pass
        raise
    result = _head_matches(s3, bucket, source)
    if result is None:
        raise MirrorError("completed private model object is missing")
    print(f"{source.name}: verified private version {result.get('VersionId')}", flush=True)
    return _result(source, result)


def _result(source: Source, head: dict[str, Any]) -> dict[str, object]:
    version = head.get("VersionId")
    etag = head.get("ETag")
    if not isinstance(version, str) or not version or not isinstance(etag, str) or not etag:
        raise MirrorError("private bucket versioning is required")
    return {
        "bytes": source.expected_bytes,
        "key": source.key,
        "name": source.name,
        "role": source.role,
        "sha256": source.sha256,
        "target_filename": source.target_filename,
        "version_id": version,
    }


def _write_result(path: Path, result: Iterable[dict[str, object]]) -> None:
    document = {
        "schema": "gen-automation/i2v-private-model-mirror/v1",
        "objects": list(result),
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--civitai-secret-id", required=True)
    parser.add_argument("--include-optional", action="store_true")
    args = parser.parse_args()
    try:
        sources = _load_sources(args.sources, include_optional=args.include_optional)
        session = boto3.Session(region_name=args.region)
        s3 = session.client("s3")
        token = _token_from_secret(session.client("secretsmanager"), args.civitai_secret_id)
        with httpx2.Client(
            timeout=httpx2.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0),
            follow_redirects=False,
            trust_env=False,
        ) as http:
            objects = tuple(
                _mirror_one(s3=s3, http=http, bucket=args.bucket, source=source, token=token)
                for source in sources
            )
        _write_result(args.result, objects)
    except (MirrorError, BotoCoreError, ClientError, OSError, httpx2.HTTPError) as error:
        print(f"I2V model mirror failed: {error}", file=sys.stderr)
        return 1
    print(f"Mirrored and verified {len(objects)} private model objects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
