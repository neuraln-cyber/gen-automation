import base64
import hashlib
from functools import partial
from typing import Any, cast
from urllib.parse import quote

import boto3
from anyio import to_thread
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from gen_automation.config import Settings
from gen_automation.storage.base import (
    MultipartPart,
    MultipartUpload,
    ObjectAlreadyExistsError,
    ObjectConflictError,
    ObjectDigest,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
    PresignedUpload,
)

PRIVATE_NO_STORE_CACHE_CONTROL = "private, no-store, max-age=0"


class S3ObjectStore:
    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
    ) -> None:
        for label, value in (
            ("S3 access key ID", access_key_id),
            ("S3 secret access key", secret_access_key),
            ("S3 session token", session_token),
        ):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(ord(character) < 33 or ord(character) == 127 for character in value)
            ):
                raise ValueError(f"{label} must be nonempty, trimmed visible text")
        if (access_key_id is None) != (secret_access_key is None):
            raise ValueError("S3 access key ID and secret must be provided together")
        if session_token is not None and access_key_id is None:
            raise ValueError("S3 session token requires an access key pair")
        self.bucket = bucket
        s3_config = {"addressing_style": "virtual"} if endpoint_url is None else {}
        client_arguments: dict[str, Any] = {
            "service_name": "s3",
            "region_name": region,
            "endpoint_url": endpoint_url,
            "config": Config(
                signature_version="s3v4",
                retries={"mode": "standard", "max_attempts": 4},
                connect_timeout=5,
                read_timeout=30,
                s3=s3_config,
            ),
        }
        if access_key_id is not None:
            client_arguments["aws_access_key_id"] = access_key_id
            client_arguments["aws_secret_access_key"] = secret_access_key
            if session_token is not None:
                client_arguments["aws_session_token"] = session_token
        self.client: Any = boto3.client(**client_arguments)

    async def _call(self, method_name: str, **parameters: Any) -> Any:
        method = getattr(self.client, method_name)
        try:
            return await to_thread.run_sync(partial(method, **parameters))
        except (BotoCoreError, ClientError) as error:
            raise ObjectStoreError(f"S3 {method_name} failed") from error

    async def ping(self) -> None:
        await self._call("head_bucket", Bucket=self.bucket)

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        fields = {
            "Content-Type": content_type,
            "Cache-Control": PRIVATE_NO_STORE_CACHE_CONTROL,
            "x-amz-server-side-encryption": "AES256",
        }
        fields.update({f"x-amz-meta-{name}": value for name, value in metadata.items()})
        conditions: list[Any] = [
            ["content-length-range", 1, max_bytes],
            {"Content-Type": content_type},
            {"Cache-Control": PRIVATE_NO_STORE_CACHE_CONTROL},
            {"x-amz-server-side-encryption": "AES256"},
        ]
        conditions.extend({f"x-amz-meta-{name}": value} for name, value in metadata.items())
        try:
            response = self.client.generate_presigned_post(
                Bucket=self.bucket,
                Key=key,
                Fields=fields,
                Conditions=conditions,
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError) as error:
            raise ObjectStoreError("S3 upload signing failed") from error
        return PresignedUpload(
            url=cast(str, response["url"]),
            method="POST",
            fields=cast(dict[str, str], response["fields"]),
            headers={},
        )

    async def presign_download(
        self,
        *,
        key: str,
        expires_in: int,
        download_name: str | None = None,
        version_id: str | None = None,
    ) -> str:
        parameters: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "ResponseCacheControl": PRIVATE_NO_STORE_CACHE_CONTROL,
        }
        if version_id is not None:
            parameters["VersionId"] = version_id
        if download_name:
            encoded_name = quote(download_name, safe="")
            parameters["ResponseContentDisposition"] = (
                f"attachment; filename*=UTF-8''{encoded_name}"
            )
        try:
            return cast(
                str,
                self.client.generate_presigned_url(
                    "get_object",
                    Params=parameters,
                    ExpiresIn=expires_in,
                    HttpMethod="GET",
                ),
            )
        except (BotoCoreError, ClientError) as error:
            raise ObjectStoreError("S3 download signing failed") from error

    async def head(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> ObjectMetadata | None:
        parameters: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
        }
        if version_id is not None:
            parameters["VersionId"] = version_id
        try:
            response = await to_thread.run_sync(partial(self.client.head_object, **parameters))
        except ClientError as error:
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
                return None
            raise ObjectStoreError("S3 head_object failed") from error
        except BotoCoreError as error:
            raise ObjectStoreError("S3 head_object failed") from error

        metadata = {
            str(key).lower(): str(value) for key, value in response.get("Metadata", {}).items()
        }
        return ObjectMetadata(
            key=key,
            byte_size=int(response["ContentLength"]),
            content_type=response.get("ContentType"),
            metadata=metadata,
            version_id=response.get("VersionId"),
            etag=str(response["ETag"]).strip('"') if response.get("ETag") else None,
        )

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        def read_object() -> bytes:
            body: Any | None = None
            try:
                parameters: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
                if version_id is not None:
                    parameters["VersionId"] = version_id
                if etag is not None:
                    parameters["IfMatch"] = etag
                response = self.client.get_object(**parameters)
                body = response["Body"]
                expected_size = int(response["ContentLength"])
                if expected_size > max_bytes:
                    raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")

                buffer = bytearray()
                total = 0
                while chunk := body.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")
                    buffer.extend(chunk)
                return bytes(buffer)
            except ObjectTooLargeError:
                raise
            except ClientError as error:
                status_code = int(
                    error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                )
                error_code = str(error.response.get("Error", {}).get("Code", ""))
                if status_code == 404 or error_code in {
                    "404",
                    "NoSuchKey",
                    "NoSuchVersion",
                    "NotFound",
                }:
                    raise ObjectNotFoundError(key) from error
                if status_code == 412 or error_code in {"412", "PreconditionFailed"}:
                    raise ObjectConflictError("S3 object read precondition failed") from error
                raise ObjectStoreError("S3 get_object failed") from error
            except BotoCoreError as error:
                raise ObjectStoreError("S3 get_object failed") from error
            finally:
                if body is not None:
                    body.close()

        return await to_thread.run_sync(read_object)

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        if not isinstance(body, bytes) or not body:
            raise ValueError("object body must be non-empty bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if len(body) > max_bytes:
            raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")
        expected_sha256 = hashlib.sha256(body).hexdigest()
        supplied_sha256 = metadata.get("sha256")
        if supplied_sha256 is not None and supplied_sha256 != expected_sha256:
            raise ValueError("object sha256 metadata does not match its bytes")
        stored_metadata = dict(metadata)
        stored_metadata["sha256"] = expected_sha256
        content_md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode(
            "ascii"
        )
        try:
            response = await to_thread.run_sync(
                partial(
                    self.client.put_object,
                    Bucket=self.bucket,
                    Key=key,
                    Body=body,
                    ContentLength=len(body),
                    ContentMD5=content_md5,
                    ContentType=content_type,
                    CacheControl=PRIVATE_NO_STORE_CACHE_CONTROL,
                    Metadata=stored_metadata,
                    ServerSideEncryption="AES256",
                    IfNoneMatch="*",
                )
            )
        except ClientError as error:
            status_code = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if status_code == 412 or error_code in {"PreconditionFailed", "412"}:
                raise ObjectAlreadyExistsError(key) from error
            if status_code == 409 or error_code in {
                "ConditionalRequestConflict",
                "409",
            }:
                raise ObjectConflictError("conditional write conflict") from error
            raise ObjectStoreError("S3 put_object failed") from error
        except BotoCoreError as error:
            raise ObjectStoreError("S3 put_object failed") from error

        response_version_id = response.get("VersionId")
        result = await self.head(
            key,
            version_id=(response_version_id if isinstance(response_version_id, str) else None),
        )
        if result is None:
            raise ObjectNotFoundError(key)
        if (
            response_version_id is not None
            and result.version_id is not None
            and result.version_id != response_version_id
        ):
            raise ObjectConflictError("conditional write version changed")
        if (
            result.byte_size != len(body)
            or result.content_type != content_type
            or result.metadata.get("sha256") != expected_sha256
        ):
            raise ObjectConflictError("conditional write verification failed")
        return result

    async def copy_if_absent(
        self,
        *,
        source_key: str,
        destination_key: str,
        content_type: str,
        metadata: dict[str, str],
        source_version_id: str | None = None,
        source_etag: str | None = None,
        storage_class: str | None = None,
    ) -> ObjectMetadata:
        copy_source: dict[str, str] = {"Bucket": self.bucket, "Key": source_key}
        if source_version_id is not None:
            copy_source["VersionId"] = source_version_id
        copy_parameters: dict[str, Any] = {}
        if source_etag is not None:
            copy_parameters["CopySourceIfMatch"] = source_etag
        if storage_class is not None:
            copy_parameters["StorageClass"] = storage_class
        try:
            response = await to_thread.run_sync(
                partial(
                    self.client.copy_object,
                    Bucket=self.bucket,
                    Key=destination_key,
                    CopySource=copy_source,
                    Metadata=metadata,
                    MetadataDirective="REPLACE",
                    ContentType=content_type,
                    CacheControl=PRIVATE_NO_STORE_CACHE_CONTROL,
                    ServerSideEncryption="AES256",
                    IfNoneMatch="*",
                    **copy_parameters,
                )
            )
        except ClientError as error:
            status_code = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if status_code == 412 or error_code in {"PreconditionFailed", "412"}:
                existing = await self.head(destination_key)
                if existing is not None:
                    raise ObjectAlreadyExistsError(destination_key) from error
                raise ObjectNotFoundError("copy source changed") from error
            if status_code == 404 or error_code in {"NoSuchKey", "NotFound", "404"}:
                raise ObjectNotFoundError(source_key) from error
            if status_code == 409 or error_code in {"ConditionalRequestConflict", "409"}:
                raise ObjectConflictError("conditional copy conflict") from error
            raise ObjectStoreError("S3 copy_object failed") from error
        except BotoCoreError as error:
            raise ObjectStoreError("S3 copy_object failed") from error
        response_version_id = response.get("VersionId")
        result = await self.head(
            destination_key,
            version_id=(response_version_id if isinstance(response_version_id, str) else None),
        )
        if result is None:
            raise ObjectNotFoundError(destination_key)
        return result

    async def read_range(
        self,
        key: str,
        *,
        start: int,
        length: int,
        version_id: str,
        etag: str | None = None,
    ) -> bytes:
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("range start must be a non-negative integer")
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise ValueError("range length must be a positive integer")

        def read_object_range() -> bytes:
            body: Any | None = None
            try:
                parameters: dict[str, Any] = {
                    "Bucket": self.bucket,
                    "Key": key,
                    "VersionId": version_id,
                    "Range": f"bytes={start}-{start + length - 1}",
                }
                if etag is not None:
                    parameters["IfMatch"] = etag
                response = self.client.get_object(**parameters)
                body = response["Body"]
                expected_size = int(response["ContentLength"])
                if expected_size != length:
                    raise ObjectConflictError("S3 range length changed")
                result = body.read(length + 1)
                if len(result) != length or body.read(1):
                    raise ObjectConflictError("S3 range body length changed")
                return bytes(result)
            except (ObjectConflictError, ObjectNotFoundError):
                raise
            except ClientError as error:
                status_code = int(
                    error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                )
                error_code = str(error.response.get("Error", {}).get("Code", ""))
                if status_code == 404 or error_code in {
                    "404",
                    "NoSuchKey",
                    "NoSuchVersion",
                    "NotFound",
                }:
                    raise ObjectNotFoundError(key) from error
                if status_code in {412, 416} or error_code in {
                    "412",
                    "416",
                    "InvalidRange",
                    "PreconditionFailed",
                }:
                    raise ObjectConflictError(
                        "S3 exact-version range precondition failed"
                    ) from error
                raise ObjectStoreError("S3 get_object range failed") from error
            except BotoCoreError as error:
                raise ObjectStoreError("S3 get_object range failed") from error
            finally:
                if body is not None:
                    body.close()

        return await to_thread.run_sync(read_object_range)

    async def sha256(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str,
        etag: str | None = None,
    ) -> ObjectDigest:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")

        def hash_object() -> ObjectDigest:
            body: Any | None = None
            try:
                parameters: dict[str, Any] = {
                    "Bucket": self.bucket,
                    "Key": key,
                    "VersionId": version_id,
                }
                if etag is not None:
                    parameters["IfMatch"] = etag
                response = self.client.get_object(**parameters)
                body = response["Body"]
                expected_size = int(response["ContentLength"])
                if expected_size > max_bytes:
                    raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")
                digest = hashlib.sha256()
                total = 0
                while chunk := body.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")
                    digest.update(chunk)
                if total != expected_size:
                    raise ObjectConflictError("S3 object length changed while hashing")
                return ObjectDigest(sha256=digest.hexdigest(), byte_size=total)
            except (ObjectConflictError, ObjectTooLargeError):
                raise
            except ClientError as error:
                status_code = int(
                    error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                )
                error_code = str(error.response.get("Error", {}).get("Code", ""))
                if status_code == 404 or error_code in {
                    "404",
                    "NoSuchKey",
                    "NoSuchVersion",
                    "NotFound",
                }:
                    raise ObjectNotFoundError(key) from error
                if status_code == 412 or error_code in {"412", "PreconditionFailed"}:
                    raise ObjectConflictError("S3 object hash precondition failed") from error
                raise ObjectStoreError("S3 get_object hash failed") from error
            except BotoCoreError as error:
                raise ObjectStoreError("S3 get_object hash failed") from error
            finally:
                if body is not None:
                    body.close()

        return await to_thread.run_sync(hash_object)

    async def create_multipart_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        storage_class: str | None = None,
    ) -> MultipartUpload:
        storage_parameters: dict[str, str] = {}
        if storage_class is not None:
            storage_parameters["StorageClass"] = storage_class
        try:
            response = await to_thread.run_sync(
                partial(
                    self.client.create_multipart_upload,
                    Bucket=self.bucket,
                    Key=key,
                    ContentType=content_type,
                    CacheControl=PRIVATE_NO_STORE_CACHE_CONTROL,
                    Metadata=metadata,
                    ServerSideEncryption="AES256",
                    **storage_parameters,
                )
            )
        except (BotoCoreError, ClientError) as error:
            raise ObjectStoreError("S3 create_multipart_upload failed") from error
        upload_id = response.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise ObjectStoreError("S3 multipart upload ID is missing")
        return MultipartUpload(key=key, upload_id=upload_id)

    async def upload_part(
        self,
        upload: MultipartUpload,
        *,
        part_number: int,
        body: bytes,
    ) -> MultipartPart:
        if (
            isinstance(part_number, bool)
            or not isinstance(part_number, int)
            or not 1 <= part_number <= 10_000
        ):
            raise ValueError("multipart part number must be between 1 and 10000")
        if not isinstance(body, bytes) or not body:
            raise ValueError("multipart part body must be non-empty bytes")
        content_md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode(
            "ascii"
        )
        try:
            response = await to_thread.run_sync(
                partial(
                    self.client.upload_part,
                    Bucket=self.bucket,
                    Key=upload.key,
                    UploadId=upload.upload_id,
                    PartNumber=part_number,
                    Body=body,
                    ContentLength=len(body),
                    ContentMD5=content_md5,
                )
            )
        except ClientError as error:
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchUpload", "404"}:
                raise ObjectNotFoundError("multipart upload does not exist") from error
            raise ObjectStoreError("S3 upload_part failed") from error
        except BotoCoreError as error:
            raise ObjectStoreError("S3 upload_part failed") from error
        part_etag = response.get("ETag")
        if not isinstance(part_etag, str) or not part_etag:
            raise ObjectStoreError("S3 multipart part ETag is missing")
        return MultipartPart(part_number=part_number, etag=part_etag)

    async def complete_multipart_upload(
        self,
        upload: MultipartUpload,
        *,
        parts: tuple[MultipartPart, ...],
        total_bytes: int,
    ) -> ObjectMetadata:
        if not parts or tuple(part.part_number for part in parts) != tuple(
            range(1, len(parts) + 1)
        ):
            raise ValueError("multipart parts must be contiguous and ordered")
        if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes <= 0:
            raise ValueError("multipart total_bytes must be a positive integer")
        try:
            response = await to_thread.run_sync(
                partial(
                    self.client.complete_multipart_upload,
                    Bucket=self.bucket,
                    Key=upload.key,
                    UploadId=upload.upload_id,
                    MultipartUpload={
                        "Parts": [
                            {"PartNumber": part.part_number, "ETag": part.etag} for part in parts
                        ]
                    },
                    IfNoneMatch="*",
                    MpuObjectSize=total_bytes,
                )
            )
        except ClientError as error:
            status_code = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if status_code == 412 or error_code in {"PreconditionFailed", "412"}:
                raise ObjectAlreadyExistsError(upload.key) from error
            if status_code == 409 or error_code in {
                "ConditionalRequestConflict",
                "409",
            }:
                raise ObjectConflictError("conditional multipart completion conflict") from error
            if status_code == 404 or error_code in {"NoSuchUpload", "404"}:
                raise ObjectNotFoundError("multipart upload does not exist") from error
            raise ObjectStoreError("S3 complete_multipart_upload failed") from error
        except BotoCoreError as error:
            raise ObjectStoreError("S3 complete_multipart_upload failed") from error

        version_id = response.get("VersionId")
        result = await self.head(
            upload.key,
            version_id=version_id if isinstance(version_id, str) else None,
        )
        if result is None:
            raise ObjectNotFoundError(upload.key)
        if result.byte_size != total_bytes:
            raise ObjectConflictError("multipart write verification failed")
        return result

    async def abort_multipart_upload(self, upload: MultipartUpload) -> None:
        try:
            await to_thread.run_sync(
                partial(
                    self.client.abort_multipart_upload,
                    Bucket=self.bucket,
                    Key=upload.key,
                    UploadId=upload.upload_id,
                )
            )
        except ClientError as error:
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchUpload", "404"}:
                return
            raise ObjectStoreError("S3 abort_multipart_upload failed") from error
        except BotoCoreError as error:
            raise ObjectStoreError("S3 abort_multipart_upload failed") from error

    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        parameters: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if version_id is not None:
            parameters["VersionId"] = version_id
        await self._call("delete_object", **parameters)

    async def close(self) -> None:
        await to_thread.run_sync(self.client.close)


def build_object_store(settings: Settings) -> ObjectStore | None:
    if not settings.storage_enabled:
        return None
    if settings.storage_bucket is None:
        raise ValueError("storage bucket is required")

    access_key_id = (
        settings.storage_access_key_id.get_secret_value()
        if settings.storage_access_key_id
        else None
    )
    secret_access_key = (
        settings.storage_secret_access_key.get_secret_value()
        if settings.storage_secret_access_key
        else None
    )
    session_token = (
        settings.storage_session_token.get_secret_value()
        if settings.storage_session_token
        else None
    )
    return S3ObjectStore(
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        endpoint_url=(
            str(settings.storage_endpoint_url)
            if settings.storage_endpoint_url is not None
            else None
        ),
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
    )
