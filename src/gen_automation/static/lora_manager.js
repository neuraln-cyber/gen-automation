(() => {
  "use strict";

  const root = document.querySelector("[data-lora-manager]");
  if (!(root instanceof HTMLElement)) return;

  const csrfToken = root.dataset.csrfToken || "";
  const canManage = root.dataset.canManage === "true";
  const listUrl = root.dataset.listUrl || "/api/v1/loras";
  const terminalImportStatuses = new Set([
    "ready",
    "completed",
    "duplicate",
    "failed",
    "cancelled",
  ]);
  const mutationKeys = new WeakMap();
  const pendingManualCompletions = new WeakMap();
  const importStatusLabels = new Map([
    ["queued", "Queued"],
    ["awaiting_upload", "Waiting for browser upload"],
    ["claimed", "Transferring and verifying"],
    ["retry_wait", "Waiting to retry"],
    ["checking", "Checking source"],
    ["resolving", "Checking source"],
    ["transferring", "Transferring"],
    ["uploading", "Uploading"],
    ["verifying", "Verifying Safetensors and SHA-256"],
    ["registering", "Registering"],
    ["activating", "Activating for the next worker"],
    ["ready", "Ready"],
    ["completed", "Ready"],
    ["duplicate", "Already in library"],
    ["failed", "Needs attention"],
    ["cancelled", "Cancelled"],
  ]);

  const stringValue = (value) => (typeof value === "string" ? value.trim() : "");
  const identifierValue = (value) => {
    if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) return String(value);
    const normalized = stringValue(value);
    return /^[1-9][0-9]*$/.test(normalized) ? normalized : "";
  };
  const integerValue = (value) => {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
  };
  const formatBytes = (rawBytes) => {
    const bytes = integerValue(rawBytes);
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let value = bytes;
    let unit = "B";
    for (const candidate of units) {
      value /= 1024;
      unit = candidate;
      if (value < 1024 || candidate === units.at(-1)) break;
    }
    return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${unit}`;
  };
  const statusLabel = (status) => (
    importStatusLabels.get(stringValue(status).toLowerCase())
      || stringValue(status).replaceAll("_", " ").replaceAll("-", " ")
      || "Working"
  );
  const importId = (operation) => stringValue(operation?.id ?? operation?.job_id);
  const importStatus = (operation) => stringValue(operation?.status ?? operation?.state).toLowerCase();
  const libraryFilterStatus = (entry) => {
    const status = stringValue(entry?.status).toLowerCase();
    const readiness = stringValue(entry?.readiness_status).toLowerCase();
    if (["retired", "removed", "revoked", "purged"].includes(status)) return "removed";
    if (["retiring", "deleting", "delete_pending"].includes(status)) return "deleting";
    if (["failed", "blocked", "error", "attention"].includes(status)
      || ["failed", "blocked", "unavailable"].includes(readiness)) return "attention";
    if (["ready", "warm_ready"].includes(readiness) || status === "active") return "ready";
    return "activating";
  };
  const setMessage = (node, message, tone = "") => {
    if (!(node instanceof HTMLElement)) return;
    node.textContent = message;
    node.classList.toggle("success", tone === "success");
    node.classList.toggle("warning", tone === "warning");
    node.classList.toggle("error", tone === "error");
  };
  const errorMessage = (payload, fallback) => {
    if (payload && typeof payload === "object") {
      const detail = stringValue(payload.detail);
      const message = stringValue(payload.message);
      if (detail) return detail;
      if (message) return message;
    }
    return fallback;
  };

  const newMutationKey = () => {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const stableMutationKey = (owner, fingerprint, scope = "default") => {
    let scoped = mutationKeys.get(owner);
    if (!(scoped instanceof Map)) {
      scoped = new Map();
      mutationKeys.set(owner, scoped);
    }
    const current = scoped.get(scope);
    if (current?.fingerprint === fingerprint) return current.key;
    const next = { fingerprint, key: newMutationKey() };
    scoped.set(scope, next);
    return next.key;
  };

  async function requestJson(url, { method = "GET", body, idempotencyKey } = {}) {
    const headers = { Accept: "application/json" };
    const options = {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers,
    };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    if (method !== "GET" && method !== "HEAD" && csrfToken) {
      headers["X-CSRF-Token"] = csrfToken;
    }
    if (method !== "GET" && method !== "HEAD" && idempotencyKey) {
      headers["Idempotency-Key"] = idempotencyKey;
    }
    const response = await fetch(url, options);
    let payload = null;
    if (response.status !== 204) {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        payload = await response.json();
      }
    }
    if (!response.ok) {
      if (response.status === 401) {
        const detail = stringValue(payload?.detail);
        if (detail === "recent authentication required") {
          throw new Error("Your recent sign-in expired. Sign in again, then retry this change.");
        }
        throw new Error("Your session expired. Sign in again, then retry.");
      }
      throw new Error(errorMessage(payload, `Request failed (${response.status}).`));
    }
    return payload && typeof payload === "object" ? payload : {};
  }

  function safeExternalUrl(rawValue, expectedHosts = null) {
    try {
      const parsed = new URL(stringValue(rawValue));
      if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
      const allowedHosts = Array.isArray(expectedHosts) ? expectedHosts : [expectedHosts].filter(Boolean);
      if (allowedHosts.length > 0 && !allowedHosts.includes(parsed.hostname)) return null;
      return parsed.href;
    } catch (_error) {
      return null;
    }
  }

  function initializeSourceChoice() {
    const fieldset = root.querySelector("[data-lora-source-choice]");
    if (!(fieldset instanceof HTMLFieldSetElement)) return;
    const panels = Array.from(root.querySelectorAll("[data-lora-source-panel]"));
    const sync = () => {
      const selected = fieldset.querySelector('input[name="lora_source_mode"]:checked');
      const mode = selected instanceof HTMLInputElement ? selected.value : "civitai";
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.loraSourcePanel !== mode;
      });
    };
    fieldset.addEventListener("change", sync);
    sync();
  }

  function normalizeResolvedCivitai(payload) {
    let files = Array.isArray(payload.files)
      ? payload.files.flatMap((value) => {
        if (!value || typeof value !== "object") return [];
        const id = identifierValue(value.id ?? value.file_id);
        const name = stringValue(value.name ?? value.filename);
        if (!id || !name) return [];
        return [{
          id,
          name,
          targetFilename: stringValue(value.target_filename) || name,
          sizeBytes: integerValue(value.size_bytes),
          sha256: stringValue(value.sha256).toLowerCase(),
          primary: value.primary === true,
        }];
      })
      : [];
    if (files.length === 0) {
      const fileId = identifierValue(payload.file_id);
      const targetFilename = stringValue(payload.target_filename);
      if (fileId && targetFilename) {
        files = [{
          id: fileId,
          name: targetFilename,
          targetFilename,
          sizeBytes: integerValue(payload.declared_size_bytes),
          sha256: stringValue(payload.sha256).toLowerCase(),
          primary: true,
        }];
      }
    }
    return {
      modelId: identifierValue(payload.model_id),
      versionId: identifierValue(payload.version_id),
      name: stringValue(payload.name ?? payload.model_name),
      versionName: stringValue(payload.version_name),
      baseModel: stringValue(payload.base_model),
      sourceUrl: safeExternalUrl(
        payload.source_url ?? payload.canonical_source_url,
        ["civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red"],
      ),
      licenseUrl: safeExternalUrl(payload.license_url),
      files,
      triggerWords: Array.isArray(payload.trigger_words ?? payload.trained_words)
        ? (payload.trigger_words ?? payload.trained_words).map(stringValue).filter(Boolean).slice(0, 100)
        : [],
      commercialImageAllowed: payload.commercial_image_allowed === true,
      providerCommercialUse: Array.isArray(payload.provider_commercial_use)
        ? payload.provider_commercial_use.map(stringValue).filter(Boolean).slice(0, 16)
        : [],
      commercialUseOverrideApplied: payload.commercial_use_override_applied === true,
      adultUseRequiresAttestation: payload.adult_use_requires_attestation === true,
    };
  }

  function normalizeCivitaiVersions(payload) {
    if (!Array.isArray(payload.versions)) return [];
    const seen = new Set();
    return payload.versions.flatMap((value) => {
      if (!value || typeof value !== "object") return [];
      const id = identifierValue(value.version_id ?? value.id);
      if (!id || seen.has(id)) return [];
      seen.add(id);
      return [{
        id,
        name: stringValue(value.name ?? value.version_name) || `Version ${id}`,
        baseModel: stringValue(value.base_model),
        targetFilename: stringValue(value.target_filename),
        sizeBytes: integerValue(value.declared_size_bytes ?? value.size_bytes),
        sha256: stringValue(value.sha256).toLowerCase(),
      }];
    });
  }

  function initializeCivitaiImport() {
    const resolveForm = root.querySelector("[data-lora-civitai-resolve-form]");
    const importForm = root.querySelector("[data-lora-civitai-import-form]");
    if (!(resolveForm instanceof HTMLFormElement) || !(importForm instanceof HTMLFormElement)) {
      return;
    }
    const urlInput = resolveForm.elements.namedItem("civitai_url");
    const resolveButton = resolveForm.querySelector('button[type="submit"]');
    const resolveStatus = root.querySelector("[data-lora-civitai-status]");
    const importStatus = root.querySelector("[data-lora-civitai-import-status]");
    const submitButton = root.querySelector("[data-lora-civitai-submit]");
    const versionChoice = root.querySelector("[data-lora-version-choice]");
    const versionSelect = root.querySelector("[data-lora-version-select]");
    const fileSelect = root.querySelector("[data-lora-resolved-file]");
    const nameInput = root.querySelector("[data-lora-resolved-name]");
    const commercialInput = importForm.elements.namedItem("commercial_use_attested");
    const adultInput = importForm.elements.namedItem("adult_use_attested");
    const overrideInput = resolveForm.elements.namedItem("commercial_use_override_attested");
    let resolved = null;

    const clearVersionChoices = () => {
      if (versionChoice instanceof HTMLElement) versionChoice.hidden = true;
      if (versionSelect instanceof HTMLSelectElement) {
        versionSelect.required = false;
        versionSelect.replaceChildren();
      }
    };

    const renderVersionChoices = (versions) => {
      resolved = null;
      importForm.hidden = true;
      if (!(versionChoice instanceof HTMLElement) || !(versionSelect instanceof HTMLSelectElement)) {
        throw new Error("This dashboard cannot show the model's version list. Refresh and try again.");
      }
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose a version";
      placeholder.disabled = true;
      placeholder.selected = true;
      versionSelect.replaceChildren(placeholder);
      versions.forEach((version) => {
        const option = document.createElement("option");
        option.value = version.id;
        const details = [
          version.baseModel,
          version.targetFilename,
          version.sizeBytes ? formatBytes(version.sizeBytes) : "",
        ].filter(Boolean);
        option.textContent = `${version.name}${details.length ? ` - ${details.join(" - ")}` : ""}`;
        versionSelect.append(option);
      });
      versionSelect.required = true;
      versionChoice.hidden = false;
      versionSelect.focus();
    };

    const syncSelectedFile = () => {
      if (!resolved || !(fileSelect instanceof HTMLSelectElement)) return;
      const file = resolved.files.find((candidate) => candidate.id === fileSelect.value);
      const sha = root.querySelector("[data-lora-resolved-sha]");
      if (sha) sha.textContent = file?.sha256 || "Verified after transfer";
    };

    const renderResolved = (value) => {
      resolved = value;
      clearVersionChoices();
      const heading = root.querySelector("[data-lora-resolved-heading]");
      const meta = root.querySelector("[data-lora-resolved-meta]");
      const baseModel = root.querySelector("[data-lora-resolved-base-model]");
      const source = root.querySelector("[data-lora-resolved-source]");
      const license = root.querySelector("[data-lora-resolved-license]");
      const modelId = root.querySelector("[data-lora-resolved-model-id]");
      const versionId = root.querySelector("[data-lora-resolved-version-id]");
      const commercialStatus = root.querySelector("[data-lora-commercial-status]");
      const commercialCopy = root.querySelector("[data-lora-commercial-attestation-copy]");
      const providerCommercialUse = root.querySelector("[data-lora-provider-commercial-use]");
      const triggerPreview = root.querySelector("[data-lora-trigger-preview]");
      const triggers = root.querySelector("[data-lora-resolved-triggers]");
      if (heading) heading.textContent = value.name || "Civitai LoRA";
      if (meta) meta.textContent = [value.versionName, value.baseModel].filter(Boolean).join(" - ");
      if (baseModel) baseModel.textContent = value.baseModel || "Unknown";
      if (nameInput instanceof HTMLInputElement) nameInput.value = value.name || value.versionName;
      if (modelId instanceof HTMLInputElement) modelId.value = value.modelId;
      if (versionId instanceof HTMLInputElement) versionId.value = value.versionId;
      if (source instanceof HTMLAnchorElement) {
        if (value.sourceUrl) source.href = value.sourceUrl;
        else source.removeAttribute("href");
      }
      if (license instanceof HTMLAnchorElement) {
        if (value.licenseUrl) license.href = value.licenseUrl;
        else license.removeAttribute("href");
      }
      if (fileSelect instanceof HTMLSelectElement) {
        fileSelect.replaceChildren();
        value.files.forEach((file) => {
          const option = document.createElement("option");
          option.value = file.id;
          option.textContent = `${file.name}${file.sizeBytes ? ` - ${formatBytes(file.sizeBytes)}` : ""}`;
          option.selected = file.primary;
          fileSelect.append(option);
        });
      }
      if (commercialStatus) {
        commercialStatus.textContent = value.commercialImageAllowed
          ? "Civitai marks commercial images allowed"
          : value.commercialUseOverrideApplied
            ? "Reviewed-license override"
            : "Commercial permission unconfirmed";
        commercialStatus.className = `status ${
          value.commercialImageAllowed || value.commercialUseOverrideApplied ? "ready" : "failed"
        }`;
      }
      if (commercialCopy) {
        commercialCopy.textContent = value.commercialUseOverrideApplied
          ? "I independently confirmed the creator's current terms permit commercial image use and authorize this recorded override."
          : "I confirm this version permits commercial image use.";
      }
      if (providerCommercialUse) {
        providerCommercialUse.textContent = value.providerCommercialUse.length > 0
          ? value.providerCommercialUse.join(", ")
          : "No commercial-use value reported";
      }
      if (commercialInput instanceof HTMLInputElement) {
        commercialInput.checked = false;
        commercialInput.disabled = !(
          value.commercialImageAllowed || value.commercialUseOverrideApplied
        );
      }
      if (adultInput instanceof HTMLInputElement) adultInput.checked = false;
      if (triggerPreview) triggerPreview.hidden = value.triggerWords.length === 0;
      if (triggers) triggers.textContent = value.triggerWords.join(", ");
      if (submitButton instanceof HTMLButtonElement) {
        submitButton.disabled = !(
          value.commercialImageAllowed || value.commercialUseOverrideApplied
        ) || value.files.length === 0;
      }
      importForm.hidden = false;
      syncSelectedFile();
      importForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };

    if (fileSelect instanceof HTMLSelectElement) {
      fileSelect.addEventListener("change", syncSelectedFile);
    }
    if (urlInput instanceof HTMLInputElement) {
      urlInput.addEventListener("input", () => {
        clearVersionChoices();
        importForm.hidden = true;
        resolved = null;
      });
    }
    if (overrideInput instanceof HTMLInputElement) {
      overrideInput.addEventListener("change", () => {
        clearVersionChoices();
        importForm.hidden = true;
        resolved = null;
        setMessage(resolveStatus, "Check the URL again to apply this license-review choice.", "warning");
      });
    }

    resolveForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!(urlInput instanceof HTMLInputElement) || !resolveForm.reportValidity()) return;
      const selectedVersionId = versionChoice instanceof HTMLElement
        && !versionChoice.hidden
        && versionSelect instanceof HTMLSelectElement
        ? identifierValue(versionSelect.value)
        : "";
      const resolveUrl = root.dataset.resolveUrl || "/api/v1/loras/civitai:resolve";
      resolveForm.setAttribute("aria-busy", "true");
      if (resolveButton instanceof HTMLButtonElement) resolveButton.disabled = true;
      importForm.hidden = true;
      setMessage(resolveStatus, "Checking the Civitai model and available Safetensors files...");
      try {
        const requestBody = { url: urlInput.value.trim() };
        if (selectedVersionId) requestBody.version_id = Number(selectedVersionId);
        requestBody.commercial_use_override_attested = (
          overrideInput instanceof HTMLInputElement && overrideInput.checked
        );
        const payload = await requestJson(resolveUrl, {
          method: "POST",
          body: requestBody,
        });
        const value = normalizeResolvedCivitai(payload);
        const versions = normalizeCivitaiVersions(payload);
        if ((!value.versionId || value.files.length === 0) && versions.length > 0 && !selectedVersionId) {
          renderVersionChoices(versions);
          setMessage(resolveStatus, "Choose the exact model version to inspect. No version was selected for you.", "warning");
          return;
        }
        if (!value.modelId || !value.versionId || value.files.length === 0) {
          throw new Error(selectedVersionId
            ? "No supported Safetensors file was found for the selected Civitai version."
            : "No supported Safetensors file was found for that Civitai version.");
        }
        if (!value.sourceUrl || !value.licenseUrl) {
          throw new Error("Civitai did not return canonical source and license references for this version.");
        }
        renderResolved(value);
        setMessage(
          resolveStatus,
          value.commercialImageAllowed
            ? "Version found. Review the file and rights confirmation below."
            : value.commercialUseOverrideApplied
              ? "Version found using your reviewed-license override. Confirm the exact file and rights below."
              : "Version found, but commercial permission remains unconfirmed.",
          value.commercialImageAllowed || value.commercialUseOverrideApplied ? "success" : "error",
        );
      } catch (error) {
        setMessage(resolveStatus, error instanceof Error ? error.message : "Could not check that URL.", "error");
      } finally {
        resolveForm.removeAttribute("aria-busy");
        if (resolveButton instanceof HTMLButtonElement) resolveButton.disabled = false;
      }
    });

    importForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (
        !resolved
        || !importForm.reportValidity()
        || !(resolved.commercialImageAllowed || resolved.commercialUseOverrideApplied)
      ) return;
      if (!(fileSelect instanceof HTMLSelectElement) || !(nameInput instanceof HTMLInputElement)) return;
      const selectedFile = resolved.files.find((candidate) => candidate.id === fileSelect.value);
      if (!selectedFile) {
        setMessage(importStatus, "Choose a Safetensors file.", "error");
        return;
      }
      const endpoint = root.dataset.civitaiImportUrl || "/api/v1/loras/imports/civitai";
      importForm.setAttribute("aria-busy", "true");
      if (submitButton instanceof HTMLButtonElement) submitButton.disabled = true;
      setMessage(importStatus, "Queuing a direct Civitai-to-private-storage transfer...");
      try {
        const command = {
          display_name: nameInput.value.trim(),
          canonical_source_url: resolved.sourceUrl,
          license_url: resolved.licenseUrl,
          target_filename: selectedFile.targetFilename,
          expected_sha256: selectedFile.sha256 || null,
          expected_byte_size: selectedFile.sizeBytes || null,
          expected_metadata: {},
          trigger_words: resolved.triggerWords,
          civitai_model_id: Number(resolved.modelId),
          civitai_version_id: Number(resolved.versionId),
          civitai_file_id: Number(selectedFile.id),
          commercial_use_override_attested: resolved.commercialUseOverrideApplied,
          commercial_use_attested: commercialInput instanceof HTMLInputElement && commercialInput.checked,
          adult_use_attested: adultInput instanceof HTMLInputElement && adultInput.checked,
        };
        await requestJson(endpoint, {
          method: "POST",
          body: command,
          idempotencyKey: stableMutationKey(importForm, JSON.stringify(command)),
        });
        setMessage(importStatus, "Transfer queued. Opening its saved progress...", "success");
        window.setTimeout(() => window.location.assign("/dashboard/loras#lora-imports"), 250);
      } catch (error) {
        setMessage(importStatus, error instanceof Error ? error.message : "Could not start this import.", "error");
        if (submitButton instanceof HTMLButtonElement) submitButton.disabled = false;
      } finally {
        importForm.removeAttribute("aria-busy");
      }
    });
  }

  function directUpload(upload, file, onProgress) {
    return new Promise((resolve, reject) => {
      const url = safeExternalUrl(upload?.url);
      const method = stringValue(upload?.method || "POST").toUpperCase();
      const fields = upload?.fields;
      if (!url || method !== "POST" || !fields || typeof fields !== "object") {
        reject(new Error("The secure upload grant was invalid. Retry the import."));
        return;
      }
      const body = new FormData();
      Object.entries(fields).forEach(([key, value]) => {
        if (typeof value === "string") body.append(key, value);
      });
      body.append("file", file, file.name);
      const request = new XMLHttpRequest();
      request.open("POST", url, true);
      request.withCredentials = false;
      const headers = upload.headers && typeof upload.headers === "object" ? upload.headers : {};
      Object.entries(headers).forEach(([key, value]) => {
        if (typeof value === "string") request.setRequestHeader(key, value);
      });
      request.upload.addEventListener("progress", (event) => {
        onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
      });
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) {
          const versionId = stringValue(request.getResponseHeader("x-amz-version-id"));
          const etag = stringValue(request.getResponseHeader("ETag")).replace(/^"|"$/g, "");
          if (!versionId || !etag) {
            reject(new Error("The upload completed without immutable S3 version details. Retry it."));
            return;
          }
          resolve({ objectVersionId: versionId, objectEtag: etag });
        }
        else reject(new Error(`Private-storage upload failed (${request.status || "network error"}).`));
      });
      request.addEventListener("error", () => reject(new Error("The network interrupted the private-storage upload.")));
      request.addEventListener("abort", () => reject(new Error("The private-storage upload was cancelled.")));
      request.send(body);
    });
  }

  function initializeManualImport() {
    const form = root.querySelector("[data-lora-manual-import-form]");
    if (!(form instanceof HTMLFormElement)) return;
    const fileInput = form.elements.namedItem("file");
    const submitButton = form.querySelector('button[type="submit"]');
    const status = root.querySelector("[data-lora-manual-status]");
    const progressPanel = root.querySelector("[data-lora-upload-progress]");
    const progressMeter = root.querySelector("[data-lora-upload-meter]");
    const progressPercent = root.querySelector("[data-lora-upload-percent]");
    const progressLabel = root.querySelector("[data-lora-upload-label]");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!form.reportValidity() || !(fileInput instanceof HTMLInputElement)) return;
      const file = fileInput.files?.[0];
      if (!file || file.size <= 0 || !file.name.toLowerCase().endsWith(".safetensors")) {
        fileInput.setCustomValidity("Choose a non-empty .safetensors file.");
        fileInput.reportValidity();
        return;
      }
      fileInput.setCustomValidity("");
      const endpoint = root.dataset.manualImportUrl || "/api/v1/loras/imports/manual";
      const name = form.elements.namedItem("name");
      const targetFilename = form.elements.namedItem("target_filename");
      const triggerWords = form.elements.namedItem("trigger_words");
      const sourceUrl = form.elements.namedItem("source_url");
      const licenseUrl = form.elements.namedItem("license_url");
      const commercial = form.elements.namedItem("commercial_use_attested");
      const adult = form.elements.namedItem("adult_use_attested");
      form.setAttribute("aria-busy", "true");
      if (submitButton instanceof HTMLButtonElement) submitButton.disabled = true;
      if (progressPanel instanceof HTMLElement) progressPanel.hidden = false;
      setMessage(status, "Creating a bounded direct-upload grant...");
      try {
        const pendingCompletion = pendingManualCompletions.get(form);
        if (pendingCompletion) {
          setMessage(status, "Confirming the upload already stored for this import...");
          await requestJson(pendingCompletion.url, {
            method: "POST",
            body: pendingCompletion.body,
            idempotencyKey: pendingCompletion.idempotencyKey,
          });
          pendingManualCompletions.delete(form);
          window.setTimeout(() => window.location.assign("/dashboard/loras#lora-imports"), 250);
          return;
        }
        const normalizedTriggerWords = [];
        const seenTriggerWords = new Set();
        const triggerWordValue = triggerWords instanceof HTMLInputElement ? triggerWords.value : "";
        for (const rawTriggerWord of triggerWordValue.split(",")) {
          const triggerWord = rawTriggerWord.trim();
          const comparable = triggerWord.toLocaleLowerCase();
          if (!triggerWord || seenTriggerWords.has(comparable)) continue;
          if (triggerWord.length > 200) {
            throw new Error("Each trigger word must be 200 characters or fewer.");
          }
          seenTriggerWords.add(comparable);
          normalizedTriggerWords.push(triggerWord);
        }
        if (normalizedTriggerWords.length > 100) {
          throw new Error("Enter no more than 100 trigger words.");
        }
        const command = {
          display_name: name instanceof HTMLInputElement ? name.value.trim() : "",
          canonical_source_url: sourceUrl instanceof HTMLInputElement ? sourceUrl.value.trim() : "",
          license_url: licenseUrl instanceof HTMLInputElement ? licenseUrl.value.trim() : "",
          target_filename: targetFilename instanceof HTMLInputElement ? targetFilename.value.trim() : "",
          expected_sha256: null,
          expected_byte_size: file.size,
          expected_metadata: {},
          trigger_words: normalizedTriggerWords,
          commercial_use_attested: commercial instanceof HTMLInputElement && commercial.checked,
          adult_use_attested: adult instanceof HTMLInputElement && adult.checked,
        };
        const payload = await requestJson(endpoint, {
          method: "POST",
          body: command,
          idempotencyKey: stableMutationKey(
            form,
            JSON.stringify(command),
            "manual-create",
          ),
        });
        const operation = payload.import && typeof payload.import === "object"
          ? payload.import
          : payload.operation ?? payload.job;
        const createdImportId = importId(operation) || stringValue(payload.import_id);
        if (!createdImportId) {
          throw new Error("The upload operation was incomplete. Retry it.");
        }
        if (importStatus(operation) !== "awaiting_upload") {
          window.setTimeout(() => window.location.assign("/dashboard/loras#lora-imports"), 250);
          return;
        }
        if (!payload.upload) {
          throw new Error("The upload operation was incomplete. Retry it.");
        }
        if (progressLabel) progressLabel.textContent = `Uploading ${file.name} directly to private storage`;
        const uploaded = await directUpload(payload.upload, file, (loaded, total) => {
          const boundedTotal = total > 0 ? total : file.size;
          const percentage = boundedTotal > 0 ? Math.min(100, Math.round((loaded / boundedTotal) * 100)) : 0;
          if (progressMeter instanceof HTMLProgressElement) progressMeter.value = percentage;
          if (progressPercent) progressPercent.textContent = `${percentage}% - ${formatBytes(loaded)} of ${formatBytes(boundedTotal)}`;
        });
        if (progressLabel) progressLabel.textContent = "Upload complete; starting server-side Safetensors verification";
        setMessage(status, "Upload complete. Verification does not use a GPU.", "success");
        const completion = {
          url: `/api/v1/loras/imports/${encodeURIComponent(createdImportId)}:complete`,
          body: {
            object_version_id: uploaded.objectVersionId,
            object_etag: uploaded.objectEtag,
            byte_size: file.size,
          },
          idempotencyKey: stableMutationKey(
            form,
            `complete:${createdImportId}:${uploaded.objectVersionId}:${uploaded.objectEtag}:${file.size}`,
            "manual-complete",
          ),
        };
        pendingManualCompletions.set(form, completion);
        await requestJson(completion.url, {
          method: "POST",
          body: completion.body,
          idempotencyKey: completion.idempotencyKey,
        });
        pendingManualCompletions.delete(form);
        window.setTimeout(() => window.location.assign("/dashboard/loras#lora-imports"), 250);
      } catch (error) {
        setMessage(status, error instanceof Error ? error.message : "Could not upload this LoRA.", "error");
        if (submitButton instanceof HTMLButtonElement) submitButton.disabled = false;
      } finally {
        form.removeAttribute("aria-busy");
      }
    });

    if (fileInput instanceof HTMLInputElement) {
      fileInput.addEventListener("change", () => {
        fileInput.setCustomValidity("");
        const file = fileInput.files?.[0];
        if (!file) return;
        const name = form.elements.namedItem("name");
        if (name instanceof HTMLInputElement && !name.value.trim()) {
          name.value = file.name.replace(/\.safetensors$/i, "");
        }
        const targetFilename = form.elements.namedItem("target_filename");
        if (targetFilename instanceof HTMLInputElement && !targetFilename.value.trim()) {
          const stem = file.name.replace(/\.safetensors$/i, "");
          const sanitizedStem = stem
            .replace(/[^A-Za-z0-9._ -]+/g, "-")
            .replace(/^[^A-Za-z0-9]+/, "")
            .replace(/[ ._-]+$/, "")
            .slice(0, 224);
          targetFilename.value = `${sanitizedStem || "uploaded-lora"}.safetensors`;
        }
      });
    }
  }

  function initializeLibrary() {
    const cards = Array.from(root.querySelectorAll("[data-lora-entry]"));
    const search = root.querySelector("[data-lora-library-search]");
    const filterButtons = Array.from(root.querySelectorAll("[data-lora-filter]"));
    const result = root.querySelector("[data-lora-filter-result]");
    const empty = root.querySelector("[data-lora-library-empty]");
    const emptyCopy = root.querySelector("[data-lora-library-empty-copy]");
    const libraryCount = root.querySelector("[data-lora-library-count]");
    const readyCount = root.querySelector("[data-lora-ready-count]");
    const storageTotal = root.querySelector("[data-lora-storage-total]");
    let activeFilter = "all";

    root.querySelectorAll("[data-format-bytes]").forEach((node) => {
      node.textContent = formatBytes(node.dataset.formatBytes);
    });
    if (libraryCount) libraryCount.textContent = String(cards.length);
    if (readyCount) {
      readyCount.textContent = String(cards.filter((card) => card.dataset.loraStatus === "ready").length);
    }
    if (storageTotal) {
      storageTotal.textContent = formatBytes(
        cards.reduce((total, card) => total + integerValue(card.dataset.loraSize), 0),
      );
    }

    const applyFilters = () => {
      const query = search instanceof HTMLInputElement ? search.value.trim().toLowerCase() : "";
      let visible = 0;
      cards.forEach((card) => {
        const matchesStatus = activeFilter === "all" || card.dataset.loraStatus === activeFilter;
        const haystack = (card.dataset.loraSearchText || "").toLowerCase();
        const matchesSearch = !query || haystack.includes(query);
        card.hidden = !(matchesStatus && matchesSearch);
        if (!card.hidden) visible += 1;
      });
      if (result) result.textContent = `${visible} of ${cards.length} shown`;
      if (empty) empty.hidden = visible !== 0;
      if (emptyCopy) {
        emptyCopy.textContent = cards.length === 0
          ? "Add one from Civitai or upload a Safetensors file above."
          : "No LoRAs match this search and status filter.";
      }
    };

    filterButtons.forEach((button) => {
      button.addEventListener("click", () => {
        activeFilter = button.dataset.loraFilter || "all";
        filterButtons.forEach((candidate) => {
          const selected = candidate === button;
          candidate.classList.toggle("active", selected);
          candidate.setAttribute("aria-pressed", String(selected));
        });
        applyFilters();
      });
    });
    if (search instanceof HTMLInputElement) search.addEventListener("input", applyFilters);
    applyFilters();

    root.addEventListener("click", async (event) => {
      const copy = event.target.closest("[data-copy-lora-triggers]");
      if (!(copy instanceof HTMLButtonElement)) return;
      const text = copy.dataset.copyLoraTriggers || "";
      const status = root.querySelector("[data-lora-library-status]");
      try {
        await navigator.clipboard.writeText(text);
        setMessage(status, "Trigger words copied.", "success");
      } catch (_error) {
        setMessage(status, "Could not copy automatically. Select the trigger words and copy them.", "warning");
      }
    });
  }

  function initializeArtifactActions() {
    if (!canManage) return;
    const dialog = root.querySelector("[data-lora-delete-dialog]");
    const confirm = root.querySelector("[data-lora-delete-confirm]");
    const deleteStatus = root.querySelector("[data-lora-delete-status]");
    const libraryStatus = root.querySelector("[data-lora-library-status]");
    let activeCard = null;
    let returnFocus = null;

    root.addEventListener("click", async (event) => {
      const retire = event.target.closest("[data-lora-retire]");
      const restore = event.target.closest("[data-lora-restore]");
      if (!(retire instanceof HTMLButtonElement) && !(restore instanceof HTMLButtonElement)) return;
      const button = retire || restore;
      const card = button.closest("[data-lora-entry]");
      if (!(card instanceof HTMLElement)) return;
      const id = card.dataset.loraId || "";
      if (!id) return;
      if (restore instanceof HTMLButtonElement) {
        restore.disabled = true;
        restore.setAttribute("aria-busy", "true");
        setMessage(libraryStatus, `Restoring ${card.querySelector("h3")?.textContent || "LoRA"}...`);
        try {
          await requestJson(`/api/v1/loras/${encodeURIComponent(id)}:restore`, {
            method: "POST",
            body: {},
            idempotencyKey: stableMutationKey(restore, `restore:${id}`),
          });
          window.location.reload();
        } catch (error) {
          setMessage(libraryStatus, error instanceof Error ? error.message : "Could not restore this LoRA.", "error");
          restore.disabled = false;
          restore.removeAttribute("aria-busy");
        }
        return;
      }
      if (!(dialog instanceof HTMLDialogElement)) return;
      activeCard = card;
      returnFocus = retire;
      const name = dialog.querySelector("[data-lora-delete-name]");
      const size = dialog.querySelector("[data-lora-delete-size-copy]");
      const checkbox = dialog.querySelector('input[name="purge_when_safe"]');
      if (name) name.textContent = card.querySelector("h3")?.textContent || "this LoRA";
      if (size) size.textContent = `Reclaim ${formatBytes(card.dataset.loraSize)} after the safe worker rollout.`;
      if (checkbox instanceof HTMLInputElement) checkbox.checked = true;
      setMessage(deleteStatus, "");
      dialog.showModal();
    });

    if (dialog instanceof HTMLDialogElement) {
      dialog.addEventListener("close", () => {
        if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus();
        if (dialog.returnValue === "cancel") activeCard = null;
      });
    }
    if (confirm instanceof HTMLButtonElement && dialog instanceof HTMLDialogElement) {
      confirm.addEventListener("click", async () => {
        if (!(activeCard instanceof HTMLElement)) return;
        const id = activeCard.dataset.loraId || "";
        const checkbox = dialog.querySelector('input[name="purge_when_safe"]');
        confirm.disabled = true;
        dialog.setAttribute("aria-busy", "true");
        setMessage(deleteStatus, "Removing this LoRA from new selections...");
        try {
          await requestJson(`/api/v1/loras/${encodeURIComponent(id)}:retire`, {
            method: "POST",
            body: {
              purge_requested: checkbox instanceof HTMLInputElement && checkbox.checked,
            },
            idempotencyKey: stableMutationKey(
              confirm,
              `retire:${id}:${checkbox instanceof HTMLInputElement && checkbox.checked}`,
            ),
          });
          dialog.close("confirmed");
          window.location.reload();
        } catch (error) {
          setMessage(deleteStatus, error instanceof Error ? error.message : "Could not delete this LoRA.", "error");
          confirm.disabled = false;
        } finally {
          dialog.removeAttribute("aria-busy");
        }
      });
    }
  }

  function initializeImportProgress() {
    const list = root.querySelector("[data-lora-import-list]");
    if (!(list instanceof HTMLElement)) return;
    const listStatus = root.querySelector("[data-lora-import-list-status]");
    const activeCount = root.querySelector("[data-lora-active-count]");
    let pollTimer = null;
    let pollDelay = 3000;

    const cards = () => Array.from(list.querySelectorAll("[data-lora-import]"));
    const updateActiveCount = () => {
      const active = cards().filter((card) => !terminalImportStatuses.has(card.dataset.importStatus || ""));
      if (activeCount) activeCount.textContent = String(active.length);
      return active.length;
    };
    const hasTransientLibraryEntry = () => Array.from(
      root.querySelectorAll("[data-lora-entry]"),
    ).some((card) => ["activating", "deleting"].includes(card.dataset.loraStatus || ""));
    const schedule = () => {
      window.clearTimeout(pollTimer);
      if (
        document.visibilityState === "hidden"
        || (updateActiveCount() === 0 && !hasTransientLibraryEntry())
      ) return;
      pollTimer = window.setTimeout(poll, pollDelay);
    };
    const updateCard = (card, operation) => {
      const status = importStatus(operation);
      const previous = card.dataset.importStatus || "";
      card.dataset.importStatus = status;
      const copy = card.querySelector(".lora-import-copy span");
      const progress = card.querySelector("progress");
      const progressCopy = card.querySelector("[data-import-progress-copy]");
      const transferred = integerValue(operation.bytes_transferred ?? operation.progress_bytes);
      const total = integerValue(operation.total_bytes);
      if (copy) copy.textContent = statusLabel(status);
      if (progress instanceof HTMLProgressElement) {
        if (total > 0) {
          progress.max = total;
          progress.value = Math.min(transferred, total);
        } else {
          progress.removeAttribute("value");
        }
      }
      if (progressCopy) {
        progressCopy.textContent = total > 0
          ? `${formatBytes(transferred)} of ${formatBytes(total)} - ${statusLabel(status)}`
          : statusLabel(status);
      }
      return previous !== status && terminalImportStatuses.has(status);
    };

    async function poll() {
      if (document.visibilityState === "hidden") return;
      try {
        const payload = await requestJson(listUrl);
        const operations = Array.isArray(payload.imports) ? payload.imports : [];
        const entries = Array.isArray(payload.entries) ? payload.entries : [];
        const entryCards = new Map(
          Array.from(root.querySelectorAll("[data-lora-entry]"))
            .map((card) => [card.dataset.loraId || "", card]),
        );
        if (entries.some((entry) => {
          const card = entryCards.get(stringValue(entry?.id));
          return card && card.dataset.loraStatus !== libraryFilterStatus(entry);
        })) {
          window.location.reload();
          return;
        }
        const existing = new Map(cards().map((card) => [card.dataset.importId || "", card]));
        const incomingIds = new Set(operations.map(importId));
        const knownIds = new Set(existing.keys());
        if (incomingIds.size !== knownIds.size || [...incomingIds].some((id) => !knownIds.has(id))) {
          window.location.reload();
          return;
        }
        let terminalTransition = false;
        operations.forEach((operation) => {
          const card = existing.get(importId(operation));
          if (card) terminalTransition = updateCard(card, operation) || terminalTransition;
        });
        if (terminalTransition) {
          window.location.reload();
          return;
        }
        pollDelay = 3000;
        setMessage(listStatus, "");
      } catch (_error) {
        pollDelay = Math.min(pollDelay * 2, 15000);
        setMessage(listStatus, "Live progress was interrupted; retrying automatically.", "warning");
      }
      schedule();
    }

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-lora-import-action]");
      if (!(button instanceof HTMLButtonElement)) return;
      const card = button.closest("[data-lora-import]");
      if (!(card instanceof HTMLElement)) return;
      const id = card.dataset.importId || "";
      const action = button.dataset.loraImportAction || "";
      if (!id || !["retry", "cancel"].includes(action)) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      setMessage(listStatus, `${action === "retry" ? "Retrying" : "Cancelling"} ${card.querySelector("strong")?.textContent || "import"}...`);
      try {
        await requestJson(`/api/v1/loras/imports/${encodeURIComponent(id)}:${action}`, {
          method: "POST",
          body: {},
          idempotencyKey: stableMutationKey(button, `${action}:${id}:${card.dataset.importStatus}`),
        });
        window.location.reload();
      } catch (error) {
        setMessage(listStatus, error instanceof Error ? error.message : `Could not ${action} this import.`, "error");
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") schedule();
      else window.clearTimeout(pollTimer);
    });
    updateActiveCount();
    schedule();
  }

  initializeSourceChoice();
  initializeCivitaiImport();
  initializeManualImport();
  initializeLibrary();
  initializeArtifactActions();
  initializeImportProgress();
})();
