import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"
STYLES = ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css"
SET_STATUS = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set_status.html"
EXPERIMENT_STATUS = (
    ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "experiment_detail.html"
)


def test_generation_surfaces_explain_the_shared_provider_billing_boundary() -> None:
    set_template = SET_STATUS.read_text(encoding="utf-8")
    experiment_template = EXPERIMENT_STATUS.read_text(encoding="utf-8")

    for template in (set_template, experiment_template):
        assert template.count("data-gpu-billing-initial-state") == 1
        assert template.count("data-gpu-billing-initial-elapsed") == 1
        assert template.count("data-gpu-billing-initial-instances") == 1
        assert template.count("data-gpu-billing-initial-fresh-for") == 1
        assert template.count("data-gpu-billing-initial-started-at") == 1
        assert template.count("data-gpu-billing-initial-observed-at") == 1
        assert template.count("data-gpu-billing-initial-estimated") == 1
        assert template.count("data-gpu-billing-time") == 1
        assert template.count("data-gpu-billing-state") == 1
        assert template.count("data-gpu-billing-estimate") == 1
        assert "Current shared GPU billing session" in template
        assert "Shared across generation and Experiment Lab work" in template
        assert "Starts only when Salad reports a running GPU instance." in template
        assert 'role="status"' in template
        assert 'aria-live="polite"' in template

    timer_markup = set_template.split("<time", 1)[1].split("</time>", 1)[0]
    assert "aria-live" not in timer_markup
    assert "aria-label" not in timer_markup
    assert "format(initial_gpu_billing_elapsed" in timer_markup

    assert '<section class="experiment-progress panel">' in experiment_template
    assert '<section class="experiment-progress panel" aria-live=' not in experiment_template
    experiment_status = experiment_template.split("data-experiment-progress-status", 1)[0].rsplit(
        "<p", 1
    )[1]
    assert 'role="status"' in experiment_status
    assert 'aria-live="polite"' in experiment_status


def test_shared_gpu_timer_uses_server_elapsed_time_and_a_monotonic_local_tick() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    timer = script.split("const createGpuBillingTimer", 1)[1].split(
        "function initializeLiveGeneratedAssets()", 1
    )[0]

    for state in ("not_started", "charging", "stopping", "paused", "ended", "stale"):
        assert f'"{state}"' in timer
    assert "payload.elapsed_seconds" in timer
    assert "payload.running_instances" in timer
    assert "container.dataset.gpuBillingInitialElapsed" in timer
    assert "container.dataset.gpuBillingInitialInstances" in timer
    assert "container.dataset.gpuBillingInitialFreshFor" in timer
    assert "container.dataset.gpuBillingInitialStartedAt" in timer
    assert "container.dataset.gpuBillingInitialObservedAt" in timer
    assert 'container.dataset.gpuBillingInitialEstimated === "true"' in timer
    assert "performance.now()" in timer
    assert "(monotonicDelta * runningInstances) / 1000" in timer
    assert "window.setInterval(renderTime, 1000)" in timer
    assert "window.clearInterval(tickTimer)" in timer
    assert "window.clearTimeout(freshnessTimer)" in timer
    assert '|| billingState === "paused"' in timer
    assert '|| billingState === "stale"' in timer
    assert '(billingState === "charging" || billingState === "stopping")' in timer
    assert "time.dateTime = `PT${elapsed}S`" in timer
    assert "time.textContent = formatGpuBillingSeconds(elapsed)" in timer
    assert "stateNode.textContent = label" in timer
    assert "estimate.hidden = payload.estimated !== true" in timer
    assert 'window.addEventListener("pagehide", destroy, { once: true })' in timer
    assert "Date.now()" not in timer
    assert "aria-live" not in timer


def test_shared_gpu_timer_freezes_at_the_server_freshness_deadline_and_recovers() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    timer = script.split("const createGpuBillingTimer", 1)[1].split(
        "function initializeLiveGeneratedAssets()", 1
    )[0]

    assert "const GPU_BILLING_MAX_FRESH_SECONDS = 90" in script
    assert "!Number.isInteger(payload.fresh_for_seconds)" in timer
    assert "payload.fresh_for_seconds < 0" in timer
    assert "payload.fresh_for_seconds," in timer
    assert "GPU_BILLING_MAX_FRESH_SECONDS," in timer
    assert "staleAtMonotonic = observedAtMonotonic + freshForSeconds * 1000" in timer
    assert "staleAtMonotonic - performance.now()" in timer
    assert "window.setTimeout(() =>" in timer
    assert "markStale(staleAtMonotonic)" in timer
    assert "Math.min(" in timer
    assert "Math.max(observedAtMonotonic, freezeAt)" in timer
    assert "boundedFreezeAt - observedAtMonotonic" in timer
    assert 'billingState = "stale"' in timer
    assert "estimate.hidden = false" in timer
    assert "stopTicking()" in timer
    assert "stopFreshnessTimer()" in timer

    # A later valid snapshot overwrites the stale state and re-arms both clocks.
    valid_render = timer.index("billingState = payload.state")
    rearm_deadline = timer.index("staleAtMonotonic = observedAtMonotonic + freshForSeconds * 1000")
    sync_ticker = timer.index("syncTicker();", rearm_deadline)
    assert valid_render < rearm_deadline < sync_ticker


