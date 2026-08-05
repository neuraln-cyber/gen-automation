(() => {
  "use strict";

  const STORAGE_KEY = "gen-automation.asset-density.v1";
  const DENSITIES = Object.freeze(["comfortable", "compact", "large"]);
  const IMAGE_LOAD_ERROR = (
    "The full-size preview could not be loaded. Reload the page to refresh its private link."
  );
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
      "\u2190/\u2192 Navigate \u00b7 Del Mark out \u00b7 Esc Close",
    );
    const exclusionHelp = createElement(
      "p",
      "asset-viewer-exclusion-help",
      "Mark out excludes an image from the final set; its raw master is retained.",
    );
    exclusionHelp.hidden = true;
    const markOut = createElement("button", "asset-viewer-mark-out", "Mark out");
    markOut.type = "button";
    markOut.dataset.assetViewerMarkOut = "";
    markOut.setAttribute("aria-pressed", "false");
    markOut.hidden = true;
    const saveExclusions = createElement(
      "button",
      "asset-viewer-save-exclusions",
      "Save 0 exclusions",
    );
    saveExclusions.type = "button";
    saveExclusions.dataset.assetViewerSaveExclusions = "";
    saveExclusions.disabled = true;
    saveExclusions.hidden = true;
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
      saveExclusions,
      select,
      more,
    );

    shell.append(header, media, footer);
    dialog.append(shell);
    document.body.append(dialog);
    return {
      close,
      copyClean,
      counter,
      dialog,
      download,
      downloadClean,
      exclusionHelp,
      fitToggle,
      image,
      markOut,
      media,
      more,
      next,
      previous,
      saveExclusions,
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
    let bulkForm = null;
    let bulkReject = null;
    let exclusionStorageKey = null;
    const refreshReviewControls = () => {
      bulkForm = document.querySelector("#bulk-action-form, [data-bulk-action-form]");
      bulkReject = bulkForm
        ? bulkForm.querySelector('button[name="action"][value="reject"]')
        : null;
      exclusionStorageKey = bulkForm
        ? `gen-automation:review-exclusions:v1:${bulkForm.getAttribute("action") || window.location.pathname}`
        : null;
    };
    refreshReviewControls();
    const markedForExclusion = new Set();
    let activeCard = null;
    let announcement = "";
    let cleanActionBusy = false;
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

    const exclusionContext = (card) => {
      if (!card || !bulkForm || !bulkReject) return null;
      const selection = card.querySelector('input[type="checkbox"][name="asset_id"]');
      const assetId = selection && selection.value ? selection.value : card.dataset.assetId;
      if (!selection || !assetId || selection.form !== bulkForm) return null;
      return {
        assetId,
        alreadyExcluded: card.dataset.decision === "reject",
        selection,
      };
    };

    const persistMarkedExclusions = () => {
      if (!exclusionStorageKey) return;
      try {
        if (markedForExclusion.size === 0) {
          window.sessionStorage.removeItem(exclusionStorageKey);
        } else {
          window.sessionStorage.setItem(
            exclusionStorageKey,
            JSON.stringify(Array.from(markedForExclusion)),
          );
        }
      } catch (_error) {
        // Review remains usable when private-browser storage is unavailable.
      }
    };
    const restoreMarkedExclusions = () => {
      if (!exclusionStorageKey) return;
      try {
        const raw = window.sessionStorage.getItem(exclusionStorageKey);
        const saved = raw ? JSON.parse(raw) : [];
        if (!Array.isArray(saved)) return;
        saved.forEach((assetId) => {
          const card = assetCards().find((candidate) => candidate.dataset.assetId === assetId);
          const context = exclusionContext(card);
          if (context && !context.alreadyExcluded) markedForExclusion.add(context.assetId);
        });
        persistMarkedExclusions();
      } catch (_error) {
        try {
          window.sessionStorage.removeItem(exclusionStorageKey);
        } catch (_storageError) {
          // Storage can be unavailable without preventing the viewer from initializing.
        }
      }
    };

    const updateExclusionControls = () => {
      const context = exclusionContext(activeCard);
      const hasReviewControls = Boolean(bulkForm && bulkReject);
      const markedCount = markedForExclusion.size;
      viewer.shortcuts.textContent = hasReviewControls
        ? "\u2190/\u2192 Navigate \u00b7 Del Mark out \u00b7 Esc Close"
        : "\u2190/\u2192 Navigate \u00b7 Esc Close";
      viewer.exclusionHelp.hidden = !context;
      viewer.markOut.hidden = !context;
      viewer.saveExclusions.hidden = !hasReviewControls;
      viewer.saveExclusions.disabled = markedCount === 0;
      viewer.saveExclusions.textContent = (
        `Save ${markedCount} exclusion${markedCount === 1 ? "" : "s"}`
      );
      viewer.saveExclusions.setAttribute(
        "aria-label",
        markedCount === 0
          ? "No images are marked for exclusion"
          : `Save ${markedCount} marked exclusion${markedCount === 1 ? "" : "s"}`,
      );

      assetCards().forEach((card) => {
        const cardContext = exclusionContext(card);
        const marked = Boolean(cardContext && markedForExclusion.has(cardContext.assetId));
        card.classList.toggle("viewer-marked-for-exclusion", marked);
      });

      if (!context) return;
      if (context.alreadyExcluded) {
        viewer.markOut.disabled = true;
        viewer.markOut.textContent = "Excluded";
        viewer.markOut.setAttribute("aria-pressed", "true");
        viewer.markOut.setAttribute(
          "aria-label",
          "Already excluded from the final set; raw master retained",
        );
        return;
      }

      const marked = markedForExclusion.has(context.assetId);
      viewer.markOut.disabled = false;
      viewer.markOut.textContent = marked ? "Undo mark out" : "Mark out";
      viewer.markOut.setAttribute("aria-pressed", String(marked));
      viewer.markOut.setAttribute(
        "aria-label",
        marked
          ? `Undo mark out for ${cardRank(activeCard)}`
          : `Mark ${cardRank(activeCard)} for exclusion; raw master retained`,
      );
    };

    const closeViewer = () => {
      if (viewer.dialog.open) viewer.dialog.close();
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
        viewer.image.src = details.source;
        viewer.image.hidden = false;
      } else {
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
      updateExclusionControls();

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
      renderCard(available[(currentIndex + offset + available.length) % available.length]);
    };

    const toggleMarkOut = () => {
      const context = exclusionContext(activeCard);
      if (!context) return false;
      if (context.alreadyExcluded) {
        announcement = "Already excluded from the final set; raw master retained.";
        updateExclusionControls();
        viewer.status.textContent = announcement;
        viewer.status.hidden = false;
        return false;
      }

      const wasMarked = markedForExclusion.delete(context.assetId);
      if (wasMarked) {
        persistMarkedExclusions();
        announcement = `${cardRank(activeCard)} mark removed; raw master retained.`;
        updateExclusionControls();
        viewer.status.textContent = announcement;
        viewer.status.hidden = false;
        return false;
      }

      markedForExclusion.add(context.assetId);
      persistMarkedExclusions();
      announcement = (
        `${cardRank(activeCard)} marked for exclusion; raw master retained until you save.`
      );
      updateExclusionControls();
      viewer.status.textContent = announcement;
      viewer.status.hidden = false;
      step(1);
      return true;
    };

    const saveMarkedExclusions = () => {
      if (!bulkForm || !bulkReject || markedForExclusion.size === 0) return;
      const selections = Array.from(
        document.querySelectorAll('input[type="checkbox"][name="asset_id"]'),
      ).filter((selection) => selection.form === bulkForm);
      const states = selections.map((selection) => ({
        checked: selection.checked,
        disabled: selection.disabled,
        selection,
      }));
      const rejectWasDisabled = bulkReject.disabled;
      let markedSelection = null;

      selections.forEach((selection) => {
        const marked = markedForExclusion.has(selection.value);
        selection.disabled = !marked;
        selection.checked = marked;
        if (marked && markedSelection === null) markedSelection = selection;
      });
      if (!markedSelection) {
        states.forEach(({ checked, disabled, selection }) => {
          selection.checked = checked;
          selection.disabled = disabled;
        });
        return;
      }

      markedSelection.dispatchEvent(new Event("change", { bubbles: true }));
      bulkReject.disabled = false;
      viewer.saveExclusions.disabled = true;
      viewer.saveExclusions.textContent = "Saving exclusions...";
      announcement = (
        `Saving ${markedForExclusion.size} exclusion${markedForExclusion.size === 1 ? "" : "s"}; raw masters retained.`
      );
      viewer.status.textContent = announcement;
      viewer.status.hidden = false;

      const restoreSelections = () => {
        states.forEach(({ checked, disabled, selection }) => {
          selection.checked = checked;
          selection.disabled = disabled;
        });
        bulkReject.disabled = rejectWasDisabled;
      };
      window.setTimeout(restoreSelections, 0);
      if (typeof bulkForm.requestSubmit === "function") {
        bulkForm.requestSubmit(bulkReject);
      } else {
        bulkReject.click();
      }
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
      if (trigger !== image && !trigger.contains(image)) {
        image.addEventListener("click", () => openViewer(card, trigger));
      }
    };

    const bindCards = (root = document) => {
      if (!root || typeof root.querySelectorAll !== "function") return;
      root.querySelectorAll("[data-asset-card], .asset-card").forEach((card) => {
        if (card.closest("[data-asset-grid]")) bindCard(card);
      });
    };

    bindCards();
    document.addEventListener("gen-automation:assets-updated", (event) => {
      const root = event.detail && event.detail.root;
      const activeAssetId = activeCard?.dataset.assetId || "";
      refreshReviewControls();
      bindCards(root || document);
      applyDensity(storedDensity());
      Array.from(markedForExclusion).forEach((assetId) => {
        const card = assetCards().find((candidate) => candidate.dataset.assetId === assetId);
        const context = exclusionContext(card);
        if (!context || context.alreadyExcluded) markedForExclusion.delete(assetId);
      });
      persistMarkedExclusions();
      restoreMarkedExclusions();
      updateExclusionControls();
      if (viewer.dialog.open && activeAssetId) {
        const refreshedCard = assetCards().find(
          (candidate) => candidate.dataset.assetId === activeAssetId,
        );
        if (refreshedCard) renderCard(refreshedCard);
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
    viewer.markOut.addEventListener("click", toggleMarkOut);
    viewer.saveExclusions.addEventListener("click", saveMarkedExclusions);
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
        if (exclusionContext(activeCard)) {
          event.preventDefault();
          toggleMarkOut();
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeViewer();
      }
    });
    viewer.dialog.addEventListener("close", () => {
      document.body.classList.remove("asset-viewer-open");
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      returnFocus = null;
      activeCard = null;
    });
    viewer.image.addEventListener("error", () => {
      announce(IMAGE_LOAD_ERROR);
    });
    viewer.image.addEventListener("load", () => {
      resetViewerScroll();
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
    restoreMarkedExclusions();
    updateExclusionControls();
    setViewerMode("fit");
  }

  initializeDensityControls();
  initializeAssetViewer();
})();
