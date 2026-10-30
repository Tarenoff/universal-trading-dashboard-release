(() => {
  const form = document.querySelector("#source-inspector-form");
  const databasePath = document.querySelector("#source-database-path");
  const status = document.querySelector("#source-inspector-status");
  const schema = document.querySelector("#source-schema");
  const table = document.querySelector("#source-table");
  const columns = document.querySelector("#source-columns");
  const tableFeedback = document.querySelector("#source-table-feedback");
  const mappingFeedback = document.querySelector("#source-mapping-feedback");
  const stepOneNext = document.querySelector("#source-step-one-next");
  const stepTwoBack = document.querySelector("#source-step-two-back");
  const stepTwoNext = document.querySelector("#source-step-two-next");
  const stepThreeBack = document.querySelector("#source-step-three-back");
  const stepThreeNext = document.querySelector("#source-step-three-next");
  const stepFourBack = document.querySelector("#source-step-four-back");
  const connectSource = document.querySelector("#source-connect");
  const previewSummary = document.querySelector("#source-preview-summary");
  const previewFeedback = document.querySelector("#source-preview-feedback");
  const previewResults = document.querySelector("#source-preview-results");
  const previewEvents = document.querySelector("#source-preview-events");
  const previewRejected = document.querySelector("#source-preview-rejected");
  const steps = document.querySelectorAll("[data-wizard-step]");
  const indicators = document.querySelectorAll("[data-wizard-indicator]");
  const mappingColumns = document.querySelectorAll("[data-mapping-column]");
  const signalTypeModes = document.querySelectorAll("[name='signal-type-mode']");
  const signalTypeColumn = document.querySelector("#mapping-signal-type-column");
  const suggestionNote = document.querySelector("#signal-type-suggestion");
  const fixedSignalType = document.querySelector("#mapping-signal-type-fixed");
  const typeMappingPanel = document.querySelector("#signal-type-mapping-panel");
  const typeMappingRows = document.querySelector("#signal-type-mapping-rows");
  const fixedTypeRows = document.querySelector("#fixed-type-mapping-rows");
  let connectionVerified = false;
  let tableVerified = false;
  let mappingValid = false;
  let loadedColumns = [];
  let discoveryGeneration = 0;
  let inspectionGeneration = 0;
  let previewGeneration = 0;
  let previewValid = false;
  let discoveryReady = false;
  let discoveryColumn = "";
  let inspectedTable = "";
  let connectElapsedTimer = null;

  function formatElapsed(milliseconds) {
    return `${Math.max(0, milliseconds / 1000).toFixed(1)}s`;
  }

  function stopConnectElapsed() {
    clearInterval(connectElapsedTimer);
    connectElapsedTimer = null;
  }

  function setFeedback(element, message, state = "") {
    element.textContent = message;
    element.dataset.state = state;
  }

  function showStep(number) {
    for (const step of steps) step.hidden = step.dataset.wizardStep !== String(number);
    for (const indicator of indicators) {
      indicator.classList.toggle("is-active", indicator.dataset.wizardIndicator === String(number));
      indicator.classList.toggle("is-complete", Number(indicator.dataset.wizardIndicator) < number);
    }
  }

  function updateStepOneNext() {
    stepOneNext.disabled = !connectionVerified || !form.checkValidity();
  }

  function updateStepTwoNext() {
    stepTwoNext.disabled = !tableVerified;
  }

  function updateStepThreeNext() {
    stepThreeNext.disabled = !mappingValid;
  }

  function updateSignalTypeMode() {
    const fixed = document.querySelector("[name='signal-type-mode']:checked").value === "fixed";
    signalTypeColumn.disabled = fixed || !tableVerified;
    fixedSignalType.disabled = !fixed || !tableVerified;
    typeMappingPanel.hidden = fixed;
    fixedTypeRows.hidden = !fixed;
  }

  function invalidateValidation() {
    invalidatePreview();
    const fixed = document.querySelector("[name='signal-type-mode']:checked").value === "fixed";
    mappingValid = tableVerified && (fixed || discoveryReady) && mappingErrors().length === 0;
    updateStepThreeNext();
  }

  function invalidatePreview() {
    previewGeneration++;
    previewValid = false;
    connectSource.disabled = true;
    previewResults.hidden = true;
  }

  function addTypeMappingRow(value, fixed = false) {
    const row = document.createElement("div");
    row.className = "signal-type-mapping-row";
    row.dataset.source = value;
    row.dataset.newId = `custom_${crypto.randomUUID().replaceAll("-", "")}`;
    const header = document.createElement("div");
    header.className = "signal-type-mapping-header";
    const source = document.createElement("strong");
    source.textContent = value;
    const mode = document.createElement("select");
    mode.className = "type-mapping-mode";
    mode.setAttribute("aria-label", `Mapping mode for ${value}`);
    mode.append(new Option("Create new signal type", "new"), new Option("Use existing signal type", "existing"));
    let include;
    if (fixed) {
      header.append(source, mode);
    } else {
      const choice = document.createElement("label");
      choice.className = "type-import-choice";
      include = document.createElement("input");
      include.type = "checkbox";
      include.className = "type-import";
      choice.append(include, document.createTextNode("Import "), source);
      header.append(choice, mode);
    }
    const fields = document.createElement("div");
    fields.className = "signal-type-new-fields";
    const example = value.replace(/[_-]+/g, " ").trim().replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
    function makeField(title, className, placeholder, max) {
      const wrapper = document.createElement("label");
      const fieldLabel = document.createElement("span");
      fieldLabel.textContent = title;
      const requiredMark = document.createElement("span");
      requiredMark.className = "required-mark";
      requiredMark.setAttribute("aria-hidden", "true");
      requiredMark.textContent = "*";
      fieldLabel.append(requiredMark);
      wrapper.append(fieldLabel);
      const input = document.createElement("input");
      input.className = className;
      input.placeholder = placeholder;
      input.maxLength = max;
      input.required = true;
      const counter = document.createElement("small");
      counter.textContent = `0 / ${max}`;
      input.addEventListener("beforeinput", (event) => {
        if (event.data && input.value.length - (input.selectionEnd - input.selectionStart) + event.data.length > max) {
          setFeedback(mappingFeedback, `${title} must be at most ${max} characters.`, "error");
        }
      });
      input.addEventListener("input", () => {
        if (className === "type-short-label") {
          const clean = input.value.replace(/[^A-Za-zА-Яа-яЁё0-9]/g, "").toUpperCase().slice(0, max);
          if (clean !== input.value.toUpperCase()) setFeedback(mappingFeedback, "Short label allows only Latin or Cyrillic letters and digits (maximum 3).", "error");
          input.value = clean;
        }
        counter.textContent = `${input.value.length} / ${max}`;
        invalidateValidation();
      });
      wrapper.append(input, counter);
      fields.append(wrapper);
    }
    makeField("Display name", "type-display-name", example || "Signal name", 30);
    const letters = value.toUpperCase().replace(/[^A-ZА-ЯЁ0-9]/g, "");
    makeField("Short label", "type-short-label", letters.slice(0, 2) || "DU", 3);
    const existing = document.createElement("div");
    existing.className = "signal-type-existing-fields";
    existing.hidden = true;
    const existingLabel = document.createElement("label");
    existingLabel.textContent = "Registered signal type";
    const target = document.createElement("select");
    target.className = "type-existing-target";
    for (const option of fixedSignalType.options) target.append(option.cloneNode(true));
    existingLabel.append(target);
    const warning = document.createElement("p");
    warning.className = "signal-type-existing-warning";
    warning.textContent = "Signals will be grouped under the selected existing type. The current dashboard does not support separating previously imported signals through these settings.";
    existing.append(existingLabel, warning);
    function updateVisibility() {
      const selected = fixed || include.checked;
      mode.hidden = !selected;
      fields.hidden = !selected || mode.value !== "new";
      existing.hidden = !selected || mode.value !== "existing";
    }
    mode.addEventListener("change", () => { updateVisibility(); invalidateValidation(); });
    if (include) include.addEventListener("change", () => { updateVisibility(); invalidateValidation(); });
    updateVisibility();
    target.addEventListener("change", invalidateValidation);
    row.append(header, fields, existing);
    (fixed ? fixedTypeRows : typeMappingRows).append(row);
  }

  async function discoverTypes() {
    discoveryColumn = signalTypeColumn.value;
    const generation = ++discoveryGeneration;
    discoveryReady = false;
    typeMappingRows.replaceChildren();
    invalidateValidation();
    if (!signalTypeColumn.value) return;
    setFeedback(mappingFeedback, "Discovering signal types…", "pending");
    const selected = signalTypeColumn.value;
    try {
      const response = await fetch("/settings/signal-sources/type-values", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ database_path: databasePath.value.trim(), table: table.value, column: selected }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Type discovery failed");
      if (generation !== discoveryGeneration) return;
      data.values.forEach((value) => addTypeMappingRow(value));
      discoveryReady = true;
      invalidateValidation();
      setFeedback(mappingFeedback, `${data.values.length} distinct type value(s) found.`, "");
    } catch (error) {
      if (generation === discoveryGeneration) setFeedback(mappingFeedback, error.message, "error");
    }
  }

  function columnLabel(column) {
    return column.type === "—" ? column.name : `${column.name} · ${column.type}`;
  }

  function populateSelect(select, placeholder) {
    const selected = select.value;
    select.replaceChildren(new Option(placeholder, ""));
    for (const column of loadedColumns) select.append(new Option(columnLabel(column), column.name));
    if (loadedColumns.some((column) => column.name === selected)) select.value = selected;
    select.disabled = !tableVerified;
  }

  function suggestSignalTypeColumn(columns) {
    const names = new Set(["signal_type", "alert_type", "event_type", "type"]);
    const matches = columns.filter((column) => names.has(column.name.toLowerCase()));
    return matches.length === 1 ? matches[0].name : "";
  }

  function populateMappings() {
    for (const select of mappingColumns) {
      populateSelect(select, select.required ? "Select a column" : "No mapping");
    }
    const suggestion = suggestSignalTypeColumn(loadedColumns);
    if (!signalTypeColumn.value && suggestion) {
      signalTypeColumn.value = suggestion;
      suggestionNote.hidden = false;
      discoverTypes();
    }
    updateSignalTypeMode();
    invalidateValidation();
  }

  function invalidateMappings() {
    discoveryGeneration++;
    discoveryReady = false;
    discoveryColumn = "";
    suggestionNote.hidden = true;
    loadedColumns = [];
    for (const select of mappingColumns) {
      select.replaceChildren(new Option(select.required ? "Select a column" : "No mapping", ""));
      select.disabled = true;
    }
    typeMappingRows.replaceChildren();
    fixedTypeRows.replaceChildren();
    addTypeMappingRow("Fixed signal type", true);
    updateSignalTypeMode();
    fixedSignalType.value = "";
    fixedSignalType.disabled = true;
    mappingValid = false;
    invalidatePreview();
    updateStepThreeNext();
    setFeedback(mappingFeedback, "", "");
  }

  function resetConnection() {
    inspectionGeneration++;
    inspectedTable = "";
    connectionVerified = false;
    tableVerified = false;
    table.replaceChildren(new Option("Check the connection first", ""));
    table.disabled = true;
    columns.replaceChildren();
    invalidateMappings();
    setFeedback(status, "", "");
    setFeedback(tableFeedback, "", "");
    updateStepOneNext();
    updateStepTwoNext();
  }

  async function inspect(selectedTable = "") {
    const response = await fetch("/settings/signal-sources/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ database_path: databasePath.value.trim(), table: selectedTable }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Connection check failed");
    return data;
  }

  function renderSchema(data, selectedTable = "") {
    table.replaceChildren(new Option("Select a table", ""));
    for (const name of data.tables) table.append(new Option(name, name, false, name === selectedTable));
    table.disabled = data.tables.length === 0;
    columns.replaceChildren(...data.columns.map((column) => {
      const item = document.createElement("li");
      item.textContent = columnLabel(column);
      return item;
    }));
    schema.hidden = false;
  }

  function mappingErrors() {
    const errors = [];
    const required = ["#mapping-event-id", "#mapping-symbol", "#mapping-timestamp"]
      .map((selector) => document.querySelector(selector));
    if (required.some((select) => !select.value)) {
      errors.push("Select Unique event ID, Symbol, and Event timestamp.");
    }
    const fixedType = document.querySelector("[name='signal-type-mode']:checked").value === "fixed";
    if (!fixedType && !signalTypeColumn.value) errors.push("Select a source column for Signal type.");
    const rows = fixedType ? [...fixedTypeRows.querySelectorAll(".signal-type-mapping-row")]
      : [...typeMappingRows.querySelectorAll(".signal-type-mapping-row")].filter((row) => row.querySelector(".type-import").checked);
    if (!rows.length) errors.push(fixedType ? "Configure a fixed signal type." : "Select at least one source type to import.");
    if (rows.some((row) => {
      const mode = row.querySelector(".type-mapping-mode").value;
      const name = row.querySelector(".type-display-name").value.trim();
      const label = row.querySelector(".type-short-label").value;
      return mode === "new" ? !name || name.length > 30 || !/^[A-ZА-ЯЁ0-9]{1,3}$/.test(label)
        : !row.querySelector(".type-existing-target").value;
    })) errors.push("Enter a display name (1–30) and short label (1–3 letters or digits), or select an existing type for every selected value.");
    const selected = [...mappingColumns]
      .filter((select) => !select.disabled && select.value)
      .map((select) => select.value);
    const duplicate = selected.find((name, index) => selected.indexOf(name) !== index);
    if (duplicate) errors.push(`${duplicate} is assigned to more than one signal field.`);
    return errors;
  }

  function previewPayload() {
    const mapping = (selector) => ({ kind: "column", column: document.querySelector(selector).value });
    const fixedType = document.querySelector("[name='signal-type-mode']:checked").value === "fixed";
    const rows = fixedType ? [...fixedTypeRows.querySelectorAll(".signal-type-mapping-row")]
      : [...typeMappingRows.querySelectorAll(".signal-type-mapping-row")].filter((row) => row.querySelector(".type-import").checked);
    const target = (row) => row.querySelector(".type-mapping-mode").value === "new"
      ? row.dataset.newId : row.querySelector(".type-existing-target").value;
    const mappings = {
      event_id: mapping("#mapping-event-id"),
      symbol: mapping("#mapping-symbol"),
      event_at: mapping("#mapping-timestamp"),
      signal_type: fixedType ? { kind: "fixed", value: target(rows[0]) } : mapping("#mapping-signal-type-column"),
    };
    if (!fixedType) mappings.signal_type.type_mapping = rows.map((row) => ({ source: row.dataset.source, target: target(row) }));
    const newTypes = rows.filter((row) => row.querySelector(".type-mapping-mode").value === "new")
      .map((row) => ({ id: row.dataset.newId, display_name: row.querySelector(".type-display-name").value.trim(), short_label: row.querySelector(".type-short-label").value }));
    return { source: { display_name: document.querySelector("#source-display-name").value.trim(), database_path: databasePath.value.trim(), table: table.value }, mappings, new_types: newTypes };
  }

  function renderPreview(data) {
    previewSummary.replaceChildren();
    for (const [name, value] of Object.entries({
      source: data.configuration.display_name,
      table: data.configuration.table,
    })) {
      const item = document.createElement("span");
      item.textContent = `${name}: ${value}`;
      previewSummary.append(item);
    }
    for (const [name, value] of Object.entries(data.configuration.mappings)) {
      const item = document.createElement("span");
      item.textContent = `${name}: ${value.kind === "fixed" ? (data.type_names[value.value] || value.value) : value.column}`;
      previewSummary.append(item);
    }
    function appendFields(list, values) {
      for (const [name, value] of values) {
        const item = document.createElement("div");
        const label = document.createElement("dt");
        const content = document.createElement("dd");
        label.textContent = name;
        content.textContent = value === null ? "NULL" : value === "" ? "(empty)" : String(value);
        item.append(label, content);
        list.append(item);
      }
    }
    previewEvents.replaceChildren(...data.events.map((event) => {
      const card = document.createElement("article");
      card.className = "source-preview-event";
      const fields = document.createElement("dl");
      fields.className = "source-preview-fields";
      appendFields(fields, [["Event ID", event.event_id], ["Symbol", event.symbol],
        ["Timestamp", event.event_at], ["Signal type", event.signal_type_display_name]]);
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Show details";
      details.addEventListener("toggle", () => { summary.textContent = details.open ? "Hide details" : "Show details"; });
      const metadata = document.createElement("dl");
      metadata.className = "source-preview-metadata";
      appendFields(metadata, Object.entries(event.metadata));
      details.append(summary, metadata);
      card.append(fields, details);
      return card;
    }));
    const rejectedList = previewRejected.querySelector("ul");
    rejectedList.replaceChildren(...data.rejected.map((item) => {
      const row = document.createElement("li");
      row.textContent = `Preview row ${item.row}: ${item.reason}`;
      return row;
    }));
    previewRejected.hidden = data.rejected.length === 0;
    previewResults.hidden = false;
  }

  async function loadPreview() {
    const generation = ++previewGeneration;
    connectSource.disabled = true;
    previewResults.hidden = true;
    setFeedback(previewFeedback, "Loading read-only preview…", "pending");
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 15_000);
    let response;
    let data;
    try {
      response = await fetch("/settings/signal-sources/preview", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(previewPayload()), signal: controller.signal,
      });
      data = await response.json();
    } catch (error) {
      if (timedOut) throw new Error("Read-only preview timed out. Check the source database and try again.");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
    if (generation !== previewGeneration) return;
    if (!response.ok) throw new Error(data.detail || "Preview failed");
    renderPreview(data);
    previewValid = true;
    connectSource.disabled = false;
    setFeedback(previewFeedback, `${data.previewed_count} example(s) shown · ${data.events.length} valid, ${data.rejected.length} invalid preview row(s).`, data.rejected.length ? "error" : "success");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) {
      setFeedback(status, "Enter a source name and SQLite database path.", "error");
      return;
    }
    if (connectionVerified) return;
    const generation = ++inspectionGeneration;
    connectionVerified = false;
    tableVerified = false;
    invalidateMappings();
    updateStepOneNext();
    updateStepTwoNext();
    setFeedback(status, "Checking read-only connection…", "pending");
    try {
      const data = await inspect();
      if (generation !== inspectionGeneration) return;
      renderSchema(data);
      connectionVerified = data.tables.length > 0;
      setFeedback(status, connectionVerified ? "Connection verified. Continue to select a table." : "Connection verified, but no user tables were found.", connectionVerified ? "success" : "error");
      updateStepOneNext();
    } catch (error) {
      if (generation === inspectionGeneration) setFeedback(status, error.message, "error");
    }
  });

  databasePath.addEventListener("input", resetConnection);
  document.querySelector("#source-display-name").addEventListener("input", () => {
    updateStepOneNext();
    invalidatePreview();
  });
  stepOneNext.addEventListener("click", () => {
    if (!connectionVerified || !form.reportValidity()) {
      setFeedback(status, "Check a valid SQLite database before continuing.", "error");
      return;
    }
    showStep(2);
    if (!table.value) setFeedback(tableFeedback, "Select a table to view its columns.", "");
  });
  stepTwoBack.addEventListener("click", () => showStep(1));
  stepTwoNext.addEventListener("click", () => {
    if (tableVerified) showStep(3);
  });
  stepThreeBack.addEventListener("click", () => showStep(2));
  stepThreeNext.addEventListener("click", async () => {
    if (!mappingValid) return;
    showStep(4);
    if (previewValid) return;
    const generation = previewGeneration + 1;
    try {
      await loadPreview();
    } catch (error) {
      if (generation === previewGeneration) {
        connectSource.disabled = false;
        setFeedback(
          previewFeedback,
          `Examples could not be displayed: ${error.message} You can still connect this valid source; importing remains disabled.`,
          "warning",
        );
      }
    }
  });
  stepFourBack.addEventListener("click", () => showStep(3));
  connectSource.addEventListener("click", async () => {
    connectSource.disabled = true;
    const startedAt = performance.now();
    const updateElapsed = () => setFeedback(
      previewFeedback,
      `Saving disabled source configuration… ${formatElapsed(performance.now() - startedAt)} elapsed.`,
      "pending",
    );
    updateElapsed();
    connectElapsedTimer = setInterval(updateElapsed, 1_000);
    try {
      const response = await fetch("/settings/signal-sources/connect", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(previewPayload()),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not connect source");
      setFeedback(previewFeedback, `${data.message} Source ID: ${data.source_id}. Completed in ${formatElapsed(performance.now() - startedAt)}.`, "success");
      window.location.assign("/settings/signal-sources");
    } catch (error) {
      connectSource.disabled = false;
      setFeedback(previewFeedback, error.message, "error");
    } finally {
      stopConnectElapsed();
    }
  });

  table.addEventListener("change", async () => {
    if (table.value === inspectedTable) return;
    inspectedTable = table.value;
    const generation = ++inspectionGeneration;
    columns.replaceChildren();
    tableVerified = false;
    invalidateMappings();
    updateStepTwoNext();
    if (!table.value) {
      setFeedback(tableFeedback, "Select a table to view its columns.", "error");
      return;
    }
    setFeedback(tableFeedback, "Loading columns…", "pending");
    try {
      const selectedTable = table.value;
      const data = await inspect(selectedTable);
      if (generation !== inspectionGeneration) return;
      renderSchema(data, selectedTable);
      loadedColumns = data.columns;
      tableVerified = loadedColumns.length > 0;
      populateMappings();
      setFeedback(tableFeedback, tableVerified ? "Columns loaded." : "The selected table has no columns.", tableVerified ? "success" : "error");
      updateStepTwoNext();
    } catch (error) {
      if (generation !== inspectionGeneration) return;
      table.value = "";
      inspectedTable = "";
      setFeedback(tableFeedback, error.message, "error");
    }
  });

  signalTypeColumn.addEventListener("change", () => {
    suggestionNote.hidden = true;
    if (signalTypeColumn.value !== discoveryColumn) discoverTypes();
  });
  addTypeMappingRow("Fixed signal type", true);
  updateSignalTypeMode();
  signalTypeModes.forEach((input) => input.addEventListener("change", () => {
    updateSignalTypeMode();
    invalidateValidation();
  }));
  [...mappingColumns, fixedSignalType].forEach((element) => {
    element.addEventListener("change", invalidateValidation);
  });
})();
