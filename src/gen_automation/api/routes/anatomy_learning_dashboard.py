"""Owner-facing status page for personalized anatomy learning."""

from collections import defaultdict
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.browser_anatomy_learning_forms import (
    AnatomyLearningFormError,
    AnatomyLearningPolicyForm,
    anatomy_learning_form_token,
    form_token_matches,
    read_anatomy_learning_policy_form,
    read_anatomy_learning_train_form,
)
from gen_automation.api.security import (
    ReleaseReader,
    authentication_service,
    require_release_manager,
)
from gen_automation.config import Settings
from gen_automation.db.models import (
    SemanticLearningPolicy,
    SemanticModelPromotion,
    SemanticTrainingRun,
)
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.middleware import content_security_policy
from gen_automation.services.authentication import (
    AuthenticatedPrincipal,
    CsrfValidationError,
)
from gen_automation.services.semantic_learning import (
    SemanticLearningError,
    SemanticLearningNotReadyError,
    ensure_semantic_learning_policy,
    request_meta_training_run,
    update_semantic_learning_policy,
)
from gen_automation.services.semantic_learning_readiness import (
    build_semantic_learning_readiness_report,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
_RECENT_HISTORY_LIMIT = 20
_NOTICES = {
    "policy-saved": "Learning settings saved. No GPU job was started.",
    "cpu-training-queued": (
        "Free CPU challenger queued (or already queued) for this exact dataset. "
        "No GPU job was started."
    ),
}


@router.get(
    "/anatomy-learning",
    response_class=HTMLResponse,
    name="dashboard_anatomy_learning",
)
async def dashboard_anatomy_learning(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    """Show owner-scoped labels, readiness gates, and training history."""

    if principal.role != AdminRole.OWNER:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Anatomy learning is owner-only",
            message=(
                "Only the owner can view personalized training data and model "
                "promotion history."
            ),
        )

    policy = await _initialize_owner_policy(
        session,
        owner_user_id=principal.user_id,
    )
    readiness = await build_semantic_learning_readiness_report(
        session,
        owner_user_id=principal.user_id,
    )
    training_runs = tuple(
        (
            await session.scalars(
                select(SemanticTrainingRun)
                .where(SemanticTrainingRun.owner_user_id == principal.user_id)
                .order_by(
                    SemanticTrainingRun.created_at.desc(),
                    SemanticTrainingRun.id.desc(),
                )
                .limit(_RECENT_HISTORY_LIMIT)
            )
        ).all()
    )
    promotions = tuple(
        (
            await session.scalars(
                select(SemanticModelPromotion)
                .where(SemanticModelPromotion.owner_user_id == principal.user_id)
                .order_by(
                    SemanticModelPromotion.created_at.desc(),
                    SemanticModelPromotion.id.desc(),
                )
                .limit(_RECENT_HISTORY_LIMIT)
            )
        ).all()
    )

    runs_by_profile: dict[str, list[SemanticTrainingRun]] = defaultdict(list)
    for training_run in training_runs:
        runs_by_profile[training_run.profile_sha256].append(training_run)
    promotions_by_profile: dict[str, list[SemanticModelPromotion]] = defaultdict(list)
    for promotion in promotions:
        promotions_by_profile[promotion.profile_sha256].append(promotion)

    csrf_token = _form_csrf_token(request, principal)
    settings: Settings = request.app.state.settings
    policy_form_key = (
        anatomy_learning_form_token(
            settings,
            session_id=principal.session_id,
            action="policy",
            parts=(str(policy.lock_version),),
        )
        if policy is not None
        else None
    )
    train_form_keys = {
        profile.profile_sha256: anatomy_learning_form_token(
            settings,
            session_id=principal.session_id,
            action="train-meta",
            parts=(profile.profile_sha256, profile.dataset_sha256),
        )
        for profile in readiness.profiles
        if profile.meta_classifier.ready and profile.meta_evaluation.ready
    }

    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/anatomy_learning.html",
            context={
                "page_title": "Anatomy learning",
                "principal": principal,
                "readiness": readiness,
                "policy": policy,
                "training_runs": training_runs,
                "promotions": promotions,
                "runs_by_profile": dict(runs_by_profile),
                "promotions_by_profile": dict(promotions_by_profile),
                "csrf_token": csrf_token,
                "policy_form_key": policy_form_key,
                "train_form_keys": train_form_keys,
                "notice": _NOTICES.get(request.query_params.get("notice", "")),
            },
        ),
    )


@router.post(
    "/anatomy-learning/policy",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_anatomy_learning_policy",
)
async def update_dashboard_anatomy_learning_policy(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    """Update the owner's standing policy without requiring fresh reauthentication."""

    if principal.role != AdminRole.OWNER:
        return _owner_only_error(request, principal)
    try:
        form = await read_anatomy_learning_policy_form(request)
        await _authorize_form_mutation(
            request,
            session=session,
            principal=principal,
            csrf_token=form.csrf_token,
        )
        settings: Settings = request.app.state.settings
        expected_key = anatomy_learning_form_token(
            settings,
            session_id=principal.session_id,
            action="policy",
            parts=(str(form.expected_lock_version),),
        )
        if not form_token_matches(form.idempotency_key, expected_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="form idempotency validation failed",
            )
        existing = await session.get(SemanticLearningPolicy, principal.user_id)
        if existing is not None and _is_policy_replay(existing, form):
            return _redirect("policy-saved")
        await update_semantic_learning_policy(
            session,
            owner_user_id=principal.user_id,
            expected_lock_version=form.expected_lock_version,
            learning_enabled=form.learning_enabled,
            auto_train_meta=form.auto_train_meta,
            auto_train_visual=form.auto_train_visual,
            auto_promote_validated=form.auto_promote_validated,
            max_visual_run_microusd=form.max_visual_run_microusd,
            minimum_new_labels_for_retrain=form.minimum_new_labels_for_retrain,
        )
        await session.commit()
    except AnatomyLearningFormError as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=error.status_code,
            message="The learning settings form was invalid. Reload and try again.",
        )
    except HTTPException as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=error.status_code,
            message="The browser session or form could not be verified. Reload and try again.",
        )
    except SemanticLearningError as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            message=str(error).capitalize() + ".",
        )
    return _redirect("policy-saved")


