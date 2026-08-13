(() => {
  "use strict";

  const root = document.querySelector("[data-i2v-studio]");
  if (!root) return;

  const apiBase = root.dataset.apiBase || "/api/v1/i2v";
  const csrfToken = root.dataset.csrfToken || "";
  const canManage = root.dataset.canManage === "true";
  const hiresProfileEnabled = root.dataset.hiresProfileEnabled === "true";
  const maxImageBytes = Number(root.dataset.maxImageBytes || 0);
  const scope = document.body.dataset.automationStorageScope || "operator";
  const draftKey = `i2v-draft-v1:${scope}`;
  const form = root.querySelector("[data-generation-form]");
  const advanced = root.querySelector("[data-advanced-settings]");
  const sourceLibrary = root.querySelector("[data-source-library]");
  const selectedCard = root.querySelector("[data-selected-source]");
  const enqueueButton = root.querySelector("[data-enqueue]");
  const globalStatus = root.querySelector("[data-global-status]");
  const presetSelect = root.querySelector("[data-preset-select]");
  const queue = root.querySelector("[data-job-queue]");
  const videoGrid = root.querySelector("[data-video-grid]");
  const loraList = root.querySelector("[data-lora-list]");
  const presetDialog = root.querySelector("[data-preset-dialog]");
  const draftState = root.querySelector("[data-draft-state]");
  const state = { selected: null, presets: [], jobs: [], sourceLimit: 100 };
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
    enqueueButton.disabled = !hiresProfileEnabled || !canManage || !state.selected || !form.elements.positive_prompt.value.trim();
    q("[data-submit-summary]").textContent = !hiresProfileEnabled
      ? "Waiting for the matching high-resolution worker rollout"
      : state.selected
      ? `${copies} ${copies === 1 ? "generation" : "generations"} will be added to the queue`
      : "Select a source to continue";
  }

  async function loadSources() {
    const items = await api(`/source-images?limit=${state.sourceLimit}`);
    sourceLibrary.replaceChildren();
    if (!items.length) {
      sourceLibrary.append(text("p", "No completed generation images are available yet.", "muted"));
      return;
    }
    items.forEach((item) => {
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
    const settings = {};
    const numbers = new Set(["frame_count", "fps", "width", "height", "seed", "steps", "high_end_step", "cfg", "high_shift", "low_shift", "loop_count"]);
    advanced.querySelectorAll("input[name], select[name]").forEach((field) => {
      if (field.type === "checkbox") settings[field.name] = field.checked;
      else if (numbers.has(field.name)) settings[field.name] = Number(field.value);
      else settings[field.name] = field.value;
    });
    settings.loras = [...loraList.querySelectorAll("[data-lora-row]")].map((row) => ({
      high: row.querySelector('[name="lora_high"]').value.trim(),
      low: row.querySelector('[name="lora_low"]').value.trim(),
      strength: Number(row.querySelector('[name="lora_strength"]').value),
    })).filter((item) => item.high || item.low);
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
    loraList.replaceChildren();
    (Array.isArray(resolved.loras) ? resolved.loras : []).forEach(addLoraRow);
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

  function addLoraRow(value = {}) {
    const row = document.createElement("div");
    row.className = "i2v-lora-row";
    row.dataset.loraRow = "";
    const high = document.createElement("input");
    high.name = "lora_high"; high.placeholder = "High .safetensors"; high.value = value.high || "";
    high.setAttribute("aria-label", "High-noise LoRA filename");
    const low = document.createElement("input");
    low.name = "lora_low"; low.placeholder = "Low .safetensors"; low.value = value.low || "";
    low.setAttribute("aria-label", "Low-noise LoRA filename");
    const strength = document.createElement("input");
    strength.name = "lora_strength"; strength.type = "number"; strength.step = "0.05"; strength.value = value.strength ?? "0.3";
    strength.setAttribute("aria-label", "LoRA strength");
    const remove = document.createElement("button");
    remove.type = "button"; remove.className = "i2v-icon-button danger"; remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove LoRA pair");
    remove.addEventListener("click", () => { row.remove(); scheduleDraftSave(); });
    row.append(high, low, strength, remove);
    row.addEventListener("input", scheduleDraftSave);
    loraList.append(row);
  }

  function updateDuration() {
    const frames = Number(form.elements.frame_count.value) || 0;
    const fps = Number(form.elements.fps.value) || 0;
    const loopEnabled = form.elements.loop.checked;
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
    q("[data-preset-update]").disabled = !canManage || !selected;
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
      video.src = item.playback_url; video.controls = true; video.preload = "metadata"; video.playsInline = true;
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
  q("[data-refresh-library]").addEventListener("click", () => loadSources().catch((error) => announce(error.message, true)));
  q("[data-load-more-sources]").addEventListener("click", () => { state.sourceLimit = 200; loadSources().catch((error) => announce(error.message, true)); });
  q("[data-clear-source]").addEventListener("click", () => setSelected(null));
  q("[data-upload-file]").addEventListener("change", (event) => uploadImage(event.target.files[0]));
  const dropzone = q("[data-upload-dropzone]");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", (event) => uploadImage(event.dataTransfer.files[0]));
  form.addEventListener("submit", enqueue);
  form.addEventListener("input", (event) => {
    if (event.target.name === "match_source_aspect") syncAspectControls();
    if (["frame_count", "fps", "loop", "loop_count"].includes(event.target.name)) updateDuration();
    scheduleDraftSave();
  });
  form.addEventListener("change", scheduleDraftSave);
  q("[data-add-lora]").addEventListener("click", () => addLoraRow());
  q("[data-refresh-queue]").addEventListener("click", () => loadQueue().catch((error) => announce(error.message, true)));
  q("[data-refresh-outputs]").addEventListener("click", () => loadOutputs().catch((error) => announce(error.message, true)));

  presetSelect.addEventListener("change", () => {
    const preset = state.presets.find((item) => item.preset_id === presetSelect.value);
    if (preset) applyPreset(preset); updatePresetButtons();
  });
  q("[data-preset-save]").addEventListener("click", () => presetDialog.showModal());
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
  q("[data-preset-dialog-form]").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") { presetDialog.close(); return; }
    const data = new FormData(event.currentTarget);
    try { await createPreset(String(data.get("name") || ""), String(data.get("description") || "")); presetDialog.close(); event.currentTarget.reset(); }
    catch (error) { announce(error.message, true); }
  });

  if (!canManage) {
    form.querySelectorAll("input, textarea, select, button").forEach((element) => { element.disabled = true; });
    announce("Your account can view image-to-video activity but cannot change the queue.");
  }
  restoreDraft(); updateDuration(); updateSubmitState();
  Promise.allSettled([loadSources(), loadPresets(), loadQueue(), loadWorker(), loadOutputs()]).then((results) => {
    const failed = results.find((result) => result.status === "rejected");
    if (failed) announce(failed.reason.message, true);
  });
  window.setInterval(() => { loadQueue().catch(() => {}); loadWorker().catch(() => {}); }, 15000);
})();
