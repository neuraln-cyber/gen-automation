(() => {
  "use strict";

  const FIELD_SELECTOR = "textarea[data-danbooru-autocomplete]";
  const ENDPOINT = "/api/v1/danbooru-tags/autocomplete";
  const LISTBOX_ID = "danbooru-tag-autocomplete-listbox";
  const MIN_QUERY_LENGTH = 2;
  const MAX_QUERY_LENGTH = 64;
  const RESULT_LIMIT = 12;
  const DEBOUNCE_MILLISECONDS = 240;
  const CLIENT_CACHE_LIMIT = 100;

  const clientCache = new Map();
  const initializedFields = new WeakSet();
  const composingFields = new WeakSet();
  const suppressedInputs = new WeakSet();
  const compactNumber = new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  });

  let activeField = null;
  let activeQuery = "";
  let suggestions = [];
  let selectedIndex = -1;
  let debounceTimer = null;
  let requestController = null;
  let requestSequence = 0;
  let positionFrame = null;

  const popup = document.createElement("div");
  popup.className = "danbooru-autocomplete";
  popup.hidden = true;
  popup.dataset.danbooruAutocompletePopup = "";

  const popupHeader = document.createElement("div");
  popupHeader.className = "danbooru-autocomplete-header";
  const popupTitle = document.createElement("strong");
  popupTitle.textContent = "Danbooru tags";
  const popupHint = document.createElement("span");
  popupHint.textContent = "↑↓ choose · Enter insert";
  popupHeader.append(popupTitle, popupHint);

  const listbox = document.createElement("ul");
  listbox.id = LISTBOX_ID;
  listbox.className = "danbooru-autocomplete-list";
  listbox.setAttribute("role", "listbox");
  listbox.setAttribute("aria-label", "Danbooru tag suggestions");

  const popupFooter = document.createElement("div");
  popupFooter.className = "danbooru-autocomplete-footer";
  popupFooter.textContent = "Canonical underscores and escaped parentheses are inserted automatically.";
  popup.append(popupHeader, listbox, popupFooter);

  const liveStatus = document.createElement("div");
  liveStatus.className = "visually-hidden";
  liveStatus.setAttribute("role", "status");
  liveStatus.setAttribute("aria-live", "polite");

  const isPromptField = (value) => value instanceof HTMLTextAreaElement
    && value.matches(FIELD_SELECTOR);

  const initializeField = (field) => {
    if (initializedFields.has(field)) return;
    initializedFields.add(field);
    field.setAttribute("role", "combobox");
    field.setAttribute("aria-autocomplete", "list");
    field.setAttribute("aria-haspopup", "listbox");
    field.setAttribute("aria-controls", LISTBOX_ID);
    field.setAttribute("aria-expanded", "false");
    field.setAttribute("autocomplete", "off");
  };

  const currentToken = (field) => {
    if (field.selectionStart === null || field.selectionEnd === null) return null;
    if (field.selectionStart !== field.selectionEnd) return null;

    const caret = field.selectionStart;
    const beforeCaret = field.value.slice(0, caret);
    const comma = beforeCaret.lastIndexOf(",");
    const lineFeed = beforeCaret.lastIndexOf("\n");
    const carriageReturn = beforeCaret.lastIndexOf("\r");
    let start = Math.max(comma, lineFeed, carriageReturn) + 1;
    const fragmentWithSpacing = field.value.slice(start, caret);
    const leadingSpacing = fragmentWithSpacing.match(/^\s*/u)?.[0] || "";
    start += leadingSpacing.length;

    let fragment = field.value.slice(start, caret);
    const attentionPrefix = fragment.match(/^[([{]+/u)?.[0] || "";
    start += attentionPrefix.length;
    fragment = fragment.slice(attentionPrefix.length);

    const lowered = fragment.trimStart().toLowerCase();
    if (
      lowered.startsWith("__")
      || lowered.startsWith("<")
      || lowered.includes("<lora:")
      || /[<>*]/u.test(fragment)
      || /:(?:-?\d+(?:\.\d*)?|\.\d+)$/u.test(fragment.trim())
    ) {
      return null;
    }

    const query = fragment
      .replace(/\\([()[\]{}])/gu, "$1")
      .trim()
      .replace(/\s+/gu, "_")
      .toLowerCase();
    if (query.length < MIN_QUERY_LENGTH || query.length > MAX_QUERY_LENGTH) return null;
    if (/\p{C}/u.test(query)) return null;
    return { start, end: caret, query };
  };

  const clearRequest = () => {
    requestSequence += 1;
    if (debounceTimer !== null) {
      window.clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (requestController !== null) {
      requestController.abort();
      requestController = null;
    }
  };

  const closePopup = ({ cancelRequest = true } = {}) => {
    if (cancelRequest) clearRequest();
    popup.hidden = true;
    suggestions = [];
    selectedIndex = -1;
    activeQuery = "";
    if (activeField !== null) {
      activeField.setAttribute("aria-expanded", "false");
      activeField.removeAttribute("aria-activedescendant");
    }
    activeField = null;
  };

  const schedulePosition = () => {
    if (popup.hidden || activeField === null || positionFrame !== null) return;
    positionFrame = window.requestAnimationFrame(() => {
      positionFrame = null;
      if (popup.hidden || activeField === null) return;
      const rect = activeField.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) {
        closePopup();
        return;
      }
      const viewportPadding = 8;
      const width = Math.min(
        Math.max(rect.width, Math.min(360, window.innerWidth - (viewportPadding * 2))),
        window.innerWidth - (viewportPadding * 2),
      );
      const left = Math.min(
        Math.max(viewportPadding, rect.left),
        window.innerWidth - width - viewportPadding,
      );
      popup.style.width = `${width}px`;
      popup.style.left = `${left}px`;
      popup.style.top = "auto";
      popup.style.bottom = "auto";
      popup.style.maxHeight = "";
      listbox.style.maxHeight = "";

      const gap = 6;
      const desiredHeight = popup.offsetHeight;
      const roomBelow = Math.max(
        0,
        window.innerHeight - rect.bottom - gap - viewportPadding,
      );
      const roomAbove = Math.max(0, rect.top - gap - viewportPadding);
      const placeBelow = roomBelow >= desiredHeight || roomBelow >= roomAbove;
      const availableHeight = placeBelow ? roomBelow : roomAbove;
      if (availableHeight < 80) {
        closePopup();
        return;
      }
      const chromeHeight = Math.max(0, popup.offsetHeight - listbox.offsetHeight);
      popup.style.maxHeight = `${availableHeight}px`;
      listbox.style.maxHeight = `${Math.max(48, availableHeight - chromeHeight)}px`;
      if (placeBelow) popup.style.top = `${rect.bottom + gap}px`;
      else popup.style.bottom = `${window.innerHeight - rect.top + gap}px`;
    });
  };

  const openPopup = (field, query) => {
    if (activeField !== null && activeField !== field) {
      activeField.setAttribute("aria-expanded", "false");
      activeField.removeAttribute("aria-activedescendant");
    }
    activeField = field;
    activeQuery = query;
    popup.hidden = false;
    field.setAttribute("aria-expanded", "true");
    schedulePosition();
  };

  const setSelectedIndex = (index, { scroll = true } = {}) => {
    if (suggestions.length === 0 || activeField === null) return;
    selectedIndex = ((index % suggestions.length) + suggestions.length) % suggestions.length;
    const options = Array.from(listbox.querySelectorAll('[role="option"]'));
    options.forEach((option, optionIndex) => {
      option.setAttribute("aria-selected", optionIndex === selectedIndex ? "true" : "false");
    });
    const activeOption = options[selectedIndex];
    if (!(activeOption instanceof HTMLElement)) return;
    activeField.setAttribute("aria-activedescendant", activeOption.id);
    if (scroll) activeOption.scrollIntoView({ block: "nearest" });
  };

  const renderMessage = (field, query, message, className = "") => {
    suggestions = [];
    selectedIndex = -1;
    listbox.replaceChildren();
    const item = document.createElement("li");
    item.className = `danbooru-autocomplete-message ${className}`.trim();
    item.setAttribute("role", "presentation");
    item.textContent = message;
    listbox.append(item);
    openPopup(field, query);
    field.removeAttribute("aria-activedescendant");
    liveStatus.textContent = message;
  };

  const renderSuggestions = (field, query, values) => {
    suggestions = values;
    selectedIndex = values.length > 0 ? 0 : -1;
    listbox.replaceChildren();

    if (values.length === 0) {
      renderMessage(field, query, "No matching Danbooru tags.");
      return;
    }

    values.forEach((suggestion, index) => {
      const item = document.createElement("li");
      item.id = `${LISTBOX_ID}-option-${index}`;
      item.className = "danbooru-autocomplete-option";
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", index === 0 ? "true" : "false");
      item.dataset.optionIndex = String(index);
      item.dataset.category = suggestion.category_label;

      const tag = document.createElement("code");
      tag.textContent = suggestion.name;
      const meta = document.createElement("span");
      meta.className = "danbooru-autocomplete-meta";
      const category = document.createElement("span");
      category.className = "danbooru-autocomplete-category";
      category.textContent = suggestion.category_label;
      const count = document.createElement("span");
      count.textContent = `${compactNumber.format(suggestion.post_count)} posts`;
      meta.append(category, count);
      item.append(tag, meta);
      listbox.append(item);
    });

    openPopup(field, query);
    setSelectedIndex(0, { scroll: false });
    liveStatus.textContent = `${values.length} Danbooru tag suggestions available.`;
  };

  const escapePromptTag = (name) => name.replace(/[()[\]]/gu, "\\$&");

  const acceptSuggestion = (index) => {
    const field = activeField;
    const suggestion = suggestions[index];
    if (field === null || suggestion === undefined) return false;
    const token = currentToken(field);
    if (token === null || token.query !== activeQuery) {
      closePopup();
      return false;
    }

    const replacement = escapePromptTag(suggestion.name);
    const nextLength = field.value.length - (token.end - token.start) + replacement.length;
    if (field.maxLength > 0 && nextLength > field.maxLength) {
      liveStatus.textContent = "This tag would exceed the prompt length limit.";
      closePopup();
      return false;
    }

    field.focus({ preventScroll: true });
    field.setRangeText(replacement, token.start, token.end, "end");
    closePopup();
    suppressedInputs.add(field);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    liveStatus.textContent = `${suggestion.name} inserted.`;
    return true;
  };

  const validSuggestion = (value) => value !== null
    && typeof value === "object"
    && typeof value.name === "string"
    && value.name.length > 0
    && value.name.length <= 200
    && Number.isInteger(value.category)
    && typeof value.category_label === "string"
    && Number.isInteger(value.post_count)
    && value.post_count >= 0;

  const remember = (query, values) => {
    if (clientCache.has(query)) clientCache.delete(query);
    clientCache.set(query, values);
    while (clientCache.size > CLIENT_CACHE_LIMIT) {
      const oldest = clientCache.keys().next().value;
      if (oldest === undefined) break;
      clientCache.delete(oldest);
    }
  };

  const loadSuggestions = async (field, token, sequence) => {
    requestController = new AbortController();
    const url = new URL(ENDPOINT, window.location.origin);
    url.searchParams.set("q", token.query);
    url.searchParams.set("limit", String(RESULT_LIMIT));
    try {
      const response = await window.fetch(url.href, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        signal: requestController.signal,
      });
      if (!response.ok) throw new Error(`tag autocomplete returned ${response.status}`);
      const payload = await response.json();
      if (sequence !== requestSequence) return;
      const current = currentToken(field);
      if (document.activeElement !== field || current === null || current.query !== token.query) return;
      if (payload === null || typeof payload !== "object" || payload.available !== true) {
        renderMessage(field, token.query, "Tag lookup is temporarily unavailable. Keep typing normally.", "is-unavailable");
        return;
      }
      const values = Array.isArray(payload.suggestions)
        ? payload.suggestions.filter(validSuggestion).slice(0, RESULT_LIMIT)
        : [];
      remember(token.query, values);
      renderSuggestions(field, token.query, values);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (sequence !== requestSequence || document.activeElement !== field) return;
      renderMessage(field, token.query, "Tag lookup is temporarily unavailable. Keep typing normally.", "is-unavailable");
    } finally {
      if (sequence === requestSequence) requestController = null;
    }
  };

  const scheduleLookup = (field, { immediate = false } = {}) => {
    initializeField(field);
    clearRequest();
    const token = currentToken(field);
    if (token === null) {
      closePopup({ cancelRequest: false });
      return;
    }

    const cached = clientCache.get(token.query);
    if (cached !== undefined) {
      clientCache.delete(token.query);
      clientCache.set(token.query, cached);
      renderSuggestions(field, token.query, cached);
      return;
    }

    const sequence = requestSequence;
    const run = () => {
      debounceTimer = null;
      renderMessage(field, token.query, "Searching Danbooru tags…", "is-loading");
      void loadSuggestions(field, token, sequence);
    };
    if (immediate) run();
    else debounceTimer = window.setTimeout(run, DEBOUNCE_MILLISECONDS);
  };

  document.querySelectorAll(FIELD_SELECTOR).forEach((field) => {
    if (field instanceof HTMLTextAreaElement) initializeField(field);
  });
  document.body.append(popup, liveStatus);

  document.addEventListener("focusin", (event) => {
    if (isPromptField(event.target)) initializeField(event.target);
  });

  document.addEventListener("input", (event) => {
    if (!isPromptField(event.target) || composingFields.has(event.target)) return;
    if (suppressedInputs.delete(event.target)) return;
    if (document.activeElement !== event.target) {
      closePopup();
      return;
    }
    scheduleLookup(event.target);
  });

  document.addEventListener("compositionstart", (event) => {
    if (isPromptField(event.target)) composingFields.add(event.target);
  });

  document.addEventListener("compositionend", (event) => {
    if (!isPromptField(event.target)) return;
    composingFields.delete(event.target);
    scheduleLookup(event.target);
  });

  document.addEventListener("keydown", (event) => {
    if (!isPromptField(event.target)) return;
    const field = event.target;
    if (event.ctrlKey && !event.altKey && !event.metaKey && event.code === "Space") {
      event.preventDefault();
      scheduleLookup(field, { immediate: true });
      return;
    }
    if (activeField !== field || popup.hidden) return;

    if (event.key === "ArrowDown" && suggestions.length > 0) {
      event.preventDefault();
      setSelectedIndex(selectedIndex + 1);
    } else if (event.key === "ArrowUp" && suggestions.length > 0) {
      event.preventDefault();
      setSelectedIndex(selectedIndex - 1);
    } else if ((event.key === "Enter" || event.key === "Tab") && selectedIndex >= 0) {
      event.preventDefault();
      acceptSuggestion(selectedIndex);
    } else if (event.key === "," && selectedIndex >= 0) {
      acceptSuggestion(selectedIndex);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closePopup();
    } else if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
      closePopup();
    }
  });

  popup.addEventListener("pointerdown", (event) => {
    const option = event.target instanceof Element
      ? event.target.closest("[data-option-index]")
      : null;
    if (!(option instanceof HTMLElement)) return;
    if (event.pointerType === "mouse") event.preventDefault();
  });

  popup.addEventListener("click", (event) => {
    const option = event.target instanceof Element
      ? event.target.closest("[data-option-index]")
      : null;
    if (!(option instanceof HTMLElement)) return;
    event.preventDefault();
    acceptSuggestion(Number.parseInt(option.dataset.optionIndex || "-1", 10));
  });

  popup.addEventListener("pointermove", (event) => {
    const option = event.target instanceof Element
      ? event.target.closest("[data-option-index]")
      : null;
    if (!(option instanceof HTMLElement)) return;
    const index = Number.parseInt(option.dataset.optionIndex || "-1", 10);
    if (Number.isInteger(index) && index >= 0 && index !== selectedIndex) {
      setSelectedIndex(index, { scroll: false });
    }
  });

  document.addEventListener("pointerdown", (event) => {
    if (popup.hidden) return;
    if (event.target instanceof Node && (popup.contains(event.target) || event.target === activeField)) return;
    closePopup();
  }, true);

  document.addEventListener("focusout", (event) => {
    if (event.target === activeField) {
      window.setTimeout(() => {
        if (document.activeElement !== activeField) closePopup();
      }, 0);
    }
  });

  document.addEventListener("click", (event) => {
    if (!isPromptField(event.target) || activeField !== event.target || popup.hidden) return;
    const token = currentToken(event.target);
    if (token === null) closePopup();
    else if (token.query !== activeQuery) scheduleLookup(event.target, { immediate: true });
    else schedulePosition();
  });

  document.addEventListener("select", (event) => {
    if (!isPromptField(event.target) || activeField !== event.target || popup.hidden) return;
    const token = currentToken(event.target);
    if (token === null || token.query !== activeQuery) closePopup();
  });

  document.addEventListener("submit", () => closePopup(), true);
  window.addEventListener("resize", schedulePosition);
  window.addEventListener("scroll", schedulePosition, true);
})();
