import html
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import NoReturn
from urllib.parse import quote
from uuid import UUID

import httpx2

from gen_automation.integrations.salad.errors import (
    SaladAPIError,
    SaladProtocolError,
    SaladRateLimitError,
    SaladTimeoutError,
    SaladTransportError,
)
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstancePage,
    SaladContainerGroupPage,
    SaladGpuAvailability,
    SaladGpuClass,
    SaladOrganizationQuotas,
    SaladQueue,
    SaladQueueJob,
    SaladQueueJobPage,
    as_json_object,
    parse_container_group,
    parse_container_group_instance,
    parse_gpu_availability,
    parse_gpu_class,
    parse_organization_quotas,
    parse_queue,
    parse_queue_job,
)

SALAD_API_BASE_URL = "https://api.salad.com/api/public"
SALAD_REQUESTS_PER_MINUTE = 240
# Salad's live Job Queues endpoint accepts page sizes through 25 but currently
# rejects 50 and 100 with HTTP 400. Keep the provider-verified maximum in one
# place so production reconciliation paths share the same efficient default.
SALAD_QUEUE_JOB_PAGE_SIZE = 25
SALAD_DEFAULT_TIMEOUT = httpx2.Timeout(
    connect=5.0,
    read=30.0,
    write=30.0,
    pool=5.0,
)
_MAX_ERROR_BODY_LENGTH = 1_000
_HTML_TAG = re.compile(r"<[^>]*>")


