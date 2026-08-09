from pathlib import Path

ROOT = Path(__file__).parents[1]
DASHBOARD_SCRIPT = ROOT / "src/gen_automation/static/dashboard.js"
ASSET_VIEWER_SCRIPT = ROOT / "src/gen_automation/static/asset_viewer.js"
REVIEW_TEMPLATE = ROOT / "src/gen_automation/templates/dashboard/review_task.html"
RELEASE_TEMPLATE = ROOT / "src/gen_automation/templates/dashboard/release_detail.html"
STYLES = ROOT / "src/gen_automation/static/dashboard_ux.css"


def test_single_review_decisions_use_compact_json_fifo_while_legacy_actions_refresh() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewActions()", 1)[1].split("const isRecord", 1)[0]
    decision_branch = handler.split(
        'if (form.matches("[data-review-decision-form]"))',
        1,
    )[1].split("if (!(await prepareReviewMutationForm", 1)[0]
    sender = script.split("const sendReviewDecision", 1)[1].split(
        "const drainReviewDecisionQueue",
        1,
    )[0]
    legacy = handler.split("if (!(await prepareReviewMutationForm", 1)[1]

    assert "REVIEW_ACTION_FORM_SELECTOR" in script
    assert "form[data-review-decision-form]" in script
    assert "form[data-bulk-action-form]" in script
    assert "form[data-x-selection-form]" in script
    assert "event.preventDefault()" in handler

    assert "enqueueReviewDecision(form, submitter, workspace)" in decision_branch
    assert "return;" in decision_branch
    assert 'Accept: "application/json"' in sender
    assert '"Content-Type": "application/json"' in sender
    assert '"Idempotency-Key": command.idempotencyKey' in sender
    assert '"X-CSRF-Token": command.csrfToken' in sender
    assert "body: command.requestBody" in sender
    assert "/api/v1/review-tasks/${encodeURIComponent(state.taskId)}/decisions" in sender
    assert "DOMParser" not in sender
    assert "workspace.replaceWith" not in sender

    # Bulk review, X selection, and the other legacy form actions still wait for
    # an authoritative refresh and replace the workspace after their response.
    assert "await fetch(form.action" in legacy
    assert 'redirect: "follow"' in legacy
    assert "new DOMParser().parseFromString" in legacy
    assert 'parsed.querySelector("[data-review-workspace]")' in legacy
    assert "preserveReviewMedia(workspace, nextWorkspace)" in legacy
    assert "workspace.replaceWith(nextWorkspace)" in legacy
    assert "window.location.reload()" not in handler
    assert "HTMLFormElement.prototype.submit" not in handler


def test_review_decision_queue_is_serial_and_chains_returned_lock_versions() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    sender = script.split("const sendReviewDecision", 1)[1].split(
        "const drainReviewDecisionQueue",
        1,
    )[0]
    drain = script.split("const drainReviewDecisionQueue", 1)[1].split(
        "const enqueueReviewDecision",
        1,
    )[0]

    assert "if (!command.requestBody)" in sender
    assert "command.expectedLockVersion = state.lockVersion" in sender
    assert "command.requestBody = JSON.stringify" in sender
    assert "expected_lock_version: command.expectedLockVersion" in sender
    assert "body: command.requestBody" in sender
    assert '"Idempotency-Key": command.idempotencyKey' in sender
    assert "payload.task_lock_version" in sender
    assert "integerValue(payload.task_lock_version, 0) <= command.expectedLockVersion" in sender

    assert "state.drainPromise" in drain
    assert "|| state.paused" in drain
    assert "|| reviewActionRequestActive" in drain
    assert "|| reviewCompletionRequestActive" in drain
    assert "while (!state.paused && state.queue.length > 0)" in drain
    assert "const command = state.queue[0]" in drain
    assert "const result = await sendReviewDecision(command, state)" in drain
    assert drain.index("await sendReviewDecision") < drain.index("state.queue.shift()")
    assert "state.lockVersion = integerValue(result.payload.task_lock_version" in drain
    assert drain.index("state.lockVersion =") < drain.index("state.queue.shift()")


