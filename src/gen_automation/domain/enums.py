from enum import StrEnum


class ReleasePhase(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    READY = "ready"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    RENDERING = "rendering"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class ResourceHealth(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    BLOCKED = "blocked"


class ComplianceResult(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"


class GenerationState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUBMITTING = "submitting"
    RUNNING = "running"
    COLLECTING = "collecting"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class GenerationAttemptState(StrEnum):
    CREATED = "created"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class AssetKind(StrEnum):
    RAW_MASTER = "raw_master"
    REVIEW_PROXY = "review_proxy"
    DERIVATIVE = "derivative"
    CONTACT_SHEET = "contact_sheet"
    ARCHIVE = "archive"


class AssetState(StrEnum):
    EXPECTED = "expected"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    AVAILABLE = "available"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


class InboxStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"


class SaladDeploymentState(StrEnum):
    PLANNED = "planned"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPED = "stopped"
    UNKNOWN = "unknown"
    FAILED = "failed"


class DesiredDeploymentState(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


class ExperimentWarmLeaseState(StrEnum):
    STARTING = "starting"
    ACTIVE = "active"
    ENDING = "ending"
    ENDED = "ended"
    EXPIRED = "expired"
    FAILED = "failed"


class BudgetState(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"


class SpendEntryType(StrEnum):
    USAGE = "usage"
    ADJUSTMENT = "adjustment"


class ScoringRunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"


class AssetScoreState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    SCORED = "scored"
    FLAGGED_BLANK = "flagged_blank"
    FLAGGED_CORRUPT = "flagged_corrupt"
    DEAD_LETTER = "dead_letter"


class RankingDisposition(StrEnum):
    REVIEW_CANDIDATE = "review_candidate"
    NEAR_DUPLICATE = "near_duplicate"
    FLAGGED_REVIEW = "flagged_review"


class SemanticAssessmentState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"


class SemanticVerdict(StrEnum):
    PASS = "pass"  # noqa: S105
    REVIEW = "review"
    SEVERE = "severe"


class SemanticIssueCode(StrEnum):
    EXTRA_FINGER = "extra_finger"
    MISSING_FINGER = "missing_finger"
    MALFORMED_HAND = "malformed_hand"
    EXTRA_TOE = "extra_toe"
    MISSING_TOE = "missing_toe"
    MALFORMED_FOOT = "malformed_foot"
    EXTRA_LIMB = "extra_limb"
    MISSING_LIMB = "missing_limb"
    DUPLICATE_BODY_PART = "duplicate_body_part"
    IMPOSSIBLE_JOINT = "impossible_joint"
    IMPLAUSIBLE_PROPORTION = "implausible_proportion"
    SEVERE_FACE_DEFORMATION = "severe_face_deformation"


class SemanticEnforcementMode(StrEnum):
    SHADOW = "shadow"
    ASSIST = "assist"
    ENFORCE = "enforce"


class SemanticFeedbackAgreement(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNSURE = "unsure"


class SemanticGroundTruth(StrEnum):
    ANATOMY_GOOD = "anatomy_good"
    ANATOMY_DEFECT = "anatomy_defect"
    UNJUDGEABLE = "unjudgeable"


class SemanticTrainingKind(StrEnum):
    META_CLASSIFIER = "meta_classifier"
    VISUAL_LORA = "visual_lora"


class SemanticTrainingState(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    PREPARING = "preparing"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SemanticPromotionDecision(StrEnum):
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class ReviewTaskState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReviewDecisionValue(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    HOLD = "hold"


class ReviewBulkAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    HOLD = "hold"
    X_ADD = "x_add"
    X_REMOVE = "x_remove"


class DerivativeJobState(StrEnum):
    REQUESTED = "requested"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MegaDeliveryState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FinishedSetArchiveState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    READY = "ready"
    FAILED = "failed"


class PublicationTarget(StrEnum):
    X = "x"
    PATREON = "patreon"


class PublicationIntentState(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    PROCESSING = "processing"
    AWAITING_HUMAN = "awaiting_human"
    UNKNOWN = "unknown"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationAttemptState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    AWAITING_HUMAN = "awaiting_human"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationStepKind(StrEnum):
    X_MEDIA_UPLOAD = "x_media_upload"
    X_CREATE_POST = "x_create_post"
    PATREON_PACKAGE = "patreon_package"
    PATREON_HANDOFF = "patreon_handoff"


class PublicationStepState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    AWAITING_HUMAN = "awaiting_human"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationApprovalAction(StrEnum):
    APPROVE = "approve"
    REVOKE = "revoke"


class PublicationRetryClass(StrEnum):
    SAFE_RETRY = "safe_retry"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class AdminRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    REVIEWER = "reviewer"
    PUBLISHER = "publisher"


class AdminEnrollmentState(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    REVOKED = "revoked"


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    REVOKED = "revoked"


class ModelArtifactKind(StrEnum):
    CHECKPOINT = "checkpoint"
    LORA = "lora"
