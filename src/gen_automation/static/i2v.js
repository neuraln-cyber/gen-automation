(() => {
  "use strict";

  const root = document.querySelector("[data-i2v-studio]");
  if (!root) return;

  const apiBase = root.dataset.apiBase || "/api/v1/i2v";
  const csrfToken = root.dataset.csrfToken || "";
  const canManage = root.dataset.canManage === "true";
  const hiresProfileEnabled = root.dataset.hiresProfileEnabled === "true";
  const initialLoraProfileEnabled = root.dataset.loraProfileEnabled === "true";
  const maxImageBytes = Number(root.dataset.maxImageBytes || 0);
  const scope = document.body.dataset.automationStorageScope || "operator";
  const draftKey = `i2v-draft-v1:${scope}`;
  const form = root.querySelector("[data-generation-form]");
  const advanced = root.querySelector("[data-advanced-settings]");
  const sourceLibrary = root.querySelector("[data-source-library]");
  const selectedCard = root.querySelector("[data-selected-source]");
  const assetIdForm = root.querySelector("[data-asset-id-form]");
  const assetIdInput = root.querySelector("[data-asset-id-input]");
  const assetIdButton = root.querySelector("[data-select-asset-id]");
  const assetIdStatus = root.querySelector("[data-asset-id-status]");
  const enqueueButton = root.querySelector("[data-enqueue]");
  const globalStatus = root.querySelector("[data-global-status]");
  const presetSelect = root.querySelector("[data-preset-select]");
  const queue = root.querySelector("[data-job-queue]");
  const videoGrid = root.querySelector("[data-video-grid]");
  const loraList = root.querySelector("[data-lora-list]");
  const loraStatus = root.querySelector("[data-lora-status]");
  const loraEffective = root.querySelector("[data-lora-effective]");
  const loraEffectiveText = root.querySelector("[data-lora-effective-text]");
  const clearLorasButton = root.querySelector("[data-clear-loras]");
  const presetDialog = root.querySelector("[data-preset-dialog]");
  const presetDialogForm = root.querySelector("[data-preset-dialog-form]");
  const presetName = root.querySelector("[data-preset-name]");
  const draftState = root.querySelector("[data-draft-state]");
  const sourcePageSize = 24;
  const state = {
    selected: null,
    presets: [],
    jobs: [],
    sourceLimit: sourcePageSize,
    loraCatalog: [],
    loraSelections: new Map(),
    loraProfileEnabled: initialLoraProfileEnabled,
    loraCatalogLoaded: false,
    loraCatalogError: false,
    maximumLoraSelections: 0,
  };
  const assetIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;
  const maxLoopDurationSeconds = 25;
  const maxLoopCycles = 20;
  const workerSettingDefaults = {
    frame_count: 81,
    fps: 16,
    width: 576,
    height: 1024,
    match_source_aspect: false,
    seed: -1,
    steps: 4,
    high_end_step: 2,
    cfg: 1,
    high_shift: 5,
    low_shift: 5,
    sampler: "euler",
    scheduler: "linear_quadratic",
    interpolation: "none",
    upscale: "none",
    loop: false,
    loop_count: 2,
    color_transfer: false,
    tiled_vae: false,
    face_fidelity: "stable_expression",
    loras: [],
  };
  let saveTimer = null;

  const q = (selector) => root.querySelector(selector);
  const text = (tag, value, className = "") => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    if (method !== "GET" && method !== "HEAD") headers.set("X-CSRF-Token", csrfToken);
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const response = await fetch(`${apiBase}${path}`, {
      credentials: "same-origin",
      ...options,
      method,
      headers,
    });
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string") detail = payload.detail;
        else if (Array.isArray(payload.detail)) detail = payload.detail.map((item) => item.msg).join("; ");
      } catch (_) { /* response was not JSON */ }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function announce(message, error = false) {
    globalStatus.textContent = message;
    globalStatus.classList.toggle("error", error);
    globalStatus.hidden = !message;
  }

  function friendlyBytes(value) {
    if (!Number.isFinite(value) || value <= 0) return "Unknown size";
    const units = ["B", "KB", "MB", "GB"];
    let size = value;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function friendlyDate(value) {
    if (!value) return "Not reported";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? "Not reported" : parsed.toLocaleString();
  }

  function setSelected(selected) {
    state.selected = selected;
    selectedCard.hidden = !selected;
    if (selected) {
      const selectedImage = q("[data-selected-image]");
      selectedImage.hidden = !selected.preview;
      if (selected.preview) selectedImage.src = selected.preview;
      else selectedImage.removeAttribute("src");
      q("[data-selected-name]").textContent = selected.name;
      q("[data-selected-size]").textContent = selected.detail || "Ready";
    }
    sourceLibrary.querySelectorAll(".i2v-source-card").forEach((card) => {
      card.classList.toggle("selected", selected?.assetId === card.dataset.assetId);
    });
    syncAspectControls();
    updateSubmitState();
    scheduleDraftSave();
  }

  function updateSubmitState() {
    const copies = Math.max(1, Number(form.elements.batch_count.value) || 1);
    const promptConflict = selectedManualPromptConflicts().length > 0;
    const loraBlocked = loraWriteBlocked();
    enqueueButton.disabled = !hiresProfileEnabled || !canManage || !state.selected || !form.elements.positive_prompt.value.trim() || promptConflict || loraBlocked;
    q("[data-submit-summary]").textContent = !hiresProfileEnabled
      ? "Waiting for the matching high-resolution worker rollout"
      : loraBlocked
      ? loraBlockMessage()
      : state.selected
      ? `${copies} ${copies === 1 ? "generation" : "generations"} will be added to the queue`
      : "Select a source to continue";
    q("[data-preset-save]").disabled = !canManage || loraBlocked;
    updatePresetButtons();
  }

  async function loadSources({ append = false } = {}) {
    const items = await api(`/source-images?limit=${state.sourceLimit}`);
    if (!append) sourceLibrary.replaceChildren();
    if (!items.length) {
      if (!append) {
        sourceLibrary.append(text("p", "No completed generation images are available yet.", "muted"));
      }
      return;
    }
    const existingAssetIds = new Set(
      [...sourceLibrary.querySelectorAll("[data-asset-id]")]
        .map((card) => card.dataset.assetId)
        .filter(Boolean),
    );
    items.forEach((item) => {
      if (existingAssetIds.has(item.asset_id)) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "i2v-source-card";
      button.dataset.assetId = item.asset_id;
      const image = document.createElement("img");
      image.src = item.preview_url;
      image.alt = item.display_name;
      image.loading = "lazy";
      button.append(image, text("span", item.display_name));
      button.addEventListener("click", () => setSelected({
        kind: "asset",
        assetId: item.asset_id,
        name: item.display_name,
        preview: item.preview_url,
        sourceWidth: item.width,
        sourceHeight: item.height,
        detail: `${item.width} × ${item.height} · ${friendlyBytes(item.byte_size)}`,
      }));
      sourceLibrary.append(button);
    });
    q("[data-load-more-sources]").hidden = state.sourceLimit >= 200 || items.length < state.sourceLimit;
    if (state.selected?.assetId) setSelected(state.selected);
  }

  function activateSourceTab(name) {
    root.querySelectorAll("[data-source-tab]").forEach((button) => {
      const active = button.dataset.sourceTab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    root.querySelectorAll("[data-source-view]").forEach((view) => {
      view.hidden = view.dataset.sourceView !== name;
    });
  }

  function setAssetIdStatus(message, error = false) {
    assetIdStatus.textContent = message;
    assetIdStatus.classList.toggle("error", error);
  }

  async function selectAssetById(event) {
    event.preventDefault();
    const assetId = assetIdInput.value.trim();
    assetIdInput.value = assetId;
    assetIdInput.setCustomValidity("");
    if (!assetIdPattern.test(assetId)) {
      const message = "Enter a complete asset UUID, including its four hyphens.";
      assetIdInput.setCustomValidity(message);
      setAssetIdStatus(message, true);
      assetIdInput.reportValidity();
      return;
    }

    assetIdForm.setAttribute("aria-busy", "true");
    assetIdInput.disabled = true;
    assetIdButton.disabled = true;
    assetIdButton.textContent = "Selecting…";
    setAssetIdStatus("Registering the generated image…");
    try {
      const input = await api("/inputs/from-asset", {
        method: "POST",
        body: JSON.stringify({ asset_id: assetId }),
      });
      const registeredAssetId = input.asset_id || assetId;
      form.elements.match_source_aspect.checked = true;
      setSelected({
        kind: "input",
        inputId: input.input_id,
        assetId: registeredAssetId,
        name: input.display_name,
        preview: `${apiBase}/source-images/${registeredAssetId}/preview/${input.sha256.slice(0, 16)}.jpg`,
        sourceWidth: input.width,
        sourceHeight: input.height,
        detail: `${input.width} × ${input.height} · ${friendlyBytes(input.byte_size)}`,
      });
      setAssetIdStatus(`Selected ${input.width} × ${input.height} source image.`);
      announce("Generated image selected. Add motion direction and queue the generation.");
    } catch (error) {
      setAssetIdStatus(error.message, true);
      announce(error.message, true);
    } finally {
      assetIdForm.setAttribute("aria-busy", "false");
      assetIdInput.disabled = !canManage;
      assetIdButton.disabled = !canManage;
      assetIdButton.textContent = "Use image";
    }
  }

  function uploadDirect(grant, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(grant.method, grant.url, true);
      Object.entries(grant.headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value));
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
      });
      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(`Private storage rejected the upload (${xhr.status})`));
      });
      xhr.addEventListener("error", () => reject(new Error("The image upload could not reach private storage")));
      if (grant.method.toUpperCase() === "POST") {
        const body = new FormData();
        Object.entries(grant.fields || {}).forEach(([name, value]) => body.append(name, value));
        body.append("file", file);
        xhr.send(body);
      } else {
        xhr.setRequestHeader("Content-Type", grant.content_type);
        xhr.send(file);
      }
    });
  }

  async function uploadImage(file) {
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      announce("Choose a JPEG, PNG, or WebP image.", true); return;
    }
    if (maxImageBytes && file.size > maxImageBytes) {
      announce(`The image exceeds the ${friendlyBytes(maxImageBytes)} upload limit.`, true); return;
    }
    const box = q("[data-upload-progress]");
    const meter = q("[data-upload-meter]");
    const percent = q("[data-upload-percent]");
    const uploadStatus = q("[data-upload-status]");
    box.hidden = false;
    meter.value = 0;
    percent.textContent = "0%";
    uploadStatus.textContent = "Preparing private upload…";
    try {
      const grant = await api("/inputs/uploads", {
        method: "POST",
        body: JSON.stringify({ display_name: file.name, content_type: file.type }),
      });
      uploadStatus.textContent = "Uploading directly to private storage…";
      await uploadDirect(grant, file, (value) => { meter.value = value; percent.textContent = `${value}%`; });
      uploadStatus.textContent = "Verifying image…";
      const input = await api(`/inputs/uploads/${grant.upload_id}:complete`, {
        method: "POST",
        body: JSON.stringify({ display_name: file.name }),
      });
      meter.value = 100;
      percent.textContent = "100%";
      uploadStatus.textContent = "Verified and ready";
      setSelected({
        kind: "input", inputId: input.input_id, name: input.display_name, preview: "",
        sourceWidth: input.width, sourceHeight: input.height,
        detail: `${input.width} × ${input.height} · ${friendlyBytes(input.byte_size)}`,
      });
      announce("Image verified. Add motion direction and queue the generation.");
    } catch (error) {
      uploadStatus.textContent = "Upload failed";
      announce(error.message, true);
    }
  }

  function collectSettings() {
    if (loraWriteBlocked()) throw new Error(loraBlockMessage());
    const settings = {};
    const numbers = new Set(["frame_count", "fps", "width", "height", "seed", "steps", "high_end_step", "cfg", "high_shift", "low_shift", "loop_count"]);
    advanced.querySelectorAll("input[name], select[name]").forEach((field) => {
      if (field.type === "checkbox") settings[field.name] = field.checked;
      else if (numbers.has(field.name)) settings[field.name] = Number(field.value);
      else settings[field.name] = field.value;
    });
    settings.loras = [...state.loraSelections].map(([catalogId, strength]) => ({
      catalog_id: catalogId,
      strength,
    }));
    return settings;
  }

  function applySettings(settings = {}) {
    const resolved = { ...workerSettingDefaults, ...settings };
    Object.entries(resolved).forEach(([name, value]) => {
      if (name === "loras") return;
      const field = advanced.querySelector(`[name="${CSS.escape(name)}"]`);
      if (!field) return;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else field.value = String(value);
    });
    setLoraSelections(Array.isArray(resolved.loras) ? resolved.loras : []);
    syncAspectControls();
    updateDuration();
  }

  function sourceNativeDimensions(sourceWidth, sourceHeight) {
    if (!(sourceWidth > 0 && sourceHeight > 0)) return null;
    const sourceRatio = sourceWidth / sourceHeight;
    let best = null;
    for (let width = 32; width <= 1024; width += 32) {
      for (let height = 32; height <= 1024; height += 32) {
        const pixels = width * height;
        if (pixels < 520000 || pixels > 830000) continue;
        const error = Math.abs(Math.log((width / height) / sourceRatio));
        if (!best || error < best.error || (error === best.error && pixels > best.pixels)) {
          best = { width, height, pixels, error };
        }
      }
    }
    return best;
  }

  function syncAspectControls() {
    const automatic = form.elements.match_source_aspect.checked;
    form.elements.width.disabled = automatic;
    form.elements.height.disabled = automatic;
    if (!automatic) return;
    const dimensions = sourceNativeDimensions(
      state.selected?.sourceWidth,
      state.selected?.sourceHeight,
    );
    if (!dimensions) return;
    form.elements.width.value = String(dimensions.width);
    form.elements.height.value = String(dimensions.height);
  }

  function setLoraSelections(values = []) {
    state.loraSelections.clear();
    values.forEach((selection) => {
      if (!selection || typeof selection.catalog_id !== "string") return;
      const strength = Number(selection.strength);
      if (Number.isFinite(strength)) state.loraSelections.set(selection.catalog_id, strength);
    });
    if (values.length && state.loraCatalogLoaded && !state.loraProfileEnabled) {
      announce(
        "This saved item contains reviewed LoRAs that are unavailable on the current profile. Clear them before queueing or saving.",
        true,
      );
    }
    renderLoraCatalog();
    syncLoraPromptPreview();
  }

  function renderLoraCatalog() {
    loraList.replaceChildren();
    const available = state.loraCatalog.filter((entry) => entry.available);
    const unavailable = [...state.loraSelections.keys()].filter((catalogId) => (
      !available.some((entry) => entry.catalog_id === catalogId)
    ));
    if (unavailable.length) {
      loraList.append(text(
        "p",
        `Saved LoRA selections are unavailable (${unavailable.join(", ")}). Clear them before saving or queueing.`,
        "i2v-lora-warning",
      ));
    }
    if (!available.length) {
      loraList.append(text("p", state.loraProfileEnabled
        ? "No reviewed LoRAs are installed on this worker profile."
        : "The current worker remains on the no-LoRA baseline.", "muted"));
      clearLorasButton.disabled = !canManage || state.loraSelections.size === 0;
      return;
    }
    available.forEach((entry) => {
      const selectedStrength = state.loraSelections.get(entry.catalog_id);
      const enabled = selectedStrength !== undefined;
      const card = document.createElement("article");
      card.className = "i2v-lora-card";
      card.dataset.loraCatalogId = entry.catalog_id;
      card.classList.toggle("selected", enabled);

      const toggleLabel = document.createElement("label");
      toggleLabel.className = "i2v-lora-toggle";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = enabled;
      toggle.disabled = !canManage;
      toggle.setAttribute("aria-label", `Enable ${entry.display_name}`);
      const title = document.createElement("span");
      const promptTerms = entry.trigger_words.length
        ? `Prompt terms: ${entry.trigger_words.join(", ")}`
        : "No trained trigger";
      title.append(
        text("strong", entry.display_name),
        text("small", promptTerms),
      );
      toggleLabel.append(toggle, title);

      const strengthLabel = document.createElement("label");
      strengthLabel.className = "i2v-lora-strength";
      strengthLabel.append(document.createTextNode("Strength"));
      const strength = document.createElement("input");
      strength.type = "number";
      strength.min = String(entry.minimum_strength);
      strength.max = String(entry.maximum_strength);
      strength.step = String(entry.strength_step);
      strength.value = String(selectedStrength ?? entry.recommended_initial_strength);
      strength.disabled = !canManage || !enabled;
      strength.required = enabled;
      strength.setAttribute("aria-label", `${entry.display_name} strength`);
      strengthLabel.append(strength);

      const usage = entry.credit_required ? "Creator credit required." : "Creator credit optional.";
      const commercial = entry.commercial_use.length
        ? `Source commercial-use metadata: ${entry.commercial_use.join(", ")}.`
        : "Source metadata records no commercial-use option.";
      const derivatives = entry.derivatives_allowed
        ? "Model derivatives permitted by recorded source metadata."
        : "Model derivatives not permitted by recorded source metadata.";
      const attribution = document.createElement("p");
      attribution.className = "i2v-lora-source";
      attribution.append(document.createTextNode(entry.credit_required
        ? `Credit ${entry.creator_name}. Source: `
        : `Creator ${entry.creator_name}. Source: `));
      const sourceLink = document.createElement("a");
      sourceLink.href = entry.canonical_source_url;
      sourceLink.target = "_blank";
      sourceLink.rel = "noopener noreferrer";
      sourceLink.textContent = "canonical Civitai model";
      attribution.append(sourceLink);
      entry.canonical_version_urls.forEach((versionUrl, index) => {
        attribution.append(document.createTextNode(index === 0 ? " · exact high version: " : " · exact low version: "));
        const versionLink = document.createElement("a");
        versionLink.href = versionUrl;
        versionLink.target = "_blank";
        versionLink.rel = "noopener noreferrer";
        versionLink.textContent = index === 0 ? "high" : "low";
        attribution.append(versionLink);
      });
      const guidance = text("p", [
        `${entry.strength_guidance}.`,
        `${entry.prompt_behavior}.`,
        entry.usage_notes,
        usage,
        commercial,
        derivatives,
      ].join(" "), "i2v-lora-guidance");
      toggle.addEventListener("change", () => {
        if (
          toggle.checked
          && state.maximumLoraSelections > 0
          && state.loraSelections.size >= state.maximumLoraSelections
        ) {
          toggle.checked = false;
          announce(`Choose at most ${state.maximumLoraSelections} reviewed LoRAs.`, true);
          return;
        }
        if (toggle.checked) state.loraSelections.set(entry.catalog_id, Number(strength.value));
        else state.loraSelections.delete(entry.catalog_id);
        renderLoraCatalog();
        syncLoraPromptPreview();
        scheduleDraftSave();
      });
      strength.addEventListener("input", () => {
        if (strength.validity.valid) {
          state.loraSelections.set(entry.catalog_id, Number(strength.value));
        }
        syncLoraPromptPreview();
        scheduleDraftSave();
      });
      card.append(toggleLabel, strengthLabel, guidance, attribution);
      loraList.append(card);
    });
    clearLorasButton.disabled = !canManage || state.loraSelections.size === 0;
  }

  function syncLoraPromptPreview() {
    const selected = state.loraCatalog.filter((entry) => (
      state.loraSelections.has(entry.catalog_id)
    ));
    loraEffective.hidden = selected.length === 0;
    if (!selected.length) return;
    const triggers = selected.flatMap((entry) => entry.automatic_trigger_words);
    const manual = selected.filter((entry) => (
      entry.automatic_trigger_words.length === 0 && entry.trigger_words.length > 0
    ));
    const authoredPrompt = form.elements.positive_prompt.value.trim();
    const appended = [];
    const present = [];
    let effectivePrompt = authoredPrompt;
    triggers.forEach((trigger) => {
      if (hasPromptTrigger(effectivePrompt, trigger)) present.push(trigger);
      else appended.push(trigger);
      effectivePrompt = appendPromptTriggerOnce(effectivePrompt, trigger);
    });
    const parts = [`Effective positive prompt: ${effectivePrompt || "(empty)"}`];
    if (appended.length) parts.push(`Worker appends once: ${appended.join(", ")}`);
    if (present.length) parts.push(`Already present: ${present.join(", ")}`);
    manual.forEach((entry) => {
      parts.push(`Choose one for ${entry.display_name}: ${entry.trigger_words.join(" / ")}`);
    });
    selectedManualPromptConflicts().forEach((entry) => {
      parts.push(`Remove conflicting ${entry.display_name} terms: choose exactly one.`);
    });
    selected.filter((entry) => entry.trigger_words.length === 0).forEach((entry) => {
      parts.push(`${entry.display_name}: descriptive prompt only`);
    });
    loraEffectiveText.textContent = parts.join(" · ");
    updateSubmitState();
  }

  function selectedManualPromptConflicts() {
    const prompt = form.elements.positive_prompt.value.trim();
    return state.loraCatalog.filter((entry) => (
      state.loraSelections.has(entry.catalog_id)
      && entry.automatic_trigger_words.length === 0
      && entry.trigger_words.filter((trigger) => hasPromptTrigger(prompt, trigger)).length > 1
    ));
  }

  function unavailableLoraSelections() {
    const availableIds = new Set(
      state.loraCatalog.filter((entry) => entry.available).map((entry) => entry.catalog_id),
    );
    return [...state.loraSelections.keys()].filter((catalogId) => !availableIds.has(catalogId));
  }

  function loraWriteBlocked() {
    return (state.loraProfileEnabled && (!state.loraCatalogLoaded || state.loraCatalogError))
      || unavailableLoraSelections().length > 0;
  }

  function loraBlockMessage() {
    if (state.loraCatalogError) return "The reviewed LoRA catalog could not be verified; queue and preset writes are blocked.";
    if (!state.loraCatalogLoaded) return "Waiting for the reviewed LoRA catalog before queueing or saving.";
    return "Saved LoRA selections are unavailable. Clear them before queueing or saving.";
  }

  function promptTriggerPattern(trigger, global = false) {
    const escaped = trigger.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
    return new RegExp(`(?<!\\w)${escaped}(?!\\w)`, global ? "giu" : "iu");
  }

  function hasPromptTrigger(prompt, trigger) {
    return promptTriggerPattern(trigger).test(prompt);
  }

  function appendPromptTriggerOnce(prompt, trigger) {
    const matches = [...prompt.matchAll(promptTriggerPattern(trigger, true))];
    if (!matches.length) return prompt ? `${prompt}, ${trigger}` : trigger;
    if (matches.length === 1) return prompt;
    const firstEnd = (matches[0].index || 0) + matches[0][0].length;
    const pieces = [prompt.slice(0, firstEnd)];
    let cursor = firstEnd;
    matches.slice(1).forEach((match) => {
      pieces.push(prompt.slice(cursor, match.index));
      cursor = (match.index || 0) + match[0].length;
    });
    pieces.push(prompt.slice(cursor));
    return pieces.join("");
  }

  async function loadLoraCatalog() {
    try {
      const catalog = await api("/loras");
      state.loraProfileEnabled = catalog.profile_enabled;
      state.maximumLoraSelections = Number(catalog.maximum_selections) || 0;
      state.loraCatalog = Array.isArray(catalog.loras) ? catalog.loras : [];
      state.loraCatalogLoaded = true;
      state.loraCatalogError = false;
      loraStatus.textContent = catalog.message;
      renderLoraCatalog();
      syncLoraPromptPreview();
      if (!state.loraProfileEnabled && state.loraSelections.size) {
        announce(
          "Saved reviewed LoRA settings cannot be reused on the current profile. Clear them before queueing or saving.",
          true,
        );
      }
    } catch (error) {
      state.loraCatalogLoaded = false;
      state.loraCatalogError = true;
      loraStatus.textContent = "The reviewed LoRA catalog could not be verified. No settings will be silently removed.";
      renderLoraCatalog();
      updateSubmitState();
      throw error;
    }
  }

  function updateDuration() {
    const frames = Number(form.elements.frame_count.value) || 0;
    const fps = Number(form.elements.fps.value) || 0;
    const loopToggle = form.elements.loop;
    const stableExpression = form.elements.face_fidelity.value === "stable_expression";
    if (stableExpression) loopToggle.checked = false;
    loopToggle.disabled = stableExpression;
    const loopEnabled = loopToggle.checked;
    const loopField = form.elements.loop_count;
    const cycleFrames = Math.max(1, (frames * 2) - 2);
    const allowedCycles = Math.min(
      maxLoopCycles,
      Math.floor((maxLoopDurationSeconds * fps) / cycleFrames),
    );
    loopField.disabled = !loopEnabled;
    loopField.max = String(Math.max(1, allowedCycles));
    if (allowedCycles > 0) {
      loopField.setCustomValidity("");
      if (Number(loopField.value) > allowedCycles) loopField.value = String(allowedCycles);
    } else if (loopEnabled) {
      loopField.setCustomValidity("One ping-pong cycle exceeds the 25-second limit.");
    } else {
      loopField.setCustomValidity("");
    }
    const loopCount = Math.max(1, Number(loopField.value) || 1);
    const outputFrames = form.elements.loop.checked
      ? cycleFrames * loopCount
      : frames;
    q("[data-duration]").textContent = `${(outputFrames / Math.max(1, fps)).toFixed(2)} seconds`;
  }

  function draftPayload() {
    return {
      positive_prompt: form.elements.positive_prompt.value,
      negative_prompt: form.elements.negative_prompt.value,
      batch_count: form.elements.batch_count.value,
      settings: collectSettings(),
    };
  }

  function scheduleDraftSave() {
    clearTimeout(saveTimer);
    draftState.textContent = "Saving draft…";
    saveTimer = setTimeout(() => {
      try { localStorage.setItem(draftKey, JSON.stringify(draftPayload())); draftState.textContent = "Draft saved locally"; }
      catch (_) { draftState.textContent = "Draft could not be saved"; }
    }, 300);
    updateSubmitState();
  }

  function restoreDraft() {
    try {
      const draft = JSON.parse(localStorage.getItem(draftKey) || "null");
      if (!draft) return;
      form.elements.positive_prompt.value = draft.positive_prompt || "";
      form.elements.negative_prompt.value = draft.negative_prompt || "";
      form.elements.batch_count.value = draft.batch_count || "1";
      applySettings(draft.settings || {});
    } catch (_) { localStorage.removeItem(draftKey); }
  }

  async function loadPresets() {
    state.presets = await api("/presets");
    const selected = presetSelect.value;
    presetSelect.replaceChildren(new Option("Custom settings", ""));
    state.presets.forEach((item) => presetSelect.add(new Option(item.name, item.preset_id)));
    if (state.presets.some((item) => item.preset_id === selected)) presetSelect.value = selected;
    updatePresetButtons();
  }

  function updatePresetButtons() {
    const selected = Boolean(presetSelect.value);
    q("[data-preset-update]").disabled = !canManage || !selected || loraWriteBlocked();
    q("[data-preset-delete]").disabled = !canManage || !selected;
  }

  function applyPreset(preset) {
    form.elements.positive_prompt.value = preset.positive_prompt;
    form.elements.negative_prompt.value = preset.negative_prompt;
    applySettings(preset.settings);
    scheduleDraftSave();
  }

  async function createPreset(name, description) {
    const preset = await api("/presets", { method: "POST", body: JSON.stringify({
      name, description,
      positive_prompt: form.elements.positive_prompt.value,
      negative_prompt: form.elements.negative_prompt.value,
      settings: collectSettings(),
    }) });
    await loadPresets();
    presetSelect.value = preset.preset_id;
    updatePresetButtons();
    announce(`Preset “${preset.name}” saved.`);
  }

  async function resolveInput() {
    if (!state.selected) throw new Error("Select a source image first");
    if (state.selected.inputId) return state.selected.inputId;
    const input = await api("/inputs/from-asset", {
      method: "POST", body: JSON.stringify({ asset_id: state.selected.assetId }),
    });
    state.selected.inputId = input.input_id;
    return input.input_id;
  }

  async function enqueue(event) {
    event.preventDefault();
    if (!form.reportValidity()) return;
    enqueueButton.disabled = true;
    enqueueButton.textContent = "Adding to queue…";
    try {
      const inputId = await resolveInput();
      const result = await api("/jobs", { method: "POST", body: JSON.stringify({
        input_id: inputId,
        preset_id: presetSelect.value || null,
        positive_prompt: form.elements.positive_prompt.value,
        negative_prompt: form.elements.negative_prompt.value,
        settings: collectSettings(),
        batch_count: Number(form.elements.batch_count.value),
      }) });
      announce(`${result.jobs.length} ${result.jobs.length === 1 ? "generation" : "generations"} added to the queue.`);
      await Promise.all([loadQueue(), loadWorker()]);
    } catch (error) { announce(error.message, true); }
    finally { enqueueButton.textContent = "Queue generation"; updateSubmitState(); }
  }

  function statusChip(value) {
    const chip = text("span", value.replaceAll("_", " "), `status ${value}`);
    return chip;
  }

  function queueAction(label, action, disabled = false) {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = label; button.disabled = disabled || !canManage;
    button.addEventListener("click", action);
    return button;
  }

  async function jobAction(path, confirmation) {
    if (confirmation && !window.confirm(confirmation)) return;
    try { await api(path, { method: "POST" }); await Promise.all([loadQueue(), loadWorker(), loadOutputs()]); }
    catch (error) { announce(error.message, true); }
  }

  async function moveJob(job, direction) {
    const queued = state.jobs.filter((item) => item.state === "queued");
    const index = queued.findIndex((item) => item.job_id === job.job_id);
    const neighbor = queued[index + direction];
    if (!neighbor) return;
    const payload = direction < 0
      ? { job_id: job.job_id, before_job_id: neighbor.job_id }
      : { job_id: job.job_id, after_job_id: neighbor.job_id };
    try { await api("/queue", { method: "PATCH", body: JSON.stringify(payload) }); await loadQueue(); }
    catch (error) { announce(error.message, true); }
  }

  function renderQueue() {
    queue.replaceChildren();
    if (!state.jobs.length) { queue.append(text("li", "The queue is empty. Add a generation when you’re ready.", "i2v-empty")); return; }
    const queued = state.jobs.filter((item) => item.state === "queued");
    state.jobs.forEach((job) => {
      const item = document.createElement("li"); item.className = "i2v-job"; item.dataset.jobId = job.job_id;
      const head = document.createElement("div"); head.className = "i2v-job-head";
      const position = job.queue_position == null ? job.state.replaceAll("_", " ") : `Queue #${job.queue_position}`;
      head.append(text("strong", position), statusChip(job.state));
      const prompt = text("p", job.positive_prompt || "No positive prompt");
      const meta = text("div", `${String(job.job_id).slice(0, 8)} · ${friendlyDate(job.created_at)}`, "i2v-job-meta");
      const actions = document.createElement("div"); actions.className = "i2v-job-actions";
      if (job.state === "queued") {
        const index = queued.findIndex((candidate) => candidate.job_id === job.job_id);
        actions.append(
          queueAction("↑", () => moveJob(job, -1), index === 0),
          queueAction("↓", () => moveJob(job, 1), index === queued.length - 1),
        );
      }
      if (["queued", "claimed", "running"].includes(job.state)) {
        actions.append(queueAction("Cancel", () => jobAction(`/jobs/${job.job_id}:cancel`, "Cancel this generation?")));
      }
      if (["failed", "cancelled"].includes(job.state)) actions.append(queueAction("Retry", () => jobAction(`/jobs/${job.job_id}:retry`)));
      const reuse = queueAction("Use settings", () => {
        form.elements.positive_prompt.value = job.positive_prompt;
        form.elements.negative_prompt.value = job.negative_prompt;
        applySettings(job.settings_snapshot || {});
        form.scrollIntoView({ behavior: "smooth", block: "start" });
        scheduleDraftSave();
      });
      actions.append(reuse);
      item.append(head, prompt, meta, actions); queue.append(item);
    });
  }

  async function loadQueue() { state.jobs = await api("/jobs"); renderQueue(); }

  async function loadWorker() {
    try {
      const worker = await api("/worker");
      const deployment = worker.deployment;
      const workerState = deployment?.state || (worker.configured ? "unknown" : "stopped");
      q("[data-worker-state]").textContent = deployment ? workerState.replaceAll("_", " ") : "No active worker";
      q("[data-worker-message]").textContent = worker.message;
      q("[data-worker-dot]").className = `i2v-worker-dot ${workerState}`;
      const facts = q("[data-worker-facts]"); facts.hidden = !deployment;
      if (deployment) {
        q("[data-worker-machine]").textContent = deployment.provider_instance_id || "Provider has not assigned one";
        q("[data-worker-heartbeat]").textContent = friendlyDate(deployment.last_heartbeat_at);
        q("[data-worker-job]").textContent = deployment.current_job_id ? String(deployment.current_job_id).slice(0, 8) : "None";
      }
    } catch (error) {
      q("[data-worker-state]").textContent = "Status unavailable";
      q("[data-worker-message]").textContent = error.message;
      q("[data-worker-dot]").className = "i2v-worker-dot unknown";
    }
  }

  async function loadOutputs() {
    const items = await api("/outputs/recent?limit=24");
    videoGrid.replaceChildren();
    if (!items.length) { videoGrid.append(text("p", "No completed videos yet. Successful generations will appear here.", "muted")); return; }
    items.forEach((item) => {
      const output = item.output;
      const card = document.createElement("article"); card.className = "i2v-video-card";
      const video = document.createElement("video");
      video.src = item.playback_url; video.controls = true; video.preload = "none"; video.playsInline = true;
      const body = document.createElement("div"); body.className = "i2v-video-body";
      const heading = document.createElement("div");
      heading.append(text("strong", `Video ${String(output.output_id).slice(0, 8)}`), statusChip("succeeded"));
      const facts = text("p", `${output.width} × ${output.height} · ${output.frame_count} frames · ${output.fps} fps · ${(output.duration_ms / 1000).toFixed(2)}s`);
      const actions = document.createElement("div"); actions.className = "i2v-video-actions";
      const download = document.createElement("a"); download.className = "secondary-button"; download.href = item.download_url; download.textContent = "Download";
      const reuse = document.createElement("button"); reuse.className = "secondary-button"; reuse.type = "button"; reuse.textContent = "Use settings";
      reuse.addEventListener("click", () => {
        const job = state.jobs.find((candidate) => candidate.job_id === output.job_id);
        if (!job) { announce("The source job is outside the current history.", true); return; }
        form.elements.positive_prompt.value = job.positive_prompt;
        form.elements.negative_prompt.value = job.negative_prompt;
        applySettings(job.settings_snapshot || {}); scheduleDraftSave(); form.scrollIntoView({ behavior: "smooth" });
      });
      actions.append(download, reuse); body.append(heading, facts, actions); card.append(video, body); videoGrid.append(card);
    });
  }

  root.querySelectorAll("[data-source-tab]").forEach((button) => button.addEventListener("click", () => activateSourceTab(button.dataset.sourceTab)));
  q("[data-refresh-library]").addEventListener("click", () => {
    state.sourceLimit = sourcePageSize;
    loadSources().catch((error) => announce(error.message, true));
  });
  q("[data-load-more-sources]").addEventListener("click", () => {
    state.sourceLimit = Math.min(200, state.sourceLimit + sourcePageSize);
    loadSources({ append: true }).catch((error) => announce(error.message, true));
  });
  assetIdForm.addEventListener("submit", selectAssetById);
  assetIdInput.addEventListener("input", () => {
    assetIdInput.setCustomValidity("");
    setAssetIdStatus("Paste an exact generated-image asset UUID.");
  });
  q("[data-clear-source]").addEventListener("click", () => setSelected(null));
  q("[data-upload-file]").addEventListener("change", (event) => uploadImage(event.target.files[0]));
  const dropzone = q("[data-upload-dropzone]");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", (event) => uploadImage(event.dataTransfer.files[0]));
  form.addEventListener("submit", enqueue);
  form.addEventListener("input", (event) => {
    if (event.target.name === "match_source_aspect") syncAspectControls();
    if (["frame_count", "fps", "loop", "loop_count", "face_fidelity"].includes(event.target.name)) updateDuration();
    if (event.target.name === "positive_prompt") syncLoraPromptPreview();
    scheduleDraftSave();
  });
  form.addEventListener("change", scheduleDraftSave);
  clearLorasButton.addEventListener("click", () => {
    state.loraSelections.clear();
    renderLoraCatalog();
    syncLoraPromptPreview();
    scheduleDraftSave();
  });
  q("[data-refresh-queue]").addEventListener("click", () => loadQueue().catch((error) => announce(error.message, true)));
  q("[data-refresh-outputs]").addEventListener("click", () => loadOutputs().catch((error) => announce(error.message, true)));

  presetSelect.addEventListener("change", () => {
    const preset = state.presets.find((item) => item.preset_id === presetSelect.value);
    if (preset) applyPreset(preset); updatePresetButtons();
  });
  q("[data-preset-save]").addEventListener("click", () => {
    presetName.disabled = false;
    presetDialog.showModal();
    presetName.focus();
  });
  presetDialog.addEventListener("close", () => {
    presetDialogForm.reset();
    presetName.disabled = true;
  });
  q("[data-preset-update]").addEventListener("click", async () => {
    const preset = state.presets.find((item) => item.preset_id === presetSelect.value); if (!preset) return;
    try {
      await api(`/presets/${preset.preset_id}`, { method: "PUT", body: JSON.stringify({
        name: preset.name, description: preset.description,
        positive_prompt: form.elements.positive_prompt.value,
        negative_prompt: form.elements.negative_prompt.value,
        settings: collectSettings(), expected_lock_version: preset.lock_version,
      }) });
      await loadPresets(); announce(`Preset “${preset.name}” updated.`);
    } catch (error) { announce(error.message, true); }
  });
  q("[data-preset-delete]").addEventListener("click", async () => {
    const preset = state.presets.find((item) => item.preset_id === presetSelect.value); if (!preset || !window.confirm(`Delete “${preset.name}”?`)) return;
    try { await api(`/presets/${preset.preset_id}`, { method: "DELETE" }); presetSelect.value = ""; await loadPresets(); announce("Preset deleted."); }
    catch (error) { announce(error.message, true); }
  });
  presetDialogForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") { presetDialog.close(); return; }
    const data = new FormData(event.currentTarget);
    try { await createPreset(String(data.get("name") || ""), String(data.get("description") || "")); presetDialog.close(); }
    catch (error) { announce(error.message, true); }
  });

  if (!canManage) {
    assetIdInput.disabled = true;
    assetIdButton.disabled = true;
    form.querySelectorAll("input, textarea, select, button").forEach((element) => { element.disabled = true; });
    announce("Your account can view image-to-video activity but cannot change the queue.");
  }
  restoreDraft(); updateDuration(); updateSubmitState();
  Promise.allSettled([
    loadSources(),
    loadLoraCatalog(),
    loadPresets(),
    loadQueue(),
    loadWorker(),
    loadOutputs(),
  ]).then((results) => {
    const failed = results.find((result) => result.status === "rejected");
    if (failed) announce(failed.reason.message, true);
  });
  window.setInterval(() => { loadQueue().catch(() => {}); loadWorker().catch(() => {}); }, 15000);
})();