def test_review_decision_retries_reuse_the_exact_request_and_idempotency_key() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    sender = script.split("const sendReviewDecision", 1)[1].split(
        "const drainReviewDecisionQueue",
        1,
    )[0]
    drain = script.split("const drainReviewDecisionQueue", 1)[1].split(
        "const enqueueReviewDecision",
        1,
    )[0]

    assert "if (!command.requestBody)" in sender
    assert sender.count("command.requestBody = JSON.stringify") == 1
    assert '"Idempotency-Key": command.idempotencyKey' in sender
    assert "body: command.requestBody" in sender
    assert 'if (result.kind === "retry")' in drain
    retry = drain.split('if (result.kind === "retry")', 1)[1].split(
        'if (result.kind === "authentication")',
        1,
    )[0]
    assert "command.retryCount += 1" in retry
    assert "await delay(retryDelay(command.retryCount))" in retry
    assert "continue" in retry
    assert "state.queue.shift()" not in retry


def test_each_queued_decision_captures_its_own_anatomy_reason() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    reason = script.split("const anatomyReasonFor", 1)[1].split(
        "const reviewCardSnapshot",
        1,
    )[0]
    enqueue = script.split("const enqueueReviewDecision", 1)[1].split(
        "async function ensureReviewAuthoritativeRefresh",
        1,
    )[0]

    assert 'decision === "reject"' in reason
    assert "anatomyToggle.checked" in reason
    assert 'anatomyIssue.value || "anatomy"' in reason
    assert "return { anatomyRequested, reason, anatomyReasons }" in reason
    assert "const { anatomyRequested, reason } = anatomyReasonFor(form, decision)" in enqueue
    assert "anatomyRequested," in enqueue
    assert "reason," in enqueue
    assert "state.queue.push(command)" in enqueue
    assert enqueue.index("anatomyReasonFor(form, decision)") < enqueue.index(
        "state.queue.push(command)"
    )


def test_definitive_failure_rolls_back_or_rebases_then_reconciles_authoritatively() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    reject = script.split("const rejectQueuedReviewDecisions", 1)[1].split(
        "function scheduleReviewDecisionReconciliation",
        1,
    )[0]
    drain = script.split("const drainReviewDecisionQueue", 1)[1].split(
        "const enqueueReviewDecision",
        1,
    )[0]
    rebase = script.split("const rebaseQueuedReviewDecisions", 1)[1].split(
        "function scheduleReviewDecisionReconciliation",
        1,
    )[0]

    assert "const rejected = state.queue.splice(0)" in reject
    assert "[...rejected].reverse().forEach" in reject
    assert "restoreReviewCardSnapshot(card, command.snapshot)" in reject
    assert "setReviewCardPending(command.assetId, -1)" in reject
    assert "const reconciled = await compactReviewSummary(state)" in reject
    assert "state.paused = !reconciled" in reject
    assert 'if (result.kind === "definitive")' in drain
    assert "result.response?.status === 409" in drain
    assert "await rebaseQueuedReviewDecisions()" in drain
    assert 'await rejectQueuedReviewDecisions("request")' in drain
    assert "const reconciled = await compactReviewSummary(state)" in rebase
    assert "applyRebasedReviewDecisions(state)" in rebase
    assert "void drainReviewDecisionQueue()" in rebase


