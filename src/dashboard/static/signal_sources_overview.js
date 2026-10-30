(() => {
  let openTypeDetails = null;
  let closeTypeTimer;
  function closeTypeDetails() {
    clearTimeout(closeTypeTimer);
    if (!openTypeDetails) return;
    openTypeDetails.details.hidden = true;
    openTypeDetails.button.setAttribute("aria-expanded", "false");
    openTypeDetails = null;
  }
  function showTypeDetails(button, details, pinned) {
    closeTypeDetails();
    details.hidden = false;
    const anchor = button.getBoundingClientRect();
    const popup = details.getBoundingClientRect();
    const left = Math.max(8, Math.min(anchor.left, window.innerWidth - popup.width - 8));
    const top = anchor.bottom + popup.height <= window.innerHeight - 8
      ? anchor.bottom : Math.max(8, anchor.top - popup.height);
    details.style.left = `${left}px`;
    details.style.top = `${top}px`;
    button.setAttribute("aria-expanded", "true");
    openTypeDetails = { button, details, pinned };
  }
  function scheduleTypeClose() {
    clearTimeout(closeTypeTimer);
    closeTypeTimer = setTimeout(() => {
      if (openTypeDetails && !openTypeDetails.pinned) closeTypeDetails();
    }, 120);
  }
  for (const [index, button] of [...document.querySelectorAll(".source-type-details-toggle")].entries()) {
    const details = button.parentElement.querySelector(".source-type-detail-list");
    details.id = `source-type-details-${index}`;
    details.setAttribute("role", "tooltip");
    button.setAttribute("aria-controls", details.id);
    button.setAttribute("aria-describedby", details.id);
    document.body.append(details);
    button.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "mouse") return;
      clearTimeout(closeTypeTimer);
      if (openTypeDetails?.button !== button) showTypeDetails(button, details, false);
    });
    button.addEventListener("pointerleave", scheduleTypeClose);
    details.addEventListener("pointerenter", () => clearTimeout(closeTypeTimer));
    details.addEventListener("pointerleave", scheduleTypeClose);
    button.addEventListener("click", () => {
      if (openTypeDetails?.button === button && openTypeDetails.pinned) closeTypeDetails();
      else showTypeDetails(button, details, true);
    });
  }
  document.addEventListener("pointerdown", (event) => {
    if (openTypeDetails && !openTypeDetails.button.contains(event.target)
        && !openTypeDetails.details.contains(event.target)) closeTypeDetails();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openTypeDetails) {
      const button = openTypeDetails.button;
      closeTypeDetails();
      button.focus();
    }
  });
  window.addEventListener("resize", closeTypeDetails);
  window.addEventListener("scroll", (event) => {
    if (event.target !== openTypeDetails?.details) closeTypeDetails();
  }, true);
  let openSourceMenu = null;
  function closeSourceMenu() {
    if (!openSourceMenu) return;
    openSourceMenu.popup.hidden = true;
    openSourceMenu.button.setAttribute("aria-expanded", "false");
    openSourceMenu = null;
  }
  for (const [index, button] of [...document.querySelectorAll(".source-menu-toggle")].entries()) {
    const popup = button.parentElement.querySelector(".source-menu-popup");
    popup.id = `source-menu-${index}`;
    button.setAttribute("aria-controls", popup.id);
    document.body.append(popup);
    button.addEventListener("click", () => {
      if (openSourceMenu?.button === button) { closeSourceMenu(); return; }
      closeSourceMenu();
      popup.hidden = false;
      const anchor = button.getBoundingClientRect();
      const bounds = popup.getBoundingClientRect();
      popup.style.left = `${Math.max(8, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 8))}px`;
      popup.style.top = `${anchor.bottom + bounds.height <= window.innerHeight - 8
        ? anchor.bottom : Math.max(8, anchor.top - bounds.height)}px`;
      button.setAttribute("aria-expanded", "true");
      openSourceMenu = { button, popup };
      popup.querySelector("button")?.focus();
    });
    popup.addEventListener("click", (event) => { if (event.target.closest("button")) closeSourceMenu(); });
  }
  document.addEventListener("pointerdown", (event) => {
    if (openSourceMenu && !openSourceMenu.button.contains(event.target)
        && !openSourceMenu.popup.contains(event.target)) closeSourceMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openSourceMenu) {
      const button = openSourceMenu.button;
      closeSourceMenu();
      button.focus();
    }
  });
  window.addEventListener("resize", closeSourceMenu);
  window.addEventListener("scroll", closeSourceMenu, true);
  const removalDialog = document.querySelector("#source-removal-dialog");
  const removalButtons = document.querySelectorAll(".remove-source-button");
  const removalClose = document.querySelector(".source-removal-close");
  const removalConfirm = document.querySelector(".source-removal-confirm");
  const removalCancelPending = document.querySelector(".source-removal-cancel-pending");
  const removalRecheck = document.querySelector(".source-removal-recheck");
  const removalStatus = document.querySelector("#source-removal-status");
  const removalExplanation = document.querySelector("#source-removal-explanation");
  let removalSource = null;
  let removalPlan = null;
  let removalBusy = false;
  let removalCompleted = false;

  function setRemovalBusy(busy) {
    removalBusy = busy;
    for (const button of removalButtons) button.disabled = busy;
    removalClose.disabled = busy;
    removalConfirm.disabled = busy || !removalPlan;
    removalCancelPending.disabled = busy;
    removalRecheck.disabled = busy;
  }
  async function removalRequest(endpoint, payload) {
    const response = await fetch(`/settings/signal-sources/${endpoint}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");
    return data;
  }
  function showRemovalPlan(plan) {
    if (plan.source_id !== removalSource.source_id || typeof plan.fingerprint !== "string"
        || !plan.fingerprint || !Number.isInteger(plan.owned_events) || plan.owned_events < 0
        || !Array.isArray(plan.exclusive_types) || !Array.isArray(plan.retained_types)) {
      throw new Error("Incomplete removal plan");
    }
    document.querySelector("#source-removal-name").textContent = plan.display_name || plan.source_id;
    document.querySelector("#source-removal-id").textContent = plan.source_id;
    document.querySelector("#source-removal-count").textContent = String(plan.owned_events);
    document.querySelector("#source-removal-exclusive").textContent = plan.exclusive_types.join(", ") || "None";
    document.querySelector("#source-removal-retained").textContent = plan.retained_types.join(", ") || "None";
  }
  function removalSuccess() {
    removalCompleted = true;
    removalPlan = null;
    removalCancelPending.hidden = true;
    removalRecheck.hidden = true;
    removalConfirm.hidden = true;
    removalExplanation.textContent = "The source and its proven owned events have been removed.";
    removalStatus.textContent = "Removal complete. Close to refresh the source list.";
    try { localStorage.setItem("signal-dashboard-source-removal", `${Date.now()}-${Math.random()}`); } catch (_) { /* Dashboard refreshes on navigation or polling. */ }
  }
  async function checkRemoval() {
    removalPlan = null;
    removalCancelPending.hidden = true;
    removalRecheck.hidden = true;
    removalConfirm.hidden = false;
    removalConfirm.textContent = "Confirm removal";
    removalExplanation.textContent = "Review the removal plan before confirming.";
    removalStatus.textContent = "Checking removal status…";
    setRemovalBusy(true);
    try {
      const state = await removalRequest("removal-status", removalSource);
      if (state.status === "completed") {
        removalSuccess();
        return;
      }
      if (state.status === "committed") {
        showRemovalPlan(state.plan);
        if (state.fingerprint !== state.plan.fingerprint) throw new Error("Recovery plan does not match");
        removalPlan = { ...removalSource, fingerprint: state.fingerprint };
        removalConfirm.textContent = "Finish removal";
        removalExplanation.textContent = "Event deletion has committed. Confirm to finish removing the source configuration. Types now in use will be kept.";
        removalStatus.textContent = "Cancellation is no longer available.";
        return;
      }
      if (state.status !== "ready" && state.status !== "pending") throw new Error("Unknown removal status");
      removalCancelPending.hidden = state.status !== "pending";
      if (state.status === "pending") removalStatus.textContent = "Removal is pending; imports are blocked.";
      const plan = await removalRequest("removal-preflight", removalSource);
      showRemovalPlan(plan);
      if (state.status === "pending" && state.fingerprint && state.fingerprint !== plan.fingerprint) {
        removalStatus.textContent = "The pending plan changed. Cancel pending removal and run preflight again.";
        return;
      }
      removalPlan = { ...removalSource, fingerprint: plan.fingerprint };
      removalStatus.textContent = state.status === "pending"
        ? "Removal is pending; imports are blocked. Confirm to resume or cancel the pending removal."
        : "No events have been removed. Confirm to delete the listed owned events and source.";
    } catch (error) {
      removalStatus.textContent = `Could not check removal: ${error.message}`;
      removalRecheck.hidden = false;
    } finally {
      setRemovalBusy(false);
    }
  }
  removalDialog?.addEventListener("cancel", (event) => {
    if (removalBusy) event.preventDefault();
  });
  removalDialog?.addEventListener("close", () => {
    if (removalCompleted) window.location.reload();
  });
  removalClose?.addEventListener("click", () => removalDialog.close());
  for (const button of removalButtons) {
    button.addEventListener("click", () => {
      if (removalBusy) return;
      removalSource = { source_id: button.dataset.sourceId };
      removalCompleted = false;
      document.querySelector("#source-removal-name").textContent = button.dataset.sourceName;
      document.querySelector("#source-removal-id").textContent = button.dataset.sourceId;
      for (const id of ["count", "exclusive", "retained"]) document.querySelector(`#source-removal-${id}`).textContent = "—";
      removalDialog.showModal();
      void checkRemoval();
    });
  }
  removalRecheck?.addEventListener("click", () => { void checkRemoval(); });
  removalConfirm?.addEventListener("click", async () => {
    if (removalBusy || !removalPlan) return;
    const selection = removalPlan;
    removalPlan = null;
    removalCancelPending.hidden = true;
    removalRecheck.hidden = true;
    setRemovalBusy(true);
    removalStatus.textContent = "Removing source…";
    try {
      const result = await removalRequest("remove", { ...selection, confirmed: true });
      if (result.status !== "completed") throw new Error("Completion was not confirmed");
      removalSuccess();
    } catch (error) {
      removalStatus.textContent = `Removal did not complete: ${error.message}. Check status before retrying.`;
      removalRecheck.hidden = false;
    } finally {
      setRemovalBusy(false);
    }
  });
  removalCancelPending?.addEventListener("click", async () => {
    if (removalBusy || !window.confirm("Cancel the pending removal? Imports can resume.")) return;
    removalPlan = null;
    setRemovalBusy(true);
    removalStatus.textContent = "Cancelling pending removal…";
    try {
      await removalRequest("removal-cancel", { ...removalSource, confirmed: true });
      removalCancelPending.hidden = true;
      removalRecheck.hidden = false;
      removalStatus.textContent = "Pending removal cancelled. Check status to create a new plan.";
    } catch (error) {
      removalStatus.textContent = `Could not cancel: ${error.message}. Check status before retrying.`;
      removalCancelPending.hidden = true;
      removalRecheck.hidden = false;
    } finally {
      setRemovalBusy(false);
    }
  });
  const importDialog = document.querySelector("#source-import-dialog");
  const importButtons = document.querySelectorAll(".import-source-button");
  const importCancel = document.querySelector(".source-import-cancel");
  const importConfirm = document.querySelector(".source-import-confirm");
  const importStatus = document.querySelector("#source-import-status");
  let importPreflight = null;
  let importBusy = false;
  let importProgressTimer = null;

  function formatElapsed(milliseconds) {
    return `${Math.max(0, milliseconds / 1000).toFixed(1)}s`;
  }

  function formatLocalDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("en-GB", {
      dateStyle: "medium", timeStyle: "short",
    }).format(date);
  }

  function refreshSourceMetrics(sourceId, metrics) {
    if (!metrics || !Number.isInteger(metrics.events)) return;
    const eventCount = document.querySelector(`.source-event-count[data-source-id="${sourceId}"]`);
    if (eventCount) eventCount.textContent = String(metrics.events);
    const lastImport = document.querySelector(`.source-last-import[data-source-id="${sourceId}"]`);
    if (lastImport && typeof metrics.last_import === "string") {
      lastImport.textContent = formatLocalDateTime(metrics.last_import);
      lastImport.title = metrics.last_import;
    }
  }

  for (const time of document.querySelectorAll(".source-last-import-time")) {
    time.textContent = formatLocalDateTime(time.dateTime);
    time.title = time.dateTime;
  }

  function setImportBusy(busy) {
    importBusy = busy;
    for (const button of importButtons) button.disabled = busy;
    importCancel.disabled = busy;
    importConfirm.disabled = busy || !importPreflight;
  }

  function stopImportProgress() {
    if (importProgressTimer !== null) window.clearInterval(importProgressTimer);
    importProgressTimer = null;
  }

  async function refreshImportProgress(sourceId) {
    try {
      const response = await fetch(
        `/settings/signal-sources/import-progress?source_id=${encodeURIComponent(sourceId)}`,
      );
      const data = await response.json();
      if (response.ok && importBusy && data.running && Number.isInteger(data.processed)) {
        importStatus.textContent = `Importing… Processed: ${data.processed} signals`;
      }
    } catch (_) {
      // The final import response remains authoritative if this optional poll fails.
    }
  }

  function startImportProgress(sourceId) {
    stopImportProgress();
    void refreshImportProgress(sourceId);
    importProgressTimer = window.setInterval(() => {
      void refreshImportProgress(sourceId);
    }, 250);
  }

  importDialog?.addEventListener("cancel", (event) => {
    if (importBusy) event.preventDefault();
  });
  importCancel?.addEventListener("click", () => importDialog.close());
  for (const button of importButtons) {
    button.addEventListener("click", async () => {
      if (importBusy) return;
      importPreflight = null;
      importCancel.textContent = "Cancel";
      setImportBusy(true);
      try {
        const response = await fetch("/settings/signal-sources/import-preflight", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_id: button.dataset.sourceId }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Preflight failed");
        importPreflight = { source_id: data.source_id, fingerprint: data.fingerprint };
        document.querySelector("#source-import-name").textContent = data.display_name || data.source_id;
        document.querySelector("#source-import-database").textContent = data.database_path;
        document.querySelector("#source-import-table").textContent = data.table;
        document.querySelector("#source-import-id").textContent = data.source_id;
        importStatus.textContent = "Preflight passed. Ready to import. No changes have been made.";
        importDialog.showModal();
      } catch (error) {
        window.alert(`Preflight failed: ${error.message}`);
      } finally {
        setImportBusy(false);
      }
    });
  }
  importConfirm?.addEventListener("click", async () => {
    if (importBusy || !importPreflight) return;
    const selection = importPreflight;
    importPreflight = null;
    setImportBusy(true);
    importStatus.textContent = "Importing…";
    const startedAt = performance.now();
    startImportProgress(selection.source_id);
    try {
      const response = await fetch("/settings/signal-sources/import-once", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...selection, confirmed: true }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Import failed");
      if (data.error) {
        importStatus.textContent = `Import error: ${data.error}. Counts may be unavailable.`;
      } else if ([data.scanned, data.inserted, data.rejected].every(Number.isInteger)) {
        refreshSourceMetrics(selection.source_id, data.metrics);
        importStatus.textContent = `Import complete in ${formatElapsed(performance.now() - startedAt)}. Scanned: ${data.scanned}; inserted: ${data.inserted}; rejected: ${data.rejected}.`;
        importCancel.textContent = "Close";
      } else {
        importStatus.textContent = "Import result is incomplete; completion and counts are unknown.";
      }
    } catch (error) {
      importStatus.textContent = `Import result unavailable: ${error.message}. Partial completion is possible.`;
    } finally {
      stopImportProgress();
      setImportBusy(false);
    }
  });
  for (const button of document.querySelectorAll(".auto-sync-button")) {
    button.addEventListener("click", async () => {
      const enabling = button.dataset.syncEnabled !== "true";
      const error = button.parentElement.querySelector(".auto-sync-error");
      const previous = button.dataset.syncEnabled === "true";
      button.disabled = true;
      if (error) error.textContent = "";
      button.dataset.syncEnabled = String(enabling);
      button.textContent = enabling ? "On" : "Off";
      button.setAttribute("aria-label", `Auto Sync ${enabling ? "on" : "off"}`);
      button.classList.toggle("is-enabled", enabling);
      button.classList.toggle("is-disabled", !enabling);
      try {
        const response = await fetch("/settings/signal-sources/auto-sync", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({source_id: button.dataset.sourceId, enabled: enabling}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Could not update Auto Sync");
      } catch (error) {
        button.dataset.syncEnabled = String(previous);
        button.textContent = previous ? "On" : "Off";
        button.setAttribute("aria-label", `Auto Sync ${previous ? "on" : "off"}`);
        button.classList.toggle("is-enabled", previous);
        button.classList.toggle("is-disabled", !previous);
        if (button.parentElement.querySelector(".auto-sync-error")) {
          button.parentElement.querySelector(".auto-sync-error").textContent = error.message;
        }
      } finally {
        button.disabled = false;
      }
    });
  }
  const editDialog = document.querySelector("#source-edit-dialog");
  const editForm = document.querySelector("#source-edit-form");
  const editName = document.querySelector("#source-edit-name");
  const editStatus = document.querySelector("#source-edit-status");
  const editSave = editForm?.querySelector(".source-edit-save");
  const editDialogHeader = document.querySelector("#source-edit-dialog-header");
  const editExplanation = document.querySelector("#source-edit-explanation");
  const editTypes = document.querySelector("#source-edit-types");
  const editTypeList = document.querySelector("#source-edit-type-list");
  const editDiscovery = document.querySelector("#source-edit-discovery");
  const editDiscoveryMessage = document.querySelector("#source-edit-discovery-message");
  const editDiscoveryRetry = document.querySelector("#source-edit-discovery-retry");
  let editTypeData = null;
  let editDrag = null;
  let editDiscoveryGeneration = 0;
  function resetEditDialogPosition() {
    editDrag = null;
    editDialog.style.removeProperty("left");
    editDialog.style.removeProperty("top");
    editDialog.style.removeProperty("margin");
    editDialog.style.removeProperty("position");
  }
  function clampEditDialogPosition(left, top) {
    const bounds = editDialog.getBoundingClientRect();
    return {
      left: Math.max(0, Math.min(left, window.innerWidth - bounds.width)),
      top: Math.max(0, Math.min(top, window.innerHeight - bounds.height)),
    };
  }
  function clampOpenEditDialogPosition() {
    if (!editDialog.open || !editDialog.style.left) return;
    const position = clampEditDialogPosition(
      Number.parseFloat(editDialog.style.left), Number.parseFloat(editDialog.style.top),
    );
    editDialog.style.left = `${position.left}px`;
    editDialog.style.top = `${position.top}px`;
  }
  if (editDialogHeader) {
    editDialogHeader.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "mouse" || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
      if (event.target.closest("button, input, select, textarea, a, [role='button'], [contenteditable='true']")) return;
      const bounds = editDialog.getBoundingClientRect();
      editDrag = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
      editDialogHeader.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    editDialogHeader.addEventListener("pointermove", (event) => {
      if (!editDrag) return;
      editDialog.style.position = "fixed";
      editDialog.style.margin = "0";
      const position = clampEditDialogPosition(event.clientX - editDrag.x, event.clientY - editDrag.y);
      editDialog.style.left = `${position.left}px`;
      editDialog.style.top = `${position.top}px`;
    });
    editDialogHeader.addEventListener("pointerup", () => { editDrag = null; });
    editDialogHeader.addEventListener("pointercancel", () => { editDrag = null; });
  }
  window.addEventListener("resize", clampOpenEditDialogPosition);
  editDialog?.addEventListener("close", () => {
    editDiscoveryGeneration++;
    resetEditDialogPosition();
  });
  document.querySelector(".source-edit-cancel")?.addEventListener("click", () => editDialog.close());
  function newSourceTypeFields(value) {
    const fields = document.createElement("div");
    fields.className = "source-edit-new-type-fields";
    const example = value.replace(/[_-]+/g, " ").trim().replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
    const letters = value.toUpperCase().replace(/[^A-ZА-ЯЁ0-9]/g, "");
    for (const [title, className, placeholder, max] of [
      ["Full Name", "source-edit-type-name", example || "Signal name", 30],
      ["Label", "source-edit-type-label", letters.slice(0, 2) || "DU", 3],
    ]) {
      const label = document.createElement("label");
      label.textContent = title;
      const input = document.createElement("input");
      input.className = className;
      input.placeholder = placeholder;
      input.maxLength = max;
      input.required = true;
      if (className === "source-edit-type-label") input.addEventListener("input", () => {
        input.value = input.value.replace(/[^A-Za-zА-Яа-яЁё0-9]/g, "").toUpperCase().slice(0, max);
      });
      label.append(input);
      fields.append(label);
    }
    return fields;
  }
  function renderImportTypes(data) {
    editTypeData = data;
    editTypeList.replaceChildren();
    const selected = new Set(data.selected_types);
    const values = [...new Set([...data.values, ...data.selected_types])];
    for (const value of values) {
      const row = document.createElement("div");
      row.className = "source-edit-type-row";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = value;
      checkbox.checked = selected.has(value);
      checkbox.className = "source-edit-type-checkbox";
      checkbox.setAttribute("aria-label", `Import ${value}`);
      const name = document.createElement("span");
      name.textContent = value;
      const configured = data.type_mapping[value];
      if (configured) {
        const detail = document.createElement("small");
        detail.textContent = `Configured: ${data.type_names[configured] || configured}`;
        row.append(checkbox, name, detail);
      } else {
        const fields = newSourceTypeFields(value);
        fields.hidden = !checkbox.checked;
        checkbox.addEventListener("change", () => { fields.hidden = !checkbox.checked; });
        row.dataset.newId = `custom_${crypto.randomUUID().replaceAll("-", "")}`;
        row.append(checkbox, name, fields);
      }
      editTypeList.append(row);
    }
    editTypes.hidden = false;
  }
  function showEditDiscovery(message, retry = false) {
    editDiscovery.hidden = false;
    editDiscoveryMessage.textContent = message;
    editDiscoveryRetry.hidden = !retry;
  }
  async function loadEditImportTypes(sourceId) {
    const generation = ++editDiscoveryGeneration;
    editTypeData = null;
    editSave.disabled = true;
    editTypes.hidden = true;
    editTypeList.replaceChildren();
    editStatus.textContent = "";
    showEditDiscovery("Loading signal types...");
    try {
      const response = await fetch(`/settings/signal-sources/${encodeURIComponent(sourceId)}/import-types`);
      const data = await response.json();
      if (generation !== editDiscoveryGeneration || !editDialog.open
          || editForm.dataset.sourceId !== sourceId) return;
      if (!response.ok) throw new Error(data.detail || "Could not load signal types");
      editTypeData = data;
      editDiscovery.hidden = true;
      if (data.fixed_type) {
        editExplanation.hidden = true;
        editStatus.textContent = "Fixed-type sources have no selectable signal types";
      } else {
        renderImportTypes(data);
      }
      editSave.disabled = false;
    } catch (error) {
      if (generation !== editDiscoveryGeneration || !editDialog.open
          || editForm.dataset.sourceId !== sourceId) return;
      showEditDiscovery(error.message || "Could not load signal types.", true);
    }
  }
  editDiscoveryRetry?.addEventListener("click", () => {
    if (editForm.dataset.sourceId) void loadEditImportTypes(editForm.dataset.sourceId);
  });
  for (const button of document.querySelectorAll(".edit-source-button")) {
    button.addEventListener("click", async () => {
      resetEditDialogPosition();
      editForm.dataset.sourceId = button.dataset.sourceId;
      editName.value = button.dataset.sourceName;
      editStatus.textContent = "";
      editDialog.showModal();
      editName.focus();
      editExplanation.hidden = false;
      void loadEditImportTypes(button.dataset.sourceId);
    });
  }
  editForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const displayName = editName.value.trim();
    if (!displayName) {
      editStatus.textContent = "Enter a source display name.";
      return;
    }
    if (!editTypeData) {
      editStatus.textContent = "Import signal types are unavailable; nothing was saved.";
      return;
    }
    if (editTypeData.fixed_type) {
      editSave.disabled = true;
      editStatus.textContent = "Saving…";
      try {
        const response = await fetch("/settings/signal-sources/edit", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_id: editForm.dataset.sourceId, display_name: displayName }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Could not save source name");
        window.alert(data.message);
        window.location.reload();
      } catch (error) {
        editSave.disabled = false;
        editStatus.textContent = error.message;
      }
      return;
    }
    const selectedRows = [...editTypeList.querySelectorAll(".source-edit-type-row")]
      .filter((row) => row.querySelector(".source-edit-type-checkbox").checked);
    const selectedTypes = selectedRows.map((row) => row.querySelector(".source-edit-type-checkbox").value);
    const newTypes = [];
    const typeMapping = {};
    for (const row of selectedRows) {
      const sourceType = row.querySelector(".source-edit-type-checkbox").value;
      if (editTypeData.type_mapping[sourceType]) {
        typeMapping[sourceType] = editTypeData.type_mapping[sourceType];
        continue;
      }
      const displayName = row.querySelector(".source-edit-type-name").value.trim();
      const shortLabel = row.querySelector(".source-edit-type-label").value;
      if (!displayName || !/^[A-ZА-ЯЁ0-9]{1,3}$/.test(shortLabel)) {
        editStatus.textContent = "Enter a Full Name and 1–3 character Label for each new signal type.";
        return;
      }
      typeMapping[sourceType] = row.dataset.newId;
      newTypes.push({ id: row.dataset.newId, display_name: displayName, short_label: shortLabel });
    }
    if (!selectedTypes.length) {
      editStatus.textContent = "Select at least one signal type to import.";
      return;
    }
    editSave.disabled = true;
    editStatus.textContent = "Saving…";
    try {
      const response = await fetch("/settings/signal-sources/edit-import-types", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_id: editForm.dataset.sourceId, display_name: displayName, selected_types: selectedTypes, type_mapping: typeMapping, new_types: newTypes }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not save import signal types");
      window.alert(data.message);
      window.location.reload();
    } catch (error) {
      editSave.disabled = false;
      editStatus.textContent = error.message;
    }
  });
  for (const button of document.querySelectorAll(".toggle-source-button")) {
    button.addEventListener("click", async () => {
      const previous = button.dataset.enabled === "true";
      const enabling = !previous;
      const errorMessage = button.parentElement.querySelector(".source-visibility-error");
      const setState = (enabled) => {
        button.dataset.enabled = String(enabled);
        button.setAttribute("aria-pressed", String(enabled));
        button.classList.toggle("is-enabled", enabled);
        button.classList.toggle("is-disabled", !enabled);
        button.textContent = enabled ? "Enabled" : "Disabled";
      };
      errorMessage.textContent = "";
      setState(enabling);
      button.disabled = true;
      try {
        const response = await fetch("/settings/signal-sources/enabled", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_id: button.dataset.sourceId, enabled: enabling }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Could not update source state");
      } catch (error) {
        setState(previous);
        errorMessage.textContent = `Could not update visibility: ${error.message}`;
      } finally {
        button.disabled = false;
      }
    });
  }
})();
