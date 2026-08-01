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

  function initializeAutomationBuilder() {
    const form = document.querySelector("[data-automation-form]");
    const builder = document.querySelector("#batch-builder");
    const list = document.querySelector("#batch-list");
    const template = document.querySelector("#batch-row-template");
    const planData = document.querySelector("#batch-plan-data");
    if (!form || !builder || !list || !template || !planData) return;

    const addButton = form.querySelector("[data-add-batch]");
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
        row.querySelector('[data-batch-action="up"]').disabled = index === 0;
        row.querySelector('[data-batch-action="down"]').disabled = index === rows.length - 1;
        row.querySelector('[data-batch-action="remove"]').disabled = rows.length === 1;
      });
      if (plannedJobCount) plannedJobCount.value = Math.max(1, totalJobs);
      if (addButton) addButton.disabled = rows.length >= 50;

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
      const targetTooLarge = desiredCount && integerValue(desiredCount.value) > totalImages;
      if (note) {
        if (missingPrompt) {
          note.textContent = "Every batch needs a prompt structure before this run can start.";
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
        button.disabled = serverDisabled || missingPrompt || Boolean(targetTooLarge) || totalImages < 1;
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

    addButton && addButton.addEventListener("click", () => {
      const row = addBatch(newBatchDefaults());
      field(row, "name").focus();
      row.scrollIntoView({ behavior: "smooth", block: "center" });
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
        row.remove();
      } else if (action === "duplicate") {
        const copy = readRow(row);
        copy.name = nextUniqueName(`${copy.name || "Batch"} copy`);
        const duplicate = addBatch(copy, row.nextElementSibling);
        field(duplicate, "name").focus();
      }
      updateBuilder();
    });

    form.querySelectorAll("[data-wildcard-token]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = lastPrompt || field(batchRows()[0], "prompt");
        const token = button.dataset.wildcardToken;
        const start = target.selectionStart ?? target.value.length;
        const end = target.selectionEnd ?? start;
        const prefix = start > 0 && !/[\s,]$/.test(target.value.slice(0, start)) ? ", " : "";
        target.setRangeText(`${prefix}${token}`, start, end, "end");
        target.focus();
        target.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });

    outputsPerJob && outputsPerJob.addEventListener("input", updateBuilder);
    desiredCount && desiredCount.addEventListener("input", updateBuilder);
    defaultPrompt && defaultPrompt.addEventListener("input", () => {
      const rows = batchRows();
      const firstPrompt = rows.length > 0 ? field(rows[0], "prompt") : null;
      if (firstPrompt && firstPrompt.value === previousDefaultPrompt) {
        firstPrompt.value = defaultPrompt.value;
      }
      previousDefaultPrompt = defaultPrompt.value;
      updateBuilder();
    });
    titleInput && titleInput.addEventListener("input", () => {
      if (slugInput && !slugWasEdited) slugInput.value = slugify(titleInput.value);
    });
    slugInput && slugInput.addEventListener("input", () => {
      slugWasEdited = Boolean(slugInput.value.trim());
    });

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
    document.querySelectorAll("[data-review-selection-controls], [data-review-tools]").forEach((item) => {
      item.hidden = false;
    });

    const updateSelection = () => {
      const selectedCheckboxes = checkboxes.filter((checkbox) => checkbox.checked);
      const selected = selectedCheckboxes.length;
      checkboxes.forEach((checkbox) => {
        const card = checkbox.closest(".asset-card");
        if (card) card.classList.toggle("is-selected", checkbox.checked);
      });
      countLabels.forEach((label) => {
        label.textContent = selected === 0
          ? "Select one or more image cards below."
          : `${selected} image${selected === 1 ? "" : "s"} selected.`;
      });
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

    checkboxes.forEach((checkbox) => checkbox.addEventListener("change", updateSelection));
    document.querySelectorAll("[data-select-all]").forEach((button) => {
      button.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => {
          const card = checkbox.closest(".asset-card");
          checkbox.checked = !card || !card.hidden;
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
            || (filter === "x" && card.dataset.selectedForX === "true");
          card.hidden = !matches;
          if (!matches) {
            const checkbox = card.querySelector('input[type="checkbox"][name="asset_id"]');
            if (checkbox) checkbox.checked = false;
          }
        });
        updateSelection();
      });
    });
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
      hires: safeFields(value.hires, ["scale", "denoise", "upscale_method"]),
      detailer: safeFields(value.detailer, [
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

  const copyButton = (label, value) => {
    const button = createNode(
      "button",
      "secondary-button generation-copy-button",
      label,
    );
    button.type = "button";
    button.disabled = typeof value !== "string";
    button.addEventListener("click", () => copyText(button, value, label));
    return button;
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
    heading.append(copyButton("Copy", resolved));
    wrapper.append(heading);
    wrapper.append(createNode("pre", "generation-prompt-text", resolved || "(empty)"));
    if (prompt.inherited === true) {
      wrapper.append(createNode("span", "generation-details-status", "Inherited from the batch default."));
    }
    if (source !== resolved) {
      const sourceHeading = createNode("div", "generation-prompt-heading");
      sourceHeading.append(createNode("strong", "", `${label} template`));
      sourceHeading.append(copyButton("Copy template", source));
      wrapper.append(sourceHeading);
      wrapper.append(createNode("pre", "generation-prompt-text", source || "(empty)"));
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

    const refinementSection = addSection(body, "Hires & face detailer");
    addGrid(refinementSection, [
      ["Hires scale", details.hires.scale],
      ["Hires denoise", details.hires.denoise],
      ["Upscale method", details.hires.upscale_method],
      ["Detailer guide size", details.detailer.guide_size],
      ["Detailer max size", details.detailer.max_size],
      ["Detailer denoise", details.detailer.denoise],
      ["BBox threshold", details.detailer.bbox_threshold],
      ["BBox dilation", details.detailer.bbox_dilation],
      ["BBox crop factor", details.detailer.bbox_crop_factor],
      ["Feather", details.detailer.feather],
    ]);

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

  initializeAutomationBuilder();
  initializeBulkReview();
  initializeGenerationDetails();
})();
