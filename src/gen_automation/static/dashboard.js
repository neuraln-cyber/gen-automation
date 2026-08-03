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
  const scopedStorageKey = (key) => {
    const scope = document.body?.dataset.automationStorageScope?.trim() || "unknown-user";
    return `${key}:${scope}`;
  };
  const AUTOMATION_PRESET_FIELDS = Object.freeze([
    "subject_id",
    "checkpoint_id",
    "workflow_id",
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

  const collectAutomationProfile = (form) => {
    const fields = {};
    AUTOMATION_PRESET_FIELDS.forEach((name) => {
      const control = namedControl(form, name);
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
    const checkpoint = namedControl(form, "checkpoint_id")?.selectedOptions?.item(0);
    const workflow = namedControl(form, "workflow_id")?.selectedOptions?.item(0);
    return {
      schema_version: 1,
      fields,
      loras,
      matches: {
        subject_name: subject?.dataset.subjectName || "",
        subject_slug: subject?.dataset.subjectSlug || "",
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
    const matchedSelects = new Set(["subject_id", "checkpoint_id", "workflow_id"]);
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
    let targetFollowsQueue = typeof form.dataset.restoredTargetFollowsQueue === "string"
      ? form.dataset.restoredTargetFollowsQueue === "true"
      : Boolean(desiredCount && !planData.value.trim());
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
        negative_prompt: optionalText(field(row, "negative_prompt").value),
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

    const addBatch = (batch, before = null) => {
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
      updateBuilder();
      return row;
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
      if (desiredCount && targetFollowsQueue
          && form.dataset.applyingAutomationProfile !== "true") {
        desiredCount.value = String(Math.max(1, Math.min(100, totalImages)));
      }
      form.dataset.targetFollowsQueue = String(targetFollowsQueue);
      addButtons.forEach((button) => { button.disabled = rows.length >= 50; });
      planData.value = JSON.stringify(rows.map(readRow));

      const batchesSummary = document.querySelector("#summary-batches");
      const imagesSummary = document.querySelector("#summary-images");
      const jobsSummary = document.querySelector("#summary-jobs");
      const targetSummary = document.querySelector("#summary-target");
      const mobileImageSummaries = document.querySelectorAll("[data-mobile-summary-images]");
      const mobileJobSummaries = document.querySelectorAll("[data-mobile-summary-jobs]");
      const mobileTargetSummaries = document.querySelectorAll("[data-mobile-summary-target]");
      const note = document.querySelector("#summary-note");
      if (batchesSummary) batchesSummary.textContent = String(rows.length);
      if (imagesSummary) imagesSummary.textContent = totalImages.toLocaleString();
      if (jobsSummary) jobsSummary.textContent = totalJobs.toLocaleString();
      if (targetSummary) targetSummary.textContent = String(integerValue(desiredCount && desiredCount.value));
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
    initialBatches.forEach((batch) => addBatch(batch));

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
      updateBuilder();
    });
    if (matchQueueTarget instanceof HTMLButtonElement) {
      matchQueueTarget.addEventListener("click", () => {
        targetFollowsQueue = true;
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
    defaultPrompt && defaultPrompt.addEventListener("input", () => {
      const rows = batchRows();
      rows.forEach((row) => {
        const prompt = field(row, "prompt");
        if (prompt.value === previousDefaultPrompt) prompt.value = defaultPrompt.value;
      });
      previousDefaultPrompt = defaultPrompt.value;
      updateBuilder();
    });
    defaultNegative && defaultNegative.addEventListener("input", () => {
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

  function initializeDeliveryAutoRefresh() {
    const panel = document.querySelector("[data-delivery-auto-refresh]");
    if (!(panel instanceof HTMLElement)) return;
    const status = panel.querySelector("[data-delivery-refresh-status]");
    const delay = Math.min(
      30000,
      Math.max(5000, integerValue(panel.dataset.deliveryAutoRefresh, 8000)),
    );
    let timer = null;
    const schedule = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (document.visibilityState === "hidden") {
          schedule();
          return;
        }
        if (status instanceof HTMLElement) status.textContent = "Checking output progress...";
        window.location.reload();
      }, delay);
    };
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") schedule();
    });
    schedule();
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
      return [...controls, ...loraWeights].find((control) => !control.checkValidity());
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
        option.textContent = preset.name;
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
        const disclosure = invalidControl.closest("details");
        if (disclosure) disclosure.open = true;
        invalidControl.focus();
        invalidControl.reportValidity();
        setStatus("Fix the highlighted setting before saving this preset.", "warning");
        return;
      }
      const existing = presets.find((preset) => preset.name.toLowerCase() === name.toLowerCase());
      const id = existing?.id || `${slugify(name) || "preset"}-${Date.now().toString(36)}`;
      const saved = {
        id,
        name,
        profile: collectAutomationProfile(form),
        updated_at: new Date().toISOString(),
      };
      presets = [saved, ...presets.filter((preset) => preset.id !== id)].slice(0, 30);
      try {
        writeStoredAutomationPresets(presets);
        render(id);
        setStatus(existing ? "Preset updated." : "Preset saved.", "success");
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
      const result = applyAutomationProfile(form, preset.profile);
      if (!result.applied) {
        setStatus("That preset contains invalid saved data.", "warning");
        return;
      }
      const invalidControl = firstInvalidPresetControl();
      if (invalidControl) {
        let disclosure = invalidControl.closest("details");
        while (disclosure) {
          disclosure.open = true;
          disclosure = disclosure.parentElement?.closest("details") || null;
        }
        invalidControl.focus();
        invalidControl.reportValidity();
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
      setStatus(`${preset.name} loaded.`, "success");
    });
    deleteButton.addEventListener("click", () => {
      const preset = presets.find((item) => item.id === select.value);
      if (!preset) return;
      presets = presets.filter((item) => item.id !== preset.id);
      try {
        writeStoredAutomationPresets(presets);
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
          schema_version: 1,
          exported_at: new Date().toISOString(),
          presets,
        }, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "generation-settings-presets.json";
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
          const imported = requested.filter((item) => (
            item && typeof item === "object"
            && typeof item.id === "string"
            && typeof item.name === "string"
            && item.profile && typeof item.profile === "object"
          )).slice(0, 30);
          if (imported.length === 0) throw new Error("empty preset export");
          const importedIds = new Set(imported.map((item) => item.id));
          const importedNames = new Set(imported.map((item) => item.name.toLowerCase()));
          presets = [
            ...imported,
            ...presets.filter(
              (item) => !importedIds.has(item.id) && !importedNames.has(item.name.toLowerCase()),
            ),
          ].slice(0, 30);
          writeStoredAutomationPresets(presets);
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
    const countLabels = document.querySelectorAll("[data-selected-count]");
    const selectionStatus = form.querySelector("[data-bulk-selection-status]");
    const currentXCount = Math.max(0, integerValue(form.dataset.xSelectedCount, 0));
    const xCapacity = Math.max(1, integerValue(form.dataset.xCapacity, 4));
    let lastClickedCheckbox = null;
    document.querySelectorAll("[data-review-selection-controls], [data-review-tools]").forEach((item) => {
      item.hidden = false;
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
      const selectedForX = selectedCards.filter((card) => card.dataset.selectedForX === "true").length;
      const netNewForX = selected - selectedForX;
      const remainingXSlots = Math.max(0, xCapacity - currentXCount);
      const exceedsXCapacity = netNewForX > remainingXSlots;

      if (acceptButton) acceptButton.disabled = selected === 0 || hasSevereSelection;
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
            || (filter === "ai" && card.dataset.aiExcluded === "true");
          card.hidden = !matches;
        });
        updateExcludedHeadings();
        updateSelection();
      });
    });
    updateExcludedHeadings();
    updateSelection();
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

    const promptSource = isRecord(value.prompts) ? value.prompts : {};
    const prompts = {};
    ["positive", "negative", "detailer_positive", "detailer_negative"].forEach((name) => {
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

  function initializeGenerationDetails() {
    document.querySelectorAll("[data-generation-details]").forEach((panel) => {
      panel.dataset.generationDetailsState = "idle";
      panel.addEventListener("toggle", () => {
        if (panel.open) loadGenerationDetails(panel);
      });
    });
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
    let timer = null;
    let networkFailures = 0;

    const safeCount = (value) => (
      Number.isInteger(value) && value >= 0 ? value : 0
    );

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
      const generated = safeCount(payload.images.generated);
      const expected = Math.max(1, safeCount(payload.images.expected));
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
      if (imageCount) imageCount.textContent = `${generated} / ${safeCount(payload.images.expected)} images`;
      if (progressBar) {
        progressBar.max = expected;
        progressBar.value = Math.min(generated, expected);
      }
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
        if (!terminal) schedule(safeCount(payload.poll_after_ms) || 5000);
      } catch (_error) {
        networkFailures += 1;
        if (detail) detail.textContent = "Connection interrupted. Retrying automatically…";
        schedule(Math.min(30000, 3000 * (2 ** Math.min(networkFailures, 3))));
      }
    };

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refresh();
    });
    schedule(1500);
  }

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
      .replace(/\b(base|hires|highres|upscale|upscaler)\b/g, " ")
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

  const reusedImageSettings = consumePendingImageProfile();
  if (!reusedImageSettings) restoreAutomationDraft();
  initializeLoraPicker();
  initializeAutomationBuilder();
  initializeWorkflowRefinement();
  initializeAutomationPresets();
  initializeAutomationDraft();
  initializeReleaseLibrary();
  initializeAssetSorting();
  initializeWildcardLibraryTools();
  initializeBulkReview();
  initializeGenerationDetails();
  clearAutomationDraftAfterQueue();
  initializeGenerationProgress();
  initializeDeliveryReauthentication();
  initializeDeliveryAutoRefresh();
})();
