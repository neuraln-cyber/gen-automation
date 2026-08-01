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
    const savePresetButton = picker.querySelector("[data-lora-save-preset]");
    const loadPresetButton = picker.querySelector("[data-lora-load-preset]");
    const maximum = Math.min(
      nativeSlots.length,
      Math.max(1, integerValue(picker.dataset.maxSelections, nativeSlots.length)),
    );
    const presetStorageKey = "gen-automation:lora-stack:v1";
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

    const storedPreset = () => {
      try {
        const raw = window.localStorage.getItem(presetStorageKey);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        const seen = new Set();
        return parsed.slice(0, maximum).flatMap((item) => {
          if (!item || typeof item !== "object") return [];
          const id = typeof item.approval_id === "string" ? item.approval_id : "";
          const weight = String(item.weight ?? "");
          if (!optionById.has(id) || seen.has(id) || !validWeight(weight)) return [];
          seen.add(id);
          return [{ id, weight }];
        });
      } catch (_error) {
        return [];
      }
    };

    const updatePresetAvailability = () => {
      if (savePresetButton) {
        savePresetButton.disabled = selections.length === 0
          || selections.some((item) => !validWeight(item.weight));
      }
      if (loadPresetButton) loadPresetButton.disabled = storedPreset().length === 0;
    };

    const syncCanonicalSlots = () => {
      nativeSlots.forEach((slot, index) => {
        const idControl = slot.querySelector("[data-lora-native-id]");
        const weightControl = slot.querySelector("[data-lora-native-weight]");
        const selection = selections[index];
        if (idControl) idControl.value = selection ? selection.id : "";
        if (weightControl) weightControl.value = selection ? selection.weight : "";
      });
      updatePresetAvailability();
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

    if (savePresetButton) {
      savePresetButton.addEventListener("click", () => {
        try {
          window.localStorage.setItem(
            presetStorageKey,
            JSON.stringify(
              selections.map((item) => ({ approval_id: item.id, weight: item.weight })),
            ),
          );
          updatePresetAvailability();
          setFeedback("LoRA stack saved in this browser.", "success");
        } catch (_error) {
          setFeedback("This browser could not save the LoRA stack.", "warning");
        }
      });
    }

    if (loadPresetButton) {
      loadPresetButton.addEventListener("click", () => {
        const saved = storedPreset();
        if (saved.length === 0) {
          updatePresetAvailability();
          setFeedback("The saved stack is no longer available.", "warning");
          return;
        }
        selections = saved;
        renderSelections();
        setFeedback("Saved LoRA stack loaded.", "success");
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
    const outputsPerJob = form.querySelector("[data-outputs-per-job]");
    const plannedJobCount = form.querySelector("[data-planned-job-count]");
    const desiredCount = form.querySelector("[data-desired-count]");
    const defaultPrompt = form.querySelector("[data-default-prompt]");
    const defaultNegative = form.querySelector("[data-default-negative]");
    const defaultDetailer = form.querySelector("[data-default-detailer]");
    const defaultDetailerNegative = form.querySelector("[data-default-detailer-negative]");
    const defaultSeed = form.querySelector("[data-default-seed]");
    const titleInput = form.querySelector("[data-title-input]");
    const slugInput = form.querySelector("[data-slug-input]");
    const submitButtons = Array.from(form.querySelectorAll(".queue-submit"));
    const serverDisabled = submitButtons.some((button) => button.disabled);
    const maximumProviderJobs = 10_000;
    let lastPrompt = null;
    let slugWasEdited = Boolean(slugInput && slugInput.value.trim());
    let previousDefaultPrompt = defaultPrompt ? defaultPrompt.value : "";

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
      negative_prompt: null,
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
      field(row, "negative_prompt").value = batch.negative_prompt ?? "";
      field(row, "detailer_prompt").value = batch.detailer_prompt ?? "";
      field(row, "detailer_negative_prompt").value = batch.detailer_negative_prompt ?? "";
      field(row, "seed").value = batch.seed ?? "";
      list.insertBefore(fragment, before);
      updateBuilder();
      return row;
    };

    const setBatchValidity = () => {
      const rows = batchRows();
      const names = new Map();
      rows.forEach((row) => {
        const nameInput = field(row, "name");
        const key = nameInput.value.trim().toLowerCase();
        const duplicate = key && names.has(key);
        nameInput.setCustomValidity(duplicate ? "Batch labels must be unique." : "");
        if (key && !duplicate) names.set(key, row);
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
        const wildcardNames = Array.from(
          field(row, "prompt").value.matchAll(wildcardPattern),
          (match) => match[1],
        );
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
              chip.className = "batch-wildcard-chip";
              chip.textContent = `__${wildcard}__`;
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
      addButtons.forEach((button) => { button.disabled = rows.length >= 50; });

      const batchesSummary = document.querySelector("#summary-batches");
      const imagesSummary = document.querySelector("#summary-images");
      const jobsSummary = document.querySelector("#summary-jobs");
      const targetSummary = document.querySelector("#summary-target");
      const mobileImageSummaries = document.querySelectorAll("[data-mobile-summary-images]");
      const mobileJobSummaries = document.querySelectorAll("[data-mobile-summary-jobs]");
      const note = document.querySelector("#summary-note");
      if (batchesSummary) batchesSummary.textContent = String(rows.length);
      if (imagesSummary) imagesSummary.textContent = totalImages.toLocaleString();
      if (jobsSummary) jobsSummary.textContent = totalJobs.toLocaleString();
      if (targetSummary) targetSummary.textContent = String(integerValue(desiredCount && desiredCount.value));
      mobileImageSummaries.forEach((item) => { item.textContent = totalImages.toLocaleString(); });
      mobileJobSummaries.forEach((item) => { item.textContent = totalJobs.toLocaleString(); });

      setBatchValidity();
      const missingPrompt = rows.some((row) => !field(row, "prompt").value.trim());
      const tooManyJobs = totalJobs > maximumProviderJobs;
      const targetTooLarge = desiredCount && integerValue(desiredCount.value) > totalImages;
      if (note) {
        if (missingPrompt) {
          note.textContent = "Every batch needs a prompt structure before this run can start.";
          note.className = "summary-note warning";
        } else if (tooManyJobs) {
          note.textContent = `Reduce the queue to ${maximumProviderJobs.toLocaleString()} GPU jobs or fewer.`;
          note.className = "summary-note warning";
        } else if (targetTooLarge) {
          note.textContent = "Reduce the final-set target or generate more images.";
          note.className = "summary-note warning";
        } else {
          note.textContent = `${totalImages.toLocaleString()} images will run as ${totalJobs.toLocaleString()} efficient GPU jobs.`;
          note.className = "summary-note ready";
        }
      }
      submitButtons.forEach((button) => {
        button.disabled = serverDisabled
          || missingPrompt
          || tooManyJobs
          || Boolean(targetTooLarge)
          || totalImages < 1;
      });
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
        negative_prompt: null,
        detailer_prompt: null,
        detailer_negative_prompt: null,
        seed: null,
      }];
    }
    initialBatches.forEach((batch) => addBatch(batch));

    addButtons.forEach((button) => button.addEventListener("click", () => {
      const row = addBatch(newBatchDefaults());
      field(row, "name").focus();
      row.scrollIntoView({ behavior: "smooth", block: "center" });
    }));

    list.addEventListener("change", (event) => {
      if (!event.target.matches("[data-batch-wildcard]")) return;
      const token = event.target.value;
      if (!token) return;
      const row = event.target.closest("[data-batch-row]");
      insertToken(field(row, "prompt"), token);
      event.target.value = "";
    });

    list.addEventListener("focusin", (event) => {
      if (event.target.matches('[data-batch-field="prompt"]')) lastPrompt = event.target;
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
    desiredCount && desiredCount.addEventListener("input", updateBuilder);
    defaultPrompt && defaultPrompt.addEventListener("input", () => {
      const rows = batchRows();
      rows.forEach((row) => {
        const prompt = field(row, "prompt");
        if (prompt.value === previousDefaultPrompt) prompt.value = defaultPrompt.value;
      });
      previousDefaultPrompt = defaultPrompt.value;
      updateBuilder();
    });
    titleInput && titleInput.addEventListener("input", () => {
      if (slugInput && !slugWasEdited) slugInput.value = slugify(titleInput.value);
    });
    slugInput && slugInput.addEventListener("input", () => {
      slugWasEdited = Boolean(slugInput.value.trim());
    });

    form.addEventListener("invalid", (event) => {
      const row = event.target.closest("[data-batch-row]");
      if (!row || !row.classList.contains("is-collapsed")) return;
      row.classList.remove("is-collapsed");
      const collapseButton = row.querySelector('[data-batch-action="collapse"]');
      if (collapseButton) {
        collapseButton.setAttribute("aria-expanded", "true");
        collapseButton.textContent = "Collapse";
      }
    }, true);

    form.addEventListener("submit", (event) => {
      setBatchValidity();
      if (!form.checkValidity()) {
        event.preventDefault();
        form.reportValidity();
        return;
      }
      const plan = batchRows().map(readRow);
      planData.value = JSON.stringify(plan);
      if (defaultPrompt && !defaultPrompt.value.trim()) defaultPrompt.value = plan[0].prompt;
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
      const selectedForX = selectedCards.filter((card) => card.dataset.selectedForX === "true").length;
      const netNewForX = selected - selectedForX;
      const remainingXSlots = Math.max(0, xCapacity - currentXCount);
      const exceedsXCapacity = netNewForX > remainingXSlots;

      if (acceptButton) acceptButton.disabled = selected === 0 || hasSevereSelection;
      if (xAddButton) {
        xAddButton.disabled = selected === 0 || netNewForX === 0 || exceedsXCapacity;
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

  function renderGenerationDetails(panel, details) {
    const body = panel.querySelector("[data-generation-details-body]");
    if (!body) return;
    const url = panel.dataset.generationDetailsUrl;
    body.replaceChildren();
    body.removeAttribute("aria-busy");

    const actions = createNode("div", "generation-details-actions");
    const serialized = JSON.stringify(details, null, 2);
    actions.append(copyButton("Copy all settings JSON", serialized));
    body.append(actions);

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
    const canToggle = ["true", "false"].every((value) => (
      workflowOptions.some((option) => option.dataset.upscalerEnabled === value)
    ));

    const render = () => {
      const selected = workflow.selectedOptions.item(0);
      const upscalerEnabled = selected?.dataset.upscalerEnabled === "true";
      const detailerEnabled = selected?.dataset.detailerEnabled === "true";
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
        toggle.disabled = !canToggle;
      }
      if (toggleLabel instanceof HTMLElement) {
        toggleLabel.textContent = upscalerEnabled ? "On" : "Off";
      }
      if (toggleDescription instanceof HTMLElement) {
        toggleDescription.textContent = upscalerEnabled
          ? "Runs the hires workflow; scale, denoise, and upscale method apply."
          : "Base dimensions only - no upscale node or second sampler pass.";
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
        const matching = workflowOptions.filter(
          (option) => !option.disabled && option.dataset.upscalerEnabled === desired,
        );
        const replacement = matching.find(
          (option) => option.dataset.detailerEnabled === current?.dataset.detailerEnabled,
        ) || matching[0];
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

  initializeLoraPicker();
  initializeAutomationBuilder();
  initializeWorkflowRefinement();
  initializeBulkReview();
  initializeGenerationDetails();
  initializeGenerationProgress();
})();
