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

  initializeAutomationBuilder();
  initializeBulkReview();
})();