def test_release_auto_bootstraps_the_single_review_workspace() -> None:
    bootstrap = RELEASE_TEMPLATE.read_text(encoding="utf-8")
    workspace = REVIEW_TEMPLATE.read_text(encoding="utf-8")
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    initializer = script.split("function initializeReviewBootstrap()", 1)[1].split(
        "const experimentFormPresent",
        1,
    )[0]

    assert "data-review-bootstrap" in bootstrap
    assert 'action="/dashboard/releases/{{ release_id }}/review-tasks"' in bootstrap
    assert "Preparing your review workspace" in bootstrap
    assert "Start ranked review" not in bootstrap
    assert "Open ranked review" not in bootstrap
    assert "Continue review" not in bootstrap

    assert "data-review-workspace" in workspace
    assert "data-open-review-viewer" in workspace
    assert "everything starts kept" in workspace
    assert "data-anatomy-feedback-form" not in workspace
    assert 'data-decision="hold"' not in workspace
    assert "Hold selected" not in workspace

    assert 'document.querySelector("form[data-review-bootstrap]")' in initializer
    assert "window.requestAnimationFrame(submit)" in initializer
    assert "form.requestSubmit()" in initializer
    assert 'form.dataset.reviewBootstrapSubmitted = "true"' in initializer


def test_review_refresh_restores_scroll_filters_sort_selection_and_focus() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "captureReviewViewState" in script
    assert "reviewScrollAnchor" in script
    assert "restoreReviewViewState" in script
    assert "const preserveReviewMedia" in script
    assert 'card.querySelector(".asset-preview")' in script
    assert "nextImage.replaceWith(currentImage)" in script
    assert "window.requestAnimationFrame(restoreScroll)" in script
    assert "window.requestAnimationFrame(() => {\n      restoreScroll();" not in script
    assert 'document.querySelector("[data-review-filter].active")' in script
    assert 'document.querySelector("[data-asset-sort].active")' in script
    assert "state.selectedAssetIds.forEach" in script
    assert "anchor.getBoundingClientRect().top - state.anchor.top" in script
    assert "window.scrollBy(0, delta)" in script
    assert "focus({ preventScroll: true })" in script
    assert 'new CustomEvent("gen-automation:assets-updated"' in script
    assert 'new CustomEvent("gen-automation:review-updated"' in script
    assert "&& !reviewViewerIsOpen()" in script


def test_bulk_shift_range_follows_the_current_visible_sorted_dom_order() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    bulk = script.split("function initializeBulkReview()", 1)[1].split(
        "let reviewActionRequestActive",
        1,
    )[0]
    range_selection = bulk.split('checkbox.addEventListener("click"', 1)[1].split(
        'checkbox.addEventListener("change"',
        1,
    )[0]

    assert 'const selectionRoot = form.closest("[data-review-workspace]") || document' in bulk
    assert "const visibleCheckboxesInDomOrder" in bulk
    assert "selectionRoot.querySelectorAll" in bulk
    assert "const visible = visibleCheckboxesInDomOrder()" in range_selection
    assert "const visible = checkboxes.filter" not in range_selection
    assert 'new CustomEvent("gen-automation:bulk-selection-changed"' in bulk
    assert "hiddenSelected," in bulk
    assert "selected," in bulk


def test_bulk_eligibility_reacts_to_optimistic_single_image_decisions() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    bulk = script.split("function initializeBulkReview()", 1)[1].split(
        "let reviewActionRequestActive",
        1,
    )[0]
    apply_decision = script.split("const applyReviewCardDecision", 1)[1].split(
        "const restoreReviewCardSnapshot",
        1,
    )[0]

    assert "let acceptedCount" in bulk
    assert "let remainingReviewSlots" in bulk
    assert "const updateTargetSelectionButtons" in bulk
    assert "updateTargetSelectionButtons();" in bulk
    assert "form.dataset.acceptedCount" in bulk
    assert 'input[type="checkbox"][name="asset_id"]:checked' in apply_decision
    assert (
        'selectedCheckbox.dispatchEvent(new Event("change", { bubbles: true }))' in apply_decision
    )


def test_review_page_has_replaceable_workspace_and_live_action_status() -> None:
    template = REVIEW_TEMPLATE.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "data-review-workspace" in template
    assert "data-review-action-status" in template
    assert 'aria-live="polite"' in template
    assert ".review-action-status" in styles
    assert '[data-review-workspace][aria-busy="true"]' in styles