def test_shared_gpu_timer_runtime_freezes_on_network_silence_and_recovers() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the GPU billing timer runtime contract")

    script = SCRIPT.read_text(encoding="utf-8")
    helper = script[
        script.index("const GPU_BILLING_STATES") : script.index(
            "function initializeLiveGeneratedAssets()"
        )
    ]
    harness = r"""
"use strict";
const integerValue = (value, fallback = 0) => {
  const parsed = Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
};
const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

let monotonicNow = 1_000;
let nextTimerId = 1;
const scheduled = new Map();
const schedule = (callback, delay, repeat) => {
  const id = nextTimerId++;
  scheduled.set(id, {
    callback,
    delay: Number(delay),
    repeat,
    at: monotonicNow + Number(delay),
  });
  return id;
};
const clear = (id) => scheduled.delete(id);
const advance = (milliseconds) => {
  const target = monotonicNow + milliseconds;
  while (true) {
    let next = null;
    for (const [id, job] of scheduled.entries()) {
      if (job.at > target) continue;
      if (next === null || job.at < next.job.at) next = { id, job };
    }
    if (next === null) break;
    monotonicNow = next.job.at;
    if (next.job.repeat) next.job.at += next.job.delay;
    else scheduled.delete(next.id);
    next.job.callback();
  }
  monotonicNow = target;
};

class HTMLElement {
  constructor() {
    this.dataset = {};
    this.textContent = "";
    this.hidden = true;
  }
}
class HTMLTimeElement extends HTMLElement {
  constructor() {
    super();
    this.dateTime = "";
  }
}
globalThis.HTMLElement = HTMLElement;
globalThis.HTMLTimeElement = HTMLTimeElement;
Object.defineProperty(globalThis, "performance", {
  value: { now: () => monotonicNow },
});
globalThis.window = {
  setInterval: (callback, delay) => schedule(callback, delay, true),
  clearInterval: clear,
  setTimeout: (callback, delay) => schedule(callback, delay, false),
  clearTimeout: clear,
  addEventListener: () => {},
};
"""
    harness += helper
    harness += r"""
const time = new HTMLTimeElement();
const state = new HTMLElement();
const estimate = new HTMLElement();
const container = new HTMLElement();
container.querySelector = (selector) => ({
  "[data-gpu-billing-time]": time,
  "[data-gpu-billing-state]": state,
  "[data-gpu-billing-estimate]": estimate,
})[selector] || null;
const controller = createGpuBillingTimer({
  querySelector: (selector) => selector === "[data-gpu-billing]" ? container : null,
});
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

assert(controller.render({
  state: "charging",
  elapsed_seconds: 100,
  running_instances: 1,
  session_started_at: "2026-08-09T00:00:00+00:00",
  observed_at: "2026-08-09T00:00:00+00:00",
  estimated: false,
  fresh_for_seconds: 2,
}), "valid charging snapshot should render");
assert(time.textContent === "00:01:40", "timer must start from the server total");
advance(1_000);
assert(time.textContent === "00:01:41", "timer must tick from the monotonic clock");

// No fetch or render occurs: the timer itself must enforce the freshness horizon.
advance(1_000);
assert(container.dataset.gpuBillingState === "stale", "silence must become stale");
assert(time.textContent === "00:01:42", "timer must freeze exactly at freshness deadline");
assert(estimate.hidden === false, "a stale total must be marked estimated");
assert(controller.isPending(), "stale open sessions must keep progress polling alive");
advance(60_000);
assert(time.textContent === "00:01:42", "stale timer must not extrapolate indefinitely");

assert(controller.render({
  state: "charging",
  elapsed_seconds: 250,
  running_instances: 1,
  session_started_at: "2026-08-09T00:00:00+00:00",
  observed_at: "2026-08-09T00:01:00+00:00",
  estimated: false,
  fresh_for_seconds: 10,
}), "a fresh provider snapshot should recover the timer");
assert(container.dataset.gpuBillingState === "charging", "valid update must restore charging");
assert(time.textContent === "00:04:10", "recovery must use the new authoritative total");
assert(estimate.hidden === true, "fresh authoritative total must clear estimated marker");

assert(!controller.render({
  state: "charging",
  elapsed_seconds: 250,
  running_instances: 1,
  estimated: false,
}), "missing freshness must fail closed");
assert(container.dataset.gpuBillingState === "stale", "malformed update must freeze stale");
assert(estimate.hidden === false, "malformed update must expose uncertainty");
controller.destroy();
assert(scheduled.size === 0, "destroy must clear tick and freshness timers");
"""

    result = subprocess.run(  # noqa: S603 - executable is resolved from PATH by shutil.which.
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_release_and_experiment_poll_until_shared_gpu_billing_finishes() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    release = script.split("function initializeGenerationProgress()", 1)[1].split(
        "const initializeControlledDuoBuilders", 1
    )[0]
    experiment = script.split("function initializeExperimentResults()", 1)[1].split(
        "function initializeReviewBootstrap()", 1
    )[0]

    assert "const gpuBilling = createGpuBillingTimer(panel)" in release
    assert "gpuBilling?.render(payload.gpu_billing)" in release
    assert "const releaseTerminal" in release
    assert "releaseTerminal && gpuBilling?.isPending() !== true" in release
    assert "if (!terminal) schedule(" in release

    assert "const gpuBilling = createGpuBillingTimer(root)" in experiment
    assert "gpuBilling?.render(payload.gpu_billing)" in experiment
    assert "comparisonComplete && gpuBilling?.isPending() !== true" in experiment
    assert "Shared GPU billing has not settled" in experiment
    assert "window.setTimeout(refreshProgress, 3000)" in experiment


def test_shared_gpu_timer_is_readable_and_not_orphaned_on_mobile() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert ".shared-gpu-billing-time" in styles
    assert "font-variant-numeric: tabular-nums" in styles
    assert '[data-gpu-billing][data-gpu-billing-state="charging"]' in styles
    assert '[data-gpu-billing][data-gpu-billing-state="stopping"]' in styles
    assert ".generation-gpu-billing { grid-column: 1 / -1; }" in styles
    assert ".experiment-gpu-billing { grid-template-columns: minmax(0, 1fr); }" in styles