def _path_segment(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must not be empty")
    return quote(value, safe="")


def _parse_model[T](data: JSONObject, parser: Callable[[JSONObject], T]) -> T:
    try:
        return parser(data)
    except ValueError as error:
        raise SaladProtocolError(str(error)) from error


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _container_group_sensitive_values(
    configuration: Mapping[str, JSONValue],
) -> tuple[str, ...]:
    """Return credential-bearing values that a provider error may echo."""

    container = configuration.get("container")
    if not isinstance(container, Mapping):
        return ()
    values: list[str] = []
    registry_authentication = container.get("registry_authentication")
    if isinstance(registry_authentication, Mapping):
        basic = registry_authentication.get("basic")
        if isinstance(basic, Mapping):
            password = basic.get("password")
            if isinstance(password, str) and password:
                values.append(password)
    environment_variables = container.get("environment_variables")
    if isinstance(environment_variables, Mapping):
        values.extend(
            value for value in environment_variables.values() if isinstance(value, str) and value
        )
    return tuple(dict.fromkeys(values))


class SaladClient:
    """Typed async adapter for the SaladCloud public REST API.

    The caller owns ``http_client`` and its lifecycle. This adapter deliberately
    does not retry mutations: Salad documents a 240 requests/minute API-key limit,
    but it does not document an idempotency key for queue job creation.
    """

    rate_limit_requests_per_minute = SALAD_REQUESTS_PER_MINUTE

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        api_key: str,
        organization: str,
        project: str,
        base_url: str = SALAD_API_BASE_URL,
        timeout: httpx2.Timeout | float = SALAD_DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError("SaladCloud API key must not be empty")
        if not base_url:
            raise ValueError("SaladCloud base URL must not be empty")
        self._http_client = http_client
        self.__api_key = api_key
        self.organization = organization
        self.project = project
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        organization_segment = _path_segment(organization, "organization")
        project_segment = _path_segment(project, "project")
        self._organization_path = f"/organizations/{organization_segment}"
        self._project_path = f"{self._organization_path}/projects/{project_segment}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"organization={self.organization!r}, "
            f"project={self.project!r}, "
            f"base_url={self.base_url!r}, "
            "api_key=<redacted>)"
        )

    def _redact(self, value: str, sensitive_values: Sequence[str] = ()) -> str:
        redacted = value.replace(self.__api_key, "[redacted]")
        for sensitive_value in sensitive_values:
            if sensitive_value:
                redacted = redacted.replace(sensitive_value, "[redacted]")
        return redacted

    def _error_details(
        self,
        response: httpx2.Response,
        *,
        sensitive_values: Sequence[str] = (),
    ) -> tuple[str, str]:
        raw_body = self._redact(response.text, sensitive_values)
        normalized_body = " ".join(html.unescape(_HTML_TAG.sub(" ", raw_body)).split())[
            :_MAX_ERROR_BODY_LENGTH
        ]
        message_parts: list[str] = []
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("title", "detail", "type"):
                value = payload.get(key)
                if isinstance(value, (str, int, float)):
                    text = self._redact(str(value), sensitive_values).strip()
                    if text and text not in message_parts:
                        message_parts.append(text)
        message = ": ".join(message_parts) or normalized_body or response.reason_phrase
        return message[:_MAX_ERROR_BODY_LENGTH], normalized_body

    def _raise_api_error(
        self,
        response: httpx2.Response,
        *,
        sensitive_values: Sequence[str] = (),
    ) -> NoReturn:
        message, response_body = self._error_details(
            response,
            sensitive_values=sensitive_values,
        )
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        if response.status_code == 429:
            raise SaladRateLimitError(
                message=message,
                response_body=response_body,
                request_id=request_id,
                retry_after_seconds=_retry_after_seconds(response.headers.get("retry-after")),
            )
        raise SaladAPIError(
            status_code=response.status_code,
            message=message,
            response_body=response_body,
            request_id=request_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        json_body: JSONObject | None = None,
        params: Mapping[str, str | int] | None = None,
        content_type: str | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> httpx2.Response:
        headers = {
            "Accept": "application/json",
            "Salad-Api-Key": self.__api_key,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        response: httpx2.Response | None = None
        request_error: SaladTimeoutError | SaladTransportError | None = None
        try:
            if json_body is None:
                response = await self._http_client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                    timeout=self._timeout,
                )
            else:
                response = await self._http_client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                )
        except httpx2.TimeoutException:
            # httpx exceptions retain their Request, including JSON bodies.
            # Raise only after leaving the handler so neither ``__cause__`` nor
            # ``__context__`` retains a credential-bearing request.
            request_error = SaladTimeoutError("SaladCloud request timed out")
        except httpx2.RequestError:
            request_error = SaladTransportError("SaladCloud request failed")
        if request_error is not None:
            raise request_error
        if response is None:
            raise SaladTransportError("SaladCloud request failed")
        if response.status_code != expected_status:
            self._raise_api_error(response, sensitive_values=sensitive_values)
        return response

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        json_body: JSONObject | None = None,
        params: Mapping[str, str | int] | None = None,
        content_type: str | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> JSONObject:
        response = await self._request(
            method,
            path,
            expected_status=expected_status,
            json_body=json_body,
            params=params,
            content_type=content_type,
            sensitive_values=sensitive_values,
        )
        try:
            payload: object = response.json()
            return as_json_object(payload, "SaladCloud response")
        except ValueError as error:
            raise SaladProtocolError("SaladCloud returned an invalid JSON object") from error

    async def get_quotas(self) -> SaladOrganizationQuotas:
        data = await self._request_json(
            "GET",
            f"{self._organization_path}/quotas",
            expected_status=200,
        )
        return _parse_model(data, parse_organization_quotas)

    async def list_gpu_classes(self) -> tuple[SaladGpuClass, ...]:
        data = await self._request_json(
            "GET",
            f"{self._organization_path}/gpu-classes",
            expected_status=200,
        )
        items = self._collection_items(data, "GPU class collection")
        return tuple(_parse_model(item, parse_gpu_class) for item in items)

    async def get_gpu_availability(
        self,
        *,
        gpu_classes: Sequence[UUID | str],
        country_codes: Sequence[str] = (),
        cpu: int | None = None,
        memory: int | None = None,
        storage_amount: int | None = None,
    ) -> SaladGpuAvailability:
        if not gpu_classes:
            raise ValueError("at least one GPU class is required")
        gpu_class_values: list[JSONValue] = []
        for gpu_class in gpu_classes:
            try:
                gpu_class_values.append(str(UUID(str(gpu_class))))
            except ValueError as error:
                raise ValueError("GPU classes must be UUIDs") from error
        payload: JSONObject = {"gpu_classes": gpu_class_values}
        if country_codes:
            payload["country_codes"] = list(country_codes)
        for name, value in (
            ("cpu", cpu),
            ("memory", memory),
            ("storage_amount", storage_amount),
        ):
            if value is not None:
                if value < 0:
                    raise ValueError(f"{name} must not be negative")
                payload[name] = value
        data = await self._request_json(
            "POST",
            f"{self._organization_path}/availability/sce-gpu-availability",
            expected_status=200,
            json_body=payload,
        )
        return _parse_model(data, parse_gpu_availability)

    async def create_queue(
        self,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> SaladQueue:
        _path_segment(name, "queue name")
        payload: JSONObject = {"name": name}
        if display_name is not None:
            payload["display_name"] = display_name
        if description is not None:
            payload["description"] = description
        data = await self._request_json(
            "POST",
            f"{self._project_path}/queues",
            expected_status=201,
            json_body=payload,
        )
        return _parse_model(data, parse_queue)

    async def get_queue(self, queue_name: str) -> SaladQueue:
        queue_segment = _path_segment(queue_name, "queue name")
        data = await self._request_json(
            "GET",
            f"{self._project_path}/queues/{queue_segment}",
            expected_status=200,
        )
        return _parse_model(data, parse_queue)

    async def list_queues(self) -> tuple[SaladQueue, ...]:
        data = await self._request_json(
            "GET",
            f"{self._project_path}/queues",
            expected_status=200,
        )
        items = self._collection_items(data, "queue collection")
        return tuple(_parse_model(item, parse_queue) for item in items)

    async def create_job(
        self,
        queue_name: str,
        *,
        input: JSONValue,
        metadata: Mapping[str, JSONValue] | None = None,
        webhook: str | None = None,
    ) -> SaladQueueJob:
        queue_segment = _path_segment(queue_name, "queue name")
        payload: JSONObject = {"input": input}
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        if webhook is not None:
            payload["webhook"] = webhook
        data = await self._request_json(
            "POST",
            f"{self._project_path}/queues/{queue_segment}/jobs",
            expected_status=201,
            json_body=payload,
        )
        return _parse_model(data, parse_queue_job)

    async def get_job(self, queue_name: str, job_id: UUID | str) -> SaladQueueJob:
        queue_segment = _path_segment(queue_name, "queue name")
        job_segment = _path_segment(str(job_id), "job ID")
        data = await self._request_json(
            "GET",
            f"{self._project_path}/queues/{queue_segment}/jobs/{job_segment}",
            expected_status=200,
        )
        return _parse_model(data, parse_queue_job)

    async def list_jobs(
        self,
        queue_name: str,
        *,
        page: int = 1,
        page_size: int = SALAD_QUEUE_JOB_PAGE_SIZE,
    ) -> SaladQueueJobPage:
        if page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= page_size <= SALAD_QUEUE_JOB_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {SALAD_QUEUE_JOB_PAGE_SIZE}")
        queue_segment = _path_segment(queue_name, "queue name")
        data = await self._request_json(
            "GET",
            f"{self._project_path}/queues/{queue_segment}/jobs",
            expected_status=200,
            params={"page": page, "page_size": page_size},
        )
        items = self._collection_items(data, "queue job collection")
        return SaladQueueJobPage(items=tuple(_parse_model(item, parse_queue_job) for item in items))

    async def cancel_job(self, queue_name: str, job_id: UUID | str) -> None:
        queue_segment = _path_segment(queue_name, "queue name")
        job_segment = _path_segment(str(job_id), "job ID")
        await self._request(
            "DELETE",
            f"{self._project_path}/queues/{queue_segment}/jobs/{job_segment}",
            expected_status=202,
        )

    async def create_container_group(
        self,
        configuration: Mapping[str, JSONValue],
    ) -> SaladContainerGroup:
        sensitive_values = _container_group_sensitive_values(configuration)
        data = await self._request_json(
            "POST",
            f"{self._project_path}/containers",
            expected_status=201,
            json_body=dict(configuration),
            sensitive_values=sensitive_values,
        )
        return _parse_model(data, parse_container_group)

    async def get_container_group(self, container_group_name: str) -> SaladContainerGroup:
        group_segment = _path_segment(container_group_name, "container group name")
        data = await self._request_json(
            "GET",
            f"{self._project_path}/containers/{group_segment}",
            expected_status=200,
        )
        return _parse_model(data, parse_container_group)

    async def list_container_groups(self) -> SaladContainerGroupPage:
        data = await self._request_json(
            "GET",
            f"{self._project_path}/containers",
            expected_status=200,
        )
        items = self._collection_items(data, "container group collection")
        return SaladContainerGroupPage(
            items=tuple(_parse_model(item, parse_container_group) for item in items)
        )

    async def list_container_group_instances(
        self,
        container_group_name: str,
    ) -> SaladContainerGroupInstancePage:
        group_segment = _path_segment(container_group_name, "container group name")
        data = await self._request_json(
            "GET",
            f"{self._project_path}/containers/{group_segment}/instances",
            expected_status=200,
        )
        values = data.get("instances")
        if not isinstance(values, list):
            raise SaladProtocolError(
                "container group instance collection.instances must be an array"
            )
        try:
            instances = tuple(
                as_json_object(item, f"container group instance collection.instances[{index}]")
                for index, item in enumerate(values)
            )
        except ValueError as error:
            raise SaladProtocolError(str(error)) from error
        return SaladContainerGroupInstancePage(
            instances=tuple(
                _parse_model(item, parse_container_group_instance) for item in instances
            )
        )

    async def update_container_group(
        self,
        container_group_name: str,
        patch: Mapping[str, JSONValue],
    ) -> SaladContainerGroup:
        group_segment = _path_segment(container_group_name, "container group name")
        data = await self._request_json(
            "PATCH",
            f"{self._project_path}/containers/{group_segment}",
            expected_status=200,
            json_body=dict(patch),
            content_type="application/merge-patch+json",
            sensitive_values=_container_group_sensitive_values(patch),
        )
        return _parse_model(data, parse_container_group)

    async def start_container_group(self, container_group_name: str) -> None:
        await self._change_container_group_state(container_group_name, "start")

    async def stop_container_group(self, container_group_name: str) -> None:
        await self._change_container_group_state(container_group_name, "stop")

    async def _change_container_group_state(
        self,
        container_group_name: str,
        action: str,
    ) -> None:
        group_segment = _path_segment(container_group_name, "container group name")
        await self._request(
            "POST",
            f"{self._project_path}/containers/{group_segment}/{action}",
            expected_status=202,
        )

    @staticmethod
    def _collection_items(data: JSONObject, context: str) -> tuple[JSONObject, ...]:
        items = data.get("items")
        if not isinstance(items, list):
            raise SaladProtocolError(f"{context}.items must be an array")
        try:
            return tuple(
                as_json_object(item, f"{context}.items[{index}]")
                for index, item in enumerate(items)
            )
        except ValueError as error:
            raise SaladProtocolError(str(error)) from error


SaladCloudClient = SaladClient
