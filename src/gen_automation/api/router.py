from fastapi import APIRouter

from gen_automation.api.routes import (
    admin_enrollment,
    authentication,
    compliance,
    danbooru_tags,
    derivatives,
    health,
    mega_deliveries,
    publications,
    releases,
    review_tasks,
    wildcards,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(authentication.router)
api_router.include_router(admin_enrollment.router)
api_router.include_router(compliance.router)
api_router.include_router(danbooru_tags.router)
api_router.include_router(derivatives.router)
api_router.include_router(mega_deliveries.router)
api_router.include_router(publications.router)
api_router.include_router(releases.router)
api_router.include_router(review_tasks.router)
api_router.include_router(wildcards.router)