def test_review_completion_hands_off_inspections_with_a_bounded_retryable_request() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewCompletionInspectionFlush()", 1)[1].split(
        "function initializeReviewActions()",
        1,
    )[0]

    assert 'form.matches("[data-review-complete-form]")' in handler
    assert "event.preventDefault()" in handler
    assert "await prepareReviewMutationForm(form, submitter)" in handler
    assert "captureCompletionInspectionIds(form)" in handler
    assert "REVIEW_INSPECTION_FLUSH_TIMEOUT_MS" not in script
    assert '"gen-automation:inspection-flush-progress"' not in handler
    assert 'submitter.textContent = "Finishing set..."' in handler
    assert "reviewActionStatusTimers" in script
    assert "false,\n          0," in handler
    assert 'form.dataset.completionSubmitting = "true"' in handler
    assert "REVIEW_COMPLETION_TIMEOUT_MS = 25000" in script
    assert "await fetch(action, options)" in handler
    assert 'redirect: "follow"' in handler
    assert "controller.abort()" in handler
    assert 'error.name === "AbortError"' in handler
    assert "Nothing was lost; click Finish again." in handler
    assert "window.location.assign(destination.href)" in handler
    assert "delete form.dataset.completionSubmitting" in handler
    assert "form.requestSubmit" not in handler
    assert "inspectionFlushPending" not in handler
    request = script.split("const attachCompletionInspectionIds", 1)[1].split(
        "function initializeReviewCompletionInspectionFlush()",
        1,
    )[0]
    assert 'field.name = "inspected_asset_id"' in request
    assert "field.dataset.completionInspectionId" in request
    assert "attachCompletionInspectionIds(form, assetIds)" in request
    assert '"gen-automation:inspection-completion-handoff"' in request


def test_finish_bulk_and_x_wait_for_authoritative_refresh_after_queued_decisions() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    prepare = script.split("const prepareReviewMutationForm", 1)[1].split(
        'window.addEventListener("online"',
        1,
    )[0]
    completion = script.split(
        "function initializeReviewCompletionInspectionFlush()",
        1,
    )[1].split("function initializeReviewActions()", 1)[0]
    actions = script.split("function initializeReviewActions()", 1)[1].split(
        "const isRecord",
        1,
    )[0]
    decision_branch_end = actions.index("if (!(await prepareReviewMutationForm")
    decision_branch = actions[:decision_branch_end]
    legacy_actions = actions[decision_branch_end:]

    assert "await waitForReviewDecisionIdle()" in prepare
    assert "await ensureReviewAuthoritativeRefresh({ force: true })" in prepare
    assert 'identity.kind === "bulk"' in script
    assert 'identity.kind === "x"' in script
    assert 'identity.kind === "complete"' in script
    assert "freshForm.requestSubmit" in prepare

    assert "await prepareReviewMutationForm(form, submitter)" in completion
    assert "await prepareReviewMutationForm(form, submitter)" not in decision_branch
    assert "await prepareReviewMutationForm(form, submitter)" in legacy_actions
    assert 'form.matches("[data-bulk-action-form]")' in script
    assert 'form.matches("[data-x-selection-form]")' in script


def test_successful_bulk_refresh_clears_the_shared_grid_and_fullscreen_selection() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    capture = script.split("const captureReviewViewState", 1)[1].split(
        "const preserveReviewMedia",
        1,
    )[0]
    restore = script.split("const restoreReviewViewState", 1)[1].split(
        "const showReviewActionStatus",
        1,
    )[0]

    assert 'clearSelection: form.matches("[data-bulk-action-form]")' in capture
    assert "if (!state.clearSelection)" in restore
    assert 'new CustomEvent("gen-automation:assets-updated"' in restore


