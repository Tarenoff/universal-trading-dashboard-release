(() => {
  const dialog = document.querySelector("#signal-type-dialog");
  const form = document.querySelector("#signal-type-form");
  const close = document.querySelector("#signal-type-close");
  const status = document.querySelector("#signal-type-status");
  const save = form.querySelector('button[type="submit"]');
  const nameInput = form.elements.namedItem("display_name");
  const labelInput = form.elements.namedItem("short_label");
  const colorInput = form.elements.namedItem("color");
  const markerShapeInput = form.elements.namedItem("marker_shape");
  const markerPositionInput = form.elements.namedItem("marker_position");
  const markerShapePicker = document.querySelector("#signal-type-shape-picker");
  const markerPreview = document.querySelector("#signal-type-marker-preview");
  const dialogHeader = document.querySelector("#signal-type-dialog-header");
  const nameCount = document.querySelector("#signal-type-name-count");
  const labelCount = document.querySelector("#signal-type-label-count");
  const appearanceTab = document.querySelector("#signal-type-tab-appearance");
  const cardsTab = document.querySelector("#signal-type-tab-cards");
  const appearancePanel = document.querySelector("#signal-type-panel-appearance");
  const cardsPanel = document.querySelector("#signal-type-panel-cards");
  const cardStatus = document.querySelector("#signal-card-status");
  const cardEditor = document.querySelector("#signal-card-editor");
  const compactFields = document.querySelector("#signal-card-compact-fields");
  const detailChoice = document.querySelector("#signal-card-detail-choice");
  const detailAdd = document.querySelector("#signal-card-detail-add");
  const detailFields = document.querySelector("#signal-card-detail-fields");
  const cardPreview = document.querySelector("#signal-card-preview");
  const cardSave = document.querySelector("#signal-card-save");
  const cardsAvailable = Boolean(
    appearanceTab && cardsTab && appearancePanel && cardsPanel && cardStatus
    && cardEditor && compactFields && detailChoice && detailAdd && detailFields
    && cardPreview && cardSave,
  );
  let busy = false;
  let appearanceSaved = false;
  let cardBusy = false;
  let editedButton = null;
  let drag = null;

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
  const cards = {
    sourceId: "", fields: [], samples: {}, previewEvent: null, compact: [], detail: [],
  };

  function updateCounts() {
    nameCount.textContent = `${nameInput.value.length} / 30`;
    labelCount.textContent = `${labelInput.value.length} / 3`;
  }

  function updateMarkerPreview() {
    updateShapePicker();
    markerPreview.replaceChildren();
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const y = { aboveBar: 8, inBar: 28, belowBar: 47 }[markerPositionInput.value] ?? 28;
    const x = 48;
    marker.setAttribute("fill", colorInput.value);
    if (markerShapeInput.value === "circle") {
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", String(x));
      circle.setAttribute("cy", String(y));
      circle.setAttribute("r", "5");
      circle.setAttribute("fill", colorInput.value);
      markerPreview.append(circle);
      return;
    }
    if (markerShapeInput.value === "square") {
      const square = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      square.setAttribute("x", String(x - 5));
      square.setAttribute("y", String(y - 5));
      square.setAttribute("width", "10");
      square.setAttribute("height", "10");
      square.setAttribute("fill", colorInput.value);
      markerPreview.append(square);
      return;
    }
    const customHalfSize = 5;
    if (markerShapeInput.value === "star") {
      const points = Array.from({ length: 10 }, (_, index) => {
        const radius = index % 2 === 0 ? customHalfSize : customHalfSize * 0.45;
        const angle = -Math.PI / 2 + index * Math.PI / 5;
        return `${x + Math.cos(angle) * radius} ${y + Math.sin(angle) * radius}`;
      });
      marker.setAttribute("d", `M${points.join(" L")}z`);
      markerPreview.append(marker);
      return;
    }
    if (markerShapeInput.value === "target") {
      const ring = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      ring.setAttribute("cx", String(x));
      ring.setAttribute("cy", String(y));
      ring.setAttribute("r", String(customHalfSize - 1));
      ring.setAttribute("fill", "none");
      ring.setAttribute("stroke", colorInput.value);
      ring.setAttribute("stroke-width", "1.5");
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("cx", String(x));
      dot.setAttribute("cy", String(y));
      dot.setAttribute("r", "1.6");
      dot.setAttribute("fill", colorInput.value);
      markerPreview.append(ring, dot);
      return;
    }
    if (markerShapeInput.value === "flag") {
      const pole = document.createElementNS("http://www.w3.org/2000/svg", "path");
      pole.setAttribute("d", `M${x - 3} ${y + 5}V${y - 5}`);
      pole.setAttribute("fill", "none");
      pole.setAttribute("stroke", colorInput.value);
      pole.setAttribute("stroke-width", "1.5");
      marker.setAttribute(
        "d",
        `M${x - 3} ${y - 5} ${x + 5} ${y - 3.5} ${x + 2} ${y} ${x - 3} ${y + 0.4}z`,
      );
      markerPreview.append(pole, marker);
      return;
    }
    if (markerShapeInput.value === "diamond") {
      marker.setAttribute(
        "d",
        `M${x} ${y - customHalfSize} ${x + customHalfSize} ${y} ${x} ${y + customHalfSize} ${x - customHalfSize} ${y}z`,
      );
      markerPreview.append(marker);
      return;
    }
    if (markerShapeInput.value === "triangleUp") {
      marker.setAttribute(
        "d",
        `M${x} ${y - customHalfSize} ${x + customHalfSize} ${y + customHalfSize} ${x - customHalfSize} ${y + customHalfSize}z`,
      );
      markerPreview.append(marker);
      return;
    }
    if (markerShapeInput.value === "triangleDown") {
      marker.setAttribute(
        "d",
        `M${x} ${y + customHalfSize} ${x + customHalfSize} ${y - customHalfSize} ${x - customHalfSize} ${y - customHalfSize}z`,
      );
      markerPreview.append(marker);
      return;
    }
    const isUp = markerShapeInput.value === "arrowUp";
    marker.setAttribute(
      "d",
      isUp
        ? `M${x} ${y - 7} ${x - 7} ${y + 1}h4v6h6v-6h4z`
        : `M${x - 3} ${y - 7}h6v6h4l-7 7-7-7h4z`,
    );
    markerPreview.append(marker);
  }

  function updateShapePicker() {
    if (!markerShapePicker) return;
    markerShapePicker.style.setProperty("--marker-shape-color", colorInput.value);
    for (const option of markerShapePicker.querySelectorAll("button")) {
      const selected = option.dataset.markerShape === markerShapeInput.value;
      option.classList.toggle("is-selected", selected);
      option.setAttribute("aria-checked", String(selected));
    }
  }

  function setTab(name) {
    if (!cardsAvailable) return;
    const cardsActive = name === "cards";
    appearanceTab.setAttribute("aria-selected", String(!cardsActive));
    cardsTab.setAttribute("aria-selected", String(cardsActive));
    appearancePanel.hidden = cardsActive;
    cardsPanel.hidden = !cardsActive;
    appearancePanel.setAttribute("aria-hidden", String(cardsActive));
    cardsPanel.setAttribute("aria-hidden", String(!cardsActive));
    dialog.classList.toggle("is-cards-tab", cardsActive);
    clampOpenDialogPosition();
    if (cardsActive && !cardEditor.hidden) renderCards();
    if (cardsActive && cardEditor.hidden) loadCardLayout();
  }

  function fieldLabel(descriptor) {
    const [namespace, value] = descriptor.split(/:(.*)/s);
    const words = (value || "").replaceAll("_", " ");
    if (namespace === "standard") return words.replace(/\b\w/g, (letter) => letter.toUpperCase());
    return words || descriptor;
  }

  function createOption(value, label, selected = false) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = selected;
    return option;
  }

  function optionLabel(field) {
    const sample = cards.samples[field];
    return sample === undefined ? fieldLabel(field) : `${fieldLabel(field)} — ${sample}`;
  }

  function selectedFields() {
    return new Set([...cards.compact.filter(Boolean), ...cards.detail]);
  }

  function normalizeCompact() {
    const used = new Set();
    cards.compact = cards.compact.slice(0, 3).map((field) => {
      if (!field || !cards.fields.includes(field) || used.has(field)) return "";
      used.add(field);
      return field;
    });
    while (cards.compact.length < 3) cards.compact.push("");
    cards.detail = cards.detail.filter((field) => cards.fields.includes(field) && !used.has(field));
  }

  function previewValue(value) {
    if (value === undefined || value === null) return "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function previewTimestamp(value) {
    if (value === undefined || value === null) return "—";
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) return "—";
    return new Intl.DateTimeFormat("en-GB", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false, timeZone: "UTC", timeZoneName: "short",
    }).format(timestamp);
  }

  function previewRows(fields, values) {
    const list = document.createElement("dl");
    for (const field of fields) {
      const label = document.createElement("dt");
      const labelText = fieldLabel(field);
      label.textContent = labelText;
      label.title = labelText;
      const value = document.createElement("dd");
      const valueText = previewValue(values[field]);
      value.textContent = valueText;
      value.title = valueText;
      list.append(label, value);
    }
    return list;
  }

  function renderPreview(target, compact, detail, technical) {
    target.replaceChildren();
    if (!cards.previewEvent) {
      const empty = document.createElement("p");
      empty.textContent = "No representative event is available for this source and signal type.";
      target.append(empty);
      return;
    }
    const values = cards.previewEvent.fields || {};
    const header = document.createElement("header");
    const type = document.createElement("span");
    type.textContent = form.dataset.displayName || "Signal type";
    const time = document.createElement("span");
    time.textContent = `Date/time ${previewTimestamp(cards.previewEvent.event_at)}`;
    header.append(type, time);
    target.append(header);
    const selectedCompact = compact.filter(Boolean);
    if (selectedCompact.length) {
      const compactGroup = document.createElement("section");
      compactGroup.className = "signal-card-preview-compact";
      const compactTitle = document.createElement("h4");
      compactTitle.textContent = "Compact fields";
      compactGroup.append(compactTitle, previewRows(selectedCompact, values));
      target.append(compactGroup);
    }
    if (detail.length) {
      const detailGroup = document.createElement("section");
      detailGroup.className = "signal-card-preview-additional";
      const detailTitle = document.createElement("h4");
      detailTitle.textContent = "Additional detailed fields";
      detailGroup.append(detailTitle, previewRows(detail, values));
      target.append(detailGroup);
    }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Technical details";
    details.append(summary);
    if (technical.length) details.append(previewRows(technical, values));
    else {
      const empty = document.createElement("p");
      empty.textContent = "No remaining observed fields.";
      details.append(empty);
    }
    target.append(details);
  }

  function renderCards() {
    normalizeCompact();
    compactFields.replaceChildren();
    for (let index = 0; index < 3; index += 1) {
      const label = document.createElement("label");
      const title = document.createElement("span");
      title.textContent = `Field ${index + 1}`;
      const select = document.createElement("select");
      select.setAttribute("aria-label", `Compact card field ${index + 1}`);
      select.append(createOption("", "Not selected", !cards.compact[index]));
      for (const field of cards.fields) {
        const usedElsewhere = cards.compact.some((value, other) => other !== index && value === field);
        const option = createOption(field, optionLabel(field), cards.compact[index] === field);
        option.disabled = usedElsewhere;
        select.append(option);
      }
      select.disabled = cards.fields.length === 0;
      select.addEventListener("change", () => {
        cards.compact[index] = select.value;
        renderCards();
      });
      label.append(title, select);
      compactFields.append(label);
    }
    const used = selectedFields();
    detailChoice.replaceChildren(createOption("", "Select a field"));
    for (const field of cards.fields) {
      if (!used.has(field)) detailChoice.append(createOption(field, optionLabel(field)));
    }
    detailChoice.disabled = detailChoice.options.length === 1;
    detailAdd.disabled = detailChoice.disabled;
    detailFields.replaceChildren();
    cards.detail.forEach((field, index) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = fieldLabel(field);
      const up = document.createElement("button");
      up.type = "button";
      up.textContent = "↑";
      up.disabled = index === 0;
      up.addEventListener("click", () => {
        [cards.detail[index - 1], cards.detail[index]] = [cards.detail[index], cards.detail[index - 1]];
        renderCards();
      });
      const down = document.createElement("button");
      down.type = "button";
      down.textContent = "↓";
      down.disabled = index === cards.detail.length - 1;
      down.addEventListener("click", () => {
        [cards.detail[index], cards.detail[index + 1]] = [cards.detail[index + 1], cards.detail[index]];
        renderCards();
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        cards.detail.splice(index, 1);
        renderCards();
      });
      item.append(name, up, down, remove);
      detailFields.append(item);
    });
    const technical = cards.fields.filter((field) => !selectedFields().has(field));
    renderPreview(cardPreview, cards.compact, cards.detail, technical);
    cardSave.disabled = cardBusy;
  }

  async function loadCardLayout() {
    if (cardBusy) return;
    cardBusy = true;
    cardEditor.hidden = true;
    cardStatus.classList.remove("is-unconfigured");
    cardStatus.textContent = "Loading card fields…";
    try {
      const context = await fetch(`/settings/signal-types/card-layout-context?type_id=${encodeURIComponent(form.dataset.typeId)}`);
      const contextData = await context.json();
      if (!context.ok) throw new Error(contextData.detail || "Could not resolve card layout source");
      cards.sourceId = contextData.source_id;
      const query = new URLSearchParams({source_id: cards.sourceId, type_id: form.dataset.typeId});
      const response = await fetch(`/settings/signal-types/card-layout?${query}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not load card layout");
      const layoutFields = data.layout ? [...data.layout.compact_fields, ...data.layout.detail_fields] : [];
      cards.fields = [...new Set([...data.available_fields, ...layoutFields])];
      cards.samples = data.field_samples || {};
      cards.previewEvent = data.representative_event || null;
      cards.compact = data.layout ? [...data.layout.compact_fields] : [];
      cards.detail = data.layout ? [...data.layout.detail_fields] : [];
      cardEditor.hidden = false;
      cardStatus.classList.toggle("is-unconfigured", !data.configured);
      cardStatus.textContent = data.field_discovery_available
        ? (data.configured ? "Saved layout loaded." : "No layout saved yet. Choose three compact fields.")
        : "EventStore field discovery is unavailable; saved fields remain editable.";
      renderCards();
    } catch (error) {
      cardStatus.classList.remove("is-unconfigured");
      cardStatus.textContent = error.message;
    } finally {
      cardBusy = false;
      if (!cardEditor.hidden) renderCards();
    }
  }

  async function saveCardLayout() {
    if (cardBusy) return;
    cardBusy = true;
    cardSave.disabled = true;
    cardStatus.textContent = "Saving card layout…";
    try {
      const response = await fetch("/settings/signal-types/card-layout", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_id: cards.sourceId,
          type_id: form.dataset.typeId,
        layout: {
          compact_fields: cards.compact.filter(Boolean), detail_fields: cards.detail,
        },
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not save card layout");
      cards.fields = [...new Set([...data.available_fields, ...cards.compact, ...cards.detail])];
      cards.samples = data.field_samples || cards.samples;
      cards.previewEvent = data.representative_event || cards.previewEvent;
      cardStatus.classList.remove("is-unconfigured");
      cardStatus.textContent = "Card layout saved.";
    } catch (error) {
      cardStatus.textContent = error.message;
    } finally {
      cardBusy = false;
      renderCards();
    }
  }

  nameInput.addEventListener("beforeinput", (event) => {
    if (event.data && nameInput.value.length - (nameInput.selectionEnd - nameInput.selectionStart) + event.data.length > 30) {
      status.textContent = "Display name must be at most 30 characters.";
    }
  });
  nameInput.addEventListener("input", () => {
    nameInput.maxLength = 30;
    if (nameInput.value.length > 30) nameInput.value = nameInput.value.slice(0, 30);
    updateCounts();
  });
  labelInput.addEventListener("beforeinput", (event) => {
    if (event.data && labelInput.value.length - (labelInput.selectionEnd - labelInput.selectionStart) + event.data.length > 3) {
      status.textContent = "Short label must be at most 3 characters.";
    }
  });
  labelInput.addEventListener("input", () => {
    labelInput.maxLength = 3;
    const clean = labelInput.value.replace(/[^A-Za-zА-Яа-яЁё0-9]/g, "").toUpperCase().slice(0, 3);
    if (clean !== labelInput.value.toUpperCase()) status.textContent = "Short label allows only Latin or Cyrillic letters and digits (maximum 3).";
    labelInput.value = clean;
    updateCounts();
  });
  colorInput.addEventListener("input", updateMarkerPreview);
  markerPositionInput.addEventListener("input", updateMarkerPreview);
  markerPositionInput.addEventListener("change", updateMarkerPreview);
  for (const option of markerShapePicker?.querySelectorAll("button") ?? []) {
    option.addEventListener("click", () => {
      markerShapeInput.value = option.dataset.markerShape;
      updateMarkerPreview();
    });
  }
  if (cardsAvailable) {
    appearanceTab.addEventListener("click", () => setTab("appearance"));
    cardsTab.addEventListener("click", () => setTab("cards"));
    detailAdd.addEventListener("click", () => {
      if (!detailChoice.value) return;
      cards.detail.push(detailChoice.value);
      renderCards();
    });
    cardSave.addEventListener("click", saveCardLayout);
  }

  for (const button of document.querySelectorAll(".signal-type-edit")) {
    button.addEventListener("click", () => {
      appearanceSaved = false;
      editedButton = button;
      form.dataset.typeId = button.dataset.typeId;
      form.dataset.displayName = button.dataset.displayName;
      for (const [name, value] of Object.entries({
        display_name: button.dataset.displayName, short_label: button.dataset.shortLabel,
        color: button.dataset.color, marker_shape: button.dataset.markerShape,
        marker_position: button.dataset.markerPosition,
      })) form.elements.namedItem(name).value = value;
      if (cardsAvailable) {
        cards.sourceId = "";
        cards.fields = [];
        cards.samples = {};
        cards.previewEvent = null;
        cards.compact = [];
        cards.detail = [];
        cardEditor.hidden = true;
        cardStatus.textContent = "";
      }
      nameInput.maxLength = Math.max(30, nameInput.value.length);
      labelInput.maxLength = Math.max(3, labelInput.value.length);
      updateCounts();
      updateMarkerPreview();
      status.textContent = "";
      save.hidden = false;
      setTab("appearance");
      resetDialogPosition();
      dialog.showModal();
    });
  }
  dialog.addEventListener("cancel", (event) => { if (busy || cardBusy) event.preventDefault(); });
  dialog.addEventListener("close", () => { resetDialogPosition(); if (appearanceSaved) window.location.reload(); });
  if (close) close.addEventListener("click", () => { if (!busy && !cardBusy) dialog.close(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || appearanceSaved || appearancePanel.hidden) return;
    busy = true;
    save.disabled = true;
    if (close) close.disabled = true;
    const display = Object.fromEntries(
      ["display_name", "short_label", "color", "marker_shape", "marker_position"]
        .map((name) => [name, form.elements.namedItem(name).value]),
    );
    try {
      const response = await fetch("/settings/signal-types/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({type_id: form.dataset.typeId, display}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not save settings");
      status.textContent = data.message;
      appearanceSaved = true;
      Object.assign(editedButton.dataset, {
        displayName: nameInput.value,
        shortLabel: labelInput.value,
        color: colorInput.value,
        markerShape: markerShapeInput.value,
        markerPosition: markerPositionInput.value,
      });
      save.hidden = true;
    } catch (error) {
      status.textContent = error.message;
    } finally {
      busy = false;
      save.disabled = false;
      if (close) close.disabled = false;
    }
  });
})();