@router.post(
    "/anatomy-learning/train",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_anatomy_learning_train",
)
async def train_dashboard_anatomy_learning_model(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    """Idempotently queue a ready CPU-only challenger for the current snapshot."""

    if principal.role != AdminRole.OWNER:
        return _owner_only_error(request, principal)
    try:
        form = await read_anatomy_learning_train_form(request)
        await _authorize_form_mutation(
            request,
            session=session,
            principal=principal,
            csrf_token=form.csrf_token,
        )
        settings: Settings = request.app.state.settings
        expected_key = anatomy_learning_form_token(
            settings,
            session_id=principal.session_id,
            action="train-meta",
            parts=(form.profile_sha256, form.dataset_sha256),
        )
        if not form_token_matches(form.idempotency_key, expected_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="form idempotency validation failed",
            )
        policy, _created = await ensure_semantic_learning_policy(
            session,
            owner_user_id=principal.user_id,
        )
        if not policy.learning_enabled:
            raise SemanticLearningError("enable learning before requesting training")
        training_run = await request_meta_training_run(
            session,
            owner_user_id=principal.user_id,
            profile_sha256=form.profile_sha256,
        )
        if training_run.dataset_sha256 != form.dataset_sha256:
            raise SemanticLearningError(
                "the learning dataset changed; reload before requesting training"
            )
        await session.commit()
    except AnatomyLearningFormError as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=error.status_code,
            message="The training request was invalid. Reload and try again.",
        )
    except HTTPException as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=error.status_code,
            message="The browser session or form could not be verified. Reload and try again.",
        )
    except SemanticLearningNotReadyError as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            message="Training is not ready: " + "; ".join(error.blockers) + ".",
        )
    except SemanticLearningError as error:
        await session.rollback()
        return _mutation_error(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            message=str(error).capitalize() + ".",
        )
    return _redirect("cpu-training-queued")


async def _initialize_owner_policy(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
) -> SemanticLearningPolicy | None:
    try:
        policy, created = await ensure_semantic_learning_policy(
            session,
            owner_user_id=owner_user_id,
        )
        if created:
            await session.commit()
        return policy
    except SemanticLearningError:
        # Development bypass principals deliberately have no persisted AdminUser.
        await session.rollback()
        return await session.get(SemanticLearningPolicy, owner_user_id)


async def _authorize_form_mutation(
    request: Request,
    *,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    csrf_token: str,
) -> None:
    manager = await require_release_manager(
        request,
        session,
        csrf_header=csrf_token,
    )
    if manager.role != AdminRole.OWNER or manager.user_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner role required",
        )
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled and not form_token_matches(
        csrf_token,
        anatomy_learning_form_token(
            settings,
            session_id=principal.session_id,
            action="csrf",
            parts=(),
        ),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )


def _form_csrf_token(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> str:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return anatomy_learning_form_token(
            settings,
            session_id=principal.session_id,
            action="csrf",
            parts=(),
        )
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name)
    if cookie_token is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF cookie is unavailable",
        )
    try:
        authentication_service(request).validate_csrf(
            principal,
            cookie_token=cookie_token,
            header_token=cookie_token,
        )
    except CsrfValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        ) from error
    return cookie_token


def _is_policy_replay(
    policy: SemanticLearningPolicy,
    form: AnatomyLearningPolicyForm,
) -> bool:
    return bool(
        policy.lock_version == form.expected_lock_version + 1
        and policy.learning_enabled == form.learning_enabled
        and policy.auto_train_meta == form.auto_train_meta
        and policy.auto_train_visual == form.auto_train_visual
        and policy.auto_promote_validated == form.auto_promote_validated
        and policy.minimum_new_labels_for_retrain
        == form.minimum_new_labels_for_retrain
        and policy.max_visual_run_microusd == form.max_visual_run_microusd
    )


def _redirect(notice: str) -> Response:
    return RedirectResponse(
        url=f"/dashboard/anatomy-learning?notice={notice}",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "private, no-store"},
    )


def _owner_only_error(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> Response:
    return _error_response(
        request,
        principal=principal,
        status_code=status.HTTP_403_FORBIDDEN,
        heading="Anatomy learning is owner-only",
        message="Only the owner can change personalized learning settings.",
    )


def _mutation_error(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    status_code: int,
    message: str,
) -> Response:
    return _error_response(
        request,
        principal=principal,
        status_code=status_code,
        heading="Learning change was not saved",
        message=message,
    )


def _secure_response(request: Request, response: Response) -> Response:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = content_security_policy(
        settings.environment
    )
    return response


def _error_response(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    status_code: int,
    heading: str,
    message: str,
) -> Response:
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/error.html",
            context={
                "page_title": heading,
                "principal": principal,
                "heading": heading,
                "message": message,
            },
            status_code=status_code,
        ),
    )
