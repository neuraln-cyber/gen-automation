(() => {
  "use strict";

  const STORAGE_KEY = "gen-automation.asset-density.v1";
  const DENSITIES = Object.freeze(["comfortable", "compact", "large"]);
  const IMAGE_LOAD_ERROR = (
    "The full-size preview could not be loaded. Reload the page to refresh its private link."
  );
  const INSPECTION_BATCH_SIZE = 25;
  const INSPECTION_IDLE_FLUSH_MS = 5000;
  const INSPECTION_RETRY_MS = 2500;
  const INSPECTION_REQUEST_TIMEOUT_MS = 8000;
  const preloadedSources = new Set();

  const createElement = (tagName, className, text) => {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };

  const isDensity = (value) => DENSITIES.includes(value);

  function storedDensity() {
    try {
      const value = window.localStorage.getItem(STORAGE_KEY);
      return isDensity(value) ? value : "comfortable";
    } catch (_error) {
      return "comfortable";
    }
  }

  function persistDensity(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch (_error) {
      // Storage can be disabled without making the viewer unusable.
    }
  }

  function densityButtons() {
    return Array.from(document.querySelectorAll("[data-asset-density-value]"));
  }

  function applyDensity(value, persist = false) {
    const density = isDensity(value) ? value : "comfortable";
    document.querySelectorAll("[data-asset-grid]").forEach((grid) => {
      grid.dataset.assetDensity = density;
    });
    densityButtons().forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.assetDensityValue === density));
    });
    if (persist) persistDensity(density);
  }

  function createDensityControls(firstGrid) {
    const controls = createElement("div", "asset-grid-density");
    controls.dataset.assetDensityControls = "";
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "Image card size");

    controls.append(createElement("span", "asset-grid-density-label", "Image size"));
    [
      ["comfortable", "Comfortable"],
      ["compact", "Compact"],
      ["large", "Large"],
    ].forEach(([value, label]) => {
      const button = createElement("button", "asset-density-button", label);
      button.type = "button";
      button.dataset.assetDensityValue = value;
      button.setAttribute("aria-pressed", "false");
      controls.append(button);
    });

    firstGrid.parentNode.insertBefore(controls, firstGrid);
    return controls;
  }

  function initializeDensityControls() {
    const firstGrid = document.querySelector("[data-asset-grid]");
    if (!firstGrid) return;

    if (!document.querySelector("[data-asset-density-controls]")) {
      createDensityControls(firstGrid);
    }
    densityButtons().forEach((button) => {
      button.addEventListener("click", () => {
        applyDensity(button.dataset.assetDensityValue, true);
      });
    });
    applyDensity(storedDensity());
  }

  function assetCards() {
    return Array.from(
      document.querySelectorAll(
        "[data-asset-grid] [data-asset-card], [data-asset-grid] .asset-card",
      ),
    );
  }

  function imageFor(card) {
    return card.querySelector("[data-asset-viewer-image], .asset-preview");
  }

  function triggerFor(card) {
    return card.querySelector("[data-asset-viewer-trigger]") || imageFor(card);
  }

  function ensureTrigger(card, image) {
    const existing = card.querySelector("[data-asset-viewer-trigger]");
    if (existing) return existing;

    const media = image.closest(".asset-media") || image.parentElement;
    if (!media) return image;

    const button = createElement("button", "asset-viewer-open-button", "Full screen");
    button.type = "button";
    button.dataset.assetViewerTrigger = "";
    media.append(button);
    return button;
  }

  function downloadFor(card) {
    return card.querySelector("[data-asset-download], a.download");
  }

  function isVisible(card) {
    if (card.hidden || card.getAttribute("aria-hidden") === "true") return false;
    return card.getClientRects().length > 0;
  }

  function visibleCards() {
    return assetCards().filter((card) => imageFor(card) && isVisible(card));
  }

  function cardRank(card) {
    const label = card.dataset.assetLabel && card.dataset.assetLabel.trim();
    if (label) return label;
    const rank = card.dataset.rank && card.dataset.rank.trim();
    if (rank) return `Rank ${rank.replace(/^#/, "#")}`;
    const rankNode = card.querySelector(".rank, .asset-rank-badge:not(.asset-score-badge)");
    return rankNode && rankNode.textContent.trim() ? rankNode.textContent.trim() : "Generated image";
  }

  function cardScore(card) {
    const score = card.dataset.score && card.dataset.score.trim();
    if (score) return score;
    const scoreNode = card.querySelector(".asset-score-badge, .score");
    return scoreNode && scoreNode.textContent.trim() ? scoreNode.textContent.trim() : "";
  }

  function sourceFor(card) {
    const trigger = triggerFor(card);
    const image = imageFor(card);
    return (
      (trigger && trigger.dataset.assetViewerSrc)
      || card.dataset.assetViewUrl
      || (image && (image.currentSrc || image.src))
      || ""
    );
  }

  function cardDetails(card) {
    const image = imageFor(card);
    const download = downloadFor(card);
    return {
      alt: image && image.alt ? image.alt : `${cardRank(card)} generated image`,
      downloadHref: download && download.href ? download.href : "",
      rank: cardRank(card),
      score: cardScore(card),
      source: sourceFor(card),
    };
  }

  function isEditableTarget(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest(
      'input, textarea, select, [contenteditable=""], [contenteditable="true"], [role="textbox"]',
    ));
  }

  function createViewer() {
    const dialog = createElement("dialog", "asset-viewer");
    dialog.id = "asset-viewer-dialog";
    dialog.dataset.assetViewer = "";
    dialog.setAttribute("aria-labelledby", "asset-viewer-title");
    dialog.setAttribute("aria-describedby", "asset-viewer-counter");

    const shell = createElement("div", "asset-viewer-shell");
    const header = createElement("header", "asset-viewer-header");
    const heading = createElement("div", "asset-viewer-heading");
    const counter = createElement("p", "asset-viewer-counter", "Image 1 of 1");
    counter.id = "asset-viewer-counter";
    counter.setAttribute("aria-live", "polite");
    const title = createElement("h2", "asset-viewer-title", "Generated image");
    title.id = "asset-viewer-title";
    const score = createElement("p", "asset-viewer-score");
    heading.append(counter, title, score);

    const headerActions = createElement("div", "asset-viewer-header-actions");
    const fitToggle = createElement("button", "asset-viewer-fit-toggle", "View at 100%");
    fitToggle.type = "button";
    fitToggle.dataset.assetViewerFitToggle = "";
    fitToggle.setAttribute("aria-pressed", "false");
    fitToggle.setAttribute("aria-label", "View image at 100% and allow scrolling");
    const close = createElement("button", "asset-viewer-close", "Close");
    close.type = "button";
    close.dataset.assetViewerClose = "";
    close.setAttribute("aria-label", "Close full-screen image viewer");
    headerActions.append(fitToggle, close);
    header.append(heading, headerActions);

    const media = createElement("div", "asset-viewer-media");
    const previous = createElement("button", "asset-viewer-step asset-viewer-previous", "\u2190");
    previous.type = "button";
    previous.dataset.assetViewerPrevious = "";
    previous.setAttribute("aria-label", "View previous image");
    const image = createElement("img", "asset-viewer-image");
    image.dataset.assetViewerDisplay = "";
    image.decoding = "async";
    const next = createElement("button", "asset-viewer-step asset-viewer-next", "\u2192");
    next.type = "button";
    next.dataset.assetViewerNext = "";
    next.setAttribute("aria-label", "View next image");
    media.append(previous, image, next);

    const footer = createElement("footer", "asset-viewer-footer");
    const status = createElement("p", "asset-viewer-status");
    status.setAttribute("role", "status");
    status.hidden = true;
    const shortcuts = createElement(
      "p",
      "asset-viewer-shortcuts",
      "\u2190/\u2192 Keep & navigate \u00b7 Del Remove \u00b7 Shift+Del Remove + anatomy \u00b7 A Defect type \u00b7 Esc Close",
    );
    const exclusionHelp = createElement(
      "p",
      "asset-viewer-exclusion-help",
      "Moving past keeps this image. Remove takes it out of the final set; its raw master is retained.",
    );
    exclusionHelp.hidden = true;
    const markOut = createElement("button", "asset-viewer-mark-out", "Remove from set");
    markOut.type = "button";
    markOut.dataset.assetViewerMarkOut = "";
    markOut.hidden = true;
    const anatomyReject = createElement(
      "button",
      "asset-viewer-anatomy-reject",
      "Remove + anatomy label",
    );
    anatomyReject.type = "button";
    anatomyReject.dataset.assetViewerAnatomyReject = "";
    anatomyReject.hidden = true;
    const defectPicker = createElement("details", "asset-viewer-defect-picker");
    defectPicker.dataset.assetViewerDefectPicker = "";
    defectPicker.hidden = true;
    const defectPickerSummary = createElement(
      "summary",
      "asset-viewer-defect-picker-summary",
      "Defect: Not specified (optional)",
    );
    const defectPickerHelp = createElement(
      "p",
      "asset-viewer-defect-picker-help",
      "Generic is enough. Choose a type only when it is obvious; the label stays provisional until the set is finished.",
    );
    const defectChips = createElement("div", "asset-viewer-defect-chips");
    defectChips.dataset.assetViewerDefectChips = "";
    defectChips.setAttribute("role", "radiogroup");
    defectChips.setAttribute("aria-label", "Optional anatomy defect type");
    defectPicker.append(defectPickerSummary, defectPickerHelp, defectChips);
    const select = createElement("button", "asset-viewer-select", "Select for bulk action");
    select.type = "button";
    select.dataset.assetViewerSelect = "";
    select.setAttribute("aria-pressed", "false");
    select.hidden = true;
    const settings = createElement("button", "asset-viewer-settings", "Prompt & settings");
    settings.type = "button";
    settings.dataset.assetViewerSettings = "";
    settings.hidden = true;
    const copyClean = createElement("button", "asset-viewer-copy-clean", "Copy clean image");
    copyClean.type = "button";
    copyClean.dataset.assetViewerCopyClean = "";
    copyClean.hidden = true;
    const downloadClean = createElement(
      "button",
      "asset-viewer-download-clean",
      "Download clean PNG",
    );
    downloadClean.type = "button";
    downloadClean.dataset.assetViewerDownloadClean = "";
    downloadClean.hidden = true;
    const download = createElement("a", "asset-viewer-download", "Download exact raw master");
    download.dataset.assetViewerDownload = "";
    const more = createElement("details", "asset-viewer-more");
    const moreSummary = createElement("summary", "asset-viewer-more-summary", "More");
    const moreBody = createElement("div", "asset-viewer-more-body");
    moreBody.append(settings, copyClean, downloadClean, download);
    more.append(moreSummary, moreBody);
    footer.append(
      status,
      shortcuts,
      exclusionHelp,
      markOut,
      anatomyReject,
      defectPicker,
      select,
      more,
    );

    shell.append(header, media, footer);
    dialog.append(shell);
    document.body.append(dialog);
    return {
      close,
      anatomyReject,
      copyClean,
      counter,
      dialog,
      download,
      downloadClean,
      defectChips,
      defectPicker,
      defectPickerSummary,
      exclusionHelp,
      fitToggle,
      image,
      markOut,
      media,
      more,
      next,
      previous,
      score,
      select,
      settings,
      shortcuts,
      status,
      title,
    };
  }

  function preload(source) {
    if (!source || preloadedSources.has(source)) return;
    preloadedSources.add(source);
    const neighbor = new Image();
    neighbor.decoding = "async";
    neighbor.src = source;
  }

  function initializeAssetViewer() {
    if (!document.querySelector("[data-asset-grid]")) return;

    const viewer = createViewer();
    const boundCards = new WeakSet();
    const boundReviewLaunchers = new WeakSet();
    let activeCard = null;
    let announcement = "";
    let cleanActionBusy = false;
    const inspectedAssetIds = new Set(
      assetCards()
        .filter((card) => card.dataset.inspected === "true")
        .map((card) => card.dataset.assetId)
        .filter(Boolean),
    );
    const pendingInspectionIds = new Set();
    const successfullyViewedSources = new Map();
    let activeInspectionBatch = null;
    let failedInspectionBatch = null;
    let inspectionFlushTimer = null;
    let inspectionRequestPromise = null;
    let inspectionAbortController = null;
    let completionInspectionHandoff = false;
    let returnFocus = null;

    const resetViewerScroll = () => {
      viewer.media.scrollTop = 0;
      viewer.media.scrollLeft = 0;
    };

    const setViewerMode = (mode) => {
      const actualSize = mode === "actual";
      viewer.dialog.dataset.assetViewerMode = actualSize ? "actual" : "fit";
      viewer.dialog.classList.toggle("asset-viewer-actual-size", actualSize);
      viewer.fitToggle.setAttribute("aria-pressed", String(actualSize));
      viewer.fitToggle.textContent = actualSize ? "Fit to screen" : "View at 100%";
      viewer.fitToggle.setAttribute(
        "aria-label",
        actualSize
          ? "Fit the entire image within the screen"
          : "View image at 100% and allow scrolling",
      );
      resetViewerScroll();
    };

    const announce = (message) => {
      announcement = message;
      viewer.status.textContent = message;
      viewer.status.hidden = !message;
    };

    const inspectionForm = () => document.querySelector("form[data-review-inspection-form]");

    const normalizedSource = (source) => {
      if (!source) return "";
      try {
        return new URL(source, document.baseURI).href;
      } catch (_error) {
        return "";
      }
    };

    const markViewerImageLoaded = () => {
      const assetId = viewer.image.dataset.inspectionAssetId || "";
      const requestedSource = normalizedSource(viewer.image.dataset.inspectionSource || "");
      const renderedSource = normalizedSource(viewer.image.currentSrc || viewer.image.src);
      if (!assetId
          || !requestedSource
          || requestedSource !== renderedSource
          || !viewer.image.complete
          || viewer.image.naturalWidth <= 0
          || activeCard?.dataset.assetId !== assetId
          || normalizedSource(sourceFor(activeCard)) !== requestedSource) return false;
      successfullyViewedSources.set(assetId, requestedSource);
      return true;
    };

    const clearFailedViewerImage = () => {
      const assetId = viewer.image.dataset.inspectionAssetId || "";
      const requestedSource = normalizedSource(viewer.image.dataset.inspectionSource || "");
      if (assetId && successfullyViewedSources.get(assetId) === requestedSource) {
        successfullyViewedSources.delete(assetId);
      }
    };

    const inspectionConfiguration = () => {
      const form = inspectionForm();
      if (!(form instanceof HTMLFormElement)) return null;
      const csrf = form.querySelector('input[name="csrf_token"]');
      const key = form.querySelector('input[name="idempotency_key"]');
      if (!(csrf instanceof HTMLInputElement)
          || !(key instanceof HTMLInputElement)
          || !csrf.value
          || !key.value) return null;
      const action = form.getAttribute("action");
      if (!action) return null;
      let endpoint;
      try {
        endpoint = new URL(action, document.baseURI);
      } catch (_error) {
        return null;
      }
      // URL generation happens behind the TLS terminator, so an absolute form
      // action can carry the internal http scheme. Review progress is strictly
      // same-origin; rebuild the endpoint on the browser's public origin to
      // avoid a mixed-content failure when finishing the set.
      const url = new URL(`${endpoint.pathname}${endpoint.search}`, window.location.origin).href;
      return { csrfToken: csrf.value, keyPrefix: key.value, url };
    };

    const setInspectionChip = (card, state) => {
      const chip = card.querySelector("[data-inspected-chip]");
      if (!(chip instanceof HTMLElement)) return;
      chip.hidden = false;
      chip.classList.toggle("pending", state !== "saved");
      chip.textContent = state === "saved" ? "Reviewed" : "Reviewed · saving";
    };

    const restoreInspectionState = () => {
      assetCards().forEach((card) => {
        const assetId = card.dataset.assetId || "";
        if (!assetId) return;
        if (inspectedAssetIds.has(assetId)) {
          card.dataset.inspected = "true";
          delete card.dataset.inspectionState;
          setInspectionChip(card, "saved");
          return;
        }
        const pending = pendingInspectionIds.has(assetId)
          || activeInspectionBatch?.assetIds.includes(assetId)
          || failedInspectionBatch?.assetIds.includes(assetId);
        if (pending) {
          card.dataset.inspectionState = "pending";
          setInspectionChip(card, "pending");
        }
      });
    };

    const markInspectionSaved = (assetIds) => {
      assetIds.forEach((assetId) => {
        inspectedAssetIds.add(assetId);
        assetCards()
          .filter((card) => card.dataset.assetId === assetId)
          .forEach((card) => {
            card.dataset.inspected = "true";
            delete card.dataset.inspectionState;
            setInspectionChip(card, "saved");
          });
      });
      document.dispatchEvent(new CustomEvent("gen-automation:review-inspections-saved", {
        detail: { assetIds: [...assetIds] },
      }));
    };

    const scheduleInspectionFlush = (delay = INSPECTION_IDLE_FLUSH_MS) => {
      if (completionInspectionHandoff) return;
      if (inspectionFlushTimer !== null) window.clearTimeout(inspectionFlushTimer);
      inspectionFlushTimer = window.setTimeout(() => {
        inspectionFlushTimer = null;
        void flushInspectionQueue();
      }, delay);
    };

    const inspectionBacklogIds = () => new Set([
      ...pendingInspectionIds,
      ...(activeInspectionBatch?.assetIds || []),
      ...(failedInspectionBatch?.assetIds || []),
    ]);

    const queueInspection = (card, { schedule = true } = {}) => {
      const assetId = card?.dataset.assetId || "";
      const source = normalizedSource(card ? sourceFor(card) : "");
      if (!assetId
          || !source
          || successfullyViewedSources.get(assetId) !== source
          || inspectedAssetIds.has(assetId)
          || pendingInspectionIds.has(assetId)
          || activeInspectionBatch?.assetIds.includes(assetId)
          || failedInspectionBatch?.assetIds.includes(assetId)) return;
      pendingInspectionIds.add(assetId);
      card.dataset.inspectionState = "pending";
      setInspectionChip(card, "pending");
      if (!schedule || completionInspectionHandoff) return;
      if (pendingInspectionIds.size >= INSPECTION_BATCH_SIZE) {
        if (inspectionFlushTimer !== null) window.clearTimeout(inspectionFlushTimer);
        inspectionFlushTimer = null;
        void flushInspectionQueue();
      } else {
        scheduleInspectionFlush();
      }
    };

    const flushInspectionQueue = (keepalive = false) => {
      if (completionInspectionHandoff) return Promise.resolve(false);
      if (inspectionRequestPromise) return inspectionRequestPromise;
      const config = inspectionConfiguration();
      if (!config) return Promise.resolve(inspectionBacklogIds().size === 0);

      let batch = failedInspectionBatch;
      if (!batch) {
        const assetIds = [...pendingInspectionIds].slice(0, INSPECTION_BATCH_SIZE);
        if (assetIds.length === 0) return Promise.resolve(true);
        assetIds.forEach((assetId) => pendingInspectionIds.delete(assetId));
        batch = {
          assetIds,
          idempotencyKey: config.keyPrefix,
        };
      }
      failedInspectionBatch = null;
      activeInspectionBatch = batch;
      if (inspectionFlushTimer !== null) window.clearTimeout(inspectionFlushTimer);
      inspectionFlushTimer = null;

      const body = new URLSearchParams();
      body.append("csrf_token", config.csrfToken);
      body.append("idempotency_key", batch.idempotencyKey);
      batch.assetIds.forEach((assetId) => body.append("asset_id", assetId));

      const controller = typeof AbortController === "function" ? new AbortController() : null;
      inspectionAbortController = controller;
      const timeoutId = controller ? window.setTimeout(() => {
        controller.abort();
      }, INSPECTION_REQUEST_TIMEOUT_MS) : null;
      const requestOptions = {
        method: "POST",
        credentials: "same-origin",
        keepalive,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-Requested-With": "fetch",
        },
        body,
      };
      if (controller) requestOptions.signal = controller.signal;

      inspectionRequestPromise = fetch(config.url, requestOptions).then(async (response) => {
        if (!response.ok) throw new Error(`inspection_http_${response.status}`);
        const payload = await response.json();
        if (!payload || payload.ok !== true || !Array.isArray(payload.inspected_asset_ids)) {
          throw new Error("inspection_response_invalid");
        }
        const confirmed = new Set(payload.inspected_asset_ids.map(String));
        if (!batch.assetIds.every((assetId) => confirmed.has(assetId))) {
          throw new Error("inspection_response_incomplete");
        }
        markInspectionSaved(batch.assetIds);
        return true;
      }).catch(() => {
        if (!completionInspectionHandoff) {
          failedInspectionBatch = batch;
          batch.assetIds.forEach((assetId) => {
            assetCards()
              .filter((card) => card.dataset.assetId === assetId)
              .forEach((card) => { card.dataset.inspectionState = "pending"; });
          });
          scheduleInspectionFlush(INSPECTION_RETRY_MS);
        }
        return false;
      }).finally(() => {
        if (timeoutId !== null) window.clearTimeout(timeoutId);
        if (inspectionAbortController === controller) inspectionAbortController = null;
        activeInspectionBatch = null;
        inspectionRequestPromise = null;
        if (!completionInspectionHandoff
            && !failedInspectionBatch
            && pendingInspectionIds.size > 0) scheduleInspectionFlush();
      });
      return inspectionRequestPromise;
    };

    const updateCleanControls = (sourceAvailable) => {
      viewer.copyClean.hidden = !sourceAvailable;
      viewer.downloadClean.hidden = !sourceAvailable;
      viewer.copyClean.disabled = cleanActionBusy || !sourceAvailable;
      viewer.downloadClean.disabled = cleanActionBusy || !sourceAvailable;
    };

    const canvasPngBlob = (canvas) => new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) {
          resolve(blob);
        } else {
          reject(new Error("clean_png_encode_failed"));
        }
      }, "image/png");
    });

    const cleanPngFor = async (card) => {
      const details = cardDetails(card);
      const source = details.source || details.downloadHref;
      if (!source) throw new Error("clean_source_unavailable");
      if (typeof window.createImageBitmap !== "function") {
        throw new Error("clean_image_decode_unsupported");
      }

      const sourceUrl = new URL(source, window.location.href);
      const response = await fetch(sourceUrl.href, {
        cache: "no-store",
        credentials: sourceUrl.origin === window.location.origin ? "same-origin" : "omit",
        referrerPolicy: "no-referrer",
      });
      if (!response.ok) throw new Error("clean_source_fetch_failed");
      const sourceBlob = await response.blob();
      const bitmap = await window.createImageBitmap(sourceBlob);
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const context = canvas.getContext("2d");
      if (!context) {
        bitmap.close();
        throw new Error("clean_canvas_unavailable");
      }

      try {
        context.drawImage(bitmap, 0, 0);
        return await canvasPngBlob(canvas);
      } finally {
        bitmap.close();
        canvas.width = 0;
        canvas.height = 0;
      }
    };

    const cleanFilenameFor = (card) => {
      const assetId = card && card.dataset.assetId ? card.dataset.assetId : "generated-image";
      return `clean-${assetId}.png`;
    };

    const downloadBlob = (blob, filename) => {
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = filename;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    };

    const performCleanAction = async (action) => {
      if (!activeCard || cleanActionBusy) return;
      const card = activeCard;
      const sourceAvailable = Boolean(cardDetails(card).source || cardDetails(card).downloadHref);
      if (!sourceAvailable) return;
      cleanActionBusy = true;
      updateCleanControls(true);
      announce("Preparing metadata-free PNG...");

      try {
        const cleanPng = await cleanPngFor(card);
        const filename = cleanFilenameFor(card);
        if (action === "copy") {
          const clipboardSupported = Boolean(
            window.isSecureContext
            && window.ClipboardItem
            && navigator.clipboard
            && typeof navigator.clipboard.write === "function",
          );
          if (clipboardSupported) {
            try {
              const item = new window.ClipboardItem({ "image/png": cleanPng });
              await navigator.clipboard.write([item]);
              announce("Clean PNG copied; embedded generation metadata was not included.");
              return;
            } catch (_error) {
              // A local download remains available when clipboard permission is denied.
            }
          }
          downloadBlob(cleanPng, filename);
          announce("Clipboard unavailable; downloaded a metadata-free PNG instead.");
          return;
        }

        downloadBlob(cleanPng, filename);
        announce("Metadata-free PNG downloaded; exact raw master remains unchanged.");
      } catch (_error) {
        announce("Could not prepare a clean PNG. Refresh the private image link and try again.");
      } finally {
        cleanActionBusy = false;
        if (activeCard) {
          const details = cardDetails(activeCard);
          updateCleanControls(Boolean(details.source || details.downloadHref));
        }
      }
    };

    const rejectionContext = (card) => {
      if (!card) return null;
      const form = card.querySelector("form[data-review-decision-form]");
      const rejectButton = form?.querySelector('button[data-decision="reject"]');
      const assetIdField = form?.querySelector('input[name="asset_id"]');
      const assetId = form?.dataset.assetId
        || (assetIdField instanceof HTMLInputElement ? assetIdField.value : "")
        || card.dataset.assetId;
      if (!(form instanceof HTMLFormElement)
          || !(rejectButton instanceof HTMLButtonElement)
          || !assetId) return null;
      const anatomyToggle = form.querySelector("[data-anatomy-training-toggle]");
      const anatomyIssue = form.querySelector("[data-anatomy-training-issue]");
      const savedAnatomyIssue = anatomyIssue instanceof HTMLSelectElement
        ? anatomyIssue.dataset.savedAnatomyIssue || ""
        : "";
      return {
        anatomyIssue: anatomyIssue instanceof HTMLSelectElement ? anatomyIssue : null,
        anatomyLabeled: anatomyToggle instanceof HTMLInputElement
          && anatomyToggle.checked
          && Boolean(savedAnatomyIssue)
          && anatomyIssue.value === savedAnatomyIssue,
        anatomyToggle: anatomyToggle instanceof HTMLInputElement ? anatomyToggle : null,
        assetId,
        alreadyRejected: card.dataset.decision === "reject",
        form,
        rejectButton,
        savedAnatomyIssue,
      };
    };

    const defectLabel = (option) => {
      const label = option?.textContent?.trim();
      if (!label || option.value === "anatomy") return "Not specified";
      return label.replace(/\b\w/g, (character) => character.toUpperCase());
    };

    const selectDefect = (context, value, { focus = false } = {}) => {
      if (!context?.anatomyIssue) return;
      const allowed = Array.from(context.anatomyIssue.options).some(
        (option) => option.value === value,
      );
      context.anatomyIssue.value = allowed ? value : "anatomy";
      context.anatomyIssue.dispatchEvent(new Event("change", { bubbles: true }));
      const selected = context.anatomyIssue.selectedOptions[0];
      viewer.defectPickerSummary.textContent = `Defect: ${defectLabel(selected)} (optional)`;
      Array.from(viewer.defectChips.querySelectorAll("[data-defect-code]")).forEach((chip) => {
        const isSelected = chip.dataset.defectCode === context.anatomyIssue.value;
        chip.setAttribute("aria-checked", String(isSelected));
        chip.tabIndex = isSelected ? 0 : -1;
        if (focus && isSelected) chip.focus({ preventScroll: true });
      });
    };

    const syncDefectPicker = (context) => {
      const canLabel = Boolean(context?.anatomyToggle && context?.anatomyIssue);
      viewer.defectPicker.hidden = !canLabel;
      if (!canLabel) {
        viewer.defectPicker.open = false;
        viewer.defectChips.replaceChildren();
        delete viewer.defectChips.dataset.optionSignature;
        return;
      }

      const options = Array.from(context.anatomyIssue.options);
      const signature = options.map((option) => `${option.value}:${defectLabel(option)}`).join("|");
      if (viewer.defectChips.dataset.optionSignature !== signature) {
        viewer.defectChips.replaceChildren();
        options.forEach((option) => {
          const chip = createElement("button", "asset-viewer-defect-chip", defectLabel(option));
          chip.type = "button";
          chip.dataset.defectCode = option.value;
          chip.setAttribute("role", "radio");
          chip.addEventListener("click", () => {
            const activeContext = rejectionContext(activeCard);
            selectDefect(activeContext, chip.dataset.defectCode || "anatomy");
            updateRejectionControls();
            announce(
              `${chip.textContent} selected. Press Shift+Delete to reject with this provisional label.`,
            );
          });
          viewer.defectChips.append(chip);
        });
        viewer.defectChips.dataset.optionSignature = signature;
      }
      selectDefect(context, context.anatomyIssue.value || "anatomy");
    };

    const updateRejectionControls = () => {
      const context = rejectionContext(activeCard);
      const canTrainAnatomy = Boolean(context?.anatomyToggle && context?.anatomyIssue);
      viewer.shortcuts.textContent = context
        ? (
          canTrainAnatomy
            ? "\u2190/\u2192 Navigate \u00b7 Del Reject \u00b7 Shift+Del Reject + anatomy \u00b7 A Defect type \u00b7 Esc Close"
            : "\u2190/\u2192 Navigate \u00b7 Del Reject \u00b7 Esc Close"
        )
        : "\u2190/\u2192 Navigate \u00b7 Esc Close";
      viewer.exclusionHelp.hidden = !context;
      viewer.markOut.hidden = !context;
      viewer.anatomyReject.hidden = !context || !canTrainAnatomy;
      syncDefectPicker(context);
      if (!context) return;

      const pending = Number.parseInt(activeCard?.dataset.reviewPendingCount || "0", 10) > 0;
      viewer.markOut.disabled = context.alreadyRejected && !context.anatomyLabeled;
      viewer.markOut.textContent = context.anatomyLabeled
          ? "Remove anatomy label"
          : context.alreadyRejected
            ? pending ? "Rejected · saving" : "Rejected"
            : "Reject";
      viewer.markOut.setAttribute(
        "aria-label",
        context.anatomyLabeled
          ? `Remove the provisional anatomy label from ${cardRank(activeCard)}; keep it rejected`
          : context.alreadyRejected
          ? "Already rejected from the final set; raw master retained"
          : `Reject ${cardRank(activeCard)} from the final set; raw master retained`,
      );

      viewer.anatomyReject.disabled = context.anatomyLabeled;
      viewer.anatomyReject.textContent = context.anatomyLabeled
          ? "Provisional anatomy label set"
          : context.alreadyRejected
            ? context.savedAnatomyIssue
              ? "Update provisional anatomy label"
              : "Add provisional anatomy label"
            : "Reject + anatomy label";
      viewer.anatomyReject.setAttribute(
        "aria-label",
        context.alreadyRejected
          ? `Provisionally label ${cardRank(activeCard)} as an anatomy defect`
          : `Reject ${cardRank(activeCard)} and provisionally label it as an anatomy defect`,
      );
    };

    const closeViewer = () => {
      if (!viewer.dialog.open) return;
      if (activeCard) queueInspection(activeCard);
      void flushInspectionQueue(true);
      viewer.dialog.close();
    };

    const renderCard = (card) => {
      const available = visibleCards();
      if (available.length === 0) {
        closeViewer();
        return;
      }
      const requestedIndex = available.indexOf(card);
      const index = requestedIndex >= 0 ? requestedIndex : 0;
      activeCard = available[index];
      const details = cardDetails(activeCard);

      if (announcement === IMAGE_LOAD_ERROR) announcement = "";

      const scoreAnnouncement = details.score ? ` · Rank score ${details.score}` : "";
      viewer.counter.textContent = (
        `Image ${index + 1} of ${available.length} · ${details.rank}${scoreAnnouncement}`
      );
      viewer.title.textContent = details.rank;
      viewer.score.textContent = details.score ? `Rank score ${details.score}` : "";
      viewer.score.hidden = !details.score;
      viewer.image.alt = details.alt;
      viewer.status.textContent = announcement;
      viewer.status.hidden = !announcement;
      resetViewerScroll();
      window.requestAnimationFrame(resetViewerScroll);

      if (details.source) {
        viewer.image.dataset.inspectionAssetId = activeCard.dataset.assetId || "";
        viewer.image.dataset.inspectionSource = details.source;
        viewer.image.src = details.source;
        viewer.image.hidden = false;
        // Cached images may already be complete and do not reliably emit another load event.
        if (viewer.image.complete && viewer.image.naturalWidth > 0) markViewerImageLoaded();
      } else {
        delete viewer.image.dataset.inspectionAssetId;
        delete viewer.image.dataset.inspectionSource;
        viewer.image.removeAttribute("src");
        viewer.image.hidden = true;
        viewer.status.textContent = "This preview is unavailable. Reload the page and try again.";
        viewer.status.hidden = false;
      }

      if (details.downloadHref) {
        viewer.download.href = details.downloadHref;
        viewer.download.hidden = false;
      } else {
        viewer.download.removeAttribute("href");
        viewer.download.hidden = true;
      }
      updateCleanControls(Boolean(details.source || details.downloadHref));

      const selection = activeCard.querySelector('input[type="checkbox"][name="asset_id"]');
      viewer.select.hidden = !selection;
      viewer.select.setAttribute("aria-pressed", String(Boolean(selection && selection.checked)));
      viewer.select.textContent = selection && selection.checked
        ? "Selected for bulk action"
        : "Select for bulk action";
      viewer.settings.hidden = !activeCard.querySelector("[data-generation-details]");
      viewer.more.open = false;
      updateRejectionControls();

      const hasMultiple = available.length > 1;
      viewer.previous.disabled = !hasMultiple;
      viewer.next.disabled = !hasMultiple;
      if (hasMultiple) {
        const previousCard = available[(index - 1 + available.length) % available.length];
        const nextCard = available[(index + 1) % available.length];
        viewer.previous.setAttribute("aria-label", `View previous image, ${cardRank(previousCard)}`);
        viewer.next.setAttribute("aria-label", `View next image, ${cardRank(nextCard)}`);
        preload(sourceFor(previousCard));
        preload(sourceFor(nextCard));
      }
    };

    const step = (offset) => {
      const available = visibleCards();
      if (available.length < 2) return;
      const currentIndex = Math.max(0, available.indexOf(activeCard));
      queueInspection(activeCard);
      renderCard(available[(currentIndex + offset + available.length) % available.length]);
    };

    const submitRejection = (
      withAnatomyTraining = false,
      { removeAnatomyLabel = false } = {},
    ) => {
      const context = rejectionContext(activeCard);
      if (!context) return false;
      if (withAnatomyTraining && (!context.anatomyToggle || !context.anatomyIssue)) {
        announce("Anatomy training is unavailable for this review. The image was not changed.");
        return false;
      }
      if (context.alreadyRejected
          && ((!withAnatomyTraining && (!context.anatomyLabeled || !removeAnatomyLabel))
            || (withAnatomyTraining && context.anatomyLabeled))) {
        announce(
          context.anatomyLabeled
            ? "Already rejected and labeled for anatomy learning."
            : "Already rejected from the final set; raw master retained.",
        );
        step(1);
        return false;
      }

      const previousAnatomyChecked = Boolean(context.anatomyToggle?.checked);
      context.form.dataset.reviewPreviousAnatomyChecked = String(previousAnatomyChecked);
      if (context.anatomyToggle) context.anatomyToggle.checked = withAnatomyTraining;
      queueInspection(activeCard);
      const available = visibleCards();
      const currentIndex = Math.max(0, available.indexOf(activeCard));
      const nextCard = available.length > 1
        ? available[(currentIndex + 1) % available.length]
        : null;
      const removingAnatomyLabel = !withAnatomyTraining
        && removeAnatomyLabel
        && context.anatomyLabeled;
      announcement = removingAnatomyLabel
        ? `Removing the provisional anatomy label from ${cardRank(activeCard)}...`
        : withAnatomyTraining
          ? `Removed ${cardRank(activeCard)} with an anatomy label · saving in background`
          : `Removed ${cardRank(activeCard)} · saving in background`;
      let queued = false;
      const acknowledgeQueue = (event) => {
        const detail = event.detail || {};
        if (detail.assetId === context.assetId && detail.decision === "reject") queued = true;
      };
      document.addEventListener("gen-automation:review-action-optimistic", acknowledgeQueue);
      context.form.requestSubmit(context.rejectButton);
      document.removeEventListener("gen-automation:review-action-optimistic", acknowledgeQueue);
      delete context.form.dataset.reviewPreviousAnatomyChecked;
      if (!queued) {
        if (context.anatomyToggle) context.anatomyToggle.checked = previousAnatomyChecked;
        updateRejectionControls();
        announce("The rejection could not be queued. Check the form and try again.");
        return false;
      }
      if (viewer.dialog.open && nextCard) {
        renderCard(nextCard);
      } else {
        updateRejectionControls();
        announce(announcement);
      }
      if (context.anatomyToggle && !context.form.isConnected) {
        context.anatomyToggle.checked = previousAnatomyChecked;
      }
      return true;
    };

    const openViewer = (card, trigger) => {
      returnFocus = trigger;
      announcement = "";
      setViewerMode("fit");
      renderCard(card);
      document.body.classList.add("asset-viewer-open");
      if (typeof viewer.dialog.showModal === "function") {
        viewer.dialog.showModal();
      } else {
        viewer.dialog.setAttribute("open", "");
      }
      viewer.close.focus();
    };

    const bindCard = (card) => {
      if (boundCards.has(card)) return;
      const image = imageFor(card);
      if (!image) return;
      const trigger = ensureTrigger(card, image);
      if (!trigger) return;
      boundCards.add(card);

      trigger.dataset.assetViewerTrigger = "";
      trigger.setAttribute("aria-haspopup", "dialog");
      trigger.setAttribute("aria-controls", viewer.dialog.id);
      if (!trigger.getAttribute("aria-label")) {
        trigger.setAttribute("aria-label", `Open ${image.alt || cardRank(card)} in full-screen viewer`);
      }

      const needsKeyboardUpgrade = !["A", "BUTTON", "INPUT"].includes(trigger.tagName);
      if (needsKeyboardUpgrade) {
        trigger.tabIndex = 0;
        trigger.setAttribute("role", "button");
        trigger.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openViewer(card, trigger);
          }
        });
      }
      trigger.addEventListener("click", (event) => {
        if (trigger.tagName === "A") event.preventDefault();
        openViewer(card, trigger);
      });
    };

    const bindCards = (root = document) => {
      if (!root || typeof root.querySelectorAll !== "function") return;
      root.querySelectorAll("[data-asset-card], .asset-card").forEach((card) => {
        if (card.closest("[data-asset-grid]")) bindCard(card);
      });
    };

    const bindReviewLaunchers = (root = document) => {
      if (!root || typeof root.querySelectorAll !== "function") return;
      root.querySelectorAll("[data-open-review-viewer]").forEach((launcher) => {
        if (!(launcher instanceof HTMLButtonElement) || boundReviewLaunchers.has(launcher)) return;
        boundReviewLaunchers.add(launcher);
        launcher.addEventListener("click", () => {
          const card = visibleCards()[0] || assetCards()[0];
          if (!card) return;
          const trigger = triggerFor(card);
          if (trigger) openViewer(card, launcher);
        });
      });
    };

    bindCards();
    bindReviewLaunchers();
    document.addEventListener("click", (event) => {
      const image = event.target instanceof Element
        ? event.target.closest("[data-asset-viewer-image], .asset-preview")
        : null;
      if (!(image instanceof HTMLImageElement)) return;
      const card = image.closest("[data-asset-card], .asset-card");
      if (!(card instanceof HTMLElement) || !card.closest("[data-asset-grid]")) return;
      const trigger = triggerFor(card);
      if (!(trigger instanceof HTMLElement) || trigger === image || trigger.contains(image)) return;
      openViewer(card, trigger);
    });
    document.addEventListener("gen-automation:assets-updated", (event) => {
      const root = event.detail && event.detail.root;
      const activeAssetId = activeCard?.dataset.assetId || "";
      bindCards(root || document);
      bindReviewLaunchers(root || document);
      restoreInspectionState();
      applyDensity(storedDensity());
      if (viewer.dialog.open && activeAssetId) {
        const refreshedCard = assetCards().find(
          (candidate) => candidate.dataset.assetId === activeAssetId,
        );
        if (refreshedCard) renderCard(refreshedCard);
      }
    });
    document.addEventListener("gen-automation:review-action-optimistic", (event) => {
      const detail = event.detail || {};
      if (viewer.dialog.open && activeCard?.dataset.assetId === detail.assetId) {
        updateRejectionControls();
      }
    });
    document.addEventListener("gen-automation:review-action-settled", (event) => {
      const detail = event.detail || {};
      if (detail.success) return;
      if (viewer.dialog.open) {
        announce(
          detail.reason === "conflict"
            ? "The review changed elsewhere; pending choices were reconciled."
            : "A pending review choice could not be saved and was restored.",
        );
        updateRejectionControls();
      }
    });

    viewer.close.addEventListener("click", closeViewer);
    viewer.fitToggle.addEventListener("click", () => {
      setViewerMode(
        viewer.dialog.dataset.assetViewerMode === "actual" ? "fit" : "actual",
      );
    });
    viewer.previous.addEventListener("click", () => step(-1));
    viewer.next.addEventListener("click", () => step(1));
    viewer.markOut.addEventListener("click", () => submitRejection(false, {
      removeAnatomyLabel: Boolean(rejectionContext(activeCard)?.anatomyLabeled),
    }));
    viewer.anatomyReject.addEventListener("click", () => submitRejection(true));
    viewer.defectChips.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const chips = Array.from(viewer.defectChips.querySelectorAll("[data-defect-code]"));
      if (chips.length === 0) return;
      event.preventDefault();
      event.stopPropagation();
      const currentIndex = Math.max(0, chips.indexOf(event.target));
      let nextIndex = currentIndex;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = chips.length - 1;
      if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + chips.length) % chips.length;
      if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % chips.length;
      const nextChip = chips[nextIndex];
      const context = rejectionContext(activeCard);
      selectDefect(context, nextChip.dataset.defectCode || "anatomy", { focus: true });
      updateRejectionControls();
    });
    viewer.copyClean.addEventListener("click", () => performCleanAction("copy"));
    viewer.downloadClean.addEventListener("click", () => performCleanAction("download"));
    viewer.select.addEventListener("click", () => {
      if (!activeCard) return;
      const selection = activeCard.querySelector('input[type="checkbox"][name="asset_id"]');
      if (!selection) return;
      selection.checked = !selection.checked;
      selection.dispatchEvent(new Event("change", { bubbles: true }));
      viewer.select.setAttribute("aria-pressed", String(selection.checked));
      viewer.select.textContent = selection.checked
        ? "Selected for bulk action"
        : "Select for bulk action";
    });
    viewer.settings.addEventListener("click", () => {
      if (!activeCard) return;
      const settingsPanel = activeCard.querySelector("[data-generation-details]");
      if (!settingsPanel) return;
      returnFocus = null;
      closeViewer();
      settingsPanel.open = true;
      window.requestAnimationFrame(() => {
        const reduceMotion = typeof window.matchMedia === "function"
          && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const summary = settingsPanel.querySelector("summary");
        const scrollTarget = summary || settingsPanel;
        scrollTarget.scrollIntoView({
          behavior: reduceMotion ? "auto" : "smooth",
          block: "center",
        });
        if (summary) summary.focus({ preventScroll: true });
      });
    });
    viewer.dialog.addEventListener("click", (event) => {
      if (event.target === viewer.dialog) closeViewer();
    });
    viewer.dialog.addEventListener("keydown", (event) => {
      if (isEditableTarget(event.target)) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        step(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        step(1);
      } else if (event.key === "Delete") {
        const context = rejectionContext(activeCard);
        if (context) {
          event.preventDefault();
          submitRejection(event.shiftKey, {
            removeAnatomyLabel: !event.shiftKey && Boolean(context.anatomyLabeled),
          });
        }
      } else if (event.key.toLowerCase() === "a") {
        const context = rejectionContext(activeCard);
        if (context?.anatomyIssue) {
          event.preventDefault();
          viewer.defectPicker.open = true;
          selectDefect(context, context.anatomyIssue.value || "anatomy", { focus: true });
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeViewer();
      }
    });
    viewer.dialog.addEventListener("close", () => {
      if (activeCard) queueInspection(activeCard);
      void flushInspectionQueue(true);
      document.body.classList.remove("asset-viewer-open");
      document.dispatchEvent(new CustomEvent("gen-automation:asset-viewer-closed"));
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      returnFocus = null;
      activeCard = null;
    });
    viewer.image.addEventListener("error", () => {
      clearFailedViewerImage();
      announce(IMAGE_LOAD_ERROR);
    });
    viewer.image.addEventListener("load", () => {
      resetViewerScroll();
      markViewerImageLoaded();
      if (announcement === IMAGE_LOAD_ERROR) announce("");
    });
    let touchStart = null;
    viewer.media.addEventListener("touchstart", (event) => {
      touchStart = null;
      if (event.touches.length !== 1 || viewer.dialog.dataset.assetViewerMode === "actual") return;
      touchStart = {
        x: event.touches[0].clientX,
        y: event.touches[0].clientY,
      };
    }, { passive: true });
    viewer.media.addEventListener("touchend", (event) => {
      const startedAt = touchStart;
      touchStart = null;
      if (!startedAt || event.changedTouches.length !== 1) return;
      const x = event.changedTouches[0].clientX - startedAt.x;
      const y = event.changedTouches[0].clientY - startedAt.y;
      if (Math.abs(x) < 55 || Math.abs(x) <= Math.abs(y) * 1.25) return;
      step(x > 0 ? -1 : 1);
    }, { passive: true });
    viewer.media.addEventListener("touchcancel", () => {
      touchStart = null;
    }, { passive: true });
    document.addEventListener("gen-automation:inspection-completion-handoff", (event) => {
      const includeAssetIds = event.detail?.includeAssetIds;
      completionInspectionHandoff = true;
      if (activeCard) queueInspection(activeCard, { schedule: false });
      if (inspectionFlushTimer !== null) window.clearTimeout(inspectionFlushTimer);
      inspectionFlushTimer = null;
      if (typeof includeAssetIds === "function") {
        includeAssetIds([...inspectionBacklogIds()]);
      }
      if (inspectionAbortController) inspectionAbortController.abort();
    });
    window.addEventListener("pagehide", () => {
      if (completionInspectionHandoff) return;
      if (activeCard) queueInspection(activeCard);
      void flushInspectionQueue(true);
    });
    updateRejectionControls();
    setViewerMode("fit");
  }

  initializeDensityControls();
  initializeAssetViewer();
})();
