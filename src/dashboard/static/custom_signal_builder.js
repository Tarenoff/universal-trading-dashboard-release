(() => {
  const dialog = document.querySelector("#custom-signal-dialog");
  if (!dialog) return;
  const dialogHeader = document.querySelector("#custom-signal-dialog-header");
  const deleteDialog = document.querySelector("#custom-signal-delete-dialog");
  const deleteName = document.querySelector("#custom-signal-delete-name");
  const deleteStatus = document.querySelector("#custom-signal-delete-status");
  const deleteCancel = document.querySelector("#custom-signal-delete-cancel");
  const deleteConfirm = document.querySelector("#custom-signal-delete-confirm");
  const form = document.querySelector("#custom-signal-form");
  const filters = document.querySelector("#custom-signal-filters");
  const status = document.querySelector("#custom-signal-status");
  const source = form.elements.namedItem("source_id");
  const name = form.elements.namedItem("display_name");
  const labelInput = form.elements.namedItem("short_label");
  const submit = document.querySelector("#custom-signal-submit");
  let fields = [];
  let editingId = null;
  let drag = null;
  let pendingDeleteButton = null;
  let deleteBusy = false;

  function resetDialogPosition() {
    drag = null;
    dialog.style.removeProperty("left");
    dialog.style.removeProperty("top");
    dialog.style.removeProperty("margin");
    dialog.style.removeProperty("position");
  }
  function clampDialogPosition(left, top) {
    const bounds = dialog.getBoundingClientRect();
    return {
      left: Math.max(0, Math.min(left, window.innerWidth - bounds.width)),
      top: Math.max(0, Math.min(top, window.innerHeight - bounds.height)),
    };
  }
  function clampOpenDialogPosition() {
    if (!dialog.open || !dialog.style.left) return;
    const position = clampDialogPosition(
      Number.parseFloat(dialog.style.left), Number.parseFloat(dialog.style.top),
    );
    dialog.style.left = `${position.left}px`;
    dialog.style.top = `${position.top}px`;
  }
  if (dialogHeader) {
    dialogHeader.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "mouse" || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
      if (event.target.closest("button, input, select, textarea, a, [role='button'], [role='tab'], [contenteditable='true']")) return;
      const bounds = dialog.getBoundingClientRect();
      drag = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
      dialogHeader.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    dialogHeader.addEventListener("pointermove", (event) => {
      if (!drag) return;
      dialog.style.position = "fixed";
      dialog.style.margin = "0";
      const position = clampDialogPosition(event.clientX - drag.x, event.clientY - drag.y);
      dialog.style.left = `${position.left}px`;
      dialog.style.top = `${position.top}px`;
    });
    dialogHeader.addEventListener("pointerup", () => { drag = null; });
    dialogHeader.addEventListener("pointercancel", () => { drag = null; });
  }
  window.addEventListener("resize", clampOpenDialogPosition);

  function label(field) {
    const [, name] = field.split(/:(.*)/s);
    const display = (name || field).replaceAll("_", " ");
    const duplicate = fields.some((candidate) => candidate !== field && candidate.split(/:(.*)/s)[1] === name);
    if (!duplicate) return display;
    return `${display} (${field.startsWith("metadata:") ? "Metadata" : "Standard"})`;
  }
  function addFilter(condition = { field: "", operator: "=", value: "" }) {
    const row = document.createElement("div");
    row.className = "custom-signal-filter";
    const field = document.createElement("select");
    field.append(new Option("Select a field", ""));
    for (const value of fields) field.append(new Option(label(value), value, false, value === condition.field));
    const operator = document.createElement("select");
    operator.className = "custom-signal-operator";
    operator.setAttribute("aria-label", "Filter operator");
    const value = document.createElement("div");
    value.className = "custom-signal-filter-value";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove filter");
    remove.title = "Remove filter";
    const removeIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    removeIcon.setAttribute("viewBox", "0 0 24 24");
    removeIcon.setAttribute("aria-hidden", "true");
    const removePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    removePath.setAttribute("d", "M5 7h14M10 11v6M14 11v6M9 7l1-2h4l1 2m-8 0 1 13h8l1-13");
    removeIcon.append(removePath);
    remove.append(removeIcon);
    remove.addEventListener("click", () => row.remove());
    field.addEventListener("change", () => loadProfile(row, { operator: "=", value: "" }));
    row.append(field, operator, value, remove);
    filters.append(row);
    if (field.value) loadProfile(row, condition);
    else value.textContent = "";
  }
  function setFieldProfile(row, profile, condition) {
    const operator = row.querySelector(".custom-signal-operator");
    const valueCell = row.querySelector(".custom-signal-filter-value");
    operator.replaceChildren();
    valueCell.replaceChildren();
    if (profile.kind === "numeric") {
      for (const item of ["=", ">=", "<="]) operator.append(new Option(item, item, false, item === condition.operator));
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.placeholder = "Numeric value";
      input.value = condition.value === "" || condition.value === undefined ? "" : String(condition.value);
      valueCell.append(input);
      return;
    }
    operator.append(new Option("=", "="));
    const select = document.createElement("select");
    select.append(new Option("Select a value", ""));
    const selected = JSON.stringify(condition.value);
    for (const item of profile.values || []) {
      const encoded = JSON.stringify(item);
      select.append(new Option(String(item), encoded, false, encoded === selected));
    }
    valueCell.append(select);
  }
  async function loadProfile(row, condition) {
    const field = row.querySelector("select");
    const valueCell = row.querySelector(".custom-signal-filter-value");
    if (!field.value) return;
    valueCell.replaceChildren();
    try {
      const response = await fetch(`/settings/custom-signals/fields?source_id=${encodeURIComponent(source.value)}&field=${encodeURIComponent(field.value)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not load field values");
      if (field.value !== data.profile?.field) return;
      setFieldProfile(row, data.profile, condition);
    } catch (error) { status.textContent = error.message; }
  }
  async function loadFields(conditions = []) {
    filters.replaceChildren();
    fields = [];
    if (!source.value) return;
    status.textContent = "Loading source fields…";
    try {
      const response = await fetch(`/settings/custom-signals/fields?source_id=${encodeURIComponent(source.value)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not load source fields");
      fields = data.available_fields || [];
      for (const condition of conditions) addFilter(condition);
      if (!conditions.length) addFilter();
      status.textContent = fields.length ? "" : "No imported fields are available for this source yet.";
    } catch (error) { status.textContent = error.message; }
  }
  function open(button) {
    editingId = button?.dataset.id || null;
    name.value = button?.dataset.name || "";
    labelInput.value = button?.dataset.label || "";
    source.value = button?.dataset.sourceId || "";
    submit.textContent = editingId ? "Save Changes" : "Create Signal";
    status.textContent = "";
    let conditions = [];
    try { conditions = button ? JSON.parse(button.dataset.conditions || "[]") : []; } catch (_) {}
    loadFields(conditions);
    resetDialogPosition();
    dialog.showModal();
  }
  document.querySelector("#add-custom-signal").addEventListener("click", () => open());
  for (const button of document.querySelectorAll(".custom-signal-edit")) button.addEventListener("click", () => open(button));
  for (const button of document.querySelectorAll(".custom-signal-toggle")) button.addEventListener("click", async () => {
    try {
      const response = await fetch(`/settings/custom-signals/${encodeURIComponent(button.dataset.id)}/enabled`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: button.dataset.enabled !== "true" }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not update custom signal");
      window.location.reload();
    } catch (error) { window.alert(error.message); }
  });
  for (const button of document.querySelectorAll(".custom-signal-delete")) button.addEventListener("click", () => {
    if (!deleteDialog || !deleteName || !deleteStatus) return;
    pendingDeleteButton = button;
    deleteName.textContent = button.dataset.name || "this custom signal";
    deleteStatus.textContent = "";
    deleteDialog.showModal();
  });
  if (deleteCancel) deleteCancel.addEventListener("click", () => deleteDialog.close());
  if (deleteConfirm) deleteConfirm.addEventListener("click", async () => {
    if (!pendingDeleteButton || deleteBusy) return;
    deleteBusy = true;
    deleteConfirm.disabled = true;
    if (deleteCancel) deleteCancel.disabled = true;
    pendingDeleteButton.disabled = true;
    try {
      const response = await fetch(`/settings/custom-signals/${encodeURIComponent(pendingDeleteButton.dataset.id)}`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not delete custom signal");
      window.location.reload();
    } catch (error) {
      pendingDeleteButton.disabled = false;
      deleteStatus.textContent = error.message;
      deleteConfirm.disabled = false;
      if (deleteCancel) deleteCancel.disabled = false;
      deleteBusy = false;
    }
  });
  source.addEventListener("change", () => loadFields());
  document.querySelector("#custom-signal-add-filter").addEventListener("click", () => addFilter());
  document.querySelector("#custom-signal-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", resetDialogPosition);
  if (deleteDialog) deleteDialog.addEventListener("cancel", (event) => { if (deleteBusy) event.preventDefault(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let conditions;
    try {
      conditions = [...filters.children].map((row) => {
        const field = row.querySelector("select").value;
        const operator = row.querySelector(".custom-signal-operator").value;
        const control = row.querySelector(".custom-signal-filter-value input, .custom-signal-filter-value select");
        if (!field || !control?.value) throw new Error("Choose a field and value for every filter.");
        if (control instanceof HTMLInputElement) {
          const value = Number(control.value);
          if (!Number.isFinite(value)) throw new Error("Numeric filters require a finite value.");
          return { field, operator, value };
        }
        return { field, operator, value: JSON.parse(control.value) };
      });
    } catch (error) { status.textContent = error.message; return; }
    if (!conditions.length) { status.textContent = "Add at least one field/value filter."; return; }
    const cleanLabel = labelInput.value.replace(/[^A-Za-zА-Яа-яЁё0-9]/g, "").toUpperCase().slice(0, 3);
    labelInput.value = cleanLabel;
    if (!cleanLabel) { status.textContent = "Label must contain 1–3 Latin or Cyrillic letters or digits."; return; }
    const payload = { source_id: source.value, display_name: name.value, short_label: cleanLabel, conditions };
    try {
      const response = await fetch(editingId ? `/settings/custom-signals/${encodeURIComponent(editingId)}` : "/settings/custom-signals", { method: editingId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not save custom signal");
      window.location.reload();
    } catch (error) { status.textContent = error.message; }
  });
})();
