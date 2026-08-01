(() => {
  "use strict";

  const STORAGE_KEY = "gen-automation.asset-density.v1";
  const DENSITIES = Object.freeze(["comfortable", "compact", "large"]);
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

    const close = createElement("button", "asset-viewer-close", "Close");
    close.type = "button";
    close.dataset.assetViewerClose = "";
    close.setAttribute("aria-label", "Close full-screen image viewer");
    header.append(heading, close);

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
    const select = createElement("button", "asset-viewer-select", "Select image");
    select.type = "button";
    select.dataset.assetViewerSelect = "";
    select.setAttribute("aria-pressed", "false");
    select.hidden = true;
    const settings = createElement("button", "asset-viewer-settings", "Prompt & settings");
    settings.type = "button";
    settings.dataset.assetViewerSettings = "";
    settings.hidden = true;
    const download = createElement("a", "asset-viewer-download", "Download exact raw master");
    download.dataset.assetViewerDownload = "";
    footer.append(status, select, settings, download);

    shell.append(header, media, footer);
    dialog.append(shell);
    document.body.append(dialog);
    return {
      close,
      counter,
      dialog,
      download,
      image,
      next,
      previous,
      score,
      select,
      settings,
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
    const cards = assetCards();
    if (cards.length === 0) return;

    const viewer = createViewer();
    let activeCard = null;
    let returnFocus = null;

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

      const scoreAnnouncement = details.score ? ` · Rank score ${details.score}` : "";
      viewer.counter.textContent = (
        `Image ${index + 1} of ${available.length} · ${details.rank}${scoreAnnouncement}`
      );
      viewer.title.textContent = details.rank;
      viewer.score.textContent = details.score ? `Rank score ${details.score}` : "Rank score unavailable";
      viewer.image.alt = details.alt;
      viewer.status.hidden = true;

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

      const selection = activeCard.querySelector('input[type="checkbox"][name="asset_id"]');
      viewer.select.hidden = !selection;
      viewer.select.setAttribute("aria-pressed", String(Boolean(selection && selection.checked)));
      viewer.select.textContent = selection && selection.checked ? "Selected" : "Select image";
      viewer.settings.hidden = !activeCard.querySelector("[data-generation-details]");

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

    const openViewer = (card, trigger) => {
      returnFocus = trigger;
      renderCard(card);
      document.body.classList.add("asset-viewer-open");
      if (typeof viewer.dialog.showModal === "function") {
        viewer.dialog.showModal();
      } else {
        viewer.dialog.setAttribute("open", "");
      }
      viewer.close.focus();
    };

    cards.forEach((card) => {
      const image = imageFor(card);
      if (!image) return;
      const trigger = ensureTrigger(card, image);
      if (!trigger) return;

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
    });

    viewer.close.addEventListener("click", closeViewer);
    viewer.previous.addEventListener("click", () => step(-1));
    viewer.next.addEventListener("click", () => step(1));
    viewer.select.addEventListener("click", () => {
      if (!activeCard) return;
      const selection = activeCard.querySelector('input[type="checkbox"][name="asset_id"]');
      if (!selection) return;
      selection.checked = !selection.checked;
      selection.dispatchEvent(new Event("change", { bubbles: true }));
      viewer.select.setAttribute("aria-pressed", String(selection.checked));
      viewer.select.textContent = selection.checked ? "Selected" : "Select image";
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
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        step(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        step(1);
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
      viewer.status.textContent = "The full-size preview could not be loaded. Reload the page to refresh its private link.";
      viewer.status.hidden = false;
    });
  }

  initializeDensityControls();
  initializeAssetViewer();
})();
