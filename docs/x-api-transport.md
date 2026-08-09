# X API transport assumptions

This module is an isolated, static-image-only transport. It does not contain
publishing orchestration, persistence, routes, provider credentials, or live-call
tests.

Official contracts checked on 2026-08-09:

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
  The current official OpenAPI also lists OAuth 1.0a `UserToken` as an
  authorization alternative for `/2/media/upload`, `/2/media/metadata`, and
  `/2/tweets`: <https://github.com/xdevplatform/docs/blob/main/openapi.json>.
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

New and replacement X teaser revisions use a metadata-free, watermarked PNG at
the admitted master's original pixel dimensions. The frozen profile encodes PNG
compression level 6 first, then retries level 9 only as a lossless size
optimization. If level 9 still exceeds 5 MiB, preparation fails terminally as
`x_lossless_png_too_large` before upload; it never silently converts to JPEG or
downscales. This behavior is frozen as renderer `pillow-derivative-v6`;
already-frozen version 4/version 5 and JPEG recipes remain executable with
their original recipe and renderer behavior.

The uploaded PNG is the exact artifact approved by this application, but X may
derive scaled or reformatted display variants after accepting it. Consequently
X is a publishing destination, not the archival source of truth; provider-side
display bytes, dimensions, transparency, and format are not guaranteed to match
the upload exactly. See X's image guidance:
<https://help.x.com/en/using-x/posting-gifs-and-pictures>.

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

The user-context credential is resolved externally and supplied as either an
OAuth 2.0 bearer strategy or an OAuth 1.0a HMAC-SHA1 signing strategy. OAuth
1.0a is restricted to exactly `GET /2/users/me` and `POST` to
`/2/media/upload`, `/2/media/metadata`, or `/2/tweets`; queries, redirects,
other hosts, ports, methods, and paths are rejected before signing. A fresh
nonce and timestamp are used for every request. Raw and RFC3986-encoded
credential values, the exact request Authorization header, and HTML-entity
forms are removed from provider errors. Short-lived clients irreversibly clear
their authorization after an error or lease exit.

All requests use bounded per-operation timeouts and explicitly disable redirect
following so neither a bearer token nor a signed OAuth 1.0a header can be
forwarded by this adapter to a redirect target.
