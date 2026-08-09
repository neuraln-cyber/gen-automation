from types import MappingProxyType
from typing import Final

WORKER_DYNAMIC_MANIFEST_MAX_BYTES: Final = 64 * 1024
WORKER_ENVIRONMENT_BINDING: Final = "GEN_WORKER_ENVIRONMENT"
WORKER_VERIFICATION_KEYS_BINDING: Final = "GEN_WORKER_VERIFICATION_KEYS"
WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING: Final = "GEN_WORKER_ALLOWED_UPLOAD_ORIGIN"
WORKER_MODEL_MANIFEST_JSON_BINDING: Final = "GEN_WORKER_MODEL_MANIFEST_JSON"
WORKER_MODEL_MANIFEST_SHA256_BINDING: Final = "GEN_WORKER_MODEL_MANIFEST_SHA256"
WORKER_ARTIFACT_BUCKET_BINDING: Final = "GEN_WORKER_ARTIFACT_BUCKET"
WORKER_ARTIFACT_REGION_BINDING: Final = "GEN_WORKER_ARTIFACT_REGION"
WORKER_ARTIFACT_ENDPOINT_URL_BINDING: Final = "GEN_WORKER_ARTIFACT_ENDPOINT_URL"
WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: Final = "GEN_WORKER_ARTIFACT_ACCESS_KEY_ID"
WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING: Final = "GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY"  # noqa: S105
WORKER_ARTIFACT_SESSION_TOKEN_BINDING: Final = "GEN_WORKER_ARTIFACT_SESSION_TOKEN"  # noqa: S105

SALAD_WORKER_RUNTIME_BINDING_REFERENCES: Final = MappingProxyType(
    {
        WORKER_ENVIRONMENT_BINDING: "deployment-config://salad-worker/environment",
        WORKER_VERIFICATION_KEYS_BINDING: ("deployment-config://salad-worker/verification-keys"),
        WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING: (
            "deployment-config://salad-worker/allowed-upload-origin"
        ),
        WORKER_MODEL_MANIFEST_JSON_BINDING: (
            "deployment-config://salad-worker/model-manifest-json"
        ),
        WORKER_MODEL_MANIFEST_SHA256_BINDING: (
            "deployment-config://salad-worker/model-manifest-sha256"
        ),
        WORKER_ARTIFACT_BUCKET_BINDING: ("deployment-config://salad-worker/artifact-bucket"),
        WORKER_ARTIFACT_REGION_BINDING: ("deployment-config://salad-worker/artifact-region"),
        WORKER_ARTIFACT_ENDPOINT_URL_BINDING: (
            "deployment-config://salad-worker/artifact-endpoint-url"
        ),
        WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: (
            "deployment-config://salad-worker/artifact-access-key-id"
        ),
        WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING: (
            "deployment-config://salad-worker/artifact-secret-access-key"
        ),
        WORKER_ARTIFACT_SESSION_TOKEN_BINDING: (
            "deployment-config://salad-worker/artifact-session-token"
        ),
    }
)

SALAD_WORKER_REQUIRED_RUNTIME_BINDINGS: Final = frozenset(
    {
        WORKER_ENVIRONMENT_BINDING,
        WORKER_VERIFICATION_KEYS_BINDING,
        WORKER_ALLOWED_UPLOAD_ORIGIN_BINDING,
        WORKER_MODEL_MANIFEST_JSON_BINDING,
        WORKER_MODEL_MANIFEST_SHA256_BINDING,
        WORKER_ARTIFACT_BUCKET_BINDING,
        WORKER_ARTIFACT_REGION_BINDING,
        WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
        WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
    }
)

SALAD_WORKER_OPTIONAL_RUNTIME_BINDINGS: Final = frozenset(
    {
        WORKER_ARTIFACT_ENDPOINT_URL_BINDING,
        WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
    }
)

SALAD_WORKER_ALLOWED_RUNTIME_BINDINGS: Final = frozenset(SALAD_WORKER_RUNTIME_BINDING_REFERENCES)
