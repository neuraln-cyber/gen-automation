(() => {
  "use strict";

  const integerValue = (value, fallback = 0) => {
    const parsed = Number.parseInt(String(value), 10);
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  const slugify = (value) => value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80)
    .replace(/-+$/g, "");

  const optionalText = (value) => value.trim() === "" ? null : value;

  const AUTOMATION_PRESET_STORAGE_KEY = "gen-automation:generation-presets:v1";
  const AUTOMATION_DRAFT_STORAGE_KEY = "gen-automation:automation-draft:v1";
  const AUTOMATION_DRAFT_SUBMISSION_KEY = "gen-automation:automation-draft-submission:v1";
  const PENDING_IMAGE_PROFILE_KEY = "gen-automation:reuse-image-settings:v1";
  const SAME_PAGE_SCROLL_STORAGE_KEY = "gen-automation:same-page-scroll:v1";
  const REVIEW_COMPLETION_TIMEOUT_MS = 25000;
  const REVIEW_DECISION_REQUEST_TIMEOUT_MS = 10000;
  const REVIEW_DECISION_RETRY_DELAYS_MS = Object.freeze([400, 900, 1800, 3500, 5000]);
  const REVIEW_ACTION_FORM_SELECTOR = [
    "form[data-review-decision-form]",
    "form[data-bulk-action-form]",
    "form[data-x-selection-form]",
    "form[data-anatomy-feedback-form]",
    "form[data-review-cancel-form]",
  ].join(",");
  const scopedStorageKey = (key) => {
    const scope = document.body?.dataset.automationStorageScope?.trim() || "unknown-user";
    return `${key}:${scope}`;
  };

  const persistSamePageScroll = () => {
    try {
      window.sessionStorage.setItem(SAME_PAGE_SCROLL_STORAGE_KEY, JSON.stringify({
        path: `${window.location.pathname}${window.location.search}`,
        x: Math.max(0, Math.round(window.scrollX)),
        y: Math.max(0, Math.round(window.scrollY)),
        saved_at: Date.now(),
      }));
    } catch (_error) {
      // Storage can be unavailable without blocking the server-rendered fallback.
    }
  };

  function initializeSamePageScrollPreservation() {
    try {
      const raw = window.sessionStorage.getItem(SAME_PAGE_SCROLL_STORAGE_KEY);
      if (raw) {
        window.sessionStorage.removeItem(SAME_PAGE_SCROLL_STORAGE_KEY);
        const saved = JSON.parse(raw);
        const currentPath = `${window.location.pathname}${window.location.search}`;
        if (
          saved
          && saved.path === currentPath
          && Number.isFinite(saved.x)
          && Number.isFinite(saved.y)
          && Date.now() - integerValue(saved.saved_at, 0) < 300000
        ) {
          window.requestAnimationFrame(() => window.scrollTo(saved.x, saved.y));
        }
      }
    } catch (_error) {
      try {
        window.sessionStorage.removeItem(SAME_PAGE_SCROLL_STORAGE_KEY);
      } catch (_storageError) {
        // No-op: scroll restoration remains a progressive enhancement.
      }
    }

    document.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.matches(REVIEW_ACTION_FORM_SELECTOR)) return;
      if ((form.method || "get").toLowerCase() !== "post") return;
      persistSamePageScroll();
    }, { capture: true });
  }
  const AUTOMATION_PRESET_FIELDS = Object.freeze([
    "subject_id",
    "subject_2_id",
    "composition_mode",
    "character_a_prompt",
    "character_b_prompt",
    "checkpoint_id",
    "workflow_id",
    "prompt",
    "negative_prompt",
    "detailer_prompt",
    "detailer_negative_prompt",
    "width",
    "height",
    "cfg",
    "steps",
    "sampler",
    "scheduler",
    "clip_skip",
    "outputs_per_job",
    "hires_scale",
    "hires_denoise",
    "hires_upscale_method",
    "detailer_guide_size",
    "detailer_max_size",
    "detailer_denoise",
    "detailer_bbox_threshold",
    "detailer_bbox_dilation",
    "detailer_bbox_crop_factor",
    "detailer_feather",
  ]);

  const namedControl = (form, name) => form.querySelector(`[name="${name}"]`);

  const readStoredAutomationPresets = () => {
    try {
      const raw = window.localStorage.getItem(scopedStorageKey(AUTOMATION_PRESET_STORAGE_KEY));
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((item) => (
        item && typeof item === "object"
        && typeof item.id === "string"
        && typeof item.name === "string"
        && item.profile && typeof item.profile === "object"
      )).slice(0, 30);
    } catch (_error) {
      return [];
    }
  };

  const writeStoredAutomationPresets = (presets) => {
    window.localStorage.setItem(
      scopedStorageKey(AUTOMATION_PRESET_STORAGE_KEY),
      JSON.stringify(presets),
    );
  };

  const normalizeAutomationPresetBatchPlan = (requested) => {
    if (!Array.isArray(requested) || requested.length < 1 || requested.length > 50) {
      return null;
    }
    try {
      if (JSON.stringify(requested).length > 400_000) return null;
    } catch (_error) {
      return null;
    }
    const names = new Set();
    const maximumSeed = 9223372036854775807n;
    const optionalPromptFields = [
      "negative_prompt",
      "detailer_prompt",
      "detailer_negative_prompt",
    ];
    const normalized = [];
    for (const batch of requested) {
      if (!batch || typeof batch !== "object") return null;
      const name = typeof batch.name === "string" ? batch.name : "";
      const normalizedName = name.trim();
      const nameKey = normalizedName.toLowerCase();
      if (!normalizedName || normalizedName !== name || normalizedName.length > 100
          || names.has(nameKey)) return null;
      if (!Number.isInteger(batch.image_count)
          || batch.image_count < 1 || batch.image_count > 80_000) return null;
      if (typeof batch.prompt !== "string"
          || !batch.prompt.trim() || batch.prompt.length > 20_000) return null;

      const optionalPrompts = {};
      let optionalPromptsValid = true;
      optionalPromptFields.forEach((fieldName) => {
        const value = batch[fieldName];
        if (value === null || typeof value === "undefined"
            || (fieldName !== "negative_prompt" && value === "")) {
          optionalPrompts[fieldName] = null;
        } else if (typeof value === "string" && value.length <= 20_000) {
          optionalPrompts[fieldName] = value;
        } else {
          optionalPromptsValid = false;
        }
      });
      if (!optionalPromptsValid) return null;

      let seed = null;
      if (batch.seed !== null && typeof batch.seed !== "undefined" && batch.seed !== "") {
        if (typeof batch.seed !== "string") return null;
        seed = batch.seed.trim();
        if (!/^-?[0-9]+$/.test(seed)) return null;
        try {
          const numericSeed = BigInt(seed);
          if (numericSeed < -1n || numericSeed > maximumSeed) return null;
        } catch (_error) {
          return null;
        }
      }

      names.add(nameKey);
      normalized.push({
        name: normalizedName,
        image_count: batch.image_count,
        prompt: batch.prompt,
        negative_prompt: optionalPrompts.negative_prompt,
        detailer_prompt: optionalPrompts.detailer_prompt,
        detailer_negative_prompt: optionalPrompts.detailer_negative_prompt,
        seed,
      });
    }
    return normalized;
  };

  const collectAutomationPresetBatchPlan = (form) => {
    const planData = form.querySelector("#batch-plan-data");
    if (!(planData instanceof HTMLTextAreaElement)) return null;
    try {
      return normalizeAutomationPresetBatchPlan(JSON.parse(planData.value || "[]"));
    } catch (_error) {
      return null;
    }
  };

  const summarizeAutomationBatchPlan = (batchPlan) => ({
    batchCount: batchPlan.length,
    imageCount: batchPlan.reduce((total, batch) => total + batch.image_count, 0),
  });

  const collectAutomationProfile = (form) => {
    const fields = {};
    AUTOMATION_PRESET_FIELDS.forEach((name) => {
      const control = name === "composition_mode"
        ? (form.querySelector('[name="composition_mode"]:checked')
          || namedControl(form, "composition_mode"))
        : namedControl(form, name);
      if (control instanceof HTMLInputElement
          || control instanceof HTMLTextAreaElement
          || control instanceof HTMLSelectElement) {
        fields[name] = control.value;
      }
    });
    const loras = Array.from(form.querySelectorAll("[data-lora-native-slot]")).flatMap((slot) => {
      const idControl = slot.querySelector("[data-lora-native-id]");
      const weightControl = slot.querySelector("[data-lora-native-weight]");
      if (!(idControl instanceof HTMLSelectElement) || !idControl.value) return [];
      const catalogOption = Array.from(form.querySelectorAll("[data-lora-option]")).find(
        (option) => option.dataset.loraId === idControl.value,
      );
      return [{
        approval_id: idControl.value,
        name: catalogOption?.dataset.loraName || "",
        sha256: catalogOption?.dataset.loraSha256 || "",
        weight: weightControl instanceof HTMLInputElement ? weightControl.value : "1",
      }];
    });
    const subject = namedControl(form, "subject_id")?.selectedOptions?.item(0);
    const secondarySubject = namedControl(form, "subject_2_id")?.selectedOptions?.item(0);
    const checkpoint = namedControl(form, "checkpoint_id")?.selectedOptions?.item(0);
    const workflow = namedControl(form, "workflow_id")?.selectedOptions?.item(0);
    return {
      schema_version: 1,
      fields,
      loras,
      matches: {
        subject_name: subject?.dataset.subjectName || "",
        subject_slug: subject?.dataset.subjectSlug || "",
        secondary_subject_name: secondarySubject?.dataset.subjectName || "",
        secondary_subject_slug: secondarySubject?.dataset.subjectSlug || "",
        checkpoint_name: checkpoint?.dataset.checkpointName || "",
        checkpoint_sha256: checkpoint?.dataset.checkpointSha256 || "",
        workflow_name: workflow?.dataset.workflowName || "",
        workflow_version: workflow?.dataset.workflowVersion || "",
        workflow_sha256: workflow?.dataset.workflowSha256 || "",
      },
    };
  };

  const resolveLoraStack = (form, requested) => {
    if (!Array.isArray(requested)) return { missing: [], selections: [] };
    const options = Array.from(form.querySelectorAll("[data-lora-option]"));
    const seen = new Set();
    const missing = [];
    const selections = [];
    requested.slice(0, 8).forEach((item, index) => {
      if (!item || typeof item !== "object") {
        missing.push(`LoRA ${index + 1} has invalid saved data.`);
        return;
      }
      const approvalId = typeof item.approval_id === "string" ? item.approval_id : "";
      const sha256 = typeof item.sha256 === "string" ? item.sha256.toLowerCase() : "";
      const name = typeof item.name === "string" ? item.name : "";
      let option = sha256
        ? options.find(
          (candidate) => (candidate.dataset.loraSha256 || "").toLowerCase() === sha256,
        )
        : null;
      if (!option && approvalId) {
        option = options.find((candidate) => candidate.dataset.loraId === approvalId);
      }
      if (!option && name) {
        option = options.find((candidate) => candidate.dataset.loraName === name);
      }
      const id = option?.dataset.loraId || "";
      const weight = String(item.weight ?? "1");
      const numericWeight = Number(weight);
      const label = name || (sha256 ? sha256.slice(0, 12) : `LoRA ${index + 1}`);
      if (!id) {
        missing.push(`${label} is not currently available.`);
        return;
      }
      if (sha256 && (option.dataset.loraSha256 || "").toLowerCase() !== sha256) {
        missing.push(`${label} exact version is unavailable; the current approved version was substituted.`);
      }
      if (seen.has(id)) {
        missing.push(`${label} is duplicated in the saved stack.`);
        return;
      }
      if (!weight.trim() || !Number.isFinite(numericWeight)
          || numericWeight < -2 || numericWeight > 2) {
        missing.push(`${label} has an invalid saved weight.`);
        return;
      }
      seen.add(id);
      selections.push({ id, weight });
    });
    return { missing, selections };
  };

  const applyAutomationProfile = (form, profile) => {
    if (!profile || typeof profile !== "object") return { applied: false, missing: [] };
    form.dataset.applyingAutomationProfile = "true";
    const fields = profile.fields && typeof profile.fields === "object" ? profile.fields : {};
    const missing = [];
    const matchedSelects = new Set([
      "subject_id",
      "subject_2_id",
      "composition_mode",
      "checkpoint_id",
      "workflow_id",
    ]);
    AUTOMATION_PRESET_FIELDS.forEach((name) => {
      if (matchedSelects.has(name)) return;
      const value = fields[name];
      const control = namedControl(form, name);
      if (typeof value !== "string") return;
      if (!(control instanceof HTMLInputElement)
          && !(control instanceof HTMLTextAreaElement)
          && !(control instanceof HTMLSelectElement)) return;
      if (control instanceof HTMLSelectElement
          && !Array.from(control.options).some((option) => option.value === value)) {
        missing.push(`Saved ${name.replaceAll("_", " ")} value is unavailable.`);
        return;
      }
      control.value = value;
    });

    const matches = profile.matches && typeof profile.matches === "object" ? profile.matches : {};
    const matchRequiredSelect = (name, matchers, description) => {
      const control = namedControl(form, name);
      if (!(control instanceof HTMLSelectElement)) return;
      let option = null;
      matchers.some((matcher) => {
        option = Array.from(control.options).find(matcher) || null;
        return option !== null;
      });
      if (option) {
        control.value = option.value;
        return;
      }
      control.value = "";
      missing.push(`${description} is not currently approved.`);
    };
    const subjectName = typeof matches.subject_name === "string" ? matches.subject_name : "";
    const subjectSlug = typeof matches.subject_slug === "string" ? matches.subject_slug : "";
    const subjectId = typeof fields.subject_id === "string" ? fields.subject_id : "";
    if (subjectName || subjectSlug || subjectId) {
      matchRequiredSelect(
        "subject_id",
        [
          (option) => Boolean(subjectId && option.value === subjectId),
          (option) => Boolean(subjectSlug && option.dataset.subjectSlug === subjectSlug),
          (option) => Boolean(subjectName && option.dataset.subjectName === subjectName),
        ],
        subjectName ? `Subject ${subjectName}` : "Saved subject",
      );
    }
    const secondarySubjectName = typeof matches.secondary_subject_name === "string"
      ? matches.secondary_subject_name
      : "";
    const secondarySubjectSlug = typeof matches.secondary_subject_slug === "string"
      ? matches.secondary_subject_slug
      : "";
    const secondarySubjectId = typeof fields.subject_2_id === "string"
      ? fields.subject_2_id
      : "";
    if (secondarySubjectName || secondarySubjectSlug || secondarySubjectId) {
      matchRequiredSelect(
        "subject_2_id",
        [
          (option) => Boolean(secondarySubjectId && option.value === secondarySubjectId),
          (option) => Boolean(
            secondarySubjectSlug && option.dataset.subjectSlug === secondarySubjectSlug
          ),
          (option) => Boolean(
            secondarySubjectName && option.dataset.subjectName === secondarySubjectName
          ),
        ],
        secondarySubjectName ? `Subject ${secondarySubjectName}` : "Saved second subject",
      );
    }
    const compositionMode = fields.composition_mode === "duo" ? "duo" : "single";
    const compositionControl = form.querySelector(
      `[name="composition_mode"][value="${compositionMode}"]`,
    );
    if (compositionControl instanceof HTMLInputElement) {
      compositionControl.checked = true;
      compositionControl.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      const compositionSelect = namedControl(form, "composition_mode");
      if (compositionSelect instanceof HTMLSelectElement) {
        compositionSelect.value = compositionMode;
        compositionSelect.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    const checkpointSha256 = typeof matches.checkpoint_sha256 === "string"
      ? matches.checkpoint_sha256.toLowerCase()
      : "";
    const checkpointId = typeof fields.checkpoint_id === "string" ? fields.checkpoint_id : "";
    if (checkpointSha256 || checkpointId) {
      const checkpointName = typeof matches.checkpoint_name === "string"
        ? matches.checkpoint_name
        : "Saved checkpoint";
      matchRequiredSelect(
        "checkpoint_id",
        [
          (option) => Boolean(checkpointId && option.value === checkpointId),
          (option) => Boolean(checkpointSha256
            && (option.dataset.checkpointSha256 || "").toLowerCase() === checkpointSha256),
        ],
        `Checkpoint ${checkpointName}`,
      );
    }
    const workflowName = typeof matches.workflow_name === "string" ? matches.workflow_name : "";
    const workflowVersion = typeof matches.workflow_version === "string"
      ? matches.workflow_version
      : "";
    const workflowSha256 = typeof matches.workflow_sha256 === "string"
      ? matches.workflow_sha256.toLowerCase()
      : "";
    const workflowId = typeof fields.workflow_id === "string" ? fields.workflow_id : "";
    if (workflowName || workflowSha256 || workflowId) {
      matchRequiredSelect(
        "workflow_id",
        [
          (option) => Boolean(workflowId && option.value === workflowId),
          (option) => Boolean(workflowSha256
            && (option.dataset.workflowSha256 || "").toLowerCase() === workflowSha256),
          (option) => Boolean(workflowName && option.dataset.workflowName === workflowName
            && (!workflowVersion || option.dataset.workflowVersion === workflowVersion)),
        ],
        workflowName
          ? `Workflow ${workflowName}${workflowVersion ? ` v${workflowVersion}` : ""}`
          : "Saved workflow",
      );
    }

    if (typeof profile.batch_prompt === "string") {
      const prompt = namedControl(form, "prompt");
      if (prompt instanceof HTMLTextAreaElement) prompt.value = profile.batch_prompt;
    }
    if (typeof profile.seed === "string") {
      const seed = namedControl(form, "seed");
      if (seed instanceof HTMLInputElement) seed.value = profile.seed;
    }

    const loraResolution = resolveLoraStack(form, profile.loras);
    const loras = loraResolution.selections;
    missing.push(...loraResolution.missing);
    const slots = Array.from(form.querySelectorAll("[data-lora-native-slot]"));
    slots.forEach((slot, index) => {
      const idControl = slot.querySelector("[data-lora-native-id]");
      const weightControl = slot.querySelector("[data-lora-native-weight]");
      if (idControl instanceof HTMLSelectElement) idControl.value = loras[index]?.id || "";
      if (weightControl instanceof HTMLInputElement) weightControl.value = loras[index]?.weight || "";
    });
    window.dispatchEvent(new CustomEvent("gen-automation:apply-lora-stack", {
      detail: { selections: loras },
    }));
    form.querySelectorAll("input, textarea, select").forEach((control) => {
      control.dispatchEvent(new Event("input", { bubbles: true }));
      if (control instanceof HTMLSelectElement) {
        control.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    delete form.dataset.applyingAutomationProfile;
    return { applied: true, missing };
  };

  function initializeImageSettingsSummary() {
    const form = document.querySelector("[data-automation-form]");
    const summary = document.querySelector("[data-image-settings-summary]");
    if (!form || !summary) return;

    const watchedFields = new Set(["width", "height", "steps", "cfg"]);
    const valueFor = (name, fallback) => {
      const control = namedControl(form, name);
      if (!(control instanceof HTMLInputElement)
          && !(control instanceof HTMLSelectElement)) return fallback;
      return control.value.trim() || fallback;
    };
    const render = () => {
      const width = valueFor("width", "—");
      const height = valueFor("height", "—");
      const steps = valueFor("steps", "—");
      const cfg = valueFor("cfg", "—");
      summary.textContent = `${width} × ${height} · ${steps} steps · CFG ${cfg}`;
    };
    const renderForSettingsChange = (event) => {
      const control = event.target;
      if ((control instanceof HTMLInputElement || control instanceof HTMLSelectElement)
          && watchedFields.has(control.name)) render();
    };

    form.addEventListener("input", renderForSettingsChange);
    form.addEventListener("change", renderForSettingsChange);
    form.addEventListener("gen-automation:profile-changed", render);
    render();
  }

  function consumePendingImageProfile() {
    const form = document.querySelector("[data-automation-form]");
    if (!form) return false;
    try {
      const raw = window.sessionStorage.getItem(scopedStorageKey(PENDING_IMAGE_PROFILE_KEY));
      if (!raw) return false;
      window.sessionStorage.removeItem(scopedStorageKey(PENDING_IMAGE_PROFILE_KEY));
      const payload = JSON.parse(raw);
      const result = applyAutomationProfile(form, payload);
      if (result.applied) {
        form.dataset.importedImageSettings = "true";
        form.dataset.profileImportWarning = result.missing.join(" ");
        return true;
      }
    } catch (_error) {
      try {
        window.sessionStorage.removeItem(scopedStorageKey(PENDING_IMAGE_PROFILE_KEY));
      } catch (_storageError) {
        // Reusing settings is optional when session storage is unavailable.
      }
    }
    return false;
  }

  const readStoredAutomationDraft = () => {
    try {
      const raw = window.localStorage.getItem(scopedStorageKey(AUTOMATION_DRAFT_STORAGE_KEY));
      if (!raw) return null;
      const draft = JSON.parse(raw);
      if (!draft || draft.schema_version !== 1 || !Array.isArray(draft.batch_plan)) return null;
      return draft;
    } catch (_error) {
      return null;
    }
  };

  function restoreAutomationDraft() {
    const form = document.querySelector("[data-automation-form]");
    const planData = document.querySelector("#batch-plan-data");
    if (!form || !(planData instanceof HTMLTextAreaElement)) return false;
    if (form.dataset.serverError === "true") {
      try {
        window.sessionStorage.removeItem(scopedStorageKey(AUTOMATION_DRAFT_SUBMISSION_KEY));
      } catch (_error) {
        // Storage availability must not replace the server-returned form values.
      }
      return false;
    }
    const draft = readStoredAutomationDraft();
    if (!draft) return false;
    const setTextControl = (name, value) => {
      const control = namedControl(form, name);
      if (typeof value !== "string") return;
      if (control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement) {
        control.value = value;
      }
    };
    setTextControl("title", draft.title);
    setTextControl("slug", draft.slug);
    setTextControl("seed", draft.seed);
    setTextControl("desired_accepted_count", draft.desired_accepted_count);
    const result = applyAutomationProfile(form, draft.profile);
    planData.value = JSON.stringify(draft.batch_plan.slice(0, 50));
    form.dataset.automationDraftRestored = "true";
    if (typeof draft.target_follows_queue === "boolean") {
      form.dataset.restoredTargetFollowsQueue = String(draft.target_follows_queue);
    }
    form.dataset.profileImportWarning = result.missing.join(" ");
    return true;
  }

  function initializeLoraPicker() {
    const form = document.querySelector("[data-automation-form]");
    const picker = document.querySelector("[data-lora-picker]");
    const nativeContainer = document.querySelector("[data-lora-native-slots]");
    const selectedList = document.querySelector("[data-lora-selected]");
    const selectionTemplate = document.querySelector("#lora-selection-template");
    if (!form || !picker || !nativeContainer || !selectedList || !selectionTemplate) return;

    const nativeSlots = Array.from(nativeContainer.querySelectorAll("[data-lora-native-slot]"));
    if (nativeSlots.length === 0) return;

    const catalog = picker.querySelector("[data-lora-catalog]");
    const catalogOptions = Array.from(picker.querySelectorAll("[data-lora-option]"));
    const searchInput = picker.querySelector("[data-lora-search]");
    const catalogEmpty = picker.querySelector("[data-lora-catalog-empty]");
    const stackEmpty = picker.querySelector("[data-lora-stack-empty]");
    const selectionCount = picker.querySelector("[data-lora-selection-count]");
    const summary = document.querySelector("[data-lora-summary]");
    const feedback = picker.querySelector("[data-lora-feedback]");
    const clearButton = picker.querySelector("[data-lora-clear]");
    const maximum = Math.min(
      nativeSlots.length,
      Math.max(1, integerValue(picker.dataset.maxSelections, nativeSlots.length)),
    );
    const optionById = new Map(
      catalogOptions.map((button) => [button.dataset.loraId, button]),
    );
    let selections = [];
    let draggedId = null;

    const setFeedback = (message, tone = "") => {
      if (!feedback) return;
      feedback.textContent = message;
      feedback.className = `lora-feedback${tone ? ` ${tone}` : ""}`;
    };

    const validWeight = (value) => {
      const parsed = Number(value);
      return String(value).trim() !== ""
        && Number.isFinite(parsed)
        && parsed >= -2
        && parsed <= 2;
    };

    const syncCanonicalSlots = () => {
      nativeSlots.forEach((slot, index) => {
        const idControl = slot.querySelector("[data-lora-native-id]");
        const weightControl = slot.querySelector("[data-lora-native-weight]");
        const selection = selections[index];
        if (idControl) idControl.value = selection ? selection.id : "";
        if (weightControl) weightControl.value = selection ? selection.weight : "";
      });
    };

    const selectedName = (id) => {
      const option = optionById.get(id);
      return option ? option.dataset.loraName || "Selected LoRA" : "Selected LoRA";
    };

    const updateCatalogFilter = () => {
      const query = searchInput ? searchInput.value.trim().toLowerCase() : "";
      let visible = 0;
      catalogOptions.forEach((button) => {
        const haystack = `${button.dataset.loraName || ""} ${button.dataset.loraSha256 || ""}`
          .toLowerCase();
        const listItem = button.closest("li");
        const matches = !query || haystack.includes(query);
        if (listItem) listItem.hidden = !matches;
        if (matches) visible += 1;
      });
      if (catalogEmpty) catalogEmpty.hidden = visible !== 0;
    };

    const focusSelectionAction = (id, action) => {
      window.requestAnimationFrame(() => {
        const row = Array.from(selectedList.querySelectorAll("[data-lora-selection]"))
          .find((item) => item.dataset.loraId === id);
        const button = row && row.querySelector(`[data-lora-selection-action="${action}"]`);
        if (button) button.focus();
      });
    };

    const renderSelections = () => {
      selectedList.replaceChildren();
      selections.forEach((selection, index) => {
        const fragment = selectionTemplate.content.cloneNode(true);
        const row = fragment.querySelector("[data-lora-selection]");
        const option = optionById.get(selection.id);
        row.dataset.loraId = selection.id;
        row.querySelector("[data-lora-position]").textContent = String(index + 1);
        row.querySelector("[data-lora-selected-name]").textContent = selectedName(selection.id);
        row.querySelector("[data-lora-selected-sha256]").textContent = option
          ? (option.dataset.loraSha256 || "").slice(0, 12)
          : "";
        const weightInput = row.querySelector("[data-lora-visible-weight]");
        weightInput.value = selection.weight;
        row.querySelector('[data-lora-selection-action="up"]').disabled = index === 0;
        row.querySelector('[data-lora-selection-action="down"]').disabled = (
          index === selections.length - 1
        );
        selectedList.append(fragment);
      });

      const selectedIds = new Set(selections.map((item) => item.id));
      catalogOptions.forEach((button) => {
        const selected = selectedIds.has(button.dataset.loraId);
        button.setAttribute("aria-pressed", String(selected));
        button.classList.toggle("selected", selected);
        const action = button.querySelector("[data-lora-option-action]");
        if (action) action.textContent = selected ? "Remove" : "Add";
      });

      if (selectionCount) {
        selectionCount.textContent = `${selections.length} of ${maximum} selected`;
      }
      if (summary) {
        summary.textContent = selections.length === 0
          ? `Optional · up to ${maximum}`
          : `${selections.length} selected · up to ${maximum}`;
      }
      if (stackEmpty) stackEmpty.hidden = selections.length !== 0;
      if (clearButton) clearButton.disabled = selections.length === 0;
      syncCanonicalSlots();
      updateCatalogFilter();
      form.dispatchEvent(new CustomEvent("gen-automation:profile-changed"));
    };

    const removeSelection = (id, announce = true) => {
      const index = selections.findIndex((item) => item.id === id);
      if (index < 0) return;
      const name = selectedName(id);
      selections.splice(index, 1);
      renderSelections();
      if (announce) setFeedback(`${name} removed from the stack.`);
    };

    const addSelection = (id) => {
      if (!optionById.has(id)) return;
      if (selections.some((item) => item.id === id)) {
        removeSelection(id);
        return;
      }
      if (selections.length >= maximum) {
        setFeedback(
          `This stack already has ${maximum} LoRAs. Remove one before adding another.`,
          "warning",
        );
        return;
      }
      selections.push({ id, weight: "1" });
      renderSelections();
      setFeedback(`${selectedName(id)} added. Set its weight in the selected stack.`, "success");
    };

    nativeSlots.forEach((slot) => {
      const idControl = slot.querySelector("[data-lora-native-id]");
      const weightControl = slot.querySelector("[data-lora-native-weight]");
      const id = idControl ? idControl.value : "";
      if (!id || !optionById.has(id) || selections.some((item) => item.id === id)) return;
      selections.push({
        id,
        weight: weightControl && weightControl.value.trim() ? weightControl.value : "1",
      });
    });

    nativeContainer.hidden = true;
    picker.hidden = false;
    renderSelections();

    if (searchInput) searchInput.addEventListener("input", updateCatalogFilter);

    if (catalog) {
      catalog.addEventListener("click", (event) => {
        const button = event.target.closest("[data-lora-option]");
        if (button) addSelection(button.dataset.loraId);
      });
    }

    if (clearButton) {
      clearButton.addEventListener("click", () => {
        if (selections.length === 0) return;
        selections = [];
        renderSelections();
        setFeedback("LoRA stack cleared.");
      });
    }

    selectedList.addEventListener("input", (event) => {
      if (!event.target.matches("[data-lora-visible-weight]")) return;
      const row = event.target.closest("[data-lora-selection]");
      const selection = selections.find((item) => item.id === row.dataset.loraId);
      if (!selection) return;
      selection.weight = event.target.value;
      event.target.setCustomValidity(
        validWeight(selection.weight) ? "" : "Enter a LoRA weight between -2 and 2.",
      );
      syncCanonicalSlots();
    });

    selectedList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-lora-selection-action]");
      if (!button) return;
      const row = button.closest("[data-lora-selection]");
      const id = row.dataset.loraId;
      const index = selections.findIndex((item) => item.id === id);
      if (index < 0) return;
      const action = button.dataset.loraSelectionAction;
      if (action === "remove") {
        removeSelection(id);
        const catalogButton = optionById.get(id);
        if (catalogButton) catalogButton.focus();
        return;
      }
      const nextIndex = action === "up" ? index - 1 : index + 1;
      if (nextIndex < 0 || nextIndex >= selections.length) return;
      const [selection] = selections.splice(index, 1);
      selections.splice(nextIndex, 0, selection);
      renderSelections();
      setFeedback(`${selectedName(id)} moved to position ${nextIndex + 1}.`);
      focusSelectionAction(id, action);
    });

    selectedList.addEventListener("dragstart", (event) => {
      const handle = event.target.closest(".lora-drag-handle");
      const row = handle && handle.closest("[data-lora-selection]");
      if (!row) {
        event.preventDefault();
        return;
      }
      draggedId = row.dataset.loraId;
      row.classList.add("dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", draggedId);
      }
    });

    selectedList.addEventListener("dragover", (event) => {
      if (!draggedId) return;
      const target = event.target.closest("[data-lora-selection]");
      if (!target || target.dataset.loraId === draggedId) return;
      event.preventDefault();
      selectedList.querySelectorAll(".drag-before, .drag-after").forEach((item) => {
        item.classList.remove("drag-before", "drag-after");
      });
      const after = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
      target.classList.add(after ? "drag-after" : "drag-before");
    });

    selectedList.addEventListener("drop", (event) => {
      const target = event.target.closest("[data-lora-selection]");
      if (!draggedId || !target || target.dataset.loraId === draggedId) return;
      event.preventDefault();
      const sourceIndex = selections.findIndex((item) => item.id === draggedId);
      const targetId = target.dataset.loraId;
      const after = target.classList.contains("drag-after");
      const [selection] = selections.splice(sourceIndex, 1);
      let targetIndex = selections.findIndex((item) => item.id === targetId);
      if (after) targetIndex += 1;
      selections.splice(targetIndex, 0, selection);
      const movedId = draggedId;
      draggedId = null;
      renderSelections();
      setFeedback(`${selectedName(movedId)} moved to position ${targetIndex + 1}.`);
    });

    selectedList.addEventListener("dragend", () => {
      draggedId = null;
      selectedList.querySelectorAll(".dragging, .drag-before, .drag-after").forEach((item) => {
        item.classList.remove("dragging", "drag-before", "drag-after");
      });
    });

    window.addEventListener("gen-automation:apply-lora-stack", (event) => {
      const requested = event.detail?.selections;
      if (!Array.isArray(requested)) return;
      const seen = new Set();
      selections = requested.slice(0, maximum).flatMap((item) => {
        const id = item && typeof item.id === "string" ? item.id : "";
        const weight = String(item?.weight ?? "1");
        if (!optionById.has(id) || seen.has(id) || !validWeight(weight)) return [];
        seen.add(id);
        return [{ id, weight }];
      });
      renderSelections();
      setFeedback("Generation preset LoRA stack applied.", "success");
    });

    form.addEventListener("submit", syncCanonicalSlots, { capture: true });
  }

  function initializeAutomationBuilder() {
    const form = document.querySelector("[data-automation-form]");
    const builder = document.querySelector("#batch-builder");
    const list = document.querySelector("#batch-list");
    const template = document.querySelector("#batch-row-template");
    const planData = document.querySelector("#batch-plan-data");
    if (!form || !builder || !list || !template || !planData) return;

    const addButtons = Array.from(form.querySelectorAll("[data-add-batch]"));
    const collapseAllButton = form.querySelector("[data-collapse-batches]");
    const outputsPerJob = form.querySelector("[data-outputs-per-job]");
    const plannedJobCount = form.querySelector("[data-planned-job-count]");
    const desiredCount = form.querySelector("[data-desired-count]");
    const matchQueueTarget = form.querySelector("[data-match-queue-target]");
    const randomEachSeedButton = form.querySelector("[data-random-each-seed]");
    const randomSeedButton = form.querySelector("[data-random-seed]");
    const batchSequenceInput = form.querySelector("[data-batch-sequence-input]");
    const batchSequenceApply = form.querySelector("[data-batch-sequence-apply]");
    const batchSequenceStatus = form.querySelector("[data-batch-sequence-status]");
    const defaultPrompt = form.querySelector("[data-default-prompt]");
    const defaultNegative = form.querySelector("[data-default-negative]");
    const defaultDetailer = form.querySelector("[data-default-detailer]");
    const defaultDetailerNegative = form.querySelector("[data-default-detailer-negative]");
    const defaultSeed = form.querySelector("[data-default-seed]");
    const detailerGuideSize = namedControl(form, "detailer_guide_size");
    const detailerMaxSize = namedControl(form, "detailer_max_size");
    const titleInput = form.querySelector("[data-title-input]");
    const slugInput = form.querySelector("[data-slug-input]");
    const submitButtons = Array.from(form.querySelectorAll(".queue-submit"));
    const serverDisabled = submitButtons.some((button) => button.disabled);
    const maximumProviderJobs = 10_000;
    let lastPrompt = null;
    let slugWasEdited = Boolean(slugInput && slugInput.value.trim());
    let previousDefaultPrompt = defaultPrompt ? defaultPrompt.value : "";
    let previousDefaultNegative = defaultNegative ? defaultNegative.value : "";
    const defaultFinalSetCap = 250;
    const restoredTargetFollowsQueue = typeof form.dataset.restoredTargetFollowsQueue === "string";
    let targetFollowsQueue = restoredTargetFollowsQueue
      ? form.dataset.restoredTargetFollowsQueue === "true"
      : false;
    let targetUsesSmartDefault = !restoredTargetFollowsQueue
      && Boolean(desiredCount && !planData.value.trim());
    delete form.dataset.restoredTargetFollowsQueue;

    builder.hidden = false;
    document.documentElement.classList.add("dashboard-enhanced");
    if (defaultPrompt) defaultPrompt.required = false;

    const field = (row, name) => row.querySelector(`[data-batch-field="${name}"]`);

    const readRow = (row) => {
      const seedInput = field(row, "seed");
      return {
        name: field(row, "name").value.trim(),
        image_count: integerValue(field(row, "image_count").value, 0),
        prompt: field(row, "prompt").value,
        // An empty negative prompt explicitly disables the shared negative prompt.
        negative_prompt: field(row, "negative_prompt").value,
        detailer_prompt: optionalText(field(row, "detailer_prompt").value),
        detailer_negative_prompt: optionalText(field(row, "detailer_negative_prompt").value),
        // Keep the decimal text exact; valid backend seeds extend beyond JS's safe integer range.
        seed: seedInput.value.trim() === "" ? null : seedInput.value.trim(),
      };
    };

    const batchRows = () => Array.from(list.querySelectorAll("[data-batch-row]"));
    const wildcardPattern = /__([a-z0-9]+(?:[._/-][a-z0-9]+)*)__/g;
    const knownWildcards = new Set(
      Array.from(template.content.querySelectorAll("[data-batch-wildcard] option")).flatMap(
        (option) => {
          const match = /^__([a-z0-9]+(?:[._/-][a-z0-9]+)*)__$/.exec(option.value);
          return match ? [match[1]] : [];
        },
      ),
    );
    const firstWildcardPattern = /__([a-z0-9]+(?:[._/-][a-z0-9]+)*)__/;

    const promptForSequenceWildcard = (wildcard) => {
      const token = `__${wildcard}__`;
      const startingPrompt = defaultPrompt ? defaultPrompt.value : "";
      if (firstWildcardPattern.test(startingPrompt)) {
        return startingPrompt.replace(firstWildcardPattern, token);
      }
      return [startingPrompt.trim(), token].filter(Boolean).join(", ");
    };

    const parseBatchSequence = () => {
      if (!(batchSequenceInput instanceof HTMLTextAreaElement)) return [];
      const lines = batchSequenceInput.value
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
      if (lines.length === 0) throw new Error("Enter at least one wildcard batch.");
      if (lines.length > 50) throw new Error("A queue can contain at most 50 batches.");

      const labelCounts = new Map();
      return lines.map((line, index) => {
        const parts = line.split(/\s+/);
        if (parts.length !== 2) {
          throw new Error(`Line ${index + 1} must use: image-count wildcard-name.`);
        }
        const imageCount = /^[1-9][0-9]*$/.test(parts[0])
          ? integerValue(parts[0], 0)
          : 0;
        const wildcard = parts[1];
        if (imageCount < 1 || imageCount > 80_000) {
          throw new Error(`Line ${index + 1} image count must be between 1 and 80,000.`);
        }
        if (!knownWildcards.has(wildcard)) {
          throw new Error(`Line ${index + 1} uses unknown wildcard __${wildcard}__.`);
        }
        const occurrence = (labelCounts.get(wildcard) || 0) + 1;
        labelCounts.set(wildcard, occurrence);
        return {
          name: occurrence === 1 ? wildcard : `${wildcard} ${occurrence}`,
          image_count: imageCount,
          prompt: promptForSequenceWildcard(wildcard),
          negative_prompt: defaultNegative ? defaultNegative.value : "",
          detailer_prompt: null,
          detailer_negative_prompt: null,
          seed: null,
        };
      });
    };

    const insertToken = (target, token) => {
      const start = target.selectionStart ?? target.value.length;
      const end = target.selectionEnd ?? start;
      const prefix = start > 0 && !/[\s,]$/.test(target.value.slice(0, start)) ? ", " : "";
      const suffix = end < target.value.length && !/^[\s,]/.test(target.value.slice(end))
        ? ", "
        : "";
      target.setRangeText(`${prefix}${token}${suffix}`, start, end, "end");
      target.focus();
      target.dispatchEvent(new Event("input", { bubbles: true }));
    };

    const nextUniqueName = (preferred, ignoredRow = null) => {
      const used = new Set(
        batchRows()
          .filter((row) => row !== ignoredRow)
          .map((row) => field(row, "name").value.trim().toLowerCase())
          .filter(Boolean),
      );
      const base = preferred.trim() || "Batch";
      if (!used.has(base.toLowerCase())) return base;
      let suffix = 2;
      while (used.has(`${base} ${suffix}`.toLowerCase())) suffix += 1;
      return `${base} ${suffix}`;
    };

    const newBatchDefaults = () => ({
      name: nextUniqueName(`Batch ${batchRows().length + 1}`),
      image_count: Math.max(1, integerValue(outputsPerJob && outputsPerJob.value, 4)),
      prompt: defaultPrompt ? defaultPrompt.value : "",
      negative_prompt: defaultNegative ? defaultNegative.value : "",
      detailer_prompt: null,
      detailer_negative_prompt: null,
      seed: null,
    });

    const addBatch = (batch, before = null, deferUpdate = false) => {
      const fragment = template.content.cloneNode(true);
      const row = fragment.querySelector("[data-batch-row]");
      field(row, "name").value = batch.name || nextUniqueName("Batch");
      field(row, "image_count").value = Math.max(1, integerValue(batch.image_count, 1));
      field(row, "prompt").value = batch.prompt || "";
      field(row, "negative_prompt").value = batch.negative_prompt
        ?? (defaultNegative ? defaultNegative.value : "");
      field(row, "detailer_prompt").value = batch.detailer_prompt ?? "";
      field(row, "detailer_negative_prompt").value = batch.detailer_negative_prompt ?? "";
      field(row, "seed").value = batch.seed ?? "";
      list.insertBefore(fragment, before);
      if (!deferUpdate) updateBuilder();
      return row;
    };

    const replaceBatchPlan = (requested) => {
      const batches = normalizeAutomationPresetBatchPlan(requested);
      if (!batches) return false;
      list.replaceChildren();
      lastPrompt = null;
      batches.forEach((batch, index) => {
        const row = addBatch(batch, null, true);
        const collapsed = index > 0;
        row.classList.toggle("is-collapsed", collapsed);
        const collapseButton = row.querySelector('[data-batch-action="collapse"]');
        if (collapseButton) {
          collapseButton.setAttribute("aria-expanded", String(!collapsed));
          collapseButton.textContent = collapsed ? "Expand" : "Collapse";
        }
      });
      if (collapseAllButton instanceof HTMLButtonElement) {
        collapseAllButton.textContent = "Collapse all";
      }
      updateBuilder();
      return true;
    };

    const unknownWildcardNames = (value) => Array.from(
      new Set(
        Array.from(String(value).matchAll(wildcardPattern), (match) => match[1])
          .filter((name) => !knownWildcards.has(name)),
      ),
    );

    const applyWildcardValidity = (control) => {
      if (!(control instanceof HTMLInputElement)
          && !(control instanceof HTMLTextAreaElement)) return false;
      const unknownWildcards = unknownWildcardNames(control.value);
      control.setCustomValidity(
        unknownWildcards.length > 0
          ? `Unknown wildcard: ${unknownWildcards.map((name) => `__${name}__`).join(", ")}.`
          : "",
      );
      return unknownWildcards.length > 0;
    };

    const setBatchValidity = () => {
      const rows = batchRows();
      const names = new Map();
      let hasUnknownWildcard = false;
      [defaultPrompt, defaultNegative, defaultDetailer, defaultDetailerNegative].forEach((control) => {
        hasUnknownWildcard = applyWildcardValidity(control) || hasUnknownWildcard;
      });
      rows.forEach((row) => {
        const nameInput = field(row, "name");
        const key = nameInput.value.trim().toLowerCase();
        const duplicate = key && names.has(key);
        nameInput.setCustomValidity(duplicate ? "Batch labels must be unique." : "");
        if (key && !duplicate) names.set(key, row);
        ["prompt", "negative_prompt", "detailer_prompt", "detailer_negative_prompt"]
          .forEach((name) => {
            hasUnknownWildcard = applyWildcardValidity(field(row, name)) || hasUnknownWildcard;
          });
      });
      const totalImages = rows.reduce(
        (total, row) => total + Math.max(0, integerValue(field(row, "image_count").value)),
        0,
      );
      if (desiredCount) {
        const target = integerValue(desiredCount.value);
        desiredCount.setCustomValidity(
          target > totalImages ? "The final-set target cannot exceed generated images." : "",
        );
      }
      let invalidDetailerSize = false;
      if (detailerGuideSize instanceof HTMLInputElement
          && detailerMaxSize instanceof HTMLInputElement) {
        const guideSize = integerValue(detailerGuideSize.value);
        const maxSize = integerValue(detailerMaxSize.value);
        invalidDetailerSize = guideSize > 0 && maxSize > 0 && maxSize < guideSize;
        detailerMaxSize.setCustomValidity(
          invalidDetailerSize
            ? "Face maximum size must be at least the face guide size."
            : "",
        );
      }
      return { hasUnknownWildcard, invalidDetailerSize };
    };

    const updateBuilder = () => {
      if (form.dataset.applyingAutomationProfile === "true") return;
      const rows = batchRows();
      const perJob = Math.max(1, integerValue(outputsPerJob && outputsPerJob.value, 1));
      let totalImages = 0;
      let totalJobs = 0;
      rows.forEach((row, index) => {
        const imageCount = Math.max(0, integerValue(field(row, "image_count").value));
        const name = field(row, "name").value.trim() || `Batch ${index + 1}`;
        totalImages += imageCount;
        totalJobs += Math.ceil(imageCount / perJob);
        row.querySelector("[data-batch-number]").textContent = `Batch ${index + 1}`;
        row.querySelector("[data-batch-heading]").textContent = name;
        const wildcardNames = ["prompt", "negative_prompt"].flatMap((promptField) =>
          Array.from(
            field(row, promptField).value.matchAll(wildcardPattern),
            (match) => match[1],
          ));
        const uniqueWildcards = Array.from(new Set(wildcardNames));
        const meta = row.querySelector("[data-batch-meta]");
        const wildcardSummary = row.querySelector("[data-batch-wildcard-summary]");
        const jobs = Math.ceil(imageCount / perJob);
        if (meta) {
          meta.textContent = `${imageCount.toLocaleString()} images · ${jobs.toLocaleString()} GPU job${jobs === 1 ? "" : "s"} · ${uniqueWildcards.length} wildcard${uniqueWildcards.length === 1 ? "" : "s"}`;
        }
        if (wildcardSummary) {
          wildcardSummary.replaceChildren(
            ...uniqueWildcards.map((wildcard) => {
              const chip = document.createElement("span");
              const known = knownWildcards.has(wildcard);
              chip.className = `batch-wildcard-chip${known ? "" : " invalid"}`;
              chip.textContent = `__${wildcard}__`;
              if (!known) chip.title = "This wildcard library does not exist.";
              return chip;
            }),
          );
          wildcardSummary.hidden = uniqueWildcards.length === 0;
        }
        row.querySelector('[data-batch-action="up"]').disabled = index === 0;
        row.querySelector('[data-batch-action="down"]').disabled = index === rows.length - 1;
        row.querySelector('[data-batch-action="remove"]').disabled = rows.length === 1;
      });
      if (plannedJobCount) plannedJobCount.value = Math.max(1, totalJobs);
      if (desiredCount && (targetFollowsQueue || targetUsesSmartDefault)
          && form.dataset.applyingAutomationProfile !== "true") {
        const maximumAccepted = Math.max(1, integerValue(desiredCount.max, 500));
        const automaticMaximum = targetFollowsQueue
          ? maximumAccepted
          : Math.min(maximumAccepted, defaultFinalSetCap);
        desiredCount.value = String(Math.max(1, Math.min(automaticMaximum, totalImages)));
      }
      form.dataset.targetFollowsQueue = String(targetFollowsQueue);
      addButtons.forEach((button) => { button.disabled = rows.length >= 50; });
      planData.value = JSON.stringify(rows.map(readRow));

      const batchesSummary = document.querySelector("#summary-batches");
      const imagesSummary = document.querySelector("#summary-images");
      const jobsSummary = document.querySelector("#summary-jobs");
      const targetSummary = document.querySelector("#summary-target");
      const queueSummaries = document.querySelectorAll("[data-queue-summary]");
      const mobileImageSummaries = document.querySelectorAll("[data-mobile-summary-images]");
      const mobileJobSummaries = document.querySelectorAll("[data-mobile-summary-jobs]");
      const mobileTargetSummaries = document.querySelectorAll("[data-mobile-summary-target]");
      const note = document.querySelector("#summary-note");
      if (batchesSummary) batchesSummary.textContent = String(rows.length);
      if (imagesSummary) imagesSummary.textContent = totalImages.toLocaleString();
      if (jobsSummary) jobsSummary.textContent = totalJobs.toLocaleString();
      if (targetSummary) targetSummary.textContent = String(integerValue(desiredCount && desiredCount.value));
      queueSummaries.forEach((item) => {
        item.textContent = `${rows.length.toLocaleString()} batch${rows.length === 1 ? "" : "es"} · ${totalImages.toLocaleString()} images`;
      });
      mobileImageSummaries.forEach((item) => { item.textContent = totalImages.toLocaleString(); });
      mobileJobSummaries.forEach((item) => { item.textContent = totalJobs.toLocaleString(); });
      mobileTargetSummaries.forEach((item) => {
        item.textContent = String(integerValue(desiredCount && desiredCount.value));
      });

      const validity = setBatchValidity();
      const unknownWildcard = validity.hasUnknownWildcard;
      const missingPrompt = rows.some((row) => !field(row, "prompt").value.trim());
      const tooManyJobs = totalJobs > maximumProviderJobs;
      const targetTooLarge = desiredCount && integerValue(desiredCount.value) > totalImages;
      if (note) {
        if (missingPrompt) {
          note.textContent = "Every batch needs a prompt structure before this run can start.";
          note.className = "summary-note warning";
        } else if (unknownWildcard) {
          note.textContent = "Fix the unknown wildcard highlighted in the batch queue.";
          note.className = "summary-note warning";
        } else if (tooManyJobs) {
          note.textContent = `Reduce the queue to ${maximumProviderJobs.toLocaleString()} GPU jobs or fewer.`;
          note.className = "summary-note warning";
        } else if (targetTooLarge) {
          note.textContent = "Reduce the final-set target or generate more images.";
          note.className = "summary-note warning";
        } else if (validity.invalidDetailerSize) {
          note.textContent = "Face maximum size must be at least the guide size.";
          note.className = "summary-note warning";
        } else {
          note.textContent = `${totalImages.toLocaleString()} images will run as ${totalJobs.toLocaleString()} efficient GPU jobs.`;
          note.className = "summary-note ready";
        }
      }
      submitButtons.forEach((button) => {
        button.disabled = serverDisabled
          || missingPrompt
          || unknownWildcard
          || tooManyJobs
          || Boolean(targetTooLarge)
          || validity.invalidDetailerSize
          || totalImages < 1;
      });
      form.dispatchEvent(new CustomEvent("gen-automation:builder-updated"));
    };

    let initialBatches = [];
    if (planData.value.trim()) {
      try {
        const parsed = JSON.parse(planData.value);
        if (Array.isArray(parsed)) initialBatches = parsed.slice(0, 50);
      } catch (_error) {
        initialBatches = [];
      }
    }
    if (initialBatches.length === 0) {
      const perJob = Math.max(1, integerValue(outputsPerJob && outputsPerJob.value, 1));
      const jobs = Math.max(1, integerValue(plannedJobCount && plannedJobCount.value, 1));
      initialBatches = [{
        name: "Batch 1",
        image_count: perJob * jobs,
        prompt: defaultPrompt ? defaultPrompt.value : "",
        negative_prompt: defaultNegative ? defaultNegative.value : "",
        detailer_prompt: null,
        detailer_negative_prompt: null,
        seed: null,
      }];
    }
    initialBatches.forEach((batch, index) => {
      const row = addBatch(batch, null, true);
      if (initialBatches.length > 1 && index > 0) {
        row.classList.add("is-collapsed");
        const collapseButton = row.querySelector('[data-batch-action="collapse"]');
        if (collapseButton) {
          collapseButton.setAttribute("aria-expanded", "false");
          collapseButton.textContent = "Expand";
        }
      }
    });

    form.addEventListener("gen-automation:replace-batch-plan", (event) => {
      if (!(event instanceof CustomEvent)
          || !event.detail || typeof event.detail !== "object") return;
      event.detail.replaced = replaceBatchPlan(event.detail.batch_plan);
    });
    form.addEventListener("gen-automation:refresh-batch-plan", updateBuilder);

    if (batchSequenceApply instanceof HTMLButtonElement
        && batchSequenceInput instanceof HTMLTextAreaElement) {
      batchSequenceApply.addEventListener("click", () => {
        try {
          const batches = parseBatchSequence();
          list.replaceChildren();
          lastPrompt = null;
          batches.forEach((batch) => addBatch(batch));
          batchRows().forEach((row) => {
            row.classList.add("is-collapsed");
            const collapseButton = row.querySelector('[data-batch-action="collapse"]');
            if (collapseButton) {
              collapseButton.setAttribute("aria-expanded", "false");
              collapseButton.textContent = "Expand";
            }
          });
          if (collapseAllButton instanceof HTMLButtonElement) {
            collapseAllButton.textContent = "Expand all";
          }
          updateBuilder();
          if (batchSequenceStatus instanceof HTMLOutputElement) {
            const total = batches.reduce((sum, batch) => sum + batch.image_count, 0);
            batchSequenceStatus.classList.remove("error");
            batchSequenceStatus.textContent = `${batches.length} batches and ${total.toLocaleString()} images queued in this order.`;
          }
        } catch (error) {
          if (batchSequenceStatus instanceof HTMLOutputElement) {
            batchSequenceStatus.classList.add("error");
            batchSequenceStatus.textContent = error instanceof Error
              ? error.message
              : "The wildcard batch list is invalid.";
          }
          batchSequenceInput.focus();
        }
      });
    }

    addButtons.forEach((button) => button.addEventListener("click", () => {
      const row = addBatch(newBatchDefaults());
      field(row, "name").focus();
      row.scrollIntoView({ behavior: "smooth", block: "center" });
    }));

    if (collapseAllButton instanceof HTMLButtonElement) {
      collapseAllButton.addEventListener("click", () => {
        const rows = batchRows();
        const collapse = rows.some((row) => !row.classList.contains("is-collapsed"));
        rows.forEach((row) => {
          row.classList.toggle("is-collapsed", collapse);
          const button = row.querySelector('[data-batch-action="collapse"]');
          if (button) {
            button.setAttribute("aria-expanded", String(!collapse));
            button.textContent = collapse ? "Expand" : "Collapse";
          }
        });
        collapseAllButton.textContent = collapse ? "Expand all" : "Collapse all";
      });
    }

    list.addEventListener("change", (event) => {
      if (!event.target.matches("[data-batch-wildcard]")) return;
      const token = event.target.value;
      if (!token) return;
      const row = event.target.closest("[data-batch-row]");
      const targetName = event.target.dataset.batchWildcardTarget || "prompt";
      insertToken(field(row, targetName), token);
      event.target.value = "";
    });

    list.addEventListener("focusin", (event) => {
      if (event.target.matches(
        '[data-batch-field="prompt"], [data-batch-field="negative_prompt"]',
      )) lastPrompt = event.target;
    });

    list.addEventListener("input", updateBuilder);
    list.addEventListener("click", (event) => {
      const button = event.target.closest("[data-batch-action]");
      if (!button) return;
      const row = button.closest("[data-batch-row]");
      const action = button.dataset.batchAction;
      if (action === "up" && row.previousElementSibling) {
        list.insertBefore(row, row.previousElementSibling);
      } else if (action === "down" && row.nextElementSibling) {
        list.insertBefore(row.nextElementSibling, row);
      } else if (action === "remove" && batchRows().length > 1) {
        const replacement = row.nextElementSibling || row.previousElementSibling;
        const removedLastPrompt = Boolean(lastPrompt && row.contains(lastPrompt));
        row.remove();
        if (removedLastPrompt) lastPrompt = replacement ? field(replacement, "prompt") : null;
      } else if (action === "duplicate") {
        const copy = readRow(row);
        copy.name = nextUniqueName(`${copy.name || "Batch"} copy`);
        const duplicate = addBatch(copy, row.nextElementSibling);
        field(duplicate, "name").focus();
      } else if (action === "collapse") {
        const collapsed = row.classList.toggle("is-collapsed");
        button.setAttribute("aria-expanded", String(!collapsed));
        button.textContent = collapsed ? "Expand" : "Collapse";
      }
      updateBuilder();
    });

    form.querySelectorAll("[data-wildcard-token]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = lastPrompt || field(batchRows()[0], "prompt");
        insertToken(target, button.dataset.wildcardToken);
      });
    });

    outputsPerJob && outputsPerJob.addEventListener("input", updateBuilder);
    [defaultDetailer, defaultDetailerNegative].forEach((control) => {
      if (control instanceof HTMLTextAreaElement) control.addEventListener("input", updateBuilder);
    });
    [detailerGuideSize, detailerMaxSize].forEach((control) => {
      if (control instanceof HTMLInputElement) control.addEventListener("input", updateBuilder);
    });
    desiredCount && desiredCount.addEventListener("input", () => {
      targetFollowsQueue = false;
      targetUsesSmartDefault = false;
      updateBuilder();
    });
    if (matchQueueTarget instanceof HTMLButtonElement) {
      matchQueueTarget.addEventListener("click", () => {
        targetFollowsQueue = true;
        targetUsesSmartDefault = false;
        updateBuilder();
        desiredCount.focus();
      });
    }
    if (randomSeedButton instanceof HTMLButtonElement && defaultSeed instanceof HTMLInputElement) {
      randomSeedButton.addEventListener("click", () => {
        const values = new Uint32Array(2);
        window.crypto.getRandomValues(values);
        const seed = ((BigInt(values[0] & 0x7fffffff) << 32n) | BigInt(values[1])).toString();
        defaultSeed.value = seed;
        defaultSeed.dispatchEvent(new Event("input", { bubbles: true }));
        defaultSeed.focus();
      });
    }
    if (randomEachSeedButton instanceof HTMLButtonElement
        && defaultSeed instanceof HTMLInputElement) {
      randomEachSeedButton.addEventListener("click", () => {
        defaultSeed.value = "-1";
        defaultSeed.dispatchEvent(new Event("input", { bubbles: true }));
        defaultSeed.focus();
      });
    }
    defaultPrompt && defaultPrompt.addEventListener("input", () => {
      if (form.dataset.applyingAutomationProfile === "true") {
        previousDefaultPrompt = defaultPrompt.value;
        return;
      }
      const rows = batchRows();
      rows.forEach((row) => {
        const prompt = field(row, "prompt");
        if (prompt.value === previousDefaultPrompt) prompt.value = defaultPrompt.value;
      });
      previousDefaultPrompt = defaultPrompt.value;
      updateBuilder();
    });
    defaultNegative && defaultNegative.addEventListener("input", () => {
      if (form.dataset.applyingAutomationProfile === "true") {
        previousDefaultNegative = defaultNegative.value;
        return;
      }
      const rows = batchRows();
      rows.forEach((row) => {
        const negativePrompt = field(row, "negative_prompt");
        if (negativePrompt.value === previousDefaultNegative) {
          negativePrompt.value = defaultNegative.value;
        }
      });
      previousDefaultNegative = defaultNegative.value;
      updateBuilder();
    });
    titleInput && titleInput.addEventListener("input", () => {
      if (slugInput && !slugWasEdited) slugInput.value = slugify(titleInput.value);
    });
    slugInput && slugInput.addEventListener("input", () => {
      slugWasEdited = Boolean(slugInput.value.trim());
    });

    form.addEventListener("invalid", (event) => {
      let disclosure = event.target.closest("details");
      while (disclosure) {
        disclosure.open = true;
        disclosure = disclosure.parentElement?.closest("details") || null;
      }
      const row = event.target.closest("[data-batch-row]");
      if (row && row.classList.contains("is-collapsed")) {
        row.classList.remove("is-collapsed");
        const collapseButton = row.querySelector('[data-batch-action="collapse"]');
        if (collapseButton) {
          collapseButton.setAttribute("aria-expanded", "true");
          collapseButton.textContent = "Collapse";
        }
      }
      window.requestAnimationFrame(() => event.target.scrollIntoView({ block: "center" }));
    }, true);

    form.addEventListener("submit", (event) => {
      setBatchValidity();
      const plan = batchRows().map(readRow);
      if (defaultPrompt && !defaultPrompt.value.trim() && plan[0]) {
        defaultPrompt.value = plan[0].prompt;
      }
      if (!form.checkValidity()) {
        event.preventDefault();
        form.reportValidity();
        return;
      }
      try {
        const submissionId = namedControl(form, "submission_id");
        if (submissionId instanceof HTMLInputElement && submissionId.value) {
          window.sessionStorage.setItem(
            scopedStorageKey(AUTOMATION_DRAFT_SUBMISSION_KEY),
            submissionId.value,
          );
        }
      } catch (_error) {
        // Draft cleanup is optional; queueing must continue when storage is blocked.
      }
      planData.value = JSON.stringify(plan);
      if (plannedJobCount) {
        const perJob = Math.max(1, integerValue(outputsPerJob && outputsPerJob.value, 1));
        plannedJobCount.value = plan.reduce(
          (total, batch) => total + Math.ceil(batch.image_count / perJob),
          0,
        );
      }
      submitButtons.forEach((button) => {
        button.disabled = true;
        button.textContent = "Freezing and queuing...";
      });
    });

    updateBuilder();
  }

  function initializeAutomationDraft() {
    const form = document.querySelector("[data-automation-form]");
    const planData = document.querySelector("#batch-plan-data");
    const status = document.querySelector("[data-automation-draft-status]");
    const clearButton = document.querySelector("[data-automation-draft-clear]");
    if (!form || !(planData instanceof HTMLTextAreaElement)) return;
    let timer = null;

    const setStatus = (message, tone = "") => {
      if (!(status instanceof HTMLElement)) return;
      status.textContent = message;
      status.className = `automation-draft-status${tone ? ` ${tone}` : ""}`;
    };

    const save = () => {
      timer = null;
      try {
        const batchPlan = JSON.parse(planData.value || "[]");
        if (!Array.isArray(batchPlan)) throw new Error("invalid batch plan");
        const title = namedControl(form, "title");
        const slug = namedControl(form, "slug");
        const seed = namedControl(form, "seed");
        const desiredCount = namedControl(form, "desired_accepted_count");
        const submissionId = namedControl(form, "submission_id");
        window.localStorage.setItem(scopedStorageKey(AUTOMATION_DRAFT_STORAGE_KEY), JSON.stringify({
          schema_version: 1,
          saved_at: new Date().toISOString(),
          title: title instanceof HTMLInputElement ? title.value : "",
          slug: slug instanceof HTMLInputElement ? slug.value : "",
          seed: seed instanceof HTMLInputElement ? seed.value : "",
          desired_accepted_count: desiredCount instanceof HTMLInputElement
            ? desiredCount.value
            : "",
          submission_id: submissionId instanceof HTMLInputElement ? submissionId.value : "",
          target_follows_queue: form.dataset.targetFollowsQueue === "true",
          profile: collectAutomationProfile(form),
          batch_plan: batchPlan.slice(0, 50),
        }));
        setStatus("Draft saved on this device.", "success");
      } catch (_error) {
        setStatus("Draft could not be saved in this browser.", "warning");
      }
    };

    const schedule = () => {
      if (timer !== null) window.clearTimeout(timer);
      setStatus("Saving draft...");
      timer = window.setTimeout(save, 500);
    };

    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);
    form.addEventListener("gen-automation:builder-updated", schedule);
    form.addEventListener("gen-automation:profile-changed", schedule);
    if (form.dataset.automationDraftRestored === "true") {
      setStatus("Draft restored from this device.", "success");
    }

    if (clearButton instanceof HTMLButtonElement) {
      clearButton.addEventListener("click", () => {
        if (timer !== null) window.clearTimeout(timer);
        timer = null;
        try {
          window.localStorage.removeItem(scopedStorageKey(AUTOMATION_DRAFT_STORAGE_KEY));
          setStatus("Saved draft cleared; the current form is unchanged.", "success");
        } catch (_error) {
          setStatus("Draft storage is unavailable in this browser.", "warning");
        }
      });
    }
  }

  const clearAutomationDraftAfterQueue = () => {
    const progress = document.querySelector("[data-generation-progress]");
    if (!(progress instanceof HTMLElement)) return;
    const submittedDraftId = progress.dataset.submittedDraftId || "";
    if (!submittedDraftId) return;
    try {
      const submissionKey = scopedStorageKey(AUTOMATION_DRAFT_SUBMISSION_KEY);
      if (window.sessionStorage.getItem(submissionKey) !== submittedDraftId) return;
      const draft = readStoredAutomationDraft();
      window.sessionStorage.removeItem(submissionKey);
      if (!draft || draft.submission_id !== submittedDraftId) return;
      window.localStorage.removeItem(scopedStorageKey(AUTOMATION_DRAFT_STORAGE_KEY));
    } catch (_error) {
      // A private-browser storage failure does not affect the queued run.
    }
  };

  function initializeDeliveryProgress() {
    const panel = document.querySelector("[data-delivery-progress]");
    if (!(panel instanceof HTMLElement)) return;
    const progressUrl = panel.dataset.deliveryProgressUrl || "";
    if (!progressUrl.startsWith("/dashboard/")) return;

    const jobsComplete = panel.querySelector("[data-delivery-jobs-complete]");
    const fullOutputs = panel.querySelector("[data-delivery-full-outputs]");
    const xOutputs = panel.querySelector("[data-delivery-x-outputs]");
    const failures = panel.querySelector("[data-delivery-failures]");
    const activeJobs = panel.querySelector("[data-delivery-active-jobs]");
    const progress = panel.querySelector("[data-delivery-output-progress]");
    const progressLabel = panel.querySelector("[data-delivery-output-progress-label]");
    const outputStatus = panel.querySelector("[data-delivery-output-status]");
    const liveStatus = panel.querySelector("[data-delivery-live-status]");
    const archiveStatus = document.querySelector("[data-delivery-archive-status]");
    const archiveDetail = document.querySelector("[data-delivery-archive-detail]");
    const archiveCardStatus = document.querySelector("[data-delivery-archive-card-status]");
    const patreonCard = document.querySelector('[data-destination="patreon"]');
    const patreonStatus = patreonCard?.querySelector("[data-destination-status]");
    const patreonDetail = patreonCard?.querySelector("[data-destination-detail]");
    const xCard = document.querySelector('[data-destination="x"]');
    const xStatus = xCard?.querySelector("[data-destination-status]");
    const xDetail = xCard?.querySelector("[data-destination-detail]");
    const megaCard = document.querySelector('[data-destination="mega"]');
    const megaStatus = megaCard?.querySelector("[data-destination-status]");
    const megaDetail = megaCard?.querySelector("[data-destination-detail]");
    const megaProgressRegion = megaCard?.querySelector("[data-mega-progress-region]");
    const megaProgressLabel = megaCard?.querySelector("[data-mega-progress-label]");
    const megaProgress = megaCard?.querySelector("[data-mega-progress]");
    const megaRemoteRow = megaCard?.querySelector("[data-mega-remote-row]");
    const megaRemotePath = megaCard?.querySelector("[data-mega-remote-path]");
    const initialOutputState = panel.dataset.deliveryOutputState || "not_started";
    const initialXOutputsReady = panel.dataset.deliveryXOutputsReady === "true";
    const initialArchiveState = panel.dataset.deliveryArchiveState || "not_started";
    const initialPatreonState = patreonCard?.dataset.destinationState || "not_prepared";
    const initialXState = xCard?.dataset.destinationState || "not_prepared";
    const initialMegaState = megaCard?.dataset.destinationState || "not_prepared";
    let timer = null;
    let requestInFlight = false;
    let stopped = false;
    let reloadRequested = false;
    let consecutiveFailures = 0;
    const maxConsecutiveFailures = 6;

    const count = (value) => Math.max(0, integerValue(value, 0));
    const setText = (node, value) => {
      if (node instanceof HTMLElement) node.textContent = value;
    };
    const delayFor = (value, fallback = 3000) => Math.min(
      60000,
      Math.max(1000, integerValue(value, fallback)),
    );
    const schedule = (delay) => {
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
      if (stopped || document.visibilityState === "hidden") return;
      timer = window.setTimeout(poll, delayFor(delay));
    };
    const reloadForNewControls = () => {
      if (reloadRequested) return;
      reloadRequested = true;
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
      window.requestAnimationFrame(() => {
        persistSamePageScroll();
        window.location.reload();
      });
    };
    const stopPolling = (message) => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
      setText(liveStatus || megaDetail || archiveStatus || outputStatus, message);
    };
    const renderOutput = (output) => {
      const knownStates = ["not_started", "rendering", "ready", "failed", "stalled"];
      const state = knownStates.includes(output.state) ? output.state : "stalled";
      const succeeded = count(output.succeeded);
      const totalJobs = count(output.total_jobs);
      const failed = count(output.failed);
      const active = count(output.active_jobs);
      const readyFull = count(output.ready_full_outputs);
      const expectedFull = count(output.expected_full_outputs);
      const readyTeasers = count(output.ready_x_teasers);
      const expectedTeasers = count(output.expected_x_teasers);
      const fullOutputsReady = output.full_outputs_ready === true;

      setText(jobsComplete, `${succeeded} / ${totalJobs}`);
      setText(fullOutputs, `${readyFull} / ${expectedFull}`);
      setText(xOutputs, `${readyTeasers} / ${expectedTeasers}`);
      setText(failures, String(failed));
      setText(activeJobs, String(active));
      setText(progressLabel, `${succeeded} / ${totalJobs} jobs complete`);
      if (progress instanceof HTMLProgressElement) {
        progress.max = Math.max(1, totalJobs);
        progress.value = Math.min(succeeded, Math.max(1, totalJobs));
        progress.textContent = `${succeeded} / ${totalJobs}`;
      }

      if (state === "not_started") {
        setText(outputStatus, "Ready to prepare clean full-set copies.");
        setText(liveStatus, "Nothing is running until you choose Prepare clean full set.");
      } else if (state === "ready") {
        setText(outputStatus, "All clean publishing copies are ready.");
        setText(liveStatus, "Copy preparation finished. Loading the next controls once.");
      } else if (state === "failed") {
        setText(
          outputStatus,
          `Copy preparation stopped with ${failed} failed job${failed === 1 ? "" : "s"}.`,
        );
        setText(
          liveStatus,
          fullOutputsReady
            ? "The clean full set is ready; ZIP preparation continues independently."
            : "No jobs are active. Retry the clean full-set copies before ZIP completion.",
        );
      } else if (state === "stalled") {
        setText(outputStatus, "Copy preparation is stalled with no active jobs.");
        setText(
          liveStatus,
          fullOutputsReady
            ? "The clean full set is ready; ZIP preparation continues independently."
            : "Operator repair or a retry is required before ZIP completion.",
        );
      } else {
        setText(
          outputStatus,
          `Creating clean publishing copies in the background (${active} active).`,
        );
        setText(liveStatus, "Progress updates here without reloading the page.");
      }
      return state;
    };
    const renderArchive = (archive) => {
      const knownStates = ["not_started", "preparing", "ready", "failed"];
      const state = knownStates.includes(archive.state) ? archive.state : "failed";
      const partCount = count(archive.part_count);
      const serverDetail = typeof archive.detail === "string" ? archive.detail.trim() : "";
      if (archiveCardStatus instanceof HTMLElement) {
        archiveCardStatus.className = `status ${state}`;
        archiveCardStatus.textContent = state === "not_started"
          ? "not started"
          : state === "failed" ? "needs attention" : state;
      }
      if (state === "ready") {
        setText(
          archiveStatus,
          `${partCount} finished-set ZIP part${partCount === 1 ? " is" : "s are"} ready.`,
        );
        setText(archiveDetail, "Loading the download controls once.");
      } else if (state === "preparing") {
        setText(archiveStatus, "Creating ZIP parts in the background.");
        setText(
          archiveDetail,
          "The page remains usable while the clean archive is assembled.",
        );
      } else if (state === "failed") {
        setText(archiveStatus, "ZIP creation failed and no archive job is active.");
        setText(
          archiveDetail,
          serverDetail || "Use Retry ZIP preparation without changing any destination.",
        );
      } else {
        setText(archiveStatus, "ZIP creation has not started.");
        setText(
          archiveDetail,
          serverDetail || "Use Prepare ZIP download when you want a downloadable archive.",
        );
      }
      return state;
    };
    const renderMega = (mega) => {
      const knownStates = ["not_prepared", "queued", "running", "published", "failed"];
      const state = knownStates.includes(mega.state) ? mega.state : "failed";
      const completed = count(mega.completed_items);
      const total = count(mega.total_items);
      const detail = typeof mega.detail === "string" ? mega.detail.trim() : "";
      const remotePath = typeof mega.remote_path === "string" ? mega.remote_path.trim() : "";

      if (megaStatus instanceof HTMLElement) {
        megaStatus.className = `status ${state}`;
        megaStatus.textContent = state === "not_prepared"
          ? "not started"
          : state === "published" ? "complete"
            : state === "failed" ? "needs attention" : state.replaceAll("_", " ");
      }
      setText(megaDetail, detail || "MEGA delivery status is unavailable.");
      setText(megaProgressLabel, `${completed} / ${total} images uploaded`);
      if (megaProgress instanceof HTMLProgressElement) {
        megaProgress.max = Math.max(1, total);
        megaProgress.value = Math.min(completed, Math.max(1, total));
        megaProgress.textContent = `${completed} / ${total}`;
      }
      if (megaProgressRegion instanceof HTMLElement) megaProgressRegion.hidden = total < 1;
      setText(megaRemotePath, remotePath);
      if (megaRemoteRow instanceof HTMLElement) megaRemoteRow.hidden = !remotePath;
      return state;
    };
    const renderPublicationDestination = (destination, statusNode, detailNode) => {
      if (!isRecord(destination)) return null;
      const knownStates = [
        "not_prepared", "queued", "running", "ready", "published", "unknown", "failed",
      ];
      const state = knownStates.includes(destination.state) ? destination.state : "failed";
      const detail = typeof destination.detail === "string" ? destination.detail.trim() : "";
      if (statusNode instanceof HTMLElement) {
        statusNode.className = `status ${state}`;
        statusNode.textContent = state === "not_prepared"
          ? "not started"
          : state === "published" ? "complete"
            : state === "failed" ? "needs attention" : state.replaceAll("_", " ");
      }
      setText(detailNode, detail || "Destination status is unavailable.");
      return state;
    };
    const render = (payload) => {
      if (
        !isRecord(payload)
        || payload.schema !== "delivery-progress/v1"
        || !isRecord(payload.outputs)
        || !isRecord(payload.archive)
        || !isRecord(payload.mega)
      ) return null;
      const fullOutputsReady = payload.outputs.full_outputs_ready === true;
      const xOutputsReady = payload.outputs.x_outputs_ready === true;
      const outputState = renderOutput(payload.outputs);
      const archiveState = renderArchive(payload.archive);
      const megaState = renderMega(payload.mega);
      const patreonState = renderPublicationDestination(
        payload.patreon,
        patreonStatus,
        patreonDetail,
      );
      const xState = renderPublicationDestination(payload.x, xStatus, xDetail);
      const providerControlsChanged = (
        megaState !== initialMegaState
        && !["not_prepared", "queued", "running"].includes(megaState)
      ) || (
        patreonState !== null
        && patreonState !== initialPatreonState
        && !["not_prepared", "queued", "running"].includes(patreonState)
      ) || (
        xState !== null
        && xState !== initialXState
        && !["not_prepared", "queued", "running"].includes(xState)
      );
      if (
        (initialOutputState !== "ready" && outputState === "ready")
        || (initialOutputState !== "failed" && outputState === "failed")
        || (!initialXOutputsReady && xOutputsReady)
        || (initialArchiveState !== "ready" && archiveState === "ready")
        || (initialArchiveState !== "failed" && archiveState === "failed")
        || providerControlsChanged
      ) {
        reloadForNewControls();
        return { polling: false, delay: null };
      }
      return {
        polling: outputState === "rendering"
          || count(payload.outputs.active_jobs) > 0
          || archiveState === "preparing"
          || payload.mega.active === true
          || payload.patreon?.active === true
          || payload.x?.active === true,
        delay: payload.poll_after_ms,
      };
    };
    async function poll() {
      if (stopped || requestInFlight || document.visibilityState === "hidden") return;
      requestInFlight = true;
      try {
        const response = await fetch(progressUrl, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (response.redirected || response.status === 401 || response.status === 403) {
          stopPolling("Session expired; sign in again or reload this page.");
          return;
        }
        if (response.status === 404) {
          stopPolling("Live updates paused; reload this page to retry.");
          return;
        }
        if (!response.ok) throw new Error("delivery progress unavailable");
        const next = render(await response.json());
        if (!next) throw new Error("invalid delivery progress response");
        consecutiveFailures = 0;
        if (!reloadRequested && next.polling) schedule(next.delay);
        else if (!reloadRequested) stopped = true;
      } catch (_error) {
        consecutiveFailures += 1;
        if (consecutiveFailures >= maxConsecutiveFailures) {
          stopPolling("Live updates paused; reload this page to retry.");
          return;
        }
        setText(
          liveStatus || megaDetail || archiveStatus || outputStatus,
          "Network interrupted live progress; retrying without reloading the page.",
        );
        const backoff = Math.min(60000, 3000 * (2 ** Math.min(4, consecutiveFailures)));
        schedule(backoff);
      } finally {
        requestInFlight = false;
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !stopped && !requestInFlight) schedule(250);
      else if (document.visibilityState === "hidden" && timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    });
    schedule(250);
  }

  function initializeDeliveryReauthentication() {
    const dialog = document.querySelector("[data-delivery-reauth-dialog]");
    const reauthenticationForm = document.querySelector("[data-delivery-reauth-form]");
    if (!(dialog instanceof HTMLDialogElement)
        || !(reauthenticationForm instanceof HTMLFormElement)
        || typeof dialog.showModal !== "function") return;

    const protectedForms = Array.from(
      document.querySelectorAll("form[data-requires-recent-auth]"),
    ).filter((form) => form instanceof HTMLFormElement);
    if (!protectedForms.length) return;

    const password = reauthenticationForm.querySelector("[data-delivery-reauth-password]");
    const totp = reauthenticationForm.querySelector("[data-delivery-reauth-totp]");
    const error = reauthenticationForm.querySelector("[data-delivery-reauth-error]");
    const cancel = reauthenticationForm.querySelector("[data-delivery-reauth-cancel]");
    const confirm = reauthenticationForm.querySelector("[data-delivery-reauth-submit]");
    if (!(password instanceof HTMLInputElement)
        || !(totp instanceof HTMLInputElement)
        || !(error instanceof HTMLElement)
        || !(cancel instanceof HTMLButtonElement)
        || !(confirm instanceof HTMLButtonElement)) return;

    let pendingForm = null;
    let pendingSubmitter = null;

    const clearCredentials = () => {
      password.value = "";
      totp.value = "";
    };
    const clearError = () => {
      error.hidden = true;
      error.textContent = "";
    };
    const resetPendingAction = () => {
      pendingForm = null;
      pendingSubmitter = null;
      clearCredentials();
      clearError();
    };
    const csrfTokenFor = (form) => {
      const control = form.querySelector('input[name="csrf_token"]');
      return control instanceof HTMLInputElement ? control.value : "";
    };
    const nativeSubmit = (form) => {
      HTMLFormElement.prototype.submit.call(form);
    };

    const sendAction = async (form, submitter = null) => {
      const body = new URLSearchParams();
      const formData = new FormData(form);
      for (const [name, value] of formData.entries()) {
        if (typeof value !== "string") {
          nativeSubmit(form);
          return;
        }
        body.append(name, value);
      }
      if (submitter instanceof HTMLButtonElement && submitter.name) {
        body.append(submitter.name, submitter.value);
      }

      form.setAttribute("aria-busy", "true");
      if (submitter instanceof HTMLButtonElement) submitter.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          redirect: "follow",
          headers: {
            Accept: "text/html",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          },
          body,
        });
        if (response.status === 401) {
          pendingForm = form;
          pendingSubmitter = submitter;
          clearCredentials();
          clearError();
          dialog.showModal();
          window.setTimeout(() => password.focus(), 0);
          return;
        }
        if (response.ok && response.redirected) {
          window.location.assign(response.url);
          return;
        }
        nativeSubmit(form);
      } catch (_error) {
        nativeSubmit(form);
      } finally {
        form.removeAttribute("aria-busy");
        if (submitter instanceof HTMLButtonElement) submitter.disabled = false;
      }
    };

    protectedForms.forEach((form) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const submitter = event.submitter instanceof HTMLButtonElement
          ? event.submitter
          : null;
        void sendAction(form, submitter);
      });
    });

    const cancelReauthentication = () => {
      resetPendingAction();
      if (dialog.open) dialog.close();
    };
    cancel.addEventListener("click", cancelReauthentication);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      cancelReauthentication();
    });

    reauthenticationForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!(pendingForm instanceof HTMLFormElement)) {
        cancelReauthentication();
        return;
      }
      const actionForm = pendingForm;
      const actionSubmitter = pendingSubmitter;
      const csrfToken = csrfTokenFor(actionForm);
      if (!csrfToken) {
        error.textContent = "This page is out of date. Reload it and try again.";
        error.hidden = false;
        return;
      }

      clearError();
      confirm.disabled = true;
      reauthenticationForm.setAttribute("aria-busy", "true");
      try {
        const response = await fetch("/api/v1/auth/reauthenticate", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            password: password.value,
            totp_code: optionalText(totp.value),
          }),
        });
        clearCredentials();
        if (!response.ok) {
          error.textContent = response.status === 401
            ? "Those details were not accepted, or your session expired. Try again or sign in anew."
            : "Confirmation is temporarily unavailable. Please try again.";
          error.hidden = false;
          password.focus();
          return;
        }
        pendingForm = null;
        pendingSubmitter = null;
        dialog.close();
        await sendAction(actionForm, actionSubmitter);
      } catch (_error) {
        clearCredentials();
        error.textContent = "The connection was interrupted. Please try again.";
        error.hidden = false;
        password.focus();
      } finally {
        confirm.disabled = false;
        reauthenticationForm.removeAttribute("aria-busy");
      }
    });
  }

  function initializeAutomationPresets() {
    const form = document.querySelector("[data-automation-form]");
    const manager = document.querySelector("[data-automation-presets]");
    if (!form || !manager) return;
    const select = manager.querySelector("[data-automation-preset-select]");
    const nameInput = manager.querySelector("[data-automation-preset-name]");
    const saveButton = manager.querySelector("[data-automation-preset-save]");
    const loadButton = manager.querySelector("[data-automation-preset-load]");
    const deleteButton = manager.querySelector("[data-automation-preset-delete]");
    const exportButton = manager.querySelector("[data-automation-preset-export]");
    const importButton = manager.querySelector("[data-automation-preset-import]");
    const importFile = manager.querySelector("[data-automation-preset-import-file]");
    const status = manager.querySelector("[data-automation-preset-status]");
    if (!(select instanceof HTMLSelectElement)
        || !(nameInput instanceof HTMLInputElement)
        || !(saveButton instanceof HTMLButtonElement)
        || !(loadButton instanceof HTMLButtonElement)
        || !(deleteButton instanceof HTMLButtonElement)) return;

    let presets = readStoredAutomationPresets();
    const setStatus = (message, tone = "") => {
      if (!(status instanceof HTMLElement)) return;
      status.textContent = message;
      status.className = `settings-preset-status${tone ? ` ${tone}` : ""}`;
    };
    const firstInvalidPresetControl = () => {
      const controls = AUTOMATION_PRESET_FIELDS
        .map((name) => namedControl(form, name))
        .filter((control) => (
          control instanceof HTMLInputElement
          || control instanceof HTMLTextAreaElement
          || control instanceof HTMLSelectElement
        ));
      const visibleLoraWeights = Array.from(
        form.querySelectorAll("[data-lora-visible-weight]"),
      );
      const loraWeights = visibleLoraWeights.length > 0
        ? visibleLoraWeights
        : Array.from(form.querySelectorAll("[data-lora-native-weight]"));
      const batchControls = Array.from(form.querySelectorAll("[data-batch-field]"));
      return [...controls, ...loraWeights, ...batchControls]
        .find((control) => !control.checkValidity());
    };
    const revealInvalidPresetControl = (control) => {
      let disclosure = control.closest("details");
      while (disclosure) {
        disclosure.open = true;
        disclosure = disclosure.parentElement?.closest("details") || null;
      }
      const row = control.closest("[data-batch-row]");
      if (row) {
        row.classList.remove("is-collapsed");
        const collapseButton = row.querySelector('[data-batch-action="collapse"]');
        if (collapseButton) {
          collapseButton.setAttribute("aria-expanded", "true");
          collapseButton.textContent = "Collapse";
        }
      }
      control.focus();
      control.reportValidity();
    };
    const render = (selectedId = select.value) => {
      select.replaceChildren();
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = presets.length === 0 ? "No saved presets" : "Choose a preset";
      select.append(placeholder);
      presets.forEach((preset) => {
        const option = document.createElement("option");
        option.value = preset.id;
        const batchPlan = normalizeAutomationPresetBatchPlan(preset.batch_plan);
        option.textContent = batchPlan
          ? `${preset.name} · ${batchPlan.length} batch${batchPlan.length === 1 ? "" : "es"}`
          : `${preset.name} · settings only`;
        select.append(option);
      });
      select.value = presets.some((preset) => preset.id === selectedId) ? selectedId : "";
      loadButton.disabled = !select.value;
      deleteButton.disabled = !select.value;
    };

    select.addEventListener("change", () => {
      loadButton.disabled = !select.value;
      deleteButton.disabled = !select.value;
      const selected = presets.find((preset) => preset.id === select.value);
      if (selected) nameInput.value = selected.name;
    });
    saveButton.addEventListener("click", () => {
      const name = nameInput.value.trim();
      if (!name) {
        nameInput.focus();
        setStatus("Enter a preset name first.", "warning");
        return;
      }
      const invalidControl = firstInvalidPresetControl();
      if (invalidControl) {
        revealInvalidPresetControl(invalidControl);
        setStatus("Fix the highlighted setting or batch before saving.", "warning");
        return;
      }
      const existing = presets.find((preset) => preset.name.toLowerCase() === name.toLowerCase());
      const hasBatchBuilder = form.querySelector("#batch-plan-data") instanceof HTMLTextAreaElement;
      const currentBatchPlan = collectAutomationPresetBatchPlan(form);
      if (hasBatchBuilder && !currentBatchPlan) {
        setStatus("Fix the batch queue before saving this preset.", "warning");
        return;
      }
      const preservedBatchPlan = normalizeAutomationPresetBatchPlan(existing?.batch_plan);
      const batchPlan = currentBatchPlan || preservedBatchPlan;
      const batchSummary = batchPlan ? summarizeAutomationBatchPlan(batchPlan) : null;
      const id = existing?.id || `${slugify(name) || "preset"}-${Date.now().toString(36)}`;
      const saved = {
        schema_version: batchPlan ? 2 : 1,
        id,
        name,
        profile: collectAutomationProfile(form),
        ...(batchPlan ? {
          batch_plan: batchPlan,
          batch_count: batchSummary.batchCount,
          image_count: batchSummary.imageCount,
        } : {}),
        updated_at: new Date().toISOString(),
      };
      const nextPresets = [saved, ...presets.filter((preset) => preset.id !== id)].slice(0, 30);
      try {
        writeStoredAutomationPresets(nextPresets);
        presets = nextPresets;
        render(id);
        const action = existing ? "updated" : "saved";
        setStatus(
          batchSummary
            ? `“${name}” ${action} · ${batchSummary.batchCount} batch${batchSummary.batchCount === 1 ? "" : "es"} · ${batchSummary.imageCount.toLocaleString()} images.`
            : `“${name}” ${action} as settings only.`,
          "success",
        );
      } catch (_error) {
        setStatus("This browser could not save the preset.", "warning");
      }
    });
    loadButton.addEventListener("click", () => {
      const preset = presets.find((item) => item.id === select.value);
      if (!preset) {
        setStatus("That preset is no longer available.", "warning");
        return;
      }
      const hasSavedBatchPlan = Object.prototype.hasOwnProperty.call(preset, "batch_plan");
      const savedBatchPlan = hasSavedBatchPlan
        ? normalizeAutomationPresetBatchPlan(preset.batch_plan)
        : null;
      if (hasSavedBatchPlan && !savedBatchPlan) {
        setStatus("That preset has an invalid saved batch queue and was not loaded.", "warning");
        return;
      }
      const hasBatchBuilder = Boolean(form.querySelector("#batch-builder"));
      const result = applyAutomationProfile(form, preset.profile);
      if (!result.applied) {
        setStatus("That preset contains invalid saved data.", "warning");
        return;
      }
      let queueReplaced = false;
      if (savedBatchPlan) {
        const replacement = { batch_plan: savedBatchPlan, replaced: false };
        form.dispatchEvent(new CustomEvent("gen-automation:replace-batch-plan", {
          detail: replacement,
        }));
        queueReplaced = replacement.replaced;
        if (!queueReplaced && hasBatchBuilder) {
          setStatus("The saved settings loaded, but the batch queue could not be restored.", "warning");
          return;
        }
      } else {
        form.dispatchEvent(new CustomEvent("gen-automation:refresh-batch-plan"));
      }
      const invalidControl = firstInvalidPresetControl();
      if (invalidControl) {
        revealInvalidPresetControl(invalidControl);
        setStatus(`${preset.name} loaded, but one or more saved values need correction.`, "warning");
        return;
      }
      if (result.missing.length > 0) {
        setStatus(
          `${preset.name} loaded with changes needed: ${result.missing.join(" ")}`,
          "warning",
        );
        return;
      }
      if (!savedBatchPlan) {
        setStatus(
          hasBatchBuilder
            ? "Older settings-only preset loaded; the current queue was left unchanged. Save it again to include every batch."
            : `${preset.name} settings loaded. This older preset has no saved batch queue.`,
          "warning",
        );
        return;
      }
      const batchSummary = summarizeAutomationBatchPlan(savedBatchPlan);
      setStatus(
        queueReplaced
          ? `“${preset.name}” loaded · ${batchSummary.batchCount} batch${batchSummary.batchCount === 1 ? "" : "es"} · ${batchSummary.imageCount.toLocaleString()} images. Queue replaced.`
          : `“${preset.name}” settings loaded. Its ${batchSummary.batchCount} saved batches will load on New set.`,
        "success",
      );
    });
    deleteButton.addEventListener("click", () => {
      const preset = presets.find((item) => item.id === select.value);
      if (!preset) return;
      const nextPresets = presets.filter((item) => item.id !== preset.id);
      try {
        writeStoredAutomationPresets(nextPresets);
        presets = nextPresets;
        nameInput.value = "";
        render();
        setStatus(`${preset.name} deleted.`);
      } catch (_error) {
        setStatus("This browser could not delete the preset.", "warning");
      }
    });
    if (exportButton instanceof HTMLButtonElement) {
      exportButton.addEventListener("click", () => {
        if (presets.length === 0) {
          setStatus("Save a preset before exporting.", "warning");
          return;
        }
        const blob = new Blob([JSON.stringify({
          schema_version: 2,
          exported_at: new Date().toISOString(),
          presets,
        }, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "automation-presets.json";
        link.hidden = true;
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
        setStatus(`${presets.length} preset${presets.length === 1 ? "" : "s"} exported.`, "success");
      });
    }
    if (importButton instanceof HTMLButtonElement && importFile instanceof HTMLInputElement) {
      importButton.addEventListener("click", () => importFile.click());
      importFile.addEventListener("change", async () => {
        const file = importFile.files?.item(0);
        importFile.value = "";
        if (!file) return;
        try {
          const payload = JSON.parse(await file.text());
          const requested = Array.isArray(payload) ? payload : payload?.presets;
          if (!Array.isArray(requested)) throw new Error("invalid preset export");
          const imported = requested.flatMap((item) => {
            if (!(item && typeof item === "object")
                || typeof item.id !== "string"
                || typeof item.name !== "string"
                || !item.profile || typeof item.profile !== "object") return [];
            if (!Object.prototype.hasOwnProperty.call(item, "batch_plan")) return [item];
            const batchPlan = normalizeAutomationPresetBatchPlan(item.batch_plan);
            if (!batchPlan) return [];
            const batchSummary = summarizeAutomationBatchPlan(batchPlan);
            return [{
              ...item,
              schema_version: 2,
              batch_plan: batchPlan,
              batch_count: batchSummary.batchCount,
              image_count: batchSummary.imageCount,
            }];
          }).slice(0, 30);
          if (imported.length === 0) throw new Error("empty preset export");
          const importedIds = new Set(imported.map((item) => item.id));
          const importedNames = new Set(imported.map((item) => item.name.toLowerCase()));
          const nextPresets = [
            ...imported,
            ...presets.filter(
              (item) => !importedIds.has(item.id) && !importedNames.has(item.name.toLowerCase()),
            ),
          ].slice(0, 30);
          writeStoredAutomationPresets(nextPresets);
          presets = nextPresets;
          render(imported[0].id);
          setStatus(`${imported.length} preset${imported.length === 1 ? "" : "s"} imported.`, "success");
        } catch (_error) {
          setStatus("Choose a valid generation preset export.", "warning");
        }
      });
    }

    render();
    if (form.dataset.importedImageSettings === "true") {
      const warning = form.dataset.profileImportWarning || "";
      setStatus(
        warning
          ? `Image settings loaded with changes needed: ${warning}`
          : "Image settings loaded. Name and save them if you want a reusable preset.",
        warning ? "warning" : "success",
      );
    }
  }

  function initializeReleaseLibrary() {
    const toolbar = document.querySelector("[data-release-library]");
    const cards = Array.from(document.querySelectorAll("[data-release-card]"));
    if (!toolbar || cards.length === 0) return;
    const search = toolbar.querySelector("[data-release-search]");
    const filters = Array.from(toolbar.querySelectorAll("[data-release-filter]"));
    const status = toolbar.querySelector("[data-release-filter-status]");
    const empty = document.querySelector("[data-release-filter-empty]");
    let activeFilter = "all";
    toolbar.hidden = false;

    const render = () => {
      const query = search instanceof HTMLInputElement ? search.value.trim().toLowerCase() : "";
      let visible = 0;
      cards.forEach((card) => {
        const filterMatches = activeFilter === "all" || card.dataset.releaseStatus === activeFilter;
        const searchText = (card.dataset.releaseSearchText || "").toLowerCase();
        const matches = filterMatches && (!query || searchText.includes(query));
        card.hidden = !matches;
        if (matches) visible += 1;
      });
      if (status instanceof HTMLOutputElement) {
        status.textContent = `${visible} automation${visible === 1 ? "" : "s"} shown`;
      }
      if (empty instanceof HTMLElement) empty.hidden = visible !== 0;
    };

    filters.forEach((button) => {
      button.addEventListener("click", () => {
        activeFilter = button.dataset.releaseFilter || "all";
        filters.forEach((candidate) => {
          const selected = candidate === button;
          candidate.classList.toggle("active", selected);
          candidate.setAttribute("aria-pressed", String(selected));
        });
        render();
      });
    });
    if (search instanceof HTMLInputElement) search.addEventListener("input", render);
    render();
  }

  function initializeAssetSorting() {
    document.querySelectorAll("[data-asset-sort-controls]").forEach((controls) => {
      const targetId = controls.dataset.assetSortTarget || "";
      const grid = document.getElementById(targetId);
      if (!(grid instanceof HTMLOListElement)) return;
      const cards = Array.from(grid.querySelectorAll(".asset-card"));
      const buttons = Array.from(controls.querySelectorAll("[data-asset-sort]"));
      const status = controls.querySelector("[data-asset-sort-status]");
      if (cards.length === 0 || buttons.length === 0) return;

      const numberFrom = (card, name) => {
        const value = Number.parseInt(card.dataset[name] || "0", 10);
        return Number.isFinite(value) ? value : 0;
      };
      const compareQuality = (left, right) => (
        numberFrom(left, "qualityOrder") - numberFrom(right, "qualityOrder")
      );
      const compareGeneration = (left, right) => (
        numberFrom(left, "batchIndex") - numberFrom(right, "batchIndex")
        || numberFrom(left, "batchImageNumber") - numberFrom(right, "batchImageNumber")
        || compareQuality(left, right)
      );

      const render = (order) => {
        const compare = order === "generation" ? compareGeneration : compareQuality;
        const regular = cards.filter((card) => card.dataset.aiExcluded !== "true").sort(compare);
        const excluded = cards.filter((card) => card.dataset.aiExcluded === "true").sort(compare);
        const excludedHeadings = Array.from(grid.querySelectorAll(".ai-excluded-heading"));
        regular.forEach((card) => grid.append(card));
        excludedHeadings.forEach((heading) => grid.append(heading));
        excluded.forEach((card) => grid.append(card));
        buttons.forEach((button) => {
          const selected = button.dataset.assetSort === order;
          button.classList.toggle("active", selected);
          button.setAttribute("aria-pressed", String(selected));
        });
        if (status instanceof HTMLOutputElement) {
          status.textContent = order === "generation"
            ? "Batch order, then image number"
            : "Highest quality first";
        }
      };

      buttons.forEach((button) => {
        button.addEventListener("click", () => render(button.dataset.assetSort || "quality"));
      });
      controls.hidden = false;
      render("quality");
    });
  }

  function initializeWildcardLibraryTools() {
    const toolbar = document.querySelector("[data-wildcard-library-tools]");
    const cards = Array.from(document.querySelectorAll("[data-wildcard-card]"));
    const search = document.querySelector("[data-wildcard-search]");
    const status = document.querySelector("[data-wildcard-filter-status]");
    const empty = document.querySelector("[data-wildcard-filter-empty]");

    const render = () => {
      if (!(search instanceof HTMLInputElement)) return;
      const query = search.value.trim().toLowerCase();
      let visible = 0;
      cards.forEach((card) => {
        const matches = !query || (card.dataset.wildcardSearchText || "").toLowerCase().includes(query);
        card.hidden = !matches;
        if (matches) visible += 1;
      });
      if (status instanceof HTMLOutputElement) {
        status.textContent = `${visible} librar${visible === 1 ? "y" : "ies"} shown`;
      }
      if (empty instanceof HTMLElement) empty.hidden = visible !== 0;
    };
    if (search instanceof HTMLInputElement) {
      if (toolbar instanceof HTMLElement) toolbar.hidden = false;
      search.addEventListener("input", render);
      render();
    }

    document.querySelectorAll("[data-copy-text]").forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", async () => {
        const value = button.dataset.copyText || "";
        const label = button.textContent;
        try {
          await navigator.clipboard.writeText(value);
          button.textContent = "Copied";
        } catch (_error) {
          const fallback = document.createElement("textarea");
          fallback.value = value;
          fallback.setAttribute("readonly", "");
          fallback.style.position = "fixed";
          fallback.style.opacity = "0";
          document.body.append(fallback);
          fallback.select();
          document.execCommand("copy");
          fallback.remove();
          button.textContent = "Copied";
        }
        window.setTimeout(() => { button.textContent = label; }, 1200);
      });
    });

    document.querySelectorAll("[data-wildcard-file-button]").forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.wildcardFileInputId || "");
        if (input instanceof HTMLInputElement && input.type === "file") input.click();
      });
    });

    document.querySelectorAll("[data-wildcard-file-input]").forEach((input) => {
      input.addEventListener("change", async () => {
        const file = input.files?.item(0);
        const target = document.getElementById(input.dataset.wildcardFileTarget || "");
        input.value = "";
        if (!file || !(target instanceof HTMLTextAreaElement)) return;
        target.value = (await file.text()).replace(/\r\n/g, "\n");
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.focus();
      });
    });

    document.querySelectorAll("[data-wildcard-download]").forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => {
        const target = document.getElementById(button.dataset.wildcardDownloadTarget || "");
        if (!(target instanceof HTMLTextAreaElement)) return;
        const blob = new Blob([target.value], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = button.dataset.wildcardDownloadName || "wildcard.txt";
        link.hidden = true;
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      });
    });
  }

  function initializeBulkReview() {
    const form = document.querySelector("#bulk-action-form, [data-bulk-action-form]");
    if (!form) return;
    const checkboxes = Array.from(
      document.querySelectorAll('input[type="checkbox"][name="asset_id"]'),
    );
    if (checkboxes.length === 0) return;
    const actionButtons = Array.from(form.querySelectorAll('button[name="action"]'));
    const acceptButton = form.querySelector('button[name="action"][value="accept"]');
    const xAddButton = form.querySelector('button[name="action"][value="x_add"]');
    const xRemoveButton = form.querySelector('button[name="action"][value="x_remove"]');
    const selectToTargetButtons = Array.from(document.querySelectorAll("[data-select-to-target]"));
    const countLabels = document.querySelectorAll("[data-selected-count]");
    const selectionStatus = form.querySelector("[data-bulk-selection-status]");
    const acceptUndecidedButton = document.querySelector("[data-accept-undecided]");
    const bulkReasonInput = form.querySelector('input[name="reason_code"]');
    const currentXCount = Math.max(0, integerValue(form.dataset.xSelectedCount, 0));
    const xCapacity = Math.max(1, integerValue(form.dataset.xCapacity, 4));
    const reviewTarget = Math.max(1, integerValue(form.dataset.reviewTarget, 1));
    const acceptedCount = Math.max(0, integerValue(form.dataset.acceptedCount, 0));
    const remainingReviewSlots = Math.max(0, reviewTarget - acceptedCount);
    let lastClickedCheckbox = null;
    document.querySelectorAll("[data-review-selection-controls], [data-review-tools]").forEach((item) => {
      item.hidden = false;
    });

    selectToTargetButtons.forEach((button) => {
      if (acceptedCount > reviewTarget) {
        const excess = acceptedCount - reviewTarget;
        button.textContent = `Goal exceeded by ${excess}`;
        button.disabled = true;
      } else if (remainingReviewSlots === 0) {
        button.textContent = "Goal reached";
        button.disabled = true;
      } else {
        button.textContent = `Select ${remainingReviewSlots} undecided to reach goal`;
        button.disabled = false;
      }
    });

    const updateExcludedHeadings = () => {
      document.querySelectorAll(".ai-excluded-heading").forEach((heading) => {
        const grid = heading.closest("[data-review-grid], [data-asset-grid]");
        const hasVisibleExcluded = Boolean(grid && Array.from(
          grid.querySelectorAll('.asset-card[data-ai-excluded="true"]'),
        ).some((card) => !card.hidden));
        heading.hidden = !hasVisibleExcluded;
      });
    };

    const updateSelection = () => {
      const selectedCheckboxes = checkboxes.filter((checkbox) => checkbox.checked);
      const selected = selectedCheckboxes.length;
      const hiddenSelected = selectedCheckboxes.filter((checkbox) => {
        const card = checkbox.closest(".asset-card");
        return card && card.hidden;
      }).length;
      checkboxes.forEach((checkbox) => {
        const card = checkbox.closest(".asset-card");
        if (card) card.classList.toggle("is-selected", checkbox.checked);
      });
      countLabels.forEach((label) => {
        label.textContent = selected === 0
          ? "Select one or more image cards below."
          : `${selected} image${selected === 1 ? "" : "s"} selected${hiddenSelected ? ` · ${hiddenSelected} hidden by filter` : ""}.`;
      });
      form.classList.toggle("has-selection", selected > 0);
      actionButtons.forEach((button) => { button.disabled = selected === 0; });

      const selectedCards = selectedCheckboxes
        .map((checkbox) => checkbox.closest(".asset-card"))
        .filter(Boolean);
      const hasSevereSelection = selectedCards.some((card) => card.dataset.aiExcluded === "true");
      const hasUnacceptedSelection = selectedCards.some((card) => card.dataset.decision !== "accept");
      const netNewAccepts = selectedCards.filter((card) => card.dataset.decision !== "accept").length;
      const acceptedAlreadyOverGoal = acceptedCount > reviewTarget;
      const exceedsReviewGoal = acceptedAlreadyOverGoal || netNewAccepts > remainingReviewSlots;
      const selectedForX = selectedCards.filter((card) => card.dataset.selectedForX === "true").length;
      const netNewForX = selected - selectedForX;
      const remainingXSlots = Math.max(0, xCapacity - currentXCount);
      const exceedsXCapacity = netNewForX > remainingXSlots;

      if (acceptButton) {
        acceptButton.disabled = selected === 0 || hasSevereSelection || exceedsReviewGoal;
      }
      if (xAddButton) {
        xAddButton.disabled = selected === 0
          || netNewForX === 0
          || exceedsXCapacity
          || hasUnacceptedSelection;
      }
      if (xRemoveButton) xRemoveButton.disabled = selectedForX === 0;

      if (selectionStatus) {
        const messages = [];
        if (hasSevereSelection) {
          messages.push("AI-excluded images need individual owner review before acceptance.");
        }
        if (acceptedAlreadyOverGoal) {
          const excess = acceptedCount - reviewTarget;
          messages.push(
            `The final set is ${excess} image${excess === 1 ? "" : "s"} over its maximum; remove kept images before restoring more.`,
          );
        } else if (exceedsReviewGoal) {
          messages.push(
            `Only ${remainingReviewSlots} final-set slot${remainingReviewSlots === 1 ? " remains" : "s remain"}; deselect ${netNewAccepts - remainingReviewSlots}.`,
          );
        }
        if (exceedsXCapacity) {
          messages.push(
            `Only ${remainingXSlots} X slot${remainingXSlots === 1 ? " remains" : "s remain"}; deselect ${netNewForX - remainingXSlots}.`,
          );
        }
        if (hasUnacceptedSelection) {
          messages.push("Keep every selected image before adding it to X.");
        }
        selectionStatus.hidden = messages.length === 0;
        selectionStatus.textContent = messages.join(" ");
      }
    };

    checkboxes.forEach((checkbox) => {
      checkbox.addEventListener("click", (event) => {
        if (event.shiftKey && lastClickedCheckbox && lastClickedCheckbox !== checkbox) {
          const visible = checkboxes.filter((item) => {
            const card = item.closest(".asset-card");
            return !card || !card.hidden;
          });
          const start = visible.indexOf(lastClickedCheckbox);
          const end = visible.indexOf(checkbox);
          if (start >= 0 && end >= 0) {
            visible.slice(Math.min(start, end), Math.max(start, end) + 1)
              .forEach((item) => { item.checked = checkbox.checked; });
          }
        }
        lastClickedCheckbox = checkbox;
      });
      checkbox.addEventListener("change", updateSelection);
    });
    document.querySelectorAll("[data-select-all]").forEach((button) => {
      button.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => {
          const card = checkbox.closest(".asset-card");
          if (!card || !card.hidden) checkbox.checked = true;
        });
        updateSelection();
      });
    });
    selectToTargetButtons.forEach((button) => {
      button.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => { checkbox.checked = false; });
        let remaining = remainingReviewSlots;
        checkboxes.forEach((checkbox) => {
          if (remaining === 0) return;
          const card = checkbox.closest(".asset-card");
          if (
            card
            && !card.hidden
            && card.dataset.decision === "undecided"
            && card.dataset.aiExcluded !== "true"
          ) {
            checkbox.checked = true;
            remaining -= 1;
          }
        });
        updateSelection();
      });
    });
    document.querySelectorAll("[data-clear-selection]").forEach((button) => {
      button.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => { checkbox.checked = false; });
        updateSelection();
      });
    });
    document.querySelectorAll("[data-clear-hidden-selection]").forEach((button) => {
      button.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => {
          const card = checkbox.closest(".asset-card");
          if (card && card.hidden) checkbox.checked = false;
        });
        updateSelection();
      });
    });
    if (acceptUndecidedButton instanceof HTMLButtonElement && acceptButton) {
      acceptUndecidedButton.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => {
          const card = checkbox.closest(".asset-card");
          checkbox.checked = Boolean(
            card
            && card.dataset.decision === "undecided"
            && card.dataset.aiExcluded !== "true",
          );
        });
        const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
        if (selected === 0) {
          if (selectionStatus) {
            selectionStatus.textContent = "There are no undecided images available to keep.";
            selectionStatus.hidden = false;
          }
          return;
        }
        if (bulkReasonInput instanceof HTMLInputElement) {
          bulkReasonInput.value = "sorting_default_accept";
        }
        updateSelection();
        // Legacy reviews can contain more masters than the final-set maximum. Cull mode keeps
        // them first and asks the owner to reject down to the configured maximum.
        acceptButton.disabled = false;
        form.requestSubmit(acceptButton);
      });
    }
    document.querySelectorAll("[data-review-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        const filter = button.dataset.reviewFilter;
        document.querySelectorAll("[data-review-filter]").forEach((item) => {
          item.classList.toggle("active", item === button);
          item.setAttribute("aria-pressed", String(item === button));
        });
        document.querySelectorAll(".asset-card[data-decision]").forEach((card) => {
          const matches = filter === "all"
            || card.dataset.decision === filter
            || (filter === "x" && card.dataset.selectedForX === "true")
            || (filter === "semantic" && card.dataset.semanticFlagged === "true");
          card.hidden = !matches;
        });
        updateExcludedHeadings();
        updateSelection();
      });
    });
    updateExcludedHeadings();
    updateSelection();
  }

  let reviewActionRequestActive = false;
  let reviewCompletionRequestActive = false;

  const reviewActionLabel = (form, submitter, selectedCount) => {
    const decision = submitter instanceof HTMLButtonElement
      ? submitter.dataset.decision || ""
      : "";
    if (decision === "accept") return "Image restored to the final set. Your position was preserved.";
    if (decision === "reject") {
      const anatomyToggle = form.querySelector("[data-anatomy-training-toggle]");
      return anatomyToggle instanceof HTMLInputElement && anatomyToggle.checked
        ? "Image removed with a provisional anatomy label. Learning waits until the set is finished."
        : "Image removed from the final set. The raw master was retained.";
    }
    if (decision === "hold") return "Image held for another pass.";

    if (form.matches("[data-bulk-action-form]")) {
      const action = submitter instanceof HTMLButtonElement ? submitter.value : "";
      const count = Math.max(1, selectedCount);
      const images = `${count} image${count === 1 ? "" : "s"}`;
      if (action === "accept") return `${images} restored to the final set. Your position was preserved.`;
      if (action === "reject") return `${images} removed from the final set. Raw masters were retained.`;
      if (action === "hold") return `${images} held for another pass.`;
      if (action === "x_add") return `${images} added to the X teaser selection.`;
      if (action === "x_remove") return `${images} removed from the X teaser selection.`;
    }

    if (form.matches("[data-x-selection-form]")) {
      const selected = form.querySelector('input[name="selected"]');
      return selected instanceof HTMLInputElement && selected.value === "true"
        ? "Image added to the X teaser selection."
        : "Image removed from the X teaser selection.";
    }
    if (form.matches("[data-anatomy-feedback-form]")) {
      return "Anatomy feedback saved and learning metrics refreshed.";
    }
    return "Review updated. Your position was preserved.";
  };

  const reviewScrollAnchor = (form) => {
    const directCard = form.closest("[data-review-asset]");
    const cards = Array.from(document.querySelectorAll("[data-review-asset]"));
    const card = directCard || cards.find((candidate) => {
      if (candidate.hidden) return false;
      const bounds = candidate.getBoundingClientRect();
      return bounds.bottom > 96 && bounds.top < window.innerHeight;
    });
    return {
      assetId: card instanceof HTMLElement ? card.dataset.assetId || "" : "",
      top: card instanceof HTMLElement ? card.getBoundingClientRect().top : 0,
      x: window.scrollX,
      y: window.scrollY,
    };
  };

  const captureReviewViewState = (form, submitter) => {
    const activeFilter = document.querySelector("[data-review-filter].active");
    const activeSort = document.querySelector("[data-asset-sort].active");
    const formCard = form.closest("[data-review-asset]");
    return {
      activeFilter: activeFilter instanceof HTMLElement
        ? activeFilter.dataset.reviewFilter || "all"
        : "all",
      activeSort: activeSort instanceof HTMLElement
        ? activeSort.dataset.assetSort || "quality"
        : "quality",
      anchor: reviewScrollAnchor(form),
      selectedAssetIds: Array.from(
        document.querySelectorAll('input[type="checkbox"][name="asset_id"]:checked'),
      ).map((input) => input.value),
      clearSelection: form.matches("[data-bulk-action-form]"),
      focusAssetId: formCard instanceof HTMLElement ? formCard.dataset.assetId || "" : "",
      focusDecision: submitter instanceof HTMLButtonElement
        ? submitter.dataset.decision || ""
        : "",
      focusAction: submitter instanceof HTMLButtonElement && submitter.name === "action"
        ? submitter.value
        : "",
    };
  };

  const preserveReviewMedia = (workspace, nextWorkspace) => {
    const currentImages = new Map();
    workspace.querySelectorAll("[data-review-asset]").forEach((card) => {
      const assetId = card.dataset.assetId || "";
      const image = card.querySelector(".asset-preview");
      if (assetId && image instanceof HTMLImageElement) currentImages.set(assetId, image);
    });

    nextWorkspace.querySelectorAll("[data-review-asset]").forEach((card) => {
      const assetId = card.dataset.assetId || "";
      const currentImage = currentImages.get(assetId);
      const nextImage = card.querySelector(".asset-preview");
      if (!(currentImage instanceof HTMLImageElement)
          || !(nextImage instanceof HTMLImageElement)) return;

      // Moving the existing node retains its decoded bitmap and browser cache state.
      // The surrounding card still comes from the authoritative server response.
      nextImage.replaceWith(currentImage);
    });

    const currentGrid = workspace.querySelector("[data-review-grid]");
    const nextGrid = nextWorkspace.querySelector("[data-review-grid]");
    if (currentGrid instanceof HTMLElement && nextGrid instanceof HTMLElement) {
      const density = currentGrid.dataset.assetDensity;
      if (density) nextGrid.dataset.assetDensity = density;
    }

    const densityControls = workspace.querySelector("[data-asset-density-controls]");
    if (densityControls instanceof HTMLElement && nextGrid?.parentNode) {
      nextGrid.parentNode.insertBefore(densityControls, nextGrid);
    }
  };

  const restoreReviewViewState = (workspace, state) => {
    initializeAssetSorting();
    initializeBulkReview();

    const sortButton = Array.from(workspace.querySelectorAll("[data-asset-sort]")).find(
      (button) => button.dataset.assetSort === state.activeSort,
    );
    if (sortButton instanceof HTMLButtonElement) sortButton.click();

    if (!state.clearSelection) {
      state.selectedAssetIds.forEach((assetId) => {
        const card = document.getElementById(`asset-${assetId}`);
        const checkbox = card?.querySelector('input[type="checkbox"][name="asset_id"]');
        if (checkbox instanceof HTMLInputElement) checkbox.checked = true;
      });
      const restored = workspace.querySelector('input[type="checkbox"][name="asset_id"]:checked');
      if (restored instanceof HTMLInputElement) {
        restored.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    const filterButton = Array.from(workspace.querySelectorAll("[data-review-filter]")).find(
      (button) => button.dataset.reviewFilter === state.activeFilter,
    );
    if (filterButton instanceof HTMLButtonElement) filterButton.click();

    document.dispatchEvent(new CustomEvent("gen-automation:assets-updated", {
      detail: { root: workspace },
    }));
    document.dispatchEvent(new CustomEvent("gen-automation:review-updated", {
      detail: { root: workspace },
    }));

    const restoreScroll = () => {
      const anchor = state.anchor.assetId
        ? document.getElementById(`asset-${state.anchor.assetId}`)
        : null;
      if (anchor instanceof HTMLElement && !anchor.hidden) {
        const delta = anchor.getBoundingClientRect().top - state.anchor.top;
        window.scrollBy(0, delta);
      } else {
        window.scrollTo(state.anchor.x, state.anchor.y);
      }
    };
    // All updates above happen in the same task. Restore before the browser paints,
    // then make one post-layout correction for controls added by update listeners.
    restoreScroll();
    window.requestAnimationFrame(restoreScroll);

    let focusTarget = null;
    if (state.focusAssetId) {
      const card = document.getElementById(`asset-${state.focusAssetId}`);
      if (state.focusDecision) {
        focusTarget = Array.from(card?.querySelectorAll("[data-decision]") || []).find(
          (button) => button.dataset.decision === state.focusDecision,
        );
      } else {
        focusTarget = card?.querySelector("button[type=submit]") || null;
      }
    } else if (state.focusAction) {
      focusTarget = Array.from(workspace.querySelectorAll('button[name="action"]')).find(
        (button) => button.value === state.focusAction,
      );
    }
    if (focusTarget instanceof HTMLElement && !focusTarget.closest("[hidden]")) {
      window.requestAnimationFrame(() => focusTarget.focus({ preventScroll: true }));
    }
  };

  const reviewActionStatusTimers = new WeakMap();

  const showReviewActionStatus = (workspace, message, isError = false, autoHideMs = null) => {
    const status = workspace.querySelector("[data-review-action-status]");
    if (!(status instanceof HTMLElement)) return;
    const existingTimer = reviewActionStatusTimers.get(status);
    if (existingTimer !== undefined) window.clearTimeout(existingTimer);
    reviewActionStatusTimers.delete(status);
    status.textContent = message;
    status.classList.toggle("error", isError);
    status.hidden = false;
    const hideAfter = autoHideMs === null ? (isError ? 6000 : 2600) : autoHideMs;
    if (hideAfter > 0) {
      reviewActionStatusTimers.set(status, window.setTimeout(() => {
        reviewActionStatusTimers.delete(status);
        if (status.isConnected) status.hidden = true;
      }, hideAfter));
    }
  };

  const reviewDecisionPersistence = {
    active: null,
    dirty: false,
    drainPromise: null,
    generation: 0,
    idleWaiters: [],
    lockVersion: 0,
    needsRebase: false,
    paused: false,
    queue: [],
    reconcilePromise: null,
    reconcileTimer: null,
    refreshPromise: null,
    taskId: "",
    workspace: null,
  };

  const reviewViewerIsOpen = () => Boolean(
    document.body.classList.contains("asset-viewer-open")
    || document.querySelector("[data-asset-viewer][open]"),
  );

  const currentReviewWorkspace = () => document.querySelector("[data-review-workspace]");

  const bindReviewDecisionWorkspace = (workspace) => {
    if (!(workspace instanceof HTMLElement)) return null;
    const taskId = workspace.dataset.reviewTaskId || "";
    if (!taskId) return null;
    const state = reviewDecisionPersistence;
    if (state.taskId && state.taskId !== taskId && (state.active || state.queue.length > 0)) {
      return null;
    }
    if (state.taskId !== taskId) {
      state.taskId = taskId;
      state.queue = [];
      state.active = null;
      state.dirty = false;
      state.generation = 0;
      state.needsRebase = false;
      state.paused = false;
    }
    state.workspace = workspace;
    if (!state.dirty && !state.active && state.queue.length === 0) {
      state.lockVersion = integerValue(workspace.dataset.reviewLockVersion, 0);
    }
    return state;
  };

  const reviewDecisionIdempotencyKey = () => {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `review-ui-${window.crypto.randomUUID()}`;
    }
    const random = Math.random().toString(16).slice(2);
    return `review-ui-${Date.now().toString(16)}-${random}`;
  };

  const decisionCountKey = (decision) => ({
    accept: "reviewAcceptedCount",
    hold: "reviewHeldCount",
    reject: "reviewRejectedCount",
    undecided: "reviewUndecidedCount",
  }[decision] || "reviewUndecidedCount");

  const updateReviewCount = (workspace, decision, delta) => {
    const key = decisionCountKey(decision);
    workspace.dataset[key] = String(Math.max(0, integerValue(workspace.dataset[key], 0) + delta));
  };

  const refreshOptimisticReviewCounts = (workspace) => {
    if (!(workspace instanceof HTMLElement)) return;
    const accepted = integerValue(workspace.dataset.reviewAcceptedCount, 0);
    const rejected = integerValue(workspace.dataset.reviewRejectedCount, 0);
    const target = Math.max(1, integerValue(workspace.dataset.reviewTarget, accepted || 1));
    const acceptedNode = workspace.querySelector('[data-review-count="accept"]');
    const rejectedNode = workspace.querySelector('[data-review-count="reject"]');
    if (acceptedNode) acceptedNode.textContent = String(accepted);
    if (rejectedNode) rejectedNode.textContent = String(rejected);
    const acceptedFilter = workspace.querySelector('[data-review-filter="accept"] span');
    const rejectedFilter = workspace.querySelector('[data-review-filter="reject"] span');
    if (acceptedFilter) acceptedFilter.textContent = String(accepted);
    if (rejectedFilter) rejectedFilter.textContent = String(rejected);
    const keptSummary = workspace.querySelector("[data-review-kept-summary]");
    if (keptSummary) keptSummary.textContent = `${accepted} / ${target} kept`;
    const progress = workspace.querySelector(".review-target-progress progress");
    if (progress instanceof HTMLProgressElement) {
      progress.value = Math.min(accepted, target);
      progress.textContent = `${accepted} / ${target}`;
    }
    const bulkForm = workspace.querySelector("[data-bulk-action-form]");
    if (bulkForm instanceof HTMLFormElement) bulkForm.dataset.acceptedCount = String(accepted);
    const finishButton = workspace.querySelector('[data-review-complete-form] button[type="submit"]');
    if (finishButton instanceof HTMLButtonElement) {
      finishButton.textContent = `Finish set with ${accepted} image${accepted === 1 ? "" : "s"}`;
      finishButton.disabled = accepted < 1
        || accepted > target
        || finishButton.dataset.reviewNonCountReady !== "true";
    }
  };

  const anatomyReasonFor = (form, decision) => {
    const anatomyToggle = form.querySelector("[data-anatomy-training-toggle]");
    const anatomyIssue = form.querySelector("[data-anatomy-training-issue]");
    const reasonInput = form.querySelector('input[name="reason_code"]');
    const anatomyRequested = decision === "reject"
      && anatomyToggle instanceof HTMLInputElement
      && anatomyToggle.checked;
    const anatomyReasons = new Set([
      "anatomy",
      ...Array.from(
        anatomyIssue instanceof HTMLSelectElement ? anatomyIssue.options : [],
      ).map((option) => option.value),
    ]);
    let reason = reasonInput instanceof HTMLInputElement ? reasonInput.value : "";
    if (anatomyRequested) {
      reason = anatomyIssue instanceof HTMLSelectElement
        ? anatomyIssue.value || "anatomy"
        : "anatomy";
    } else if (
      anatomyReasons.has(reason)
      || reason === "sorting_default_accept"
      || (decision !== "accept" && reason === "semantic_severe_override")
    ) {
      reason = "";
    }
    if (reasonInput instanceof HTMLInputElement) reasonInput.value = reason;
    return { anatomyRequested, reason, anatomyReasons };
  };

  const reviewCardSnapshot = (card) => {
    const form = card.querySelector("form[data-review-decision-form]");
    const reason = form?.querySelector('input[name="reason_code"]');
    const anatomyToggle = form?.querySelector("[data-anatomy-training-toggle]");
    const anatomyIssue = form?.querySelector("[data-anatomy-training-issue]");
    const previousAnatomyChecked = form instanceof HTMLFormElement
      ? form.dataset.reviewPreviousAnatomyChecked
      : undefined;
    return {
      anatomyChecked: previousAnatomyChecked === "true"
        ? true
        : previousAnatomyChecked === "false"
          ? false
          : anatomyToggle instanceof HTMLInputElement ? anatomyToggle.checked : false,
      anatomyIssue: anatomyIssue instanceof HTMLSelectElement ? anatomyIssue.value : "",
      anatomySavedIssue: anatomyIssue instanceof HTMLSelectElement
        ? anatomyIssue.dataset.savedAnatomyIssue || ""
        : "",
      decision: card.dataset.decision || "undecided",
      reason: reason instanceof HTMLInputElement ? reason.value : "",
    };
  };

  const applyActiveReviewFilterToCard = (workspace, card) => {
    if (!(workspace instanceof HTMLElement) || !(card instanceof HTMLElement)) return;
    const activeFilter = workspace.querySelector("[data-review-filter].active");
    if (!(activeFilter instanceof HTMLElement)) return;
    const filter = activeFilter.dataset.reviewFilter || "all";
    const matches = filter === "all"
      || card.dataset.decision === filter
      || (filter === "x" && card.dataset.selectedForX === "true")
      || (filter === "semantic" && card.dataset.semanticFlagged === "true");
    card.hidden = !matches;
  };

  const applyReviewCardDecision = (card, decision, reason = "") => {
    if (!(card instanceof HTMLElement)) return;
    const previousDecision = card.dataset.decision || "undecided";
    const workspace = card.closest("[data-review-workspace]");
    if (workspace instanceof HTMLElement && previousDecision !== decision) {
      updateReviewCount(workspace, previousDecision, -1);
      updateReviewCount(workspace, decision, 1);
    }
    card.dataset.decision = decision;
    ["accept", "reject", "hold", "undecided"].forEach((value) => {
      card.classList.toggle(`decision-${value}`, value === decision);
    });
    const chip = card.querySelector("[data-review-decision-chip]");
    if (chip instanceof HTMLElement) {
      ["accept", "reject", "hold", "undecided"].forEach((value) => {
        chip.classList.toggle(value, value === decision);
      });
      chip.textContent = decision === "accept"
        ? "kept"
        : decision === "reject" ? "rejected" : decision;
    }
    card.querySelectorAll("button[data-decision]").forEach((button) => {
      if (button instanceof HTMLButtonElement) button.hidden = button.dataset.decision === decision;
    });
    const form = card.querySelector("form[data-review-decision-form]");
    const reasonInput = form?.querySelector('input[name="reason_code"]');
    if (reasonInput instanceof HTMLInputElement) reasonInput.value = reason;
    const anatomyToggle = form?.querySelector("[data-anatomy-training-toggle]");
    const anatomyIssue = form?.querySelector("[data-anatomy-training-issue]");
    const allowedAnatomyReasons = new Set(Array.from(
      anatomyIssue instanceof HTMLSelectElement ? anatomyIssue.options : [],
    ).map((option) => option.value));
    const anatomyReason = decision === "reject" && allowedAnatomyReasons.has(reason)
      ? reason
      : "";
    if (anatomyToggle instanceof HTMLInputElement) anatomyToggle.checked = Boolean(anatomyReason);
    if (anatomyIssue instanceof HTMLSelectElement) {
      anatomyIssue.value = anatomyReason || "anatomy";
      anatomyIssue.dataset.savedAnatomyIssue = anatomyReason;
    }
    const anatomyChip = card.querySelector("[data-anatomy-provisional-chip]");
    if (anatomyChip instanceof HTMLElement) {
      anatomyChip.hidden = !anatomyReason;
      anatomyChip.textContent = anatomyReason
        ? `Anatomy: ${anatomyReason.replaceAll("_", " ")} · provisional`
        : "";
    }
    if (workspace instanceof HTMLElement) {
      applyActiveReviewFilterToCard(workspace, card);
      refreshOptimisticReviewCounts(workspace);
    }
  };

  const restoreReviewCardSnapshot = (card, snapshot) => {
    applyReviewCardDecision(card, snapshot.decision, snapshot.reason);
    const form = card.querySelector("form[data-review-decision-form]");
    const anatomyToggle = form?.querySelector("[data-anatomy-training-toggle]");
    const anatomyIssue = form?.querySelector("[data-anatomy-training-issue]");
    if (anatomyToggle instanceof HTMLInputElement) anatomyToggle.checked = snapshot.anatomyChecked;
    if (anatomyIssue instanceof HTMLSelectElement) {
      anatomyIssue.value = snapshot.anatomyIssue;
      anatomyIssue.dataset.savedAnatomyIssue = snapshot.anatomySavedIssue;
    }
  };

  const updateReviewSaveStatus = (message = "", stateName = "") => {
    const state = reviewDecisionPersistence;
    const workspace = state.workspace?.isConnected ? state.workspace : currentReviewWorkspace();
    if (!(workspace instanceof HTMLElement)) return;
    const status = workspace.querySelector("[data-review-save-status]");
    if (!(status instanceof HTMLElement)) return;
    const pending = state.queue.length;
    status.hidden = !message && pending === 0;
    status.textContent = message || (pending
      ? `Saving ${pending} change${pending === 1 ? "" : "s"}…`
      : "");
    status.classList.toggle("is-retrying", stateName === "retrying");
    status.classList.toggle("is-error", stateName === "error");
  };

  const setReviewCardPending = (assetId, pending, stateName = "pending") => {
    const card = document.getElementById(`asset-${assetId}`);
    if (!(card instanceof HTMLElement)) return;
    const count = Math.max(0, integerValue(card.dataset.reviewPendingCount, 0) + pending);
    card.dataset.reviewPendingCount = String(count);
    if (count > 0) {
      card.dataset.reviewSaveState = stateName;
    } else {
      delete card.dataset.reviewSaveState;
      delete card.dataset.reviewPendingCount;
    }
    const chip = card.querySelector("[data-review-save-chip]");
    if (chip instanceof HTMLElement) {
      chip.hidden = count === 0;
      chip.textContent = stateName === "retrying" ? "Waiting to save" : "Saving";
      chip.classList.toggle("is-retrying", stateName === "retrying");
    }
  };

  const resolveReviewDecisionIdle = () => {
    const state = reviewDecisionPersistence;
    if (state.active || state.queue.length > 0) return;
    const waiters = state.idleWaiters.splice(0);
    waiters.forEach((resolve) => resolve());
  };

  const waitForReviewDecisionIdle = () => {
    const state = reviewDecisionPersistence;
    if (!state.active && state.queue.length === 0) return Promise.resolve();
    return new Promise((resolve) => state.idleWaiters.push(resolve));
  };

  const retryDelay = (retryCount) => REVIEW_DECISION_RETRY_DELAYS_MS[
    Math.min(retryCount, REVIEW_DECISION_RETRY_DELAYS_MS.length - 1)
  ];

  const delay = (milliseconds) => new Promise((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });

  const compactReviewSummary = async (state) => {
    try {
      const response = await fetch(`/api/v1/review-tasks/${encodeURIComponent(state.taskId)}`, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return false;
      const payload = await response.json();
      if (!isRecord(payload) || !Array.isArray(payload.assets)) return false;
      state.lockVersion = integerValue(payload.lock_version, state.lockVersion);
      const workspace = state.workspace?.isConnected ? state.workspace : currentReviewWorkspace();
      if (!(workspace instanceof HTMLElement)) return false;
      payload.assets.forEach((asset) => {
        if (!isRecord(asset) || !asset.asset_id) return;
        const card = document.getElementById(`asset-${asset.asset_id}`);
        if (card instanceof HTMLElement) {
          applyReviewCardDecision(card, asset.decision || "undecided", asset.reason_code || "");
        }
      });
      workspace.dataset.reviewLockVersion = String(state.lockVersion);
      workspace.dataset.reviewAcceptedCount = String(integerValue(payload.accepted_count, 0));
      workspace.dataset.reviewRejectedCount = String(integerValue(payload.rejected_count, 0));
      workspace.dataset.reviewHeldCount = String(integerValue(payload.held_count, 0));
      workspace.dataset.reviewUndecidedCount = String(integerValue(payload.undecided_count, 0));
      refreshOptimisticReviewCounts(workspace);
      return true;
    } catch (_error) {
      return false;
    }
  };

  const rejectQueuedReviewDecisions = async (reason) => {
    const state = reviewDecisionPersistence;
    const rejected = state.queue.splice(0);
    state.active = null;
    [...rejected].reverse().forEach((command) => {
      const card = document.getElementById(`asset-${command.assetId}`);
      if (card instanceof HTMLElement) restoreReviewCardSnapshot(card, command.snapshot);
    });
    rejected.forEach((command) => {
      setReviewCardPending(command.assetId, -1);
      document.dispatchEvent(new CustomEvent("gen-automation:review-action-settled", {
        detail: {
          assetId: command.assetId,
          anatomyRequested: command.anatomyRequested,
          commandId: command.id,
          decision: command.decision,
          reason,
          success: false,
        },
      }));
    });
    state.paused = true;
    state.needsRebase = false;
    updateReviewSaveStatus("Review changed elsewhere; reconciling before saving more…", "error");
    const reconciled = await compactReviewSummary(state);
    state.paused = !reconciled;
    state.dirty = true;
    if (reconciled) {
      if (state.queue.length > 0) applyRebasedReviewDecisions(state);
      updateReviewSaveStatus("Review reconciled. New changes can continue.");
      if (state.queue.length > 0) void drainReviewDecisionQueue();
    } else {
      state.needsRebase = state.queue.length > 0;
      updateReviewSaveStatus("Could not reconcile yet. Keep reviewing; saving will retry.", "error");
      scheduleReviewDecisionReconciliation();
    }
    resolveReviewDecisionIdle();
  };

  const applyRebasedReviewDecisions = (state) => {
    state.queue.forEach((command) => {
      const card = document.getElementById(`asset-${command.assetId}`);
      if (card instanceof HTMLElement) {
        command.snapshot = reviewCardSnapshot(card);
        applyReviewCardDecision(card, command.decision, command.reason);
      }
      command.expectedLockVersion = null;
      command.idempotencyKey = reviewDecisionIdempotencyKey();
      command.requestBody = null;
      command.retryCount = 0;
    });
    state.needsRebase = false;
  };

  const rebaseQueuedReviewDecisions = async () => {
    const state = reviewDecisionPersistence;
    state.active = null;
    state.paused = true;
    state.needsRebase = true;
    updateReviewSaveStatus(
      "Review state changed while saving; reconciling queued choices\u2026",
      "retrying",
    );
    const reconciled = await compactReviewSummary(state);
    if (!reconciled) {
      updateReviewSaveStatus(
        "Queued choices are safe and waiting for the review state to reconnect.",
        "retrying",
      );
      scheduleReviewDecisionReconciliation();
      return;
    }
    applyRebasedReviewDecisions(state);
    state.paused = false;
    updateReviewSaveStatus();
    void drainReviewDecisionQueue();
  };

  function scheduleReviewDecisionReconciliation(delayMs = 2500) {
    const state = reviewDecisionPersistence;
    if (!state.paused || state.reconcilePromise || state.reconcileTimer !== null) return;
    state.reconcileTimer = window.setTimeout(() => {
      state.reconcileTimer = null;
      let reconciled = false;
      state.reconcilePromise = compactReviewSummary(state).then((result) => {
        if (result) {
          reconciled = true;
          if (state.needsRebase) applyRebasedReviewDecisions(state);
          state.paused = false;
          updateReviewSaveStatus();
          void drainReviewDecisionQueue();
        }
      }).finally(() => {
        state.reconcilePromise = null;
        if (!reconciled) scheduleReviewDecisionReconciliation(5000);
      });
    }, delayMs);
  }

  const sendReviewDecision = async (command, state) => {
    if (!command.requestBody) {
      command.expectedLockVersion = state.lockVersion;
      command.requestBody = JSON.stringify({
        asset_id: command.assetId,
        decision: command.decision,
        expected_lock_version: command.expectedLockVersion,
        reason_code: command.reason || null,
        note: command.note || null,
      });
    }
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeoutId = controller ? window.setTimeout(
      () => controller.abort(),
      REVIEW_DECISION_REQUEST_TIMEOUT_MS,
    ) : null;
    const options = {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "follow",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": command.idempotencyKey,
        "X-CSRF-Token": command.csrfToken,
      },
      body: command.requestBody,
    };
    if (controller) options.signal = controller.signal;
    try {
      const response = await fetch(
        `/api/v1/review-tasks/${encodeURIComponent(state.taskId)}/decisions`,
        options,
      );
      if (response.status === 401) return { kind: "authentication", response };
      if (response.status >= 500 || [408, 425, 429].includes(response.status)) {
        return { kind: "retry" };
      }
      if (!response.ok) return { kind: "definitive", response };
      const payload = await response.json();
      if (
        !isRecord(payload)
        || String(payload.asset_id || "") !== command.assetId
        || payload.decision !== command.decision
        || integerValue(payload.task_lock_version, 0) <= command.expectedLockVersion
      ) {
        return { kind: "retry" };
      }
      return { kind: "success", payload };
    } catch (_error) {
      return { kind: "retry" };
    } finally {
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    }
  };

  const drainReviewDecisionQueue = () => {
    const state = reviewDecisionPersistence;
    if (
      state.drainPromise
      || state.paused
      || reviewActionRequestActive
      || reviewCompletionRequestActive
    ) return state.drainPromise || Promise.resolve();
    state.drainPromise = (async () => {
      while (!state.paused && state.queue.length > 0) {
        const command = state.queue[0];
        state.active = command;
        const result = await sendReviewDecision(command, state);
        if (result.kind === "retry") {
          command.retryCount += 1;
          setReviewCardPending(command.assetId, 0, "retrying");
          updateReviewSaveStatus(
            `Connection interrupted. ${state.queue.length} change${state.queue.length === 1 ? "" : "s"} waiting; retrying…`,
            "retrying",
          );
          await delay(retryDelay(command.retryCount));
          continue;
        }
        if (result.kind === "authentication") {
          persistSamePageScroll();
          window.location.assign("/login");
          return;
        }
        if (result.kind === "definitive") {
          if (result.response?.status === 409) {
            await rebaseQueuedReviewDecisions();
          } else {
            await rejectQueuedReviewDecisions("request");
          }
          return;
        }

        state.lockVersion = integerValue(result.payload.task_lock_version, state.lockVersion + 1);
        state.queue.shift();
        state.active = null;
        command.retryCount = 0;
        setReviewCardPending(command.assetId, -1);
        document.dispatchEvent(new CustomEvent("gen-automation:review-action-settled", {
          detail: {
            assetId: command.assetId,
            anatomyRequested: command.anatomyRequested,
            commandId: command.id,
            decision: command.decision,
            success: true,
          },
        }));
        updateReviewSaveStatus();
      }
    })().finally(() => {
      state.active = null;
      state.drainPromise = null;
      resolveReviewDecisionIdle();
      if (!state.paused && state.queue.length > 0) void drainReviewDecisionQueue();
    });
    return state.drainPromise;
  };

  const enqueueReviewDecision = (form, submitter, workspace) => {
    const state = bindReviewDecisionWorkspace(workspace);
    const card = form.closest("[data-review-asset]");
    const assetIdField = form.querySelector('input[name="asset_id"]');
    const csrfField = form.querySelector('input[name="csrf_token"]');
    const assetId = form.dataset.assetId
      || (assetIdField instanceof HTMLInputElement ? assetIdField.value : "");
    const decision = submitter instanceof HTMLButtonElement
      ? submitter.dataset.decision || submitter.value
      : "";
    if (!state
        || !(card instanceof HTMLElement)
        || !(csrfField instanceof HTMLInputElement)
        || !csrfField.value
        || !assetId
        || !["accept", "reject", "hold"].includes(decision)) return false;

    const snapshot = reviewCardSnapshot(card);
    const { anatomyRequested, reason } = anatomyReasonFor(form, decision);
    const noteField = form.querySelector('[name="note"]');
    const commandId = reviewDecisionIdempotencyKey();
    const command = {
      anatomyRequested,
      assetId,
      csrfToken: csrfField.value,
      decision,
      expectedLockVersion: null,
      id: commandId,
      idempotencyKey: commandId,
      note: noteField instanceof HTMLInputElement || noteField instanceof HTMLTextAreaElement
        ? noteField.value
        : "",
      reason,
      requestBody: null,
      retryCount: 0,
      snapshot,
    };
    applyReviewCardDecision(card, decision, reason);
    setReviewCardPending(assetId, 1);
    state.queue.push(command);
    state.dirty = true;
    state.generation += 1;
    updateReviewSaveStatus();
    document.dispatchEvent(new CustomEvent("gen-automation:review-action-optimistic", {
      detail: {
        anatomyRequested,
        assetId,
        commandId: command.id,
        decision,
        reason,
      },
    }));
    void drainReviewDecisionQueue();
    return true;
  };

  async function ensureReviewAuthoritativeRefresh({ force = false } = {}) {
    const state = reviewDecisionPersistence;
    if (!state.dirty) return true;
    if (state.active || state.queue.length > 0) await waitForReviewDecisionIdle();
    if (!state.dirty) return true;
    if (reviewViewerIsOpen() && !force) return false;
    if (state.refreshPromise) return state.refreshPromise;

    const generation = state.generation;
    const workspace = state.workspace?.isConnected ? state.workspace : currentReviewWorkspace();
    if (!(workspace instanceof HTMLElement)) return false;
    const stateAnchorForm = workspace.querySelector(
      "[data-review-complete-form], [data-bulk-action-form], [data-review-decision-form]",
    );
    let viewState = stateAnchorForm instanceof HTMLFormElement
      ? captureReviewViewState(stateAnchorForm, null)
      : null;
    if (viewState) viewState.clearSelection = false;
    updateReviewSaveStatus("Syncing the reviewed set…");
    state.refreshPromise = (async () => {
      try {
        const response = await fetch(window.location.href, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "text/html", "X-Requested-With": "fetch" },
        });
        if (!response.ok) return false;
        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        const nextWorkspace = parsed.querySelector("[data-review-workspace]");
        if (!(nextWorkspace instanceof HTMLElement)) return false;
        if (
          state.generation !== generation
          || state.active
          || state.queue.length > 0
          || (reviewViewerIsOpen() && !force)
        ) {
          return false;
        }
        const latestAnchorForm = workspace.querySelector(
          "[data-review-complete-form], [data-bulk-action-form], [data-review-decision-form]",
        );
        if (latestAnchorForm instanceof HTMLFormElement) {
          viewState = captureReviewViewState(latestAnchorForm, null);
          viewState.clearSelection = false;
        }
        preserveReviewMedia(workspace, nextWorkspace);
        workspace.replaceWith(nextWorkspace);
        if (viewState) restoreReviewViewState(nextWorkspace, viewState);
        bindReviewDecisionWorkspace(nextWorkspace);
        state.lockVersion = integerValue(nextWorkspace.dataset.reviewLockVersion, state.lockVersion);
        state.dirty = false;
        state.needsRebase = false;
        state.paused = false;
        if (state.reconcileTimer !== null) window.clearTimeout(state.reconcileTimer);
        state.reconcileTimer = null;
        updateReviewSaveStatus();
        return true;
      } catch (_error) {
        return false;
      }
    })().finally(() => {
      state.refreshPromise = null;
    });
    return state.refreshPromise;
  }

  const reviewFormIdentity = (form, submitter) => ({
    action: submitter instanceof HTMLButtonElement && submitter.name === "action"
      ? submitter.value
      : "",
    assetId: form.dataset.assetId || "",
    decision: submitter instanceof HTMLButtonElement ? submitter.dataset.decision || "" : "",
    kind: form.matches("[data-bulk-action-form]")
      ? "bulk"
      : form.matches("[data-x-selection-form]")
        ? "x"
        : form.matches("[data-review-complete-form]")
          ? "complete"
          : form.matches("[data-review-cancel-form]") ? "cancel" : "feedback",
  });

  const findReviewFormAfterRefresh = (identity) => {
    if (identity.kind === "bulk") return document.querySelector("[data-bulk-action-form]");
    if (identity.kind === "complete") return document.querySelector("[data-review-complete-form]");
    if (identity.kind === "cancel") return document.querySelector("[data-review-cancel-form]");
    if (identity.kind === "x") {
      return Array.from(document.querySelectorAll("[data-x-selection-form]")).find(
        (form) => form.dataset.assetId === identity.assetId,
      ) || null;
    }
    return null;
  };

  const findReviewSubmitterAfterRefresh = (form, identity) => {
    if (!(form instanceof HTMLFormElement)) return null;
    if (identity.action) {
      return Array.from(form.querySelectorAll('button[name="action"]')).find(
        (button) => button.value === identity.action,
      ) || null;
    }
    if (identity.decision) {
      return form.querySelector(`button[data-decision="${identity.decision}"]`);
    }
    return form.querySelector('button[type="submit"]');
  };

  const prepareReviewMutationForm = async (form, submitter) => {
    if (form.dataset.reviewQueuePrepared === "true") {
      delete form.dataset.reviewQueuePrepared;
      return true;
    }
    const state = bindReviewDecisionWorkspace(form.closest("[data-review-workspace]"));
    if (!state || (!state.dirty && !state.active && state.queue.length === 0)) return true;
    const identity = reviewFormIdentity(form, submitter);
    await waitForReviewDecisionIdle();
    let refreshed = await ensureReviewAuthoritativeRefresh({ force: true });
    if (!refreshed && state.dirty) refreshed = await ensureReviewAuthoritativeRefresh({ force: true });
    if (!refreshed) {
      updateReviewSaveStatus("Review changes are saved, but controls could not refresh. Try again.", "error");
      return false;
    }
    const freshForm = findReviewFormAfterRefresh(identity);
    const freshSubmitter = findReviewSubmitterAfterRefresh(freshForm, identity);
    if (!(freshForm instanceof HTMLFormElement)) return false;
    freshForm.dataset.reviewQueuePrepared = "true";
    freshForm.requestSubmit(freshSubmitter instanceof HTMLElement ? freshSubmitter : undefined);
    return false;
  };

  window.addEventListener("online", () => {
    const state = reviewDecisionPersistence;
    if (state.paused) scheduleReviewDecisionReconciliation(0);
  });
  window.addEventListener("beforeunload", (event) => {
    const state = reviewDecisionPersistence;
    if (!state.active && state.queue.length === 0) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const attachCompletionInspectionIds = (form, assetIds) => {
    form.querySelectorAll("[data-completion-inspection-id]").forEach((field) => field.remove());
    [...new Set(Array.from(assetIds || [], String).filter(Boolean))].forEach((assetId) => {
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = "inspected_asset_id";
      field.value = assetId;
      field.dataset.completionInspectionId = "";
      form.append(field);
    });
  };

  const captureCompletionInspectionIds = (form) => {
    document.dispatchEvent(new CustomEvent("gen-automation:inspection-completion-handoff", {
      detail: {
        includeAssetIds(assetIds) {
          attachCompletionInspectionIds(form, assetIds);
        },
      },
    }));
  };

  function initializeReviewCompletionInspectionFlush() {
    document.addEventListener("submit", async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.matches("[data-review-complete-form]")) {
        return;
      }
      event.preventDefault();
      const submitter = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
      if (!(await prepareReviewMutationForm(form, submitter))) return;
      if (form.dataset.completionSubmitting === "true") return;

      const workspace = form.closest("[data-review-workspace]");
      const originalLabel = submitter?.textContent || "Finish set";
      form.dataset.completionSubmitting = "true";
      reviewCompletionRequestActive = true;
      if (submitter) {
        submitter.textContent = "Finishing set...";
        submitter.disabled = true;
      }
      if (workspace instanceof HTMLElement) {
        workspace.setAttribute("aria-busy", "true");
        showReviewActionStatus(
          workspace,
          "Finishing set...",
          false,
          0,
        );
      }
      captureCompletionInspectionIds(form);

      const body = new URLSearchParams();
      new FormData(form).forEach((value, name) => {
        if (typeof value === "string") body.append(name, value);
      });
      const configuredAction = new URL(
        form.getAttribute("action") || window.location.href,
        document.baseURI,
      );
      const action = new URL(
        `${configuredAction.pathname}${configuredAction.search}`,
        window.location.origin,
      ).href;
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timeoutId = controller ? window.setTimeout(
        () => controller.abort(),
        REVIEW_COMPLETION_TIMEOUT_MS,
      ) : null;
      let navigationStarted = false;

      try {
        const options = {
          method: "POST",
          credentials: "same-origin",
          redirect: "follow",
          headers: {
            Accept: "text/html",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "fetch",
          },
          body,
        };
        if (controller) options.signal = controller.signal;
        const response = await fetch(action, options);
        const destination = new URL(response.url || action, window.location.href);
        if (response.status === 401 || destination.pathname === "/login") {
          persistSamePageScroll();
          navigationStarted = true;
          window.location.assign(destination.href);
          return;
        }
        if (!response.ok) {
          if (workspace instanceof HTMLElement) {
            showReviewActionStatus(
              workspace,
              response.status === 409 || response.status === 503
                ? "Finishing briefly conflicted with another save. Nothing was lost; click Finish again."
                : "The set could not be finished. Nothing was lost; click Finish again.",
              true,
              0,
            );
          }
          return;
        }
        persistSamePageScroll();
        navigationStarted = true;
        window.location.assign(destination.href);
      } catch (error) {
        const timedOut = error && typeof error === "object" && error.name === "AbortError";
        if (workspace instanceof HTMLElement) {
          showReviewActionStatus(
            workspace,
            timedOut
              ? "Finishing took too long and was stopped safely. Nothing was lost; click Finish again."
              : "The connection was interrupted while finishing. Nothing was lost; click Finish again.",
            true,
            0,
          );
        }
      } finally {
        if (timeoutId !== null) window.clearTimeout(timeoutId);
        if (!navigationStarted && form.isConnected) {
          reviewCompletionRequestActive = false;
          delete form.dataset.completionSubmitting;
          if (workspace instanceof HTMLElement) workspace.removeAttribute("aria-busy");
          if (submitter?.isConnected) {
            submitter.textContent = originalLabel;
            submitter.disabled = false;
          }
          if (reviewDecisionPersistence.queue.length > 0) {
            void drainReviewDecisionQueue();
          }
        }
      }
    });
  }

  function initializeReviewActions() {
    if (!document.querySelector("[data-review-workspace]")) return;

    document.addEventListener("submit", async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.matches(REVIEW_ACTION_FORM_SELECTOR)) {
        return;
      }
      event.preventDefault();

      const workspace = form.closest("[data-review-workspace]");
      if (!(workspace instanceof HTMLElement)) return;
      const submitter = event.submitter instanceof HTMLButtonElement
        ? event.submitter
        : null;
      if (form.matches("[data-review-decision-form]")) {
        enqueueReviewDecision(form, submitter, workspace);
        return;
      }
      if (!(await prepareReviewMutationForm(form, submitter))) return;

      const assetIdField = form.querySelector('input[name="asset_id"]');
      const assetId = form.dataset.assetId
        || (assetIdField instanceof HTMLInputElement ? assetIdField.value : "");
      const decision = submitter?.dataset.decision || "";
      const anatomyRequested = false;

      if (reviewActionRequestActive) {
        showReviewActionStatus(workspace, "The previous review action is still saving.", true);
        document.dispatchEvent(new CustomEvent("gen-automation:review-action-settled", {
          detail: { assetId, anatomyRequested, decision, success: false, reason: "busy" },
        }));
        return;
      }

      const selectedCount = form.matches("[data-bulk-action-form]")
        ? Array.from(
          document.querySelectorAll('input[type="checkbox"][name="asset_id"]:checked'),
        ).filter((input) => input.form === form).length
        : 1;
      const viewState = captureReviewViewState(form, submitter);
      const body = new URLSearchParams();
      new FormData(form).forEach((value, name) => {
        if (typeof value === "string") body.append(name, value);
      });
      if (submitter?.name) body.append(submitter.name, submitter.value);

      reviewActionRequestActive = true;
      let actionSucceeded = false;
      let actionFailure = "request";
      workspace.setAttribute("aria-busy", "true");
      if (submitter) submitter.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          redirect: "follow",
          headers: {
            Accept: "text/html",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "fetch",
          },
          body,
        });
        const destination = new URL(response.url, window.location.href);
        if (response.status === 401 || destination.pathname === "/login") {
          actionFailure = "authentication";
          persistSamePageScroll();
          window.location.assign(destination.href);
          return;
        }
        if (!response.ok) {
          actionFailure = response.status === 409 ? "conflict" : "server";
          showReviewActionStatus(
            workspace,
            response.status === 409
              ? "This review changed in another tab. Refresh once, then continue."
              : "The review action could not be saved. Nothing was changed in this view.",
            true,
          );
          return;
        }

        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        const nextWorkspace = parsed.querySelector("[data-review-workspace]");
        if (!(nextWorkspace instanceof HTMLElement)) {
          actionFailure = "navigation";
          persistSamePageScroll();
          window.location.assign(destination.href);
          return;
        }

        preserveReviewMedia(workspace, nextWorkspace);
        workspace.replaceWith(nextWorkspace);
        restoreReviewViewState(nextWorkspace, viewState);
        const state = bindReviewDecisionWorkspace(nextWorkspace);
        if (state) {
          state.lockVersion = integerValue(
            nextWorkspace.dataset.reviewLockVersion,
            state.lockVersion,
          );
          if (state.queue.length > 0) {
            applyRebasedReviewDecisions(state);
            state.queue.forEach((command) => setReviewCardPending(command.assetId, 1));
            state.dirty = true;
          } else {
            state.dirty = false;
          }
        }
        showReviewActionStatus(
          nextWorkspace,
          reviewActionLabel(form, submitter, selectedCount),
        );
        actionSucceeded = true;
      } catch (_error) {
        actionFailure = "network";
        showReviewActionStatus(
          workspace,
          "The connection was interrupted. Your position is unchanged; try the action again.",
          true,
        );
      } finally {
        reviewActionRequestActive = false;
        if (workspace.isConnected) workspace.removeAttribute("aria-busy");
        if (submitter?.isConnected) submitter.disabled = false;
        document.dispatchEvent(new CustomEvent("gen-automation:review-action-settled", {
          detail: {
            assetId,
            anatomyRequested,
            decision,
            success: actionSucceeded,
            reason: actionSucceeded ? "" : actionFailure,
          },
        }));
        if (reviewDecisionPersistence.queue.length > 0) {
          void drainReviewDecisionQueue();
        }
      }
    });
  }

  const isRecord = (value) => (
    value !== null && typeof value === "object" && !Array.isArray(value)
  );

  const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

  const safeScalar = (value) => (
    typeof value === "string" || typeof value === "number" || typeof value === "boolean"
      ? value
      : undefined
  );

  const safeFields = (value, names) => {
    if (!isRecord(value)) return {};
    const result = {};
    names.forEach((name) => {
      if (!hasOwn(value, name)) return;
      const selected = safeScalar(value[name]);
      if (selected !== undefined) result[name] = selected;
    });
    return result;
  };

  const safeArray = (value, projector) => {
    if (!Array.isArray(value)) return [];
    return value.map(projector).filter((item) => item !== null);
  };

  const safePrompt = (value) => {
    if (!isRecord(value)) return null;
    const prompt = safeFields(value, [
      "source",
      "resolved",
      "source_sha256",
      "resolved_sha256",
      "inherited",
    ]);
    return Object.keys(prompt).length > 0 ? prompt : null;
  };

  function sanitizedGenerationDetails(value) {
    if (!isRecord(value) || value.available !== true) return { available: false };

    const compositionMode = isRecord(value.composition) && value.composition.mode === "duo"
      ? "duo"
      : "single";
    const promptSource = isRecord(value.prompts) ? value.prompts : {};
    const prompts = {};
    [
      "positive",
      "character_a",
      "character_b",
      "negative",
      "detailer_positive",
      "detailer_negative",
    ].forEach((name) => {
      if ((name === "character_a" || name === "character_b") && compositionMode !== "duo") {
        return;
      }
      const prompt = safePrompt(promptSource[name]);
      if (prompt !== null) prompts[name] = prompt;
    });

    const checkpoint = safeFields(value.checkpoint, ["name", "sha256"]);
    const loras = safeArray(value.loras, (item) => {
      const selected = safeFields(item, ["name", "sha256", "weight"]);
      return Object.keys(selected).length > 0 ? selected : null;
    });
    const wildcardSource = isRecord(value.wildcards) ? value.wildcards : {};
    const wildcardVersions = safeArray(wildcardSource.versions, (item) => {
      const selected = safeFields(item, [
        "name",
        "library_id",
        "version_id",
        "version_no",
        "entries_sha256",
        "entry_count",
      ]);
      return Object.keys(selected).length > 0 ? selected : null;
    });
    const wildcardSelections = safeArray(wildcardSource.selections, (item) => {
      const selected = safeFields(item, [
        "field",
        "occurrence",
        "depth",
        "name",
        "version_id",
        "version_no",
        "entries_sha256",
        "entry_index",
        "entry_sha256",
      ]);
      return Object.keys(selected).length > 0 ? selected : null;
    });
    const subjects = safeArray(value.subjects, (item) => {
      const selected = safeFields(item, ["name", "canonical_age"]);
      return Object.keys(selected).length > 0 ? selected : null;
    });

    return {
      available: true,
      asset: safeFields(value.asset, ["id", "width", "height", "image_format", "sha256"]),
      job: safeFields(value.job, [
        "id",
        "release_version_id",
        "ordinal",
        "state",
        "expected_output_count",
      ]),
      provider: safeFields(value.provider, ["name"]),
      output: safeFields(value.output, ["index"]),
      batch: value.batch === null
        ? null
        : safeFields(value.batch, ["index", "name", "image_offset", "image_count"]),
      subjects,
      composition: { mode: compositionMode },
      prompts,
      sampling: safeFields(value.sampling, [
        "seed",
        "width",
        "height",
        "steps",
        "cfg",
        "sampler",
        "scheduler",
        "clip_skip",
      ]),
      hires: safeFields(value.hires, ["enabled", "scale", "denoise", "upscale_method"]),
      detailer: safeFields(value.detailer, [
        "enabled",
        "guide_size",
        "max_size",
        "denoise",
        "bbox_threshold",
        "bbox_dilation",
        "bbox_crop_factor",
        "feather",
      ]),
      checkpoint,
      loras,
      workflow: safeFields(value.workflow, ["name", "version", "sha256"]),
      wildcards: {
        ...safeFields(wildcardSource, ["schema_version"]),
        versions: wildcardVersions,
        selections: wildcardSelections,
      },
      integrity: safeFields(value.integrity, [
        "asset_sha256",
        "job_parameters_sha256",
        "release_specification_sha256",
        "approval_snapshot_sha256",
      ]),
    };
  }

  const createNode = (tagName, className, textValue) => {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (textValue !== undefined) node.textContent = String(textValue);
    return node;
  };

  const hasDisplayValue = (value) => (
    value !== undefined && value !== null && value !== ""
  );

  const displayValue = (value) => {
    if (value === true) return "Yes";
    if (value === false) return "No";
    return hasDisplayValue(value) ? String(value) : "Not set";
  };

  const addSection = (parent, heading) => {
    const section = createNode("section", "generation-settings-section");
    section.append(createNode("h3", "", heading));
    parent.append(section);
    return section;
  };

  const addGrid = (parent, entries) => {
    const visible = entries.filter(
      (entry) => entry && entry.length === 2 && entry[1] !== undefined,
    );
    if (visible.length === 0) return null;
    const grid = createNode("dl", "generation-settings-grid");
    visible.forEach(([label, value]) => {
      const item = createNode("div", "generation-setting");
      item.append(createNode("dt", "", label));
      item.append(createNode("dd", "", displayValue(value)));
      grid.append(item);
    });
    parent.append(grid);
    return grid;
  };

  const fallbackCopyText = (value) => {
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    return copied;
  };

  const copyText = async (button, value, restingLabel) => {
    try {
      let copied = false;
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        try {
          await navigator.clipboard.writeText(value);
          copied = true;
        } catch (_clipboardError) {
          copied = false;
        }
      }
      if (!copied) copied = fallbackCopyText(value);
      if (!copied) {
        throw new Error("clipboard unavailable");
      }
      button.textContent = "Copied";
    } catch (_error) {
      button.textContent = "Copy failed";
    }
    window.setTimeout(() => { button.textContent = restingLabel; }, 1600);
  };

  const copyButton = (label, value, accessibleLabel = label) => {
    const button = createNode(
      "button",
      "secondary-button generation-copy-button",
      label,
    );
    button.type = "button";
    button.setAttribute("aria-label", accessibleLabel);
    button.disabled = typeof value !== "string";
    button.addEventListener("click", () => copyText(button, value, label));
    return button;
  };

  const promptTextBlock = (label, value) => {
    const block = createNode("pre", "generation-prompt-text", value || "(empty)");
    block.tabIndex = 0;
    block.setAttribute("role", "region");
    block.setAttribute("aria-label", label);
    return block;
  };

  const appendFallbackLink = (body, url) => {
    const fallback = createNode(
      "a",
      "generation-details-fallback",
      "Open sanitized settings JSON",
    );
    fallback.href = url;
    fallback.dataset.generationDetailsFallback = "";
    fallback.target = "_blank";
    fallback.rel = "noopener noreferrer";
    body.append(fallback);
  };

  const promptLabels = {
    positive: "Positive prompt",
    character_a: "Left character prompt",
    character_b: "Right character prompt",
    negative: "Negative prompt",
    detailer_positive: "Detailer prompt",
    detailer_negative: "Detailer negative prompt",
  };

  const appendPrompt = (section, name, prompt) => {
    const wrapper = createNode("div", "generation-prompt");
    const heading = createNode("div", "generation-prompt-heading");
    const label = promptLabels[name] || name;
    const resolved = typeof prompt.resolved === "string" ? prompt.resolved : "";
    const source = typeof prompt.source === "string" ? prompt.source : "";
    heading.append(createNode("strong", "", label));
    heading.append(copyButton("Copy", resolved, `Copy ${label.toLowerCase()}`));
    wrapper.append(heading);
    wrapper.append(promptTextBlock(`${label} text`, resolved));
    if (prompt.inherited === true) {
      wrapper.append(createNode("span", "generation-details-status", "Inherited from the batch default."));
    }
    if (source !== resolved) {
      const sourceHeading = createNode("div", "generation-prompt-heading");
      sourceHeading.append(createNode("strong", "", `${label} template`));
      sourceHeading.append(
        copyButton("Copy template", source, `Copy ${label.toLowerCase()} template`),
      );
      wrapper.append(sourceHeading);
      wrapper.append(promptTextBlock(`${label} template text`, source));
    }
    section.append(wrapper);
  };

  const addChipList = (parent, values, emptyLabel) => {
    const list = createNode("ul", "generation-chip-list");
    const labels = values.length > 0 ? values : [emptyLabel];
    labels.forEach((value) => list.append(createNode("li", "generation-chip", value)));
    parent.append(list);
  };

  const preferredPromptText = (prompt) => {
    if (!prompt || typeof prompt !== "object") return "";
    if (typeof prompt.source === "string") return prompt.source;
    return typeof prompt.resolved === "string" ? prompt.resolved : "";
  };

  const forgeStyleImageInfo = (details) => {
    const positive = details.prompts.positive?.resolved || "";
    const negative = details.prompts.negative?.resolved || "";
    const regionalPrompts = details.composition.mode === "duo"
      ? [
        "Composition: two characters (left / right)",
        `Left character prompt: ${details.prompts.character_a?.resolved || ""}`,
        `Right character prompt: ${details.prompts.character_b?.resolved || ""}`,
      ]
      : [];
    const loraTags = details.loras.map((lora) => (
      `<lora:${displayValue(lora.name)}:${displayValue(lora.weight)}>`
    ));
    const positiveWithLoras = positive.includes("<lora:")
      ? positive
      : [positive, loraTags.join(" ")].filter(Boolean).join(" ");
    const sampling = [
      `Steps: ${displayValue(details.sampling.steps)}`,
      `Sampler: ${displayValue(details.sampling.sampler)}`,
      `Schedule type: ${displayValue(details.sampling.scheduler)}`,
      `CFG scale: ${displayValue(details.sampling.cfg)}`,
      `Seed: ${displayValue(details.sampling.seed)}`,
      `Size: ${displayValue(details.sampling.width)}x${displayValue(details.sampling.height)}`,
      `Model hash: ${displayValue(details.checkpoint.sha256).slice(0, 10)}`,
      `Model: ${displayValue(details.checkpoint.name)}`,
      `Clip skip: ${displayValue(details.sampling.clip_skip)}`,
    ];
    if (details.hires.enabled === true) {
      sampling.push(
        `Hires upscale: ${displayValue(details.hires.scale)}`,
        `Denoising strength: ${displayValue(details.hires.denoise)}`,
        `Hires upscaler: ${displayValue(details.hires.upscale_method)}`,
      );
    }
    if (details.detailer.enabled === true) {
      sampling.push(
        "ADetailer model: face_yolov8n.pt",
        `ADetailer prompt: ${details.prompts.detailer_positive?.resolved || ""}`,
        `ADetailer negative prompt: ${details.prompts.detailer_negative?.resolved || ""}`,
        `ADetailer confidence: ${displayValue(details.detailer.bbox_threshold)}`,
        `ADetailer dilate erode: ${displayValue(details.detailer.bbox_dilation)}`,
        `ADetailer mask blur: ${displayValue(details.detailer.feather)}`,
        `ADetailer denoising strength: ${displayValue(details.detailer.denoise)}`,
      );
    }
    const loras = details.loras.map((lora) => (
      `${displayValue(lora.name)}: ${displayValue(lora.sha256).slice(0, 12)}`
    ));
    if (loras.length > 0) sampling.push(`Lora hashes: "${loras.join(", ")}"`);
    return [
      positiveWithLoras,
      ...regionalPrompts,
      `Negative prompt: ${negative}`,
      sampling.join(", "),
    ].join("\n");
  };

  const automationProfileFromDetails = (details) => {
    const fields = {};
    const assign = (name, value) => {
      if (hasDisplayValue(value)) fields[name] = String(value);
    };
    assign("negative_prompt", preferredPromptText(details.prompts.negative));
    assign("composition_mode", details.composition.mode);
    assign("character_a_prompt", preferredPromptText(details.prompts.character_a));
    assign("character_b_prompt", preferredPromptText(details.prompts.character_b));
    assign("detailer_prompt", preferredPromptText(details.prompts.detailer_positive));
    assign("detailer_negative_prompt", preferredPromptText(details.prompts.detailer_negative));
    ["width", "height", "cfg", "steps", "sampler", "scheduler", "clip_skip"].forEach((name) => {
      assign(name, details.sampling[name]);
    });
    if (details.hires.enabled === true) {
      assign("hires_scale", details.hires.scale);
      assign("hires_denoise", details.hires.denoise);
      assign("hires_upscale_method", details.hires.upscale_method);
    }
    if (details.detailer.enabled === true) {
      assign("detailer_guide_size", details.detailer.guide_size);
      assign("detailer_max_size", details.detailer.max_size);
      assign("detailer_denoise", details.detailer.denoise);
      assign("detailer_bbox_threshold", details.detailer.bbox_threshold);
      assign("detailer_bbox_dilation", details.detailer.bbox_dilation);
      assign("detailer_bbox_crop_factor", details.detailer.bbox_crop_factor);
      assign("detailer_feather", details.detailer.feather);
    }
    return {
      schema_version: 1,
      fields,
      seed: hasDisplayValue(details.sampling.seed) ? String(details.sampling.seed) : "",
      batch_prompt: preferredPromptText(details.prompts.positive),
      loras: details.loras.map((lora) => ({
        name: typeof lora.name === "string" ? lora.name : "",
        sha256: typeof lora.sha256 === "string" ? lora.sha256 : "",
        weight: hasDisplayValue(lora.weight) ? String(lora.weight) : "1",
      })),
      matches: {
        subject_name: details.subjects[0]?.name || "",
        secondary_subject_name: details.subjects[1]?.name || "",
        checkpoint_name: details.checkpoint.name || "",
        checkpoint_sha256: details.checkpoint.sha256 || "",
        workflow_name: details.workflow.name || "",
        workflow_version: hasDisplayValue(details.workflow.version)
          ? String(details.workflow.version)
          : "",
        workflow_sha256: details.workflow.sha256 || "",
      },
    };
  };

  const reuseImageSettingsButton = (details) => {
    const label = "Use in new automation";
    const button = createNode("button", "secondary-button generation-copy-button", label);
    button.type = "button";
    button.addEventListener("click", () => {
      try {
        window.sessionStorage.setItem(
          scopedStorageKey(PENDING_IMAGE_PROFILE_KEY),
          JSON.stringify(automationProfileFromDetails(details)),
        );
        button.textContent = "Opening automation…";
        window.location.assign("/dashboard/new-set");
      } catch (_error) {
        button.textContent = "Could not reuse settings";
        window.setTimeout(() => { button.textContent = label; }, 1800);
      }
    });
    return button;
  };

  function renderGenerationDetails(panel, details) {
    const body = panel.querySelector("[data-generation-details-body]");
    if (!body) return;
    const url = panel.dataset.generationDetailsUrl;
    body.replaceChildren();
    body.removeAttribute("aria-busy");

    const actions = createNode("div", "generation-details-actions");
    const serialized = JSON.stringify(details, null, 2);
    actions.append(copyButton("Copy all settings JSON", serialized));
    actions.append(copyButton("Copy Forge-style info", forgeStyleImageInfo(details)));
    actions.append(reuseImageSettingsButton(details));
    body.append(actions);
    body.append(createNode(
      "p",
      "generation-details-status",
      "Automation masters are stored without embedded prompt or workflow metadata; exact settings remain available here.",
    ));

    const runSection = addSection(body, "Image run");
    const outputIndex = details.output.index;
    const batchOffset = details.batch && details.batch.image_offset;
    const batchImageNumber = (
      typeof outputIndex === "number" && typeof batchOffset === "number"
        ? batchOffset + outputIndex + 1
        : undefined
    );
    const batchPosition = (
      batchImageNumber !== undefined && details.batch && details.batch.image_count !== undefined
        ? `${batchImageNumber} of ${details.batch.image_count}`
        : batchImageNumber
    );
    const jobPosition = (
      typeof outputIndex === "number" && hasDisplayValue(details.job.expected_output_count)
        ? `${outputIndex + 1} of ${details.job.expected_output_count}`
        : undefined
    );
    addGrid(runSection, [
      ["Batch", details.batch ? details.batch.name : "Default batch"],
      ["Batch image", batchPosition],
      ["Job image", jobPosition],
      ["Subject", details.subjects.map((item) => item.name).filter(Boolean).join(", ")],
      ["Composition", (
        details.composition.mode === "duo" ? "Two characters (left / right)" : "Single character"
      )],
    ]);

    const promptsSection = addSection(body, "Prompts used");
    Object.entries(promptLabels).forEach(([name]) => {
      if (details.prompts[name]) appendPrompt(promptsSection, name, details.prompts[name]);
    });

    const samplingSection = addSection(body, "Sampling");
    addGrid(samplingSection, [
      ["Seed", details.sampling.seed],
      ["Size", (
        hasDisplayValue(details.sampling.width) && hasDisplayValue(details.sampling.height)
          ? `${details.sampling.width} × ${details.sampling.height}`
          : undefined
      )],
      ["Steps", details.sampling.steps],
      ["CFG", details.sampling.cfg],
      ["Sampler", details.sampling.sampler],
      ["Scheduler", details.sampling.scheduler],
      ["Clip skip", details.sampling.clip_skip],
    ]);

    const modelSection = addSection(body, "Model & style");
    addGrid(modelSection, [
      ["Checkpoint", details.checkpoint.name],
      ["Checkpoint SHA-256", details.checkpoint.sha256],
    ]);
    addChipList(
      modelSection,
      details.loras.map((lora) => {
        const weight = hasDisplayValue(lora.weight) ? ` @ ${lora.weight}` : "";
        const digest = hasDisplayValue(lora.sha256) ? ` · ${lora.sha256}` : "";
        return `${displayValue(lora.name)}${weight}${digest}`;
      }),
      "No LoRAs",
    );

    const upscalerEnabled = details.hires.enabled === true;
    const upscalerKnown = typeof details.hires.enabled === "boolean";
    const detailerEnabled = details.detailer.enabled === true;
    const detailerKnown = typeof details.detailer.enabled === "boolean";
    const refinementSection = addSection(
      body,
      upscalerEnabled ? "Hires & face detailer" : "Face detailer",
    );
    const refinementRows = [[
      "Full-image upscaler",
      upscalerKnown
        ? (upscalerEnabled ? "On" : "Off - no upscale node in workflow")
        : "Workflow evidence unavailable",
    ]];
    if (upscalerEnabled) {
      refinementRows.push(
        ["Hires scale", details.hires.scale],
        ["Hires denoise", details.hires.denoise],
        ["Upscale method", details.hires.upscale_method],
      );
    }
    refinementRows.push([
      "Face detailer",
      detailerKnown ? (detailerEnabled ? "On" : "Off") : "Workflow evidence unavailable",
    ]);
    if (detailerEnabled) {
      refinementRows.push(
        ["Detailer guide size", details.detailer.guide_size],
        ["Detailer max size", details.detailer.max_size],
        ["Detailer denoise", details.detailer.denoise],
        ["BBox threshold", details.detailer.bbox_threshold],
        ["BBox dilation", details.detailer.bbox_dilation],
        ["BBox crop factor", details.detailer.bbox_crop_factor],
        ["Feather", details.detailer.feather],
      );
    }
    addGrid(refinementSection, refinementRows);

    if (details.wildcards.versions.length > 0 || details.wildcards.selections.length > 0) {
      const wildcardSection = addSection(body, "Wildcard evidence");
      if (details.wildcards.selections.length > 0) {
        addChipList(
          wildcardSection,
          details.wildcards.selections.map((selection) => {
            const version = hasDisplayValue(selection.version_no)
              ? ` v${selection.version_no}`
              : "";
            const entry = hasDisplayValue(selection.entry_index)
              ? ` · entry index ${selection.entry_index}`
              : "";
            const field = hasDisplayValue(selection.field) ? `${selection.field}: ` : "";
            return `${field}${displayValue(selection.name)}${version}${entry}`;
          }),
          "No wildcard selections",
        );
      }
      if (details.wildcards.versions.length > 0) {
        addGrid(
          wildcardSection,
          details.wildcards.versions.map((version) => [
            `${displayValue(version.name)} version`,
            [
              hasDisplayValue(version.version_no) ? `v${version.version_no}` : null,
              hasDisplayValue(version.entry_count) ? `${version.entry_count} entries` : null,
              version.entries_sha256,
            ].filter(hasDisplayValue).join(" · "),
          ]),
        );
      }
    }

    const advanced = createNode("details", "generation-advanced");
    advanced.append(createNode("summary", "", "Workflow & integrity"));
    const advancedContent = createNode("div", "generation-advanced-content");
    addGrid(advancedContent, [
      ["Workflow", details.workflow.name],
      ["Workflow version", details.workflow.version],
      ["Workflow SHA-256", details.workflow.sha256],
      ["Job parameters SHA-256", details.integrity.job_parameters_sha256],
      ["Release specification SHA-256", details.integrity.release_specification_sha256],
      ["Approval snapshot SHA-256", details.integrity.approval_snapshot_sha256],
      ["Provider", details.provider.name],
      ["Output index", outputIndex],
      ["Job state", details.job.state],
      ["Job ordinal", details.job.ordinal],
      ["Job ID", details.job.id],
      ["Release version ID", details.job.release_version_id],
      ["Asset ID", details.asset.id],
      ["Asset size", (
        hasDisplayValue(details.asset.width) && hasDisplayValue(details.asset.height)
          ? `${details.asset.width} × ${details.asset.height}`
          : undefined
      )],
      ["Asset format", details.asset.image_format],
      ["Asset SHA-256", details.integrity.asset_sha256 || details.asset.sha256],
    ]);
    Object.entries(promptLabels).forEach(([name, label]) => {
      const prompt = details.prompts[name];
      if (!prompt) return;
      addGrid(advancedContent, [
        [`${label} source SHA-256`, prompt.source_sha256],
        [`${label} resolved SHA-256`, prompt.resolved_sha256],
      ]);
    });
    advanced.append(advancedContent);
    body.append(advanced);
    appendFallbackLink(body, url);
  }

  function generationDetailsError(panel, message, retry = true) {
    const body = panel.querySelector("[data-generation-details-body]");
    if (!body) return;
    const url = panel.dataset.generationDetailsUrl;
    body.replaceChildren();
    body.removeAttribute("aria-busy");
    const status = createNode("p", "generation-details-status error", message);
    status.setAttribute("role", "status");
    body.append(status);
    if (retry) {
      const retryButton = createNode(
        "button",
        "secondary-button generation-copy-button",
        "Retry",
      );
      retryButton.type = "button";
      retryButton.addEventListener("click", () => loadGenerationDetails(panel, true));
      body.append(retryButton);
    }
    appendFallbackLink(body, url);
  }

  async function loadGenerationDetails(panel, force = false) {
    if (!force && ["loading", "loaded"].includes(panel.dataset.generationDetailsState)) return;
    const body = panel.querySelector("[data-generation-details-body]");
    const status = panel.querySelector("[data-generation-details-status]");
    const url = panel.dataset.generationDetailsUrl;
    if (!body || !url) return;
    panel.dataset.generationDetailsState = "loading";
    body.setAttribute("aria-busy", "true");
    if (status) status.textContent = "Loading exact generation settings…";

    try {
      const response = await fetch(url, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.redirected) {
        panel.dataset.generationDetailsState = "error";
        generationDetailsError(
          panel,
          "Your session has expired. Sign in again, then retry.",
        );
        return;
      }
      if (!response.ok) {
        panel.dataset.generationDetailsState = "error";
        if (response.status === 401 || response.status === 403) {
          generationDetailsError(panel, "Your session no longer permits access. Sign in again, then retry.");
        } else if (response.status === 404) {
          generationDetailsError(panel, "Generation settings are not available for this image.", false);
        } else if (response.status === 409) {
          generationDetailsError(panel, "The stored generation settings could not be verified.");
        } else if (response.status === 429) {
          generationDetailsError(panel, "Too many requests. Wait a moment, then retry.");
        } else {
          generationDetailsError(panel, "Generation settings could not be loaded. Try again.");
        }
        return;
      }
      const payload = sanitizedGenerationDetails(await response.json());
      if (!payload.available) {
        panel.dataset.generationDetailsState = "loaded";
        generationDetailsError(
          panel,
          "Exact generation settings are unavailable for this older or unverifiable image.",
          false,
        );
        return;
      }
      renderGenerationDetails(panel, payload);
      panel.dataset.generationDetailsState = "loaded";
    } catch (_error) {
      panel.dataset.generationDetailsState = "error";
      generationDetailsError(panel, "A network error interrupted loading. Check your connection and retry.");
    }
  }

  const initializedGenerationDetails = new WeakSet();

  function bindGenerationDetails(root = document) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    root.querySelectorAll("[data-generation-details]").forEach((panel) => {
      if (initializedGenerationDetails.has(panel)) return;
      initializedGenerationDetails.add(panel);
      panel.dataset.generationDetailsState ||= "idle";
      panel.addEventListener("toggle", () => {
        if (panel.open) loadGenerationDetails(panel);
      });
    });
  }

  function initializeGenerationDetails() {
    bindGenerationDetails();
    document.addEventListener("gen-automation:assets-updated", (event) => {
      const root = event.detail && event.detail.root;
      bindGenerationDetails(root || document);
    });
  }

  function initializeLiveGeneratedAssets() {
    const panel = document.querySelector("[data-live-generated-assets]");
    if (!(panel instanceof HTMLElement) || !panel.dataset.liveAssetsUrl) return;
    const grid = panel.querySelector("[data-live-assets-grid]");
    const count = panel.querySelector("[data-live-assets-count]");
    const status = panel.querySelector("[data-live-assets-status]");
    const empty = panel.querySelector("[data-live-assets-empty]");
    const latest = panel.querySelector("[data-live-assets-latest]");
    const latestImage = panel.querySelector("[data-live-assets-latest-image]");
    const latestTitle = panel.querySelector("[data-live-assets-latest-title]");
    const latestBatch = panel.querySelector("[data-live-assets-latest-batch]");
    const latestPosition = panel.querySelector("[data-live-assets-latest-position]");
    const latestResolution = panel.querySelector("[data-live-assets-latest-resolution]");
    const latestOpenButtons = panel.querySelectorAll("[data-live-assets-latest-open]");
    const gridHeading = panel.querySelector("[data-live-assets-grid-heading]");
    if (!(grid instanceof HTMLElement)) return;

    const assetIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    const assets = new Map();
    let cursor = "";
    let expected = Math.max(0, integerValue(panel.dataset.liveAssetsExpected, 0));
    let timer = null;
    let networkFailures = 0;
    let terminalAfterRefresh = false;
    let stopped = false;
    let refreshing = false;
    let refreshRequested = false;
    let immediatePages = 0;
    let latestAssetId = "";

    const positiveInteger = (value, fallback = 0) => {
      const parsed = integerValue(value, fallback);
      return parsed > 0 ? parsed : fallback;
    };

    const safeAssetUrl = (value, assetId) => {
      if (typeof value !== "string" || value.length === 0 || value.length > 16_384) return "";
      try {
        const url = new URL(value, window.location.href);
        const expectedPrefix = `/dashboard/assets/${assetId.toLowerCase()}/`;
        return url.origin === window.location.origin
          && url.pathname.toLowerCase().startsWith(expectedPrefix)
          ? url.href
          : "";
      } catch (_error) {
        return "";
      }
    };

    const normalizedAsset = (item) => {
      if (!isRecord(item) || typeof item.asset_id !== "string"
          || !assetIdPattern.test(item.asset_id)) return null;
      const assetId = item.asset_id.toLowerCase();
      const viewUrl = safeAssetUrl(item.view_url, assetId);
      if (!viewUrl) return null;
      const ordinal = Math.max(0, integerValue(item.ordinal, 0));
      const outputIndex = Math.max(0, integerValue(item.output_index, 0));
      const suppliedPosition = positiveInteger(item.queue_position, 0)
        || positiveInteger(item.position, 0);
      const position = suppliedPosition || ordinal + outputIndex + 1;
      const batchName = typeof item.batch_name === "string"
        ? item.batch_name.trim().slice(0, 120)
        : "";
      return {
        assetId,
        batchImageNumber: positiveInteger(item.batch_image_number, 0),
        batchIndex: Math.max(0, integerValue(item.batch_index, 0)),
        batchName,
        downloadUrl: safeAssetUrl(item.download_url, assetId),
        generationDetailsUrl: safeAssetUrl(item.generation_details_url, assetId),
        height: positiveInteger(item.height, 1),
        ordinal,
        outputIndex,
        position,
        viewUrl,
        width: positiveInteger(item.width, 1),
      };
    };

    const assetLabel = (asset) => {
      const image = `Image ${asset.position}`;
      return asset.batchName ? `${image} · ${asset.batchName}` : image;
    };

    const createGenerationDetails = (asset) => {
      if (!asset.generationDetailsUrl) return null;
      const details = createNode("details", "generation-details");
      details.dataset.generationDetails = "";
      details.dataset.generationDetailsUrl = asset.generationDetailsUrl;
      details.append(createNode("summary", "", "Full prompt & generation settings"));
      const body = createNode("div", "generation-details-body");
      body.dataset.generationDetailsBody = "";
      const detailsStatus = createNode(
        "p",
        "generation-details-status",
        "Open this section to load the exact settings for this image.",
      );
      detailsStatus.dataset.generationDetailsStatus = "";
      detailsStatus.setAttribute("role", "status");
      const fallback = createNode(
        "a",
        "generation-details-fallback",
        "Open sanitized settings JSON",
      );
      fallback.dataset.generationDetailsFallback = "";
      fallback.href = asset.generationDetailsUrl;
      fallback.target = "_blank";
      fallback.rel = "noopener noreferrer";
      body.append(detailsStatus, fallback);
      details.append(body);
      return details;
    };

    const createAssetCard = (asset) => {
      const label = assetLabel(asset);
      const card = createNode("li", "asset-card live-generation-asset-card");
      card.dataset.assetCard = "";
      card.dataset.assetId = asset.assetId;
      card.dataset.assetLabel = label;
      card.dataset.assetPosition = String(asset.position);
      card.dataset.assetViewUrl = asset.viewUrl;

      const media = createNode("div", "asset-media");
      const image = createNode("img", "asset-preview");
      image.src = asset.viewUrl;
      image.alt = `${label} generated image`;
      image.width = asset.width;
      image.height = asset.height;
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      const badge = createNode("span", "asset-rank-badge live-generation-asset-badge", `#${asset.position}`);
      media.append(image, badge);

      const body = createNode("div", "asset-body");
      const titleRow = createNode("div", "rank-line");
      titleRow.append(
        createNode("span", "rank", `Image ${asset.position}`),
        createNode("span", "live-generation-asset-batch", asset.batchName || "Generated"),
      );
      const detailParts = [];
      if (asset.batchImageNumber > 0) detailParts.push(`batch image ${asset.batchImageNumber}`);
      detailParts.push(`${asset.width} × ${asset.height}`);
      body.append(
        titleRow,
        createNode(
          "p",
          "muted live-generation-asset-meta",
          `Verified raw master · ${detailParts.join(" · ")} · ranking pending`,
        ),
      );
      const details = createGenerationDetails(asset);
      if (details) body.append(details);
      if (asset.downloadUrl) {
        const download = createNode("a", "download", "Download exact raw master");
        download.dataset.assetDownload = "";
        download.href = asset.downloadUrl;
        download.rel = "noopener noreferrer";
        body.append(download);
      }
      card.append(media, body);
      return card;
    };

    const compareAssets = (left, right) => (
      left.position - right.position
      || left.ordinal - right.ordinal
      || left.outputIndex - right.outputIndex
      || left.assetId.localeCompare(right.assetId)
    );

    const reorderCards = () => {
      const ordered = Array.from(assets.values()).sort(compareAssets);
      ordered.forEach((asset, index) => {
        const current = grid.children.item(index);
        if (current !== asset.card) grid.insertBefore(asset.card, current || null);
      });
    };

    const renderLatestAsset = (asset) => {
      if (!(latest instanceof HTMLElement) || !(latestImage instanceof HTMLImageElement)) return;
      latestAssetId = asset.assetId;
      const label = assetLabel(asset);
      latestImage.src = asset.viewUrl;
      latestImage.alt = `${label} latest generated image`;
      latestImage.width = asset.width;
      latestImage.height = asset.height;
      if (latestTitle) latestTitle.textContent = `Image ${asset.position}`;
      if (latestBatch) {
        const batchDetails = asset.batchImageNumber > 0
          ? `${asset.batchName || "Generated"} \u00b7 batch image ${asset.batchImageNumber}`
          : (asset.batchName || "Generated");
        latestBatch.textContent = batchDetails;
      }
      if (latestPosition) latestPosition.textContent = `#${asset.position}`;
      if (latestResolution) latestResolution.textContent = `${asset.width} \u00d7 ${asset.height}`;
      latestOpenButtons.forEach((button) => {
        button.setAttribute("aria-label", `Open ${label} in the full-screen viewer`);
      });
      latest.hidden = false;
      if (gridHeading instanceof HTMLElement) gridHeading.hidden = false;
    };

    const openLatestAsset = () => {
      const card = assets.get(latestAssetId)?.card;
      if (!(card instanceof HTMLElement)) return;
      const trigger = card.querySelector("[data-asset-viewer-trigger], .asset-preview");
      if (trigger instanceof HTMLElement) trigger.click();
    };

    latestOpenButtons.forEach((button) => {
      button.addEventListener("click", openLatestAsset);
    });

    const renderCount = () => {
      if (count) count.textContent = expected > 0
        ? `${assets.size} / ${expected} ready`
        : `${assets.size} ready`;
      if (empty) empty.hidden = assets.size > 0;
    };

    const addAssets = (items) => {
      let added = 0;
      let latestAdded = null;
      items.forEach((item) => {
        const asset = normalizedAsset(item);
        if (!asset || assets.has(asset.assetId)) return;
        asset.card = createAssetCard(asset);
        assets.set(asset.assetId, asset);
        latestAdded = asset;
        added += 1;
      });
      if (added === 0) return 0;
      reorderCards();
      renderLatestAsset(latestAdded);
      renderCount();
      document.dispatchEvent(new CustomEvent("gen-automation:assets-updated", {
        detail: { root: grid, generated: assets.size, expected },
      }));
      return added;
    };

    const schedule = (delay) => {
      if (stopped) return;
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(refresh, Math.min(30_000, Math.max(2_000, delay)));
    };

    const stopWithMessage = (message) => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
      if (status) status.textContent = message;
    };

    async function refresh() {
      if (stopped) return;
      if (refreshing) {
        refreshRequested = true;
        return;
      }
      if (document.visibilityState === "hidden") {
        schedule(10_000);
        return;
      }
      refreshing = true;
      try {
        const url = new URL(panel.dataset.liveAssetsUrl, window.location.href);
        if (cursor) url.searchParams.set("cursor", cursor);
        const response = await fetch(url.href, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (response.redirected || response.status === 401 || response.status === 403) {
          stopWithMessage("Your session no longer permits live image previews. Sign in again to continue.");
          return;
        }
        if (!response.ok) throw new Error("generated assets unavailable");
        const payload = await response.json();
        if (!isRecord(payload)) throw new Error("invalid generated assets response");
        const items = Array.isArray(payload.items)
          ? payload.items
          : (Array.isArray(payload.assets) ? payload.assets : null);
        if (items === null) throw new Error("invalid generated assets response");
        const added = addAssets(items);
        const previousCursor = cursor;
        if (typeof payload.cursor === "string" && payload.cursor.length <= 4096) {
          cursor = payload.cursor;
        } else if (typeof payload.next_cursor === "string" && payload.next_cursor.length <= 4096) {
          cursor = payload.next_cursor;
        }
        networkFailures = 0;
        if (status) {
          status.textContent = added > 0
            ? `${added} new ${added === 1 ? "image" : "images"} ready. ${assets.size} loaded in queue order.`
            : (assets.size > 0 ? `${assets.size} images ready. Waiting for more.` : "Waiting for the first verified image.");
        }
        if (payload.has_more === true) {
          if (cursor !== previousCursor && immediatePages < 20) {
            immediatePages += 1;
            refreshRequested = true;
          } else {
            immediatePages = 0;
            schedule(2_000);
          }
          return;
        }
        immediatePages = 0;
        if (payload.complete === true || terminalAfterRefresh) {
          stopWithMessage(
            assets.size > 0
              ? `All ${assets.size} available images are shown. Ranking may still be finishing.`
              : "Generation finished without an available image preview.",
          );
          return;
        }
        schedule(positiveInteger(payload.poll_after_ms, 3_000));
      } catch (_error) {
        immediatePages = 0;
        networkFailures += 1;
        if (status) status.textContent = "Live previews were interrupted. Retrying automatically…";
        schedule(Math.min(30_000, 3_000 * (2 ** Math.min(networkFailures, 3))));
      } finally {
        refreshing = false;
        if (refreshRequested && !stopped) {
          refreshRequested = false;
          window.setTimeout(refresh, 0);
        }
      }
    }

    document.addEventListener("gen-automation:generation-progress", (event) => {
      const detail = event.detail;
      if (!isRecord(detail)) return;
      const nextExpected = Math.max(0, integerValue(detail.expected, expected));
      if (nextExpected !== expected) {
        expected = nextExpected;
        renderCount();
      }
      if (detail.terminal === true) {
        terminalAfterRefresh = true;
        refresh();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refresh();
    });
    renderCount();
    window.setTimeout(refresh, 500);
  }

  function initializeGenerationProgress() {
    const panel = document.querySelector("[data-generation-progress]");
    if (!panel || !panel.dataset.progressUrl) return;

    const stageKeys = new Set([
      "queued",
      "gpu_starting",
      "generating",
      "scoring",
      "review",
      "paused",
      "cancelled",
      "error",
    ]);
    const stageBadge = document.querySelector("[data-progress-stage-badge]");
    const stageLabel = panel.querySelector("[data-progress-stage-label]");
    const imageCount = panel.querySelector("[data-progress-image-count]");
    const progressBar = panel.querySelector("[data-progress-bar]");
    const detail = panel.querySelector("[data-progress-detail]");
    const jobCount = panel.querySelector("[data-progress-job-count]");
    const activeJobs = panel.querySelector("[data-progress-active-jobs]");
    const scoringCount = panel.querySelector("[data-progress-scoring-count]");
    const errorBox = panel.querySelector("[data-progress-error]");
    const nextLink = panel.querySelector("[data-progress-next]");
    const releasePhase = document.querySelector("[data-progress-release-phase]");
    const jobStates = document.querySelector("[data-progress-job-states]");
    const stopOpen = document.querySelector("[data-stop-generation-open]");
    const stopDialog = document.querySelector("[data-stop-generation-dialog]");
    const stopForm = document.querySelector("[data-stop-generation-form]");
    const stopCancel = document.querySelector("[data-stop-generation-cancel]");
    const stopConfirm = document.querySelector("[data-stop-generation-confirm]");
    const stopError = document.querySelector("[data-stop-generation-error]");
    const stopStatus = panel.querySelector("[data-stop-generation-status]");
    let timer = null;
    let networkFailures = 0;
    let liveGenerated = 0;
    let liveExpected = 0;
    let stopSubmissionPending = false;

    const safeCount = (value) => (
      Number.isInteger(value) && value >= 0 ? value : 0
    );

    const renderImageProgress = (generated, expected) => {
      const displayedExpected = safeCount(expected);
      const progressMaximum = Math.max(1, displayedExpected);
      if (imageCount) imageCount.textContent = `${generated} / ${displayedExpected} images`;
      if (progressBar) {
        progressBar.max = progressMaximum;
        progressBar.value = Math.min(generated, progressMaximum);
      }
    };

    const renderStopState = (payload) => {
      const stop = isRecord(payload.stop) ? payload.stop : {};
      const requested = stop.requested === true;
      const settled = stop.settled === true;
      const available = stop.available === true;
      panel.dataset.stopGenerationRequested = requested ? "true" : "false";
      if (stopOpen instanceof HTMLButtonElement) {
        stopOpen.hidden = !available;
        stopOpen.disabled = requested || stopSubmissionPending;
        stopOpen.textContent = requested || stopSubmissionPending
          ? "Stopping generation…"
          : "Stop generation";
      }
      if (stopStatus) {
        stopStatus.hidden = !requested && !stopSubmissionPending;
        if (requested) {
          stopStatus.textContent = settled
            ? `Generation stopped. All ${liveGenerated} completed ${liveGenerated === 1 ? "image was" : "images were"} saved.`
            : `Stopping generation. ${liveGenerated} verified ${liveGenerated === 1 ? "image is" : "images are"} saved; active GPU jobs are being cancelled.`;
        } else if (stopSubmissionPending) {
          stopStatus.textContent = "Requesting generation stop…";
        } else {
          stopStatus.textContent = "";
        }
      }
    };

    const updatePipeline = (stage) => {
      panel.querySelectorAll("[data-pipeline-stage]").forEach((item) => {
        const order = ["queued", "gpu_starting", "generating", "scoring", "review"]
          .indexOf(item.dataset.pipelineStage);
        item.classList.toggle("completed", order >= 0 && order + 1 < stage.step);
        item.classList.toggle("active", order >= 0 && order + 1 === stage.step);
        if (order >= 0 && order + 1 === stage.step) {
          item.setAttribute("aria-current", "step");
        } else {
          item.removeAttribute("aria-current");
        }
      });
    };

    const renderJobStates = (states) => {
      if (!jobStates || !isRecord(states)) return;
      const items = Object.entries(states)
        .filter(([name, count]) => /^[a-z_]+$/.test(name) && Number.isInteger(count) && count > 0)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([name, count]) => {
          const item = createNode("li");
          const label = createNode("span", `status ${name}`);
          label.textContent = `${name.replaceAll("_", " ")} · ${count}`;
          item.append(label);
          return item;
        });
      jobStates.replaceChildren(...items);
      jobStates.hidden = items.length === 0;
    };

    const render = (payload) => {
      if (!isRecord(payload) || !isRecord(payload.stage)
        || !isRecord(payload.images) || !isRecord(payload.jobs)) return false;
      const stageKey = stageKeys.has(payload.stage.key) ? payload.stage.key : "error";
      const stageStep = Math.min(5, Math.max(1, safeCount(payload.stage.step)));
      const stage = { key: stageKey, step: stageStep };
      const expected = safeCount(payload.images.expected);
      const stopRequested = isRecord(payload.stop) && payload.stop.requested === true;
      liveExpected = stopRequested ? expected : Math.max(liveExpected, expected);
      liveGenerated = Math.max(liveGenerated, safeCount(payload.images.generated));
      const completedJobs = safeCount(payload.jobs.completed);
      const totalJobs = safeCount(payload.jobs.total);

      panel.dataset.progressStage = stageKey;
      if (stageBadge) {
        stageBadge.className = `status ${stageKey}`;
        stageBadge.textContent = typeof payload.stage.label === "string"
          ? payload.stage.label
          : "Updating";
      }
      if (stageLabel && typeof payload.stage.label === "string") {
        stageLabel.textContent = payload.stage.label;
      }
      if (detail && typeof payload.stage.detail === "string") {
        detail.textContent = payload.stage.detail;
      }
      renderImageProgress(liveGenerated, liveExpected);
      if (jobCount) jobCount.textContent = `${completedJobs} / ${totalJobs} complete`;
      if (activeJobs) activeJobs.textContent = String(safeCount(payload.jobs.active));
      if (scoringCount) {
        scoringCount.textContent = isRecord(payload.scoring)
          ? `${safeCount(payload.scoring.completed)} / ${safeCount(payload.scoring.total)}`
          : "Not started";
      }
      if (releasePhase && typeof payload.phase === "string") {
        releasePhase.textContent = payload.phase.replaceAll("_", " ");
      }
      renderJobStates(payload.jobs.states);
      updatePipeline(stage);
      renderStopState(payload);

      if (errorBox) {
        const message = isRecord(payload.error) && typeof payload.error.message === "string"
          ? payload.error.message
          : "";
        errorBox.textContent = message;
        errorBox.hidden = !message;
      }
      if (nextLink) {
        const nextUrl = typeof payload.next_url === "string"
          && payload.next_url.startsWith("/dashboard/releases/")
          ? payload.next_url
          : "";
        nextLink.hidden = payload.ready_for_review !== true || !nextUrl;
        if (nextUrl) nextLink.href = nextUrl;
      }
      return true;
    };

    const schedule = (delay) => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(refresh, Math.min(30000, Math.max(2000, delay)));
    };

    const refresh = async () => {
      if (document.visibilityState === "hidden") {
        schedule(10000);
        return;
      }
      try {
        const response = await fetch(panel.dataset.progressUrl, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (response.redirected || !response.ok) throw new Error("progress unavailable");
        const payload = await response.json();
        if (!render(payload)) throw new Error("invalid progress response");
        networkFailures = 0;
        const terminal = payload.ready_for_review === true
          || ["cancelled"].includes(payload.stage.key)
          || (payload.stage.key === "error" && payload.error && payload.error.retryable === false);
        document.dispatchEvent(new CustomEvent("gen-automation:generation-progress", {
          detail: {
            expected: safeCount(payload.images.expected),
            generated: safeCount(payload.images.generated),
            stage: payload.stage.key,
            terminal,
          },
        }));
        if (!terminal) schedule(safeCount(payload.poll_after_ms) || 5000);
      } catch (_error) {
        networkFailures += 1;
        if (detail) detail.textContent = "Connection interrupted. Retrying automatically…";
        schedule(Math.min(30000, 3000 * (2 ** Math.min(networkFailures, 3))));
      }
    };

    if (stopOpen instanceof HTMLButtonElement
      && stopDialog instanceof HTMLDialogElement
      && stopForm instanceof HTMLFormElement) {
      stopOpen.addEventListener("click", () => {
        if (typeof stopDialog.showModal === "function") {
          stopDialog.showModal();
        } else if (window.confirm(
          "Stop generation now? Finished uploads stay saved, but the image currently rendering may be interrupted.",
        )) {
          stopForm.requestSubmit();
        }
      });
      if (stopCancel instanceof HTMLButtonElement) {
        stopCancel.addEventListener("click", () => stopDialog.close());
      }
      stopForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (stopSubmissionPending) return;
        const stopUrl = new URL(stopForm.action, window.location.href);
        if (stopUrl.origin !== window.location.origin) return;
        stopSubmissionPending = true;
        if (stopConfirm instanceof HTMLButtonElement) stopConfirm.disabled = true;
        if (stopError) stopError.hidden = true;
        renderStopState({ stop: { requested: false, available: false } });
        try {
          const body = new URLSearchParams(new FormData(stopForm));
          const response = await fetch(stopUrl, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
              Accept: "application/json",
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            body,
          });
          const payload = await response.json();
          if (response.redirected || !response.ok || !render(payload)) {
            throw new Error(
              isRecord(payload) && typeof payload.detail === "string"
                ? payload.detail
                : "Generation could not be stopped.",
            );
          }
          stopSubmissionPending = false;
          stopDialog.close();
          renderStopState(payload);
          document.dispatchEvent(new CustomEvent("gen-automation:generation-progress", {
            detail: {
              expected: safeCount(payload.images.expected),
              generated: safeCount(payload.images.generated),
              stage: payload.stage.key,
              terminal: payload.ready_for_review === true || payload.stage.key === "cancelled",
            },
          }));
          schedule(500);
        } catch (error) {
          stopSubmissionPending = false;
          if (stopConfirm instanceof HTMLButtonElement) stopConfirm.disabled = false;
          if (stopOpen instanceof HTMLButtonElement) stopOpen.disabled = false;
          if (stopError) {
            stopError.textContent = error instanceof Error
              ? error.message
              : "Generation could not be stopped.";
            stopError.hidden = false;
          }
          renderStopState({ stop: { requested: false, available: true } });
        }
      });
    }

    document.addEventListener("gen-automation:assets-updated", (event) => {
      const eventDetail = event.detail;
      if (!isRecord(eventDetail) || !Number.isInteger(eventDetail.generated)) return;
      liveGenerated = Math.max(liveGenerated, safeCount(eventDetail.generated));
      liveExpected = Math.max(liveExpected, safeCount(eventDetail.expected));
      renderImageProgress(liveGenerated, liveExpected);
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refresh();
    });
    schedule(1500);
  }

  const initializeCharacterComposition = () => {
    const form = document.querySelector("[data-automation-form]");
    const builder = document.querySelector("[data-composition-builder]");
    const workflow = document.querySelector("[data-workflow-profile]");
    if (!form || !(builder instanceof HTMLElement) || !(workflow instanceof HTMLSelectElement)) {
      return;
    }
    const modeControls = Array.from(
      builder.querySelectorAll('[name="composition_mode"]'),
    ).filter((control) => control instanceof HTMLInputElement);
    const firstSubject = namedControl(form, "subject_id");
    const secondSubject = namedControl(form, "subject_2_id");
    const firstPrompt = namedControl(form, "character_a_prompt");
    const secondPrompt = namedControl(form, "character_b_prompt");
    const secondCard = builder.querySelector('[data-character-card="b"]');
    const promptFields = Array.from(builder.querySelectorAll("[data-character-prompt-field]"));
    const actions = builder.querySelector("[data-composition-actions]");
    const preview = builder.querySelector("[data-composition-preview]");
    const previewA = builder.querySelector("[data-composition-preview-a]");
    const previewB = builder.querySelector("[data-composition-preview-b]");
    const firstSide = builder.querySelector("[data-character-side-a]");
    const firstPosition = builder.querySelector("[data-character-position-a]");
    const status = builder.querySelector("[data-composition-status]");
    const swap = builder.querySelector("[data-swap-characters]");
    if (!(firstSubject instanceof HTMLSelectElement)
      || !(secondSubject instanceof HTMLSelectElement)
      || !(firstPrompt instanceof HTMLTextAreaElement)
      || !(secondPrompt instanceof HTMLTextAreaElement)) return;

    const workflowOptions = Array.from(workflow.options).filter(
      (option) => ["true", "false"].includes(option.dataset.regionalPrompting),
    );
    const workflowFamily = (option) => (option?.dataset.workflowName || "")
      .toLowerCase()
      .replace(/\b(base|hires|highres|upscale|upscaler|couple|duo|regional)\b/g, " ")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
    const pairedWorkflow = (current, desiredRegional) => {
      const family = workflowFamily(current);
      if (!family) return null;
      return workflowOptions.find((option) => (
        !option.disabled
        && option !== current
        && option.dataset.regionalPrompting === desiredRegional
        && option.dataset.upscalerEnabled === current?.dataset.upscalerEnabled
        && option.dataset.detailerEnabled === current?.dataset.detailerEnabled
        && option.dataset.workflowVersion === current?.dataset.workflowVersion
        && workflowFamily(option) === family
      )) || null;
    };
    const selectedName = (control, fallback) => (
      control.selectedOptions.item(0)?.dataset.subjectName || fallback
    );
    const suggestedPrompt = (control) => selectedName(control, "");
    const setAutomaticPrompt = (control, subjectControl) => {
      const previous = control.dataset.automaticCharacterPrompt || "";
      const suggested = suggestedPrompt(subjectControl);
      if (!control.value.trim() || control.value === previous) control.value = suggested;
      control.dataset.automaticCharacterPrompt = suggested;
    };
    const activeMode = () => (
      modeControls.find((control) => control.checked)?.value === "duo" ? "duo" : "single"
    );
    const cacheDuoValues = () => {
      builder.dataset.duoSubjectId = secondSubject.value;
      builder.dataset.duoFirstPrompt = firstPrompt.value;
      builder.dataset.duoSecondPrompt = secondPrompt.value;
    };
    const restoreDuoValues = () => {
      if (!secondSubject.value && builder.dataset.duoSubjectId) {
        secondSubject.value = builder.dataset.duoSubjectId;
      }
      if (!firstPrompt.value && builder.dataset.duoFirstPrompt) {
        firstPrompt.value = builder.dataset.duoFirstPrompt;
      }
      if (!secondPrompt.value && builder.dataset.duoSecondPrompt) {
        secondPrompt.value = builder.dataset.duoSecondPrompt;
      }
      setAutomaticPrompt(firstPrompt, firstSubject);
      setAutomaticPrompt(secondPrompt, secondSubject);
    };
    const clearSingleOnlyFields = () => {
      cacheDuoValues();
      secondSubject.value = "";
      firstPrompt.value = "";
      secondPrompt.value = "";
    };
    const pairWorkflow = (duo) => {
      const selected = workflow.selectedOptions.item(0);
      const desired = duo ? "true" : "false";
      if (selected?.dataset.regionalPrompting === desired) return true;
      const replacement = pairedWorkflow(selected, desired);
      if (!replacement) return false;
      workflow.value = replacement.value;
      workflow.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    };
    const render = ({ modeChanged = false } = {}) => {
      const duo = activeMode() === "duo";
      if (modeChanged) {
        if (duo) restoreDuoValues();
        else clearSingleOnlyFields();
      }
      if (secondCard instanceof HTMLElement) secondCard.hidden = !duo;
      promptFields.forEach((field) => { field.hidden = !duo; });
      if (actions instanceof HTMLElement) actions.hidden = !duo;
      if (preview instanceof HTMLElement) preview.hidden = !duo;
      if (firstSide instanceof HTMLElement) firstSide.textContent = duo ? "Character 1" : "Character";
      if (firstPosition instanceof HTMLElement) firstPosition.hidden = !duo;
      secondSubject.required = duo;
      firstPrompt.required = duo;
      secondPrompt.required = duo;
      secondSubject.setCustomValidity(
        duo && firstSubject.value === secondSubject.value
          ? "Choose two different approved characters."
          : "",
      );
      const paired = pairWorkflow(duo);
      modeControls.forEach((control) => {
        control.setCustomValidity(paired ? "" : "No matching workflow is available for this composition.");
      });
      if (previewA instanceof HTMLElement) {
        previewA.textContent = selectedName(firstSubject, "Character 1");
      }
      if (previewB instanceof HTMLElement) {
        previewB.textContent = selectedName(secondSubject, "Character 2");
      }
      if (status instanceof HTMLElement) {
        status.textContent = !paired
          ? "A matching standard/couple workflow with the same hires and detailer settings is unavailable."
          : (duo
            ? "Regional prompting keeps the first character on the left and the second on the right."
            : "");
        status.className = `composition-status${paired ? "" : " warning"}`;
        status.hidden = paired && !duo;
      }
      form.dispatchEvent(new CustomEvent("gen-automation:profile-changed"));
    };

    modeControls.forEach((control) => {
      control.addEventListener("change", () => render({ modeChanged: true }));
    });
    firstSubject.addEventListener("change", () => {
      if (activeMode() === "duo") setAutomaticPrompt(firstPrompt, firstSubject);
      render();
    });
    secondSubject.addEventListener("change", () => {
      if (activeMode() === "duo") setAutomaticPrompt(secondPrompt, secondSubject);
      render();
    });
    workflow.addEventListener("change", () => render());
    if (swap instanceof HTMLButtonElement) {
      swap.addEventListener("click", () => {
        const subjectValue = firstSubject.value;
        const promptValue = firstPrompt.value;
        firstSubject.value = secondSubject.value;
        firstPrompt.value = secondPrompt.value;
        secondSubject.value = subjectValue;
        secondPrompt.value = promptValue;
        firstPrompt.dataset.automaticCharacterPrompt = suggestedPrompt(firstSubject);
        secondPrompt.dataset.automaticCharacterPrompt = suggestedPrompt(secondSubject);
        render();
      });
    }
    render();
  };

  const initializeWorkflowRefinement = () => {
    const workflow = document.querySelector("[data-workflow-profile]");
    const status = document.querySelector("[data-workflow-refinement-status]");
    if (!(workflow instanceof HTMLSelectElement) || !(status instanceof HTMLElement)) return;
    const hiresSettings = Array.from(document.querySelectorAll("[data-hires-setting]"));
    const detailerPresetControl = document.querySelector("[data-detailer-preset-control]");
    const detailerPresetState = document.querySelector("[data-detailer-preset-state]");
    const a1111SampleDetailerPreset = Object.freeze({
      detailer_prompt: "sexy, expressive, ",
      detailer_negative_prompt: "closed eyes, ",
      detailer_guide_size: "768",
      detailer_max_size: "1536",
      detailer_denoise: "0.4",
      detailer_bbox_threshold: "0.3",
      detailer_bbox_dilation: "4",
      detailer_bbox_crop_factor: "1.5",
      detailer_feather: "4",
    });
    const detailerPresetFields = Object.entries(a1111SampleDetailerPreset).map(([name, value]) => ({
      field: document.querySelector(`[name="${name}"]`),
      value,
    }));
    const toggleControl = document.querySelector("[data-upscaler-control]");
    const toggle = document.querySelector("[data-upscaler-toggle]");
    const toggleLabel = document.querySelector("[data-upscaler-toggle-label]");
    const toggleDescription = document.querySelector("[data-upscaler-toggle-description]");
    const workflowOptions = Array.from(workflow.options).filter(
      (option) => ["true", "false"].includes(option.dataset.upscalerEnabled),
    );
    const hiresFallbackValues = Object.freeze({
      hires_scale: "1",
      hires_denoise: "0.35",
      hires_upscale_method: "bislerp",
    });
    const normalizeHiddenHiresValues = () => {
      Object.entries(hiresFallbackValues).forEach(([name, fallback]) => {
        const control = namedControl(workflow.form, name);
        if (!(control instanceof HTMLInputElement)
            && !(control instanceof HTMLSelectElement)) return;
        if (!control.value || !control.checkValidity()) control.value = fallback;
      });
    };
    const workflowFamily = (option) => (option?.dataset.workflowName || "")
      .toLowerCase()
      .replace(/\b(base|hires|highres|upscale|upscaler|couple|duo|regional)\b/g, " ")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
    const pairedWorkflow = (current, desired) => {
      const family = workflowFamily(current);
      if (!family) return null;
      return workflowOptions.find((option) => (
        !option.disabled
        && option !== current
        && option.dataset.upscalerEnabled === desired
        && option.dataset.detailerEnabled === current?.dataset.detailerEnabled
        && option.dataset.regionalPrompting === current?.dataset.regionalPrompting
        && option.dataset.workflowVersion === current?.dataset.workflowVersion
        && workflowFamily(option) === family
      )) || null;
    };

    const render = () => {
      const selected = workflow.selectedOptions.item(0);
      const upscalerEnabled = selected?.dataset.upscalerEnabled === "true";
      const detailerEnabled = selected?.dataset.detailerEnabled === "true";
      const pair = pairedWorkflow(selected, upscalerEnabled ? "false" : "true");
      if (!upscalerEnabled) normalizeHiddenHiresValues();
      const presetMatches = detailerPresetFields.every(({ field, value }) => (
        (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)
        && field.value === value
      ));
      status.textContent = upscalerEnabled
        ? `Full-image upscaler: On.${detailerEnabled ? " Face detailer: On." : " Face detailer: Off."}`
        : `Full-image upscaler: Off - this workflow has no upscale node.${detailerEnabled ? " Face detailer remains on." : " Face detailer is also off."}`;
      hiresSettings.forEach((field) => {
        field.hidden = !upscalerEnabled;
      });
      if (toggle instanceof HTMLInputElement) {
        toggle.checked = upscalerEnabled;
        toggle.disabled = !pair;
      }
      if (toggleLabel instanceof HTMLElement) {
        toggleLabel.textContent = upscalerEnabled ? "On" : "Off";
      }
      if (toggleDescription instanceof HTMLElement) {
        toggleDescription.textContent = pair
          ? (upscalerEnabled
            ? "Runs the paired hires workflow; scale, denoise, and upscale method apply."
            : "Base dimensions only - no upscale node or second sampler pass.")
          : "Choose a named workflow above; no matching base/hires pair is available.";
      }
      if (detailerPresetControl instanceof HTMLButtonElement) {
        detailerPresetControl.disabled = !detailerEnabled;
      }
      if (detailerPresetState instanceof HTMLElement) {
        detailerPresetState.textContent = detailerEnabled
          ? (presetMatches ? "Attached A1111 sample" : "Custom")
          : "Unavailable for this workflow";
      }
    };

    if (toggleControl instanceof HTMLElement) toggleControl.hidden = false;
    workflow.addEventListener("change", render);
    if (toggle instanceof HTMLInputElement) {
      toggle.addEventListener("change", () => {
        const desired = toggle.checked ? "true" : "false";
        const current = workflow.selectedOptions.item(0);
        const replacement = pairedWorkflow(current, desired);
        if (!replacement) {
          render();
          return;
        }
        workflow.value = replacement.value;
        workflow.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    if (detailerPresetControl instanceof HTMLButtonElement) {
      detailerPresetControl.addEventListener("click", () => {
        const selected = workflow.selectedOptions.item(0);
        if (selected?.dataset.detailerEnabled !== "true") return;
        detailerPresetFields.forEach(({ field, value }) => {
          if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return;
          field.value = value;
          field.dispatchEvent(new Event("input", { bubbles: true }));
        });
        render();
      });
    }
    detailerPresetFields.forEach(({ field }) => {
      if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return;
      field.addEventListener("input", render);
    });
    render();
  };

  function initializeExperimentLab() {
    const form = document.querySelector("[data-experiment-form]");
    if (!(form instanceof HTMLFormElement)) return;
    const planField = form.querySelector("[data-experiment-plan]");
    const list = form.querySelector("[data-experiment-variant-list]");
    const template = document.querySelector("#experiment-variant-template");
    const labelInput = form.querySelector("[data-experiment-label]");
    const saveButton = form.querySelector("[data-experiment-save-variant]");
    const clearButton = form.querySelector("[data-experiment-clear-editor]");
    const editorHeading = form.querySelector("[data-experiment-editor-heading]");
    const editorStatus = form.querySelector("[data-experiment-editor-status]");
    const outputCount = form.querySelector("[data-experiment-output-count]");
    const empty = form.querySelector("[data-experiment-empty]");
    const count = form.querySelector("[data-experiment-count]");
    const submit = form.querySelector("[data-experiment-submit]");
    const submitStatus = form.querySelector("[data-experiment-submit-status]");
    const randomSeed = form.querySelector("[data-experiment-random-seed]");
    const baseSeed = form.querySelector("[data-experiment-base-seed]");
    const composition = namedControl(form, "composition_mode");
    const secondSubject = form.querySelector("[data-experiment-second-subject]");
    const regionalPrompts = form.querySelector("[data-experiment-regional-prompts]");
    if (!(planField instanceof HTMLTextAreaElement)
        || !(list instanceof HTMLOListElement)
        || !(template instanceof HTMLTemplateElement)
        || !(labelInput instanceof HTMLInputElement)
        || !(saveButton instanceof HTMLButtonElement)) return;

    const maximum = 12;
    const numericNames = new Set([
      "seed", "width", "height", "cfg", "steps", "clip_skip", "hires_scale",
      "hires_denoise", "detailer_guide_size", "detailer_max_size", "detailer_denoise",
      "detailer_bbox_threshold", "detailer_bbox_dilation", "detailer_bbox_crop_factor",
      "detailer_feather",
    ]);
    const profileNames = [
      "subject_id", "subject_2_id", "composition_mode", "character_a_prompt",
      "character_b_prompt", "checkpoint_id", "workflow_id", "prompt", "negative_prompt",
      "detailer_prompt", "detailer_negative_prompt", "seed", "width", "height", "cfg",
      "steps", "sampler", "scheduler", "clip_skip", "hires_scale", "hires_denoise",
      "hires_upscale_method", "detailer_guide_size", "detailer_max_size",
      "detailer_denoise", "detailer_bbox_threshold", "detailer_bbox_dilation",
      "detailer_bbox_crop_factor", "detailer_feather",
    ];
    let variants = [];
    let editingIndex = null;

    const nextLabel = () => `Variant ${String.fromCharCode(65 + Math.min(variants.length, 25))}`;
    const setEditorStatus = (message, tone = "") => {
      if (!(editorStatus instanceof HTMLElement)) return;
      editorStatus.textContent = message;
      editorStatus.className = `experiment-editor-status${tone ? ` ${tone}` : ""}`;
    };
    const formValue = (name) => {
      const control = namedControl(form, name);
      if (control instanceof HTMLInputElement
          || control instanceof HTMLTextAreaElement
          || control instanceof HTMLSelectElement) return control.value;
      return "";
    };
    const collect = () => {
      const result = { label: labelInput.value.trim() };
      profileNames.forEach((name) => {
        const raw = formValue(name);
        result[name] = numericNames.has(name) ? Number(raw) : raw;
      });
      result.loras = Array.from(form.querySelectorAll("[data-lora-native-slot]")).flatMap((slot) => {
        const id = slot.querySelector("[data-lora-native-id]");
        const weight = slot.querySelector("[data-lora-native-weight]");
        if (!(id instanceof HTMLSelectElement) || !id.value) return [];
        return [{ approval_id: id.value, weight: Number(weight?.value || "1") }];
      });
      return result;
    };
    const profileIsValid = (variant) => (
      variant.label.length > 0
      && variant.prompt.trim().length > 0
      && variant.subject_id.length > 0
      && variant.checkpoint_id.length > 0
      && variant.workflow_id.length > 0
      && profileNames.filter((name) => numericNames.has(name)).every(
        (name) => Number.isFinite(variant[name]),
      )
      && variant.loras.every((item) => Number.isFinite(item.weight))
    );
    const profileIsWarmReady = (variant) => {
      const checkpoint = Array.from(namedControl(form, "checkpoint_id")?.options || [])
        .find((option) => option.value === variant.checkpoint_id);
      if (checkpoint?.dataset.warmReady !== "true") return false;
      return variant.loras.every((item) => {
        const option = form.querySelector(`[data-lora-option][data-lora-id="${item.approval_id}"]`);
        return option?.dataset.warmReady === "true";
      });
    };
    const dispatchProfile = (variant) => {
      profileNames.forEach((name) => {
        const control = namedControl(form, name);
        if (!(control instanceof HTMLInputElement)
            && !(control instanceof HTMLTextAreaElement)
            && !(control instanceof HTMLSelectElement)) return;
        control.value = String(variant[name] ?? "");
        control.dispatchEvent(new Event("change", { bubbles: true }));
        control.dispatchEvent(new Event("input", { bubbles: true }));
      });
      window.dispatchEvent(new CustomEvent("gen-automation:apply-lora-stack", {
        detail: {
          selections: variant.loras.map((item) => ({
            id: item.approval_id,
            weight: String(item.weight),
          })),
        },
      }));
      labelInput.value = variant.label;
    };
    const valueLabel = (name, value) => {
      const control = namedControl(form, name);
      if (!(control instanceof HTMLSelectElement)) return String(value);
      const option = Array.from(control.options).find((candidate) => candidate.value === value);
      return (option?.textContent || String(value)).split(" · ")[0].trim();
    };
    const stackKey = (variant) => variant.loras
      .map((item) => `${item.approval_id}:${item.weight}`)
      .join("|");
    const diffLabels = (variant, baseline, index) => {
      if (index === 0 || !baseline) return ["Baseline"];
      const diffs = [];
      if (variant.checkpoint_id !== baseline.checkpoint_id) {
        diffs.push(`Checkpoint: ${valueLabel("checkpoint_id", variant.checkpoint_id)}`);
      }
      if (variant.workflow_id !== baseline.workflow_id) {
        diffs.push(`Workflow: ${valueLabel("workflow_id", variant.workflow_id)}`);
      }
      if (stackKey(variant) !== stackKey(baseline)) diffs.push("LoRA stack");
      if (variant.prompt !== baseline.prompt) diffs.push("Prompt");
      if (variant.negative_prompt !== baseline.negative_prompt) diffs.push("Negative prompt");
      if (variant.steps !== baseline.steps) diffs.push(`Steps ${variant.steps}`);
      if (variant.cfg !== baseline.cfg) diffs.push(`CFG ${variant.cfg}`);
      if (variant.sampler !== baseline.sampler || variant.scheduler !== baseline.scheduler) {
        diffs.push(`${variant.sampler} / ${variant.scheduler}`);
      }
      if (variant.width !== baseline.width || variant.height !== baseline.height) {
        diffs.push(`${variant.width}×${variant.height}`);
      }
      if (variant.detailer_prompt !== baseline.detailer_prompt
          || variant.detailer_denoise !== baseline.detailer_denoise) diffs.push("Face detailer");
      if (variant.hires_scale !== baseline.hires_scale
          || variant.hires_denoise !== baseline.hires_denoise) diffs.push("Hires settings");
      return diffs.length ? diffs : ["Same settings as A"];
    };
    const serialize = () => {
      planField.value = JSON.stringify(variants);
    };
    const render = () => {
      list.replaceChildren();
      variants.forEach((variant, index) => {
        const fragment = template.content.cloneNode(true);
        const card = fragment.querySelector("[data-experiment-variant-card]");
        const letter = fragment.querySelector("[data-experiment-variant-letter]");
        const name = fragment.querySelector("[data-experiment-variant-name]");
        const diffs = fragment.querySelector("[data-experiment-diffs]");
        card.dataset.experimentIndex = String(index);
        letter.textContent = String.fromCharCode(65 + index);
        name.value = variant.label;
        diffLabels(variant, variants[0], index).forEach((label) => {
          diffs.append(createNode("span", "experiment-diff-chip", label));
        });
        list.append(fragment);
      });
      const images = variants.length * Math.max(1, integerValue(outputCount?.value, 2));
      form.querySelector("[data-experiment-summary-variants]").textContent = String(variants.length);
      form.querySelector("[data-experiment-summary-images]").textContent = String(images);
      form.querySelector("[data-experiment-summary-jobs]").textContent = String(variants.length);
      if (count) count.textContent = `${variants.length} / ${maximum}`;
      if (empty) empty.hidden = variants.length !== 0;
      const ready = variants.length >= 2;
      if (submit instanceof HTMLButtonElement) submit.disabled = !ready;
      if (submitStatus) {
        submitStatus.textContent = ready
          ? `${images} images in ${variants.length} jobs, queued for one allocation.`
          : "Add at least two variants.";
      }
      serialize();
    };
    const finishEdit = () => {
      editingIndex = null;
      labelInput.value = nextLabel();
      saveButton.textContent = "Add current profile";
      if (editorHeading) editorHeading.textContent = `Build ${nextLabel()}`;
    };

    try {
      const restored = JSON.parse(planField.value || "[]");
      if (Array.isArray(restored)) variants = restored.slice(0, maximum);
    } catch (_error) {
      variants = [];
    }
    labelInput.value = nextLabel();
    render();

    saveButton.addEventListener("click", () => {
      if (!form.checkValidity()) {
        form.reportValidity();
        setEditorStatus("Fix the highlighted profile setting before saving this variant.", "warning");
        return;
      }
      const variant = collect();
      if (!profileIsValid(variant)) {
        setEditorStatus("Complete the prompt, model choices, and numeric settings first.", "warning");
        form.reportValidity();
        return;
      }
      if (!profileIsWarmReady(variant)) {
        setEditorStatus(
          "This profile includes a checkpoint or LoRA that is not in the worker manifest. Onboard it and redeploy the worker before queueing this variant.",
          "warning",
        );
        return;
      }
      const duplicateName = variants.some((item, index) => (
        index !== editingIndex && item.label.toLowerCase() === variant.label.toLowerCase()
      ));
      if (duplicateName) {
        setEditorStatus("Use a unique label for each variant.", "warning");
        labelInput.focus();
        return;
      }
      if (editingIndex === null) {
        if (variants.length >= maximum) {
          setEditorStatus(`The comparison is limited to ${maximum} variants.`, "warning");
          return;
        }
        variants.push(variant);
        setEditorStatus(`${variant.label} added. Change only what you want to compare.`, "success");
      } else {
        variants[editingIndex] = variant;
        setEditorStatus(`${variant.label} updated.`, "success");
      }
      finishEdit();
      render();
    });

    list.addEventListener("input", (event) => {
      const name = event.target.closest("[data-experiment-variant-name]");
      const card = event.target.closest("[data-experiment-variant-card]");
      if (!(name instanceof HTMLInputElement) || !card) return;
      const index = integerValue(card.dataset.experimentIndex, -1);
      if (!variants[index]) return;
      variants[index].label = name.value.slice(0, 80);
      serialize();
    });
    list.addEventListener("click", (event) => {
      const button = event.target.closest("[data-experiment-action]");
      const card = event.target.closest("[data-experiment-variant-card]");
      if (!(button instanceof HTMLButtonElement) || !card) return;
      const index = integerValue(card.dataset.experimentIndex, -1);
      const variant = variants[index];
      if (!variant) return;
      if (button.dataset.experimentAction === "load") {
        editingIndex = index;
        dispatchProfile(variant);
        saveButton.textContent = "Update variant";
        if (editorHeading) editorHeading.textContent = `Editing ${variant.label}`;
        form.querySelector("[data-experiment-editor]")?.scrollIntoView({ behavior: "smooth" });
      } else if (button.dataset.experimentAction === "duplicate") {
        if (variants.length >= maximum) return;
        const duplicate = JSON.parse(JSON.stringify(variant));
        duplicate.label = `${variant.label} copy`.slice(0, 80);
        variants.splice(index + 1, 0, duplicate);
        render();
      } else if (button.dataset.experimentAction === "delete") {
        variants.splice(index, 1);
        finishEdit();
        render();
      }
    });
    if (clearButton instanceof HTMLButtonElement) {
      clearButton.addEventListener("click", () => {
        finishEdit();
        setEditorStatus("Edit cleared; the profile fields are unchanged.");
      });
    }
    if (outputCount instanceof HTMLSelectElement) outputCount.addEventListener("change", render);
    if (randomSeed instanceof HTMLButtonElement && baseSeed instanceof HTMLInputElement) {
      randomSeed.addEventListener("click", () => {
        const values = new Uint32Array(2);
        window.crypto.getRandomValues(values);
        baseSeed.value = String((BigInt(values[0]) << 31n) | BigInt(values[1] >>> 1));
      });
    }
    if (composition instanceof HTMLSelectElement) {
      const renderComposition = () => {
        const duo = composition.value === "duo";
        if (secondSubject) secondSubject.hidden = !duo;
        if (regionalPrompts) regionalPrompts.hidden = !duo;
      };
      composition.addEventListener("change", renderComposition);
      renderComposition();
    }
    form.addEventListener("submit", (event) => {
      variants.forEach((variant) => { variant.label = variant.label.trim(); });
      if (variants.length < 2 || variants.some((variant) => (
        !profileIsValid(variant) || !profileIsWarmReady(variant)
      ))) {
        event.preventDefault();
        if (submitStatus) submitStatus.textContent = "Fix invalid variants before queueing.";
        return;
      }
      serialize();
      if (submit instanceof HTMLButtonElement) {
        submit.disabled = true;
        submit.textContent = "Queuing comparison…";
      }
    }, { capture: true });
  }

  function initializeExperimentWarmSession() {
    document.querySelectorAll("[data-experiment-warm-session]").forEach((panel) => {
      const statusNode = panel.querySelector("[data-warm-session-status]");
      const heading = panel.querySelector("[data-warm-session-heading]");
      const actions = panel.querySelector("[data-warm-session-actions]");
      const buttons = Array.from(panel.querySelectorAll("[data-warm-start], [data-warm-extend], [data-warm-end]"));
      const csrf = panel.dataset.csrfToken || "";
      let endpointAvailable = false;
      let statusTimer = 0;
      const safeEndpoint = (value) => {
        try {
          const url = new URL(value, window.location.href);
          return url.origin === window.location.origin ? url.href : "";
        } catch (_error) { return ""; }
      };
      const render = (payload) => {
        if (!isRecord(payload) || payload.available !== true) return false;
        endpointAvailable = true;
        if (actions) actions.hidden = false;
        const state = ["off", "starting", "warm", "ending"].includes(payload.state)
          ? payload.state : "off";
        if (heading) heading.textContent = state === "warm" ? "GPU warm for follow-up tests" : `Warm session: ${state}`;
        const seconds = Math.max(0, integerValue(payload.remaining_seconds, 0));
        const cost = typeof payload.hourly_rate_usd === "string" ? payload.hourly_rate_usd : "";
        if (statusNode) {
          if (state === "warm") {
            statusNode.textContent = `${Math.ceil(seconds / 60)} minutes remain${cost ? ` at up to $${cost}/hour` : ""}.`;
          } else if (state === "starting") {
            statusNode.textContent = "Allocating the single GPU now. Keep editing; queued variants will reuse it when ready.";
          } else if (state === "ending") {
            statusNode.textContent = "Releasing the warm worker and returning to scale-to-zero.";
          } else {
            statusNode.textContent = "Batch mode remains available and scales to zero after the queue.";
          }
        }
        buttons.forEach((button) => {
          if (button.hasAttribute("data-warm-start")) button.hidden = state !== "off";
          if (button.hasAttribute("data-warm-extend")) button.hidden = state !== "warm";
          if (button.hasAttribute("data-warm-end")) button.hidden = !["starting", "warm"].includes(state);
        });
        return state;
      };
      const statusUrl = safeEndpoint(panel.dataset.warmStatusUrl || "");
      if (!statusUrl) return;
      const scheduleStatusRefresh = (state) => {
        window.clearTimeout(statusTimer);
        const delay = ["starting", "ending"].includes(state)
          ? 5000
          : state === "warm" ? 15000 : 0;
        if (delay) statusTimer = window.setTimeout(refreshStatus, delay);
      };
      const refreshStatus = async () => {
        try {
          const response = await fetch(statusUrl, {
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
          });
          if ([404, 501].includes(response.status)) {
            if (statusNode) statusNode.textContent = "Queue all variants together for one allocation; optional warm follow-up is not enabled yet.";
            return;
          }
          if (!response.ok) throw new Error("warm status unavailable");
          const state = render(await response.json());
          if (state) scheduleStatusRefresh(state);
        } catch (_error) {
          if (statusNode) statusNode.textContent = "Warm-session status is temporarily unavailable. Normal batch mode still works.";
        }
      };
      refreshStatus();
      buttons.forEach((button) => button.addEventListener("click", async () => {
        if (!endpointAvailable) return;
        const url = safeEndpoint(
          button.hasAttribute("data-warm-start") ? panel.dataset.warmStartUrl
            : button.hasAttribute("data-warm-extend") ? panel.dataset.warmExtendUrl
              : panel.dataset.warmEndUrl,
        );
        if (!url) return;
        buttons.forEach((item) => { item.disabled = true; });
        try {
          const body = button.hasAttribute("data-warm-end") ? {} : { duration_minutes: 15 };
          const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf },
            body: JSON.stringify(body),
          });
          if (!response.ok) throw new Error("warm session update failed");
          const state = render(await response.json());
          if (state) scheduleStatusRefresh(state);
        } catch (_error) {
          if (statusNode) statusNode.textContent = "Warm session could not be changed. The queued experiment is unaffected.";
        } finally {
          buttons.forEach((item) => { item.disabled = false; });
        }
      }));
    });
  }

  function initializeExperimentResults() {
    const root = document.querySelector("[data-experiment-results]");
    if (!(root instanceof HTMLElement)) return;
    const statusNode = root.querySelector("[data-experiment-results-status]");
    const progressLabel = root.querySelector("[data-experiment-progress-label]");
    const progressPercent = root.querySelector("[data-experiment-progress-percent]");
    const progressbar = root.querySelector("[data-experiment-progressbar]");
    const progressFill = root.querySelector("[data-experiment-progress-fill]");
    const known = new Set();
    let stopped = false;
    const safeUrl = (value) => {
      try {
        const url = new URL(value, window.location.href);
        return url.origin === window.location.origin ? url.href : "";
      } catch (_error) { return ""; }
    };
    const progressUrl = safeUrl(root.dataset.progressUrl || "");
    const assetsUrl = safeUrl(root.dataset.assetsUrl || "");
    const assetUrl = (value, id) => {
      const url = safeUrl(value);
      return url && new URL(url).pathname.startsWith(`/dashboard/assets/${id}/`) ? url : "";
    };
    const assetCard = (asset) => {
      const id = typeof asset.asset_id === "string" ? asset.asset_id : "";
      const view = assetUrl(asset.view_url, id);
      if (!id || !view) return null;
      const card = createNode("article", "asset-card experiment-asset-card");
      card.dataset.assetCard = "";
      card.dataset.assetId = id;
      card.dataset.outputIndex = String(Math.max(0, integerValue(asset.output_index, 0)));
      const link = createNode("a", "asset-preview-link");
      link.href = view;
      const image = document.createElement("img");
      image.src = view;
      image.alt = `Generated sample ${Math.max(0, integerValue(asset.output_index, 0)) + 1}`;
      image.loading = "lazy";
      image.width = Math.max(1, integerValue(asset.width, 1));
      image.height = Math.max(1, integerValue(asset.height, 1));
      link.append(image);
      card.append(link);
      const footer = createNode("div", "experiment-asset-footer");
      footer.append(createNode("strong", "", `Sample ${Math.max(0, integerValue(asset.output_index, 0)) + 1}`));
      const download = assetUrl(asset.download_url, id);
      if (download) {
        const anchor = createNode("a", "text-button", "Download");
        anchor.href = download;
        footer.append(anchor);
      }
      card.append(footer);
      const detailsUrl = assetUrl(asset.generation_details_url, id);
      if (detailsUrl) {
        const details = createNode("details", "generation-details");
        details.dataset.generationDetails = "";
        details.dataset.generationDetailsUrl = detailsUrl;
        details.append(createNode("summary", "", "Full prompt & settings"));
        const body = createNode("div", "generation-details-body");
        body.dataset.generationDetailsBody = "";
        const loading = createNode("p", "generation-details-status", "Open to load exact settings.");
        loading.dataset.generationDetailsStatus = "";
        body.append(loading);
        details.append(body);
        card.append(details);
      }
      return card;
    };
    const refreshAssets = async () => {
      if (!assetsUrl || stopped) return;
      try {
        const response = await fetch(assetsUrl, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("asset refresh failed");
        const payload = await response.json();
        if (!isRecord(payload) || !Array.isArray(payload.variants)) throw new Error("invalid assets");
        payload.variants.forEach((variant) => {
          if (!isRecord(variant) || !Array.isArray(variant.assets)) return;
          const index = Math.max(0, integerValue(variant.index, 0));
          const grid = root.querySelector(`[data-experiment-variant-assets="${index}"]`);
          const empty = root.querySelector(`[data-experiment-variant-empty="${index}"]`);
          if (!(grid instanceof HTMLElement)) return;
          variant.assets.sort((a, b) => integerValue(a.output_index, 0) - integerValue(b.output_index, 0));
          variant.assets.forEach((asset) => {
            if (!isRecord(asset) || known.has(asset.asset_id)) return;
            const card = assetCard(asset);
            if (!card) return;
            known.add(asset.asset_id);
            grid.append(card);
          });
          if (empty) empty.hidden = grid.childElementCount > 0;
          document.dispatchEvent(new CustomEvent("gen-automation:assets-updated", { detail: { root: grid } }));
        });
        if (statusNode) statusNode.textContent = "Results are updated automatically.";
      } catch (_error) {
        if (statusNode) statusNode.textContent = "Network interrupted live results; retrying automatically.";
      }
    };
    const refreshProgress = async () => {
      if (!progressUrl || stopped) return;
      try {
        const response = await fetch(progressUrl, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("progress failed");
        const payload = await response.json();
        if (!isRecord(payload) || !Array.isArray(payload.variants)) throw new Error("invalid progress");
        const generated = Math.max(0, integerValue(payload.generated, 0));
        const expected = Math.max(0, integerValue(payload.expected, 0));
        const percent = Math.max(0, Math.min(100, Number(payload.percent) || 0));
        if (progressLabel) progressLabel.textContent = `${generated} / ${expected} images`;
        if (progressPercent) progressPercent.textContent = `${percent.toFixed(1)}%`;
        if (progressFill) progressFill.style.width = `${percent}%`;
        if (progressbar) {
          progressbar.setAttribute("aria-valuemax", String(expected));
          progressbar.setAttribute("aria-valuenow", String(generated));
        }
        payload.variants.forEach((variant) => {
          if (!isRecord(variant) || !isRecord(variant.stage) || !isRecord(variant.images)) return;
          const node = root.querySelector(`[data-experiment-variant="${integerValue(variant.index, 0)}"] [data-experiment-variant-status]`);
          if (node) node.textContent = `${variant.stage.label || "Queued"} · ${integerValue(variant.images.generated, 0)}/${integerValue(variant.images.expected, 0)}`;
        });
        await refreshAssets();
        if (payload.complete === true) {
          stopped = true;
          if (statusNode) statusNode.textContent = "Comparison complete. Open any image to inspect it at full size.";
          return;
        }
      } catch (_error) {
        if (statusNode) statusNode.textContent = "Network interrupted live progress; retrying automatically.";
      }
      window.setTimeout(refreshProgress, 3000);
    };
    root.querySelectorAll("[data-experiment-result-tab]").forEach((tab) => {
      tab.addEventListener("click", () => {
        root.querySelectorAll("[data-experiment-result-tab]").forEach((candidate) => candidate.setAttribute("aria-selected", String(candidate === tab)));
        root.querySelectorAll("[data-experiment-variant]").forEach((column) => column.classList.toggle("mobile-selected", column.dataset.experimentVariant === tab.dataset.experimentResultTab));
      });
    });
    root.querySelector('[data-experiment-variant="0"]')?.classList.add("mobile-selected");
    refreshProgress();
  }

  function initializeReviewBootstrap() {
    const form = document.querySelector("form[data-review-bootstrap]");
    if (!(form instanceof HTMLFormElement)) return;
    const submit = () => {
      if (!form.isConnected || form.dataset.reviewBootstrapSubmitted === "true") return;
      form.dataset.reviewBootstrapSubmitted = "true";
      form.setAttribute("aria-busy", "true");
      form.requestSubmit();
    };
    window.requestAnimationFrame(submit);
  }

  const experimentFormPresent = Boolean(document.querySelector("[data-experiment-form]"));
  const reusedImageSettings = consumePendingImageProfile();
  if (!reusedImageSettings && !experimentFormPresent) restoreAutomationDraft();
  initializeSamePageScrollPreservation();
  initializeLoraPicker();
  initializeAutomationBuilder();
  initializeWorkflowRefinement();
  initializeCharacterComposition();
  initializeImageSettingsSummary();
  initializeAutomationPresets();
  if (!experimentFormPresent) initializeAutomationDraft();
  initializeReleaseLibrary();
  initializeAssetSorting();
  initializeWildcardLibraryTools();
  initializeBulkReview();
  initializeReviewActions();
  initializeReviewCompletionInspectionFlush();
  initializeGenerationDetails();
  initializeLiveGeneratedAssets();
  clearAutomationDraftAfterQueue();
  initializeGenerationProgress();
  initializeDeliveryReauthentication();
  initializeDeliveryProgress();
  initializeExperimentLab();
  initializeExperimentWarmSession();
  initializeExperimentResults();
  initializeReviewBootstrap();
})();