def test_optimistic_review_updates_filters_finish_state_and_avoids_idle_full_refresh() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    apply_decision = script.split("const applyReviewCardDecision", 1)[1].split(
        "const restoreReviewCardSnapshot",
        1,
    )[0]
    counts = script.split("const refreshOptimisticReviewCounts", 1)[1].split(
        "const anatomyReasonFor",
        1,
    )[0]
    drain = script.split("const drainReviewDecisionQueue", 1)[1].split(
        "const enqueueReviewDecision",
        1,
    )[0]

    assert "applyActiveReviewFilterToCard(workspace, card)" in apply_decision
    assert 'finishButton.dataset.reviewNonCountReady !== "true"' in counts
    assert "accepted < 1" in counts and "accepted > target" in counts
    assert "scheduleReviewAuthoritativeRefresh" not in script
    assert "ensureReviewAuthoritativeRefresh" not in drain


def test_reconciliation_and_legacy_mutations_preserve_newly_queued_choices() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    reject = script.split("const rejectQueuedReviewDecisions", 1)[1].split(
        "const applyRebasedReviewDecisions",
        1,
    )[0]
    legacy = script.split("function initializeReviewActions()", 1)[1].split(
        "const isRecord",
        1,
    )[0]

    assert "if (state.queue.length > 0) applyRebasedReviewDecisions(state)" in reject
    assert "state.needsRebase = state.queue.length > 0" in reject
    assert "if (state.queue.length > 0) void drainReviewDecisionQueue()" in reject
    assert "applyRebasedReviewDecisions(state)" in legacy
    assert "setReviewCardPending(command.assetId, 1)" in legacy
    assert "void drainReviewDecisionQueue()" in legacy


def test_asset_viewer_rebinds_review_controls_after_in_place_refresh() -> None:
    script = ASSET_VIEWER_SCRIPT.read_text(encoding="utf-8")
    refresh_listener = script.split(
        'document.addEventListener("gen-automation:assets-updated"',
        1,
    )[1].split(
        'document.addEventListener("gen-automation:review-action-settled"',
        1,
    )[0]

    assert "const boundCards = new WeakSet()" in script
    assert "bindCards(root || document)" in refresh_listener
    assert "const activeAssetId = activeCard?.dataset.assetId" in refresh_listener
    assert "if (viewer.dialog.open && activeAssetId)" in refresh_listener
    assert "assetCards().find" in refresh_listener
    assert "candidate.dataset.assetId === activeAssetId" in refresh_listener
    assert "renderCard(refreshedCard)" in refresh_listener
    assert refresh_listener.index("bindCards(root || document)") < refresh_listener.index(
        "renderCard(refreshedCard)"
    )


def test_review_refresh_notifies_the_viewer_after_replacing_the_workspace() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewActions()", 1)[1].split("const isRecord", 1)[0]
    restore = script.split("const restoreReviewViewState", 1)[1].split(
        "const showReviewActionStatus",
        1,
    )[0]

    replace_index = handler.index("workspace.replaceWith(nextWorkspace)")
    restore_call_index = handler.index("restoreReviewViewState(nextWorkspace, viewState)")
    success_index = handler.index("actionSucceeded = true")

    assert 'new CustomEvent("gen-automation:assets-updated"' in restore
    assert replace_index < restore_call_index < success_index


def test_server_round_trip_forms_preserve_scroll_application_wide() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "SAME_PAGE_SCROLL_STORAGE_KEY" in script
    assert "persistSamePageScroll" in script
    assert "initializeSamePageScrollPreservation" in script
    assert 'document.addEventListener("submit"' in script
    assert "window.sessionStorage.setItem(SAME_PAGE_SCROLL_STORAGE_KEY" in script
    assert "window.sessionStorage.removeItem(SAME_PAGE_SCROLL_STORAGE_KEY)" in script
    assert "window.scrollTo(saved.x, saved.y)" in script
    assert "persistSamePageScroll();\n        window.location.reload();" in script
