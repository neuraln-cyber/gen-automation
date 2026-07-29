from collections.abc import Mapping
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.session import get_session
from gen_automation.integrations.salad.webhooks import (
    SaladWebhookBodyTooLargeError,
    SaladWebhookError,
    SaladWebhookHeaderError,
    SaladWebhookPayloadError,
    SaladWebhookSignatureError,
    SaladWebhookVerifier,
)
from gen_automation.services.salad_webhooks import (
    SaladWebhookPayloadContractError,
    SaladWebhookReplayConflictError,
    record_verified_salad_webhook,
)

router = APIRouter(tags=["provider-webhooks"])
Session = Annotated[AsyncSession, Depends(get_session)]
_SIGNED_HEADER_NAMES = frozenset(
    {
        b"webhook-id",
        b"webhook-timestamp",
        b"webhook-signature",
    }
)


def _signature_headers(request: Request) -> Mapping[str, str]:
    selected: dict[str, str] = {}
    raw_headers = cast(list[tuple[bytes, bytes]], request.scope.get("headers", []))
    for raw_name, raw_value in raw_headers:
        name = raw_name.lower()
        if name not in _SIGNED_HEADER_NAMES:
            continue
        try:
            decoded_name = name.decode("ascii")
            decoded_value = raw_value.decode("ascii")
        except UnicodeDecodeError:
            raise SaladWebhookHeaderError("invalid webhook header") from None
        if decoded_name in selected:
            raise SaladWebhookHeaderError("duplicate webhook header")
        selected[decoded_name] = decoded_value
    return selected


@router.post("/webhooks/salad", status_code=status.HTTP_204_NO_CONTENT)
async def receive_salad_webhook(request: Request, session: Session) -> Response:
    verifier: SaladWebhookVerifier | None = request.app.state.salad_webhook_verifier
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Salad webhook receiver is not configured",
        )

    try:
        callback = await verifier.verify_stream(
            chunks=request.stream(),
            headers=_signature_headers(request),
        )
        result = await record_verified_salad_webhook(session, callback)
    except SaladWebhookBodyTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="webhook body exceeds the size limit",
        ) from error
    except (
        SaladWebhookHeaderError,
        SaladWebhookSignatureError,
        SaladWebhookPayloadError,
        SaladWebhookPayloadContractError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid Salad webhook",
        ) from error
    except SaladWebhookReplayConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conflicting Salad webhook replay",
        ) from error
    except SaladWebhookError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid Salad webhook",
        ) from error

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.headers["Webhook-Replayed"] = str(result.replayed).lower()
    return response
