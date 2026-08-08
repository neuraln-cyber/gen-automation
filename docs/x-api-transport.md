# X API transport assumptions

This module is an isolated, static-image-only transport. It does not contain
publishing orchestration, persistence, routes, provider credentials, or live-call
tests.

Official contracts checked on 2026-07-28:

- X documents OAuth user access tokens for posting and the `POST /2/tweets`
  endpoint: <https://docs.x.com/x-api/posts/create-post>.
- `made_with_ai: true` tells X that a post contains AI-generated media and causes
  the post to be labelled accordingly:
  <https://docs.x.com/x-api/posts/create-post>.
- X documents `POST /2/media/upload` for image upload. Its current official
  OpenAPI schema exposes both `application/json` and `multipart/form-data`, models
  `media` as either OpenAPI `format: binary` or `format: byte`, requires
  `media_category`, and documents HTTP 200:
  <https://raw.githubusercontent.com/xdevplatform/xdk/main/latest-openapi.json>.
  OpenAPI `format: byte` is a base64-encoded string, which is the JSON variant used
  here. X's media guidance also explicitly permits binary base64-encoded image
  content:
  <https://docs.x.com/x-api/media/upload-media> and
  <https://docs.x.com/x-api/media/quickstart/best-practices>.
- Static images are limited here to JPEG, PNG, or WEBP and at most 5 MB. X's
  documented post limit is four photos:
  <https://docs.x.com/x-api/media/introduction> and
  <https://docs.x.com/x-api/media/quickstart/best-practices>.
- X documents per-media adult-content warnings at `POST /2/media/metadata` under
  `metadata.sensitive_media_warning.adult_content`, with HTTP 200. The documented
  response shape is
  `data.associated_metadata.sensitive_media_warning.adult_content`:
  <https://docs.x.com/x-api/media/create-media-metadata> and
  <https://raw.githubusercontent.com/xdevplatform/xdk/main/latest-openapi.json>.

The transport sends JSON base64 for the documented one-shot image-upload flow
and requires exact HTTP 200 responses for upload and, when requested, metadata.
It also requires the upload response fields needed to publish safely (`id`,
`media_key`, `expires_after_secs`, and `size`). When `adult_content` is true (the
default), it attaches the warning and refuses to return an uploaded-media object
unless X echoes `adult_content: true`. When false, it deliberately skips the
metadata endpoint. GIF/video upload and asynchronous media processing are
intentionally out of scope. Their contracts require the chunked
initialize/append/finalize/status flow.

Post creation always sends `made_with_ai: true`, independently of the adult-media
choice.

Post text is capped at 4,096 UTF-8 bytes before request construction. This is a
transport/request-size bound, not an attempt to duplicate X's weighted text
validation, and it does not guarantee X will accept a post of that size.
The publication workflow applies a stricter 280 UTF-8-byte preflight before
uploading media, targeting ordinary X accounts; this wider transport bound is
only a defense-in-depth protocol ceiling.

X's create-post reference does not document an idempotency-key field or header.
Consequently every operation in this client is attempted exactly once. A
connect/pool failure is classified as retryable, a provider HTTP rejection is
classified as retryable or terminal by status, and a timeout or transport
failure after request bytes may have been sent is classified as ambiguous. An
ambiguous post-creation result must be reconciled before any explicit retry.

The OAuth token must be resolved externally and supplied when constructing a
client. This module keeps it only in process memory, never persists or logs it,
and redacts it from representations and provider error bodies. The caller-owned
HTTP client must likewise avoid authorization-header logging.

All requests use bounded per-operation timeouts and explicitly disable redirect
following so a bearer token cannot be forwarded by this adapter to a redirect
target.
