const search = document.querySelector("#symbol-search");
const filterContainer = document.querySelector(".filters");
const symbolList = document.querySelector("#symbol-list");
const emptyResults = document.querySelector("#empty-results");
const main = document.querySelector("main");
const marketSummary = document.querySelector(".market-summary");
const sidebarScrollStorageKey = "signal-dashboard-sidebar-scroll-top";
const displayTimezoneStorageKey = "signal-dashboard-display-timezone";
const refreshIntervalMilliseconds = 30_000;
const displayTimezoneChoices = Object.freeze([
  { id: "browser", label: "Browser Local" },
  { id: "UTC", label: "UTC" },
  { id: "Europe/Moscow", label: "Moscow" },
  { id: "Europe/Berlin", label: "Berlin" },
  { id: "Europe/London", label: "London" },
  { id: "America/New_York", label: "New York" },
  { id: "Asia/Dubai", label: "Dubai" },
  { id: "Asia/Tokyo", label: "Tokyo" },
]);

function browserDisplayTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function savedDisplayTimezone() {
  const saved = localStorage.getItem(displayTimezoneStorageKey);
  return displayTimezoneChoices.some(({ id }) => id === saved) ? saved : "browser";
}

let selectedDisplayTimezone = savedDisplayTimezone();

function displayTimezone() {
  return selectedDisplayTimezone === "browser"
    ? browserDisplayTimezone()
    : selectedDisplayTimezone;
}

function dateParts(date, timezone, options) {
  return Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    hourCycle: "h23",
    ...options,
  }).formatToParts(date).map((part) => [part.type, part.value]));
}

function displayTimestamp(value, { seconds = false } = {}) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const parts = dateParts(date, displayTimezone(), {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}),
  });
  return `${parts.day}.${parts.month}.${parts.year} ${parts.hour}:${parts.minute}${seconds ? `:${parts.second}` : ""}`;
}

function displaySidebarTimestamp(value, now = new Date()) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const timezone = displayTimezone();
  const eventParts = dateParts(date, timezone, {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
  const nowParts = dateParts(now, timezone, { day: "2-digit", month: "2-digit", year: "numeric" });
  const time = `${eventParts.hour}:${eventParts.minute}`;
  return eventParts.day === nowParts.day
    && eventParts.month === nowParts.month
    && eventParts.year === nowParts.year
    ? `Today ${time}`
    : `${eventParts.day}.${eventParts.month} ${time}`;
}

function displayTimezoneOffset(timezone, date = new Date()) {
  const offset = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone, timeZoneName: "longOffset",
  }).formatToParts(date).find((part) => part.type === "timeZoneName")?.value;
  if (!offset || offset === "GMT") return "UTC";
  return offset.replace("GMT", "UTC").replace(/([+-])0(\d)(?::00)?$/, "$1$2");
}

function displayTimezoneChoiceLabel(choice, date = new Date()) {
  const timezone = choice.id === "browser" ? browserDisplayTimezone() : choice.id;
  return `${choice.label} (${displayTimezoneOffset(timezone, date)})`;
}

function refreshSidebarTimestamps() {
  for (const time of symbolList.querySelectorAll("time[datetime]")) {
    time.textContent = displaySidebarTimestamp(time.dateTime);
  }
}

window.addEventListener("storage", (event) => {
  if (event.key === "signal-dashboard-source-removal") window.location.reload();
  if (event.key === displayTimezoneStorageKey) {
    selectedDisplayTimezone = savedDisplayTimezone();
    refreshSidebarTimestamps();
  }
});
const bybitLinearWebSocketUrl = "wss://stream.bybit.com/v5/public/linear";
const signalTypes = Object.values(window.SignalTypeRegistry);
const signalTypesByEventType = new Map(
  signalTypes.map((signalType) => [signalType.event_type, signalType]),
);
const signalTypeRegistryOrder = new Map(
  signalTypes.map((signalType, index) => [signalType.event_type, index]),
);
let availableEventTypes = new Set(JSON.parse(filterContainer.dataset.availableTypes));
const eventCardFieldDefinitions = Object.freeze({
  price: { label: "Price", order: 10, format: (value) => value },
  alert_level: { label: "Signal level", order: 20, format: (value) => String(value).toUpperCase() },
  source_interval: { label: "Timeframe", order: 30, format: (value) => /^\d+$/.test(String(value)) ? `${value}m` : value },
  direction: { label: "Direction", order: 40, format: (value) => String(value).toUpperCase() },
});

function signalTypeFor(eventType) {
  return signalTypesByEventType.get(eventType) ?? signalTypes.find(
    ({ short_label }) => short_label === eventType,
  );
}

function defaultCollapsedEventCardFields(eventType) {
  return eventType === "pump_pullback"
    ? ["alert_level", "direction"]
    : ["alert_level", "source_interval"];
}

function selectedEventTypes() {
  const selectedKeys = new Set(
    (filterContainer.dataset.activeFilter ?? "all").split(","),
  );
  return new Set(
    signalTypes.filter(({ filter_key }) => selectedKeys.has(filter_key)).map(
      ({ event_type }) => event_type,
    ),
  );
}

function filterValueFor(eventTypes) {
  const filterKeys = signalTypes.filter(({ event_type }) => eventTypes.has(event_type))
    .map(({ filter_key }) => filter_key);
  return filterKeys.length ? filterKeys.join(",") : "all";
}

let signalFilterTooltip = null;

function hideSignalFilterTooltip() {
  if (signalFilterTooltip) signalFilterTooltip.hidden = true;
}

window.addEventListener("scroll", hideSignalFilterTooltip, true);
window.addEventListener("resize", hideSignalFilterTooltip);

function attachSignalFilterTooltip(button, displayName) {
  button.addEventListener("pointerenter", (event) => {
    if (event.pointerType !== "mouse") return;
    if (!signalFilterTooltip) {
      signalFilterTooltip = document.createElement("div");
      signalFilterTooltip.className = "signal-filter-tooltip";
      signalFilterTooltip.setAttribute("role", "tooltip");
      document.body.append(signalFilterTooltip);
    }
    signalFilterTooltip.textContent = displayName;
    signalFilterTooltip.hidden = false;
    const anchor = button.getBoundingClientRect();
    const tooltip = signalFilterTooltip.getBoundingClientRect();
    signalFilterTooltip.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - tooltip.width - 8))}px`;
    const below = anchor.bottom + 5;
    signalFilterTooltip.style.top = `${below + tooltip.height <= window.innerHeight - 8
      ? below : Math.max(8, anchor.top - tooltip.height - 5)}px`;
  });
  button.addEventListener("pointerleave", hideSignalFilterTooltip);
  button.addEventListener("click", hideSignalFilterTooltip);
}

function createFilterButton({ filter_key, event_type, short_label, display_name }, eventTypes) {
  const filter = document.createElement("button");
  filter.className = `filter${eventTypes.has(event_type) ? " is-active" : ""}`;
  filter.type = "button";
  filter.dataset.filter = filter_key;
  filter.dataset.eventType = event_type;
  filter.textContent = short_label;
  attachSignalFilterTooltip(filter, display_name);
  return filter;
}

function renderFilterButtons() {
  hideSignalFilterTooltip();
  const eventTypes = selectedEventTypes();
  const allFilter = document.createElement("button");
  allFilter.className = `filter${eventTypes.size === 0 ? " is-active" : ""}`;
  allFilter.type = "button";
  allFilter.dataset.filter = "all";
  allFilter.textContent = "ALL";

  const fragment = document.createDocumentFragment();
  fragment.append(allFilter);
  const typeFilters = document.createElement("div");
  typeFilters.className = "signal-type-filters";
  for (const signalType of signalTypes) {
    if (!availableEventTypes.has(signalType.event_type)) continue;
    typeFilters.append(createFilterButton(signalType, eventTypes));
  }
  fragment.append(typeFilters);
  filterContainer.replaceChildren(fragment);
}

renderFilterButtons();

function updateAvailableFilters(data) {
  availableEventTypes = new Set(data.available_event_types);
  const changed = filterContainer.dataset.activeFilter !== data.active_filter;
  filterContainer.dataset.activeFilter = data.active_filter;
  renderFilterButtons();
  if (changed) {
    const url = new URL(window.location.href);
    url.searchParams.set("filter", data.active_filter);
    history.replaceState(history.state, "", url);
  }
}

function countedSignalTypes(eventTypeCounts, eventTypeLatestAt = {}) {
  return signalTypes.flatMap((signalType) => {
    const count = Number(eventTypeCounts[signalType.event_type]);
    return Number.isFinite(count) && count > 0 ? [{
      signalType,
      count,
      latestEventAt: eventTypeLatestAt[signalType.event_type],
    }] : [];
  }).sort((left, right) => {
    const latestDifference = Date.parse(right.latestEventAt) - Date.parse(left.latestEventAt);
    return Number.isFinite(latestDifference) && latestDifference !== 0
      ? latestDifference
      : signalTypeRegistryOrder.get(left.signalType.event_type)
        - signalTypeRegistryOrder.get(right.signalType.event_type);
  });
}

function signalCount(label, count, color) {
  const counter = document.createElement("small");
  counter.className = "signal-count";
  counter.style.color = color;
  counter.textContent = `${label} ${count}`;
  return counter;
}

function renderSignalCounters(container, eventTypeCounts, eventTypeLatestAt = {}) {
  const entries = countedSignalTypes(eventTypeCounts, eventTypeLatestAt);
  const fragment = document.createDocumentFragment();
  if (entries.length <= 2) {
    for (const { signalType, count } of entries) {
      fragment.append(signalCount(signalType.short_label, count, signalType.color));
    }
    container.replaceChildren(fragment);
    return;
  }

  const summary = document.createElement("span");
  summary.className = "signal-count-summary";
  summary.tabIndex = 0;
  summary.setAttribute("role", "button");
  summary.setAttribute("aria-expanded", "false");
  summary.textContent = `${entries.length} types`;
  const breakdown = document.createElement("span");
  breakdown.className = "signal-count-breakdown";
  for (const { signalType, count } of entries) {
    breakdown.append(signalCount(signalType.short_label, count, signalType.color));
  }
  summary.append(breakdown);
  const toggleBreakdown = (event) => {
    event.preventDefault();
    event.stopPropagation();
    const expanded = summary.classList.toggle("is-expanded");
    summary.setAttribute("aria-expanded", String(expanded));
  };
  summary.addEventListener("click", toggleBreakdown);
  summary.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") toggleBreakdown(event);
  });
  fragment.append(summary);
  container.replaceChildren(fragment);
}

function hydrateSignalCounters() {
  for (const container of document.querySelectorAll(".signal-counters")) {
    renderSignalCounters(
      container,
      JSON.parse(container.dataset.eventTypeCounts),
      JSON.parse(container.dataset.eventTypeLatestAt),
    );
  }
}

hydrateSignalCounters();
refreshSidebarTimestamps();

function symbolItems() {
  return symbolList.querySelectorAll(".symbol");
}

function restoreSidebarScrollPosition() {
  const savedScrollTop = Number.parseInt(
    sessionStorage.getItem(sidebarScrollStorageKey) ?? "",
    10,
  );
  if (Number.isNaN(savedScrollTop) || !symbolList) return;

  symbolList.scrollTop = savedScrollTop;
  symbolList.querySelector(".is-selected")?.scrollIntoView({ block: "nearest" });
}

function applyFilters() {
  const query = search.value.trim().toLowerCase();
  let visible = 0;
  for (const symbol of symbolItems()) {
    const matchesSearch = symbol.dataset.baseSymbol.toLowerCase().includes(query);
    const show = matchesSearch;
    symbol.hidden = !show;
    visible += Number(show);
  }
  emptyResults.hidden = visible !== 0;
}

search.addEventListener("input", applyFilters);
restoreSidebarScrollPosition();

symbolList.addEventListener("click", (event) => {
  if (event.target.closest(".symbol")) {
    sessionStorage.setItem(sidebarScrollStorageKey, String(symbolList.scrollTop));
  }
});

filterContainer.addEventListener("click", (event) => {
    const filter = event.target.closest(".filter");
    if (!filter || !filterContainer.contains(filter)) return;
    const eventTypes = selectedEventTypes();
    if (filter.dataset.filter === "all") {
      eventTypes.clear();
    } else if (eventTypes.has(filter.dataset.eventType)) {
      eventTypes.delete(filter.dataset.eventType);
    } else {
      eventTypes.add(filter.dataset.eventType);
    }
    const url = new URL(window.location.href);
    url.searchParams.set("filter", filterValueFor(eventTypes));
    url.searchParams.delete("symbol");
    window.location.assign(url);
});

function activeFilter() {
  return filterValueFor(selectedEventTypes());
}

function refreshSidebar(items, selectedSymbol, scrollTop) {
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const symbol = document.createElement("a");
    symbol.className = `symbol${item.symbol === selectedSymbol ? " is-selected" : ""}`;
    symbol.href = `/?symbol=${encodeURIComponent(item.symbol)}&filter=${activeFilter()}`;
    symbol.dataset.symbol = item.symbol;
    symbol.dataset.baseSymbol = item.symbol.replace(/USDT$/, "");
    const name = document.createElement("span");
    name.className = "symbol-name";
    name.textContent = item.symbol.replace(/USDT$/, "");
    const counters = document.createElement("span");
    counters.className = "signal-counters";
    renderSignalCounters(
      counters,
      item.event_type_counts,
      item.event_type_latest_at,
    );
    const time = document.createElement("time");
    time.dateTime = item.latest_event.event_at;
    time.textContent = displaySidebarTimestamp(item.latest_event.event_at);
    symbol.append(name, counters, time);
    fragment.append(symbol);
  }
  symbolList.replaceChildren(fragment);
  applyFilters();
  symbolList.scrollTop = scrollTop;
}

const marketSummaryAdapterByChartSource = Object.freeze({
  local_sqlite: "bybit_linear",
  bybit_api: "bybit_linear",
  binance_usdm_api: "binance_usdm",
});
let activeMarketSummaryAdapter = null;
let activeMarketSummarySymbol = null;
let stopActiveMarketSummary = null;

function publishCurrentMarketPrice(symbol, price) {
  const currentPrice = Number(price);
  window.dispatchEvent(new CustomEvent("dashboard-current-market-price", {
    detail: {
      symbol,
      price: Number.isFinite(currentPrice) ? currentPrice : null,
    },
  }));
}

function summaryAdapterForChartSource(chartSource) {
  return marketSummaryAdapterByChartSource[chartSource] ?? null;
}

function startMarketSummaryForChartSource(symbol, chartSource) {
  const adapter = summaryAdapterForChartSource(chartSource);
  if (adapter === null) {
    if (activeMarketSummaryAdapter === null && symbol === activeMarketSummarySymbol) {
      return;
    }
    stopActiveMarketSummary?.();
    stopActiveMarketSummary = null;
    activeMarketSummaryAdapter = null;
    activeMarketSummarySymbol = symbol;
    clearMarketSummary(symbol, "UNAVAILABLE", "unavailable");
    return;
  }
  if (adapter === activeMarketSummaryAdapter && symbol === activeMarketSummarySymbol) {
    return;
  }
  stopActiveMarketSummary?.();
  stopActiveMarketSummary = null;
  activeMarketSummaryAdapter = adapter;
  activeMarketSummarySymbol = symbol;
  clearMarketSummary(symbol);
  if (adapter === "bybit_linear") {
    stopActiveMarketSummary = startBybitMarketSummary(symbol) ?? null;
  } else if (adapter === "binance_usdm") {
    stopActiveMarketSummary = startBinanceMarketSummary(symbol) ?? null;
  }
}

function clearMarketSummary(symbol, statusText = "", statusClass = "") {
  if (!marketSummary || marketSummary.dataset.symbol !== symbol) return;
  const status = document.querySelector("#market-summary-status");
  const priceValue = marketSummary.querySelector("[data-market-price]");
  const priceChange = marketSummary.querySelector("[data-market-price-change]");
  const fields = [
    "highPrice24h", "lowPrice24h", "turnover24h", "openInterestValue",
    "fundingRate", "nextFundingTime",
  ];
  priceValue.textContent = "—";
  priceChange.textContent = "—";
  priceChange.classList.remove("is-positive", "is-negative");
  for (const field of fields) {
    marketSummary.querySelector(`[data-market-field="${field}"]`).textContent = "—";
  }
  status.textContent = statusText;
  status.classList.toggle("is-reconnecting", statusClass === "reconnecting");
  status.classList.toggle("is-stale", statusClass === "stale");
  status.classList.toggle("is-unavailable", statusClass === "unavailable");
  publishCurrentMarketPrice(symbol, null);
}

function startBinanceRestMarketSummary(symbol) {
  if (!marketSummary || marketSummary.dataset.symbol !== symbol) return;

  const status = document.querySelector("#market-summary-status");
  const fields = Object.fromEntries(
    [...marketSummary.querySelectorAll("[data-market-field]")].map((field) => [
      field.dataset.marketField, field,
    ]),
  );
  const priceValue = marketSummary.querySelector("[data-market-price]");
  const priceChange = marketSummary.querySelector("[data-market-price-change]");
  let stopped = false;

  function formatNumber(value, options = {}) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("en-US", {
      maximumSignificantDigits: 7, ...options,
    }).format(number);
  }

  function formatCompactMoney(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("en-US", {
      notation: "compact", compactDisplay: "short", maximumSignificantDigits: 4,
    }).format(number);
  }

  function formatPercent(value, signed = false) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${signed && number > 0 ? "+" : ""}${formatNumber(number * 100, {
      maximumFractionDigits: 4,
    })}%`;
  }

  function formatChangePercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${number > 0 ? "+" : ""}${new Intl.NumberFormat("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(number * 100)}%`;
  }

  function formatFundingCountdown(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp)) return "—";
    const remainingSeconds = Math.max(0, Math.floor((timestamp - Date.now()) / 1000));
    const hours = Math.floor(remainingSeconds / 3600);
    const minutes = Math.floor((remainingSeconds % 3600) / 60);
    const seconds = remainingSeconds % 60;
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function setStatus(value) {
    status.textContent = value;
    status.classList.toggle("is-reconnecting", value === "RECONNECTING");
    status.classList.toggle("is-stale", value === "STALE");
    status.classList.remove("is-unavailable");
  }

  async function refresh() {
    try {
      const response = await fetch(
        `/api/market-summary/binance?symbol=${encodeURIComponent(symbol)}`,
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Binance market summary is unavailable");
      if (stopped || marketSummary.dataset.symbol !== symbol) return;
      setStatus("");
      render(data);
    } catch (_) {
      if (!stopped) clearMarketSummary(symbol, "UNAVAILABLE", "unavailable");
    }
  }

  function render(data) {
    const change = Number(data.price_change_percent) / 100;
    const absoluteChange = Number(data.price_change);
    priceValue.textContent = formatNumber(data.last_price);
    priceChange.textContent = Number.isFinite(change) && Number.isFinite(absoluteChange)
      ? `${change > 0 ? "+" : ""}${formatNumber(absoluteChange)} (${formatChangePercent(change)})`
      : "—";
    fields.highPrice24h.textContent = formatNumber(data.high_price);
    fields.lowPrice24h.textContent = formatNumber(data.low_price);
    fields.turnover24h.textContent = formatCompactMoney(data.quote_volume);
    fields.openInterestValue.textContent = "—";
    fields.fundingRate.textContent = formatPercent(data.last_funding_rate);
    fields.nextFundingTime.textContent = formatFundingCountdown(data.next_funding_time);
    priceChange.classList.toggle("is-positive", change > 0);
    priceChange.classList.toggle("is-negative", change < 0);
  }

  const refreshTimer = window.setInterval(refresh, refreshIntervalMilliseconds);
  function stop() {
    if (stopped) return;
    stopped = true;
    window.clearInterval(refreshTimer);
  }
  window.addEventListener("pagehide", stop, { once: true });
  refresh();
  return stop;
}

function startBinanceMarketSummary(symbol) {
  if (!marketSummary || marketSummary.dataset.symbol !== symbol) return;

  const status = document.querySelector("#market-summary-status");
  const fields = Object.fromEntries(
    [...marketSummary.querySelectorAll("[data-market-field]")].map((field) => [
      field.dataset.marketField, field,
    ]),
  );
  const priceValue = marketSummary.querySelector("[data-market-price]");
  const priceChange = marketSummary.querySelector("[data-market-price-change]");
  const ticker = {};
  const markPrice = {};
  const streamSymbol = symbol.toLowerCase();
  let socket;
  let fallbackTimer;
  let staleTimer;
  let stopped = false;
  let fallbackStarted = false;
  let lastTickerUpdateAt = 0;

  function formatNumber(value, options = {}) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("en-US", {
      maximumSignificantDigits: 7, ...options,
    }).format(number);
  }

  function formatCompactMoney(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("en-US", {
      notation: "compact", compactDisplay: "short", maximumSignificantDigits: 4,
    }).format(number);
  }

  function formatPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${formatNumber(number * 100, { maximumFractionDigits: 4 })}%`;
  }

  function formatChangePercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${number > 0 ? "+" : ""}${new Intl.NumberFormat("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(number * 100)}%`;
  }

  function formatFundingCountdown(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp)) return "—";
    const remainingSeconds = Math.max(0, Math.floor((timestamp - Date.now()) / 1000));
    const hours = Math.floor(remainingSeconds / 3600);
    const minutes = Math.floor((remainingSeconds % 3600) / 60);
    const seconds = remainingSeconds % 60;
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function render() {
    const change = Number(ticker.P) / 100;
    const absoluteChange = Number(ticker.p);
    priceValue.textContent = formatNumber(ticker.c);
    priceChange.textContent = Number.isFinite(change) && Number.isFinite(absoluteChange)
      ? `${change > 0 ? "+" : ""}${formatNumber(absoluteChange)} (${formatChangePercent(change)})`
      : "—";
    fields.highPrice24h.textContent = formatNumber(ticker.h);
    fields.lowPrice24h.textContent = formatNumber(ticker.l);
    fields.turnover24h.textContent = formatCompactMoney(ticker.q);
    fields.openInterestValue.textContent = "—";
    fields.fundingRate.textContent = formatPercent(markPrice.r);
    fields.nextFundingTime.textContent = formatFundingCountdown(markPrice.T);
    priceChange.classList.toggle("is-positive", change > 0);
    priceChange.classList.toggle("is-negative", change < 0);
  }

  function setStatus(value) {
    status.textContent = value;
    status.classList.toggle("is-reconnecting", value === "RECONNECTING");
    status.classList.remove("is-stale", "is-unavailable");
  }

  function startFallback() {
    if (stopped || fallbackStarted) return;
    fallbackStarted = true;
    window.clearTimeout(fallbackTimer);
    socket?.close();
    publishCurrentMarketPrice(symbol, null);
    restStop = startBinanceRestMarketSummary(symbol) ?? null;
  }

  function connect() {
    if (stopped || fallbackStarted || marketSummary.dataset.symbol !== symbol) return;
    setStatus("");
    try {
      socket = new WebSocket(
        `wss://fstream.binance.com/market/stream?streams=${streamSymbol}@ticker/${streamSymbol}@markPrice@1s`,
      );
    } catch (_) {
      startFallback();
      return;
    }
    fallbackTimer = window.setTimeout(startFallback, 5_000);
    socket.addEventListener("message", (event) => {
      const data = JSON.parse(event.data).data;
      if (!data || data.s !== symbol) return;
      if (data.e === "24hrTicker") {
        Object.assign(ticker, data);
        lastTickerUpdateAt = Date.now();
        publishCurrentMarketPrice(symbol, ticker.c);
      } else if (data.e === "markPriceUpdate") Object.assign(markPrice, data);
      else return;
      window.clearTimeout(fallbackTimer);
      setStatus("");
      render();
    });
    socket.addEventListener("close", () => {
      publishCurrentMarketPrice(symbol, null);
      if (!stopped && !fallbackStarted) startFallback();
    });
    socket.addEventListener("error", () => socket.close());
  }

  let restStop = null;
  staleTimer = window.setInterval(() => {
    if (lastTickerUpdateAt && Date.now() - lastTickerUpdateAt > 30_000) {
      publishCurrentMarketPrice(symbol, null);
    }
  }, 1_000);
  function stop() {
    if (stopped) return;
    stopped = true;
    window.clearTimeout(fallbackTimer);
    window.clearInterval(staleTimer);
    socket?.close();
    restStop?.();
    publishCurrentMarketPrice(symbol, null);
  }
  window.addEventListener("pagehide", stop, { once: true });
  connect();
  return stop;
}

function startBybitMarketSummary(symbol) {
  if (!marketSummary || marketSummary.dataset.symbol !== symbol) return;

  const status = document.querySelector("#market-summary-status");
  const fields = Object.fromEntries(
    [...marketSummary.querySelectorAll("[data-market-field]")].map((field) => [
      field.dataset.marketField, field,
    ]),
  );
  const priceValue = marketSummary.querySelector("[data-market-price]");
  const priceChange = marketSummary.querySelector("[data-market-price-change]");
  const ticker = {};
  let socket;
  let reconnectTimer;
  let heartbeatTimer;
  let reconnectAttempts = 0;
  let stopped = false;
  let lastUpdateAt = 0;

  function formatNumber(value, options = {}) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("en-US", {
      maximumSignificantDigits: 7, ...options,
    }).format(number);
  }

  function formatCompactMoney(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("en-US", {
      notation: "compact", compactDisplay: "short", maximumSignificantDigits: 4,
    }).format(number);
  }

  function formatPercent(value, signed = false) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${signed && number > 0 ? "+" : ""}${formatNumber(number * 100, {
      maximumFractionDigits: 4,
    })}%`;
  }

  function formatChangePercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${number > 0 ? "+" : ""}${new Intl.NumberFormat("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(number * 100)}%`;
  }

  function displayedPricePrecision(value) {
    const formatted = formatNumber(value);
    return formatted.includes(".") ? formatted.split(".")[1].length : 0;
  }

  function formatAbsoluteChange(value, price) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const precision = displayedPricePrecision(price);
    return new Intl.NumberFormat("en-US", {
      minimumFractionDigits: precision, maximumFractionDigits: precision,
    }).format(number);
  }

  function formatFundingCountdown(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp)) return "—";
    const remainingSeconds = Math.max(0, Math.floor((timestamp - Date.now()) / 1000));
    const hours = Math.floor(remainingSeconds / 3600);
    const minutes = Math.floor((remainingSeconds % 3600) / 60);
    const seconds = remainingSeconds % 60;
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function render() {
    const lastPrice = Number(ticker.lastPrice);
    const change = Number(ticker.price24hPcnt);
    const absoluteChange = lastPrice * change / (1 + change);
    priceValue.textContent = formatNumber(ticker.lastPrice);
    priceChange.textContent = Number.isFinite(absoluteChange)
      ? `${change > 0 ? "+" : ""}${formatAbsoluteChange(absoluteChange, ticker.lastPrice)} (${formatChangePercent(change)})`
      : "—";
    fields.highPrice24h.textContent = formatNumber(ticker.highPrice24h);
    fields.lowPrice24h.textContent = formatNumber(ticker.lowPrice24h);
    fields.turnover24h.textContent = formatCompactMoney(ticker.turnover24h);
    fields.openInterestValue.textContent = formatCompactMoney(ticker.openInterestValue);
    fields.fundingRate.textContent = formatPercent(ticker.fundingRate);
    fields.nextFundingTime.textContent = formatFundingCountdown(ticker.nextFundingTime);
    priceChange.classList.toggle("is-positive", change > 0);
    priceChange.classList.toggle("is-negative", change < 0);
  }

  function setStatus(value) {
    status.textContent = value;
    status.classList.toggle("is-reconnecting", value === "RECONNECTING");
    status.classList.toggle("is-stale", value === "STALE");
    status.classList.remove("is-unavailable");
  }

  function scheduleReconnect() {
    if (stopped || reconnectTimer) return;
    setStatus("RECONNECTING");
    const delay = Math.min(1_000 * 2 ** reconnectAttempts, 30_000);
    reconnectAttempts += 1;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = undefined;
      connect();
    }, delay);
  }

  function connect() {
    if (stopped || marketSummary.dataset.symbol !== symbol) return;
    setStatus(reconnectAttempts ? "RECONNECTING" : "");
    socket = new WebSocket(bybitLinearWebSocketUrl);
    socket.addEventListener("open", () => {
      if (stopped || socket?.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ op: "subscribe", args: [`tickers.${symbol}`] }));
      heartbeatTimer = window.setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ op: "ping" }));
      }, 20_000);
    });
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (stopped || marketSummary.dataset.symbol !== symbol || message.topic !== `tickers.${symbol}`) return;
      if (message.type !== "snapshot" && message.type !== "delta") return;
      Object.assign(ticker, message.data);
      lastUpdateAt = Date.now();
      reconnectAttempts = 0;
      setStatus("");
      publishCurrentMarketPrice(symbol, ticker.lastPrice);
      render();
    });
    socket.addEventListener("close", () => {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = undefined;
      publishCurrentMarketPrice(symbol, null);
      scheduleReconnect();
    });
    socket.addEventListener("error", () => socket.close());
  }

  const staleTimer = window.setInterval(() => {
    if (lastUpdateAt && Date.now() - lastUpdateAt > 30_000) {
      setStatus("STALE");
      publishCurrentMarketPrice(symbol, null);
    }
    render();
  }, 1_000);
  function stop() {
    if (stopped) return;
    stopped = true;
    window.clearTimeout(reconnectTimer);
    window.clearInterval(heartbeatTimer);
    window.clearInterval(staleTimer);
    socket?.close();
    publishCurrentMarketPrice(symbol, null);
  }
  window.addEventListener("pagehide", stop, { once: true });
  connect();
  return stop;
}

const chartElement = document.querySelector("#chart");
if (chartElement) {
  const localFilterContainer = document.querySelector("#local-signal-filters");
  const marketDataSource = document.querySelector("#market-data-source");
  const volumeElement = document.querySelector("#volume-chart");
  const openInterestElement = document.querySelector("#open-interest-chart");
  const timeframeButtons = document.querySelector("#timeframe-buttons");
  const timeframeSettings = document.querySelector("#timeframe-settings");
  const timeframeSettingsPanel = document.querySelector("#timeframe-settings-panel");
  const measurementRuler = document.querySelector("#measurement-ruler");
  const chartDate = document.querySelector("#chart-date");
  const chartDateDay = document.querySelector("#chart-date-day");
  const chartDateMonth = document.querySelector("#chart-date-month");
  const chartDateYear = document.querySelector("#chart-date-year");
  const chartDatePickerElement = document.querySelector("#chart-date-picker");
  const chartDateGo = document.querySelector("#chart-date-go");
  const chartDateStatus = document.querySelector("#chart-date-status");
  const chartStack = document.querySelector(".chart-stack");
  const emptyChart = document.querySelector("#chart-empty");
  const chartError = document.querySelector("#chart-error");
  const chartErrorMessage = document.querySelector("#chart-error-message");
  const chartRetry = document.querySelector("#chart-retry");
  const hyperliquidHistoryDialog = document.querySelector("#hyperliquid-history-dialog");
  const hyperliquidHistoryMessage = document.querySelector("#hyperliquid-history-message");
  const hyperliquidHistoryOkay = document.querySelector("#hyperliquid-history-okay");
  const eventCards = document.querySelector(".event-cards");
  const timezoneControl = document.querySelector("#display-timezone-control");
  const timezoneButton = document.querySelector("#display-timezone-button");
  const timezoneMenu = document.querySelector("#display-timezone-menu");
  let selectedEventId = null;
  let eventRows = [];
  let detailedEventId = null;
  let eventListScrollTop = 0;
  const expandedEventIds = new Set();
  const chartOptions = (timeScaleVisible) => ({
    autoSize: true,
    layout: {
      background: { color: "#171b25" }, textColor: "#aeb9cc", attributionLogo: false,
    },
    grid: { vertLines: { color: "#242c3b" }, horzLines: { color: "#242c3b" } },
    rightPriceScale: { minimumWidth: 80 },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      visible: timeScaleVisible,
      tickMarkFormatter: formatDisplayTickMark,
    },
    // Lightweight Charts keeps the bar beneath the pointer anchored while
    // scaling. Do not also interpret the wheel as horizontal scrolling.
    handleScale: { mouseWheel: true },
    handleScroll: { mouseWheel: false },
    localization: { locale: "en-GB", timeFormatter: formatDisplayChartTime },
  });
  const priceChart = LightweightCharts.createChart(chartElement, {
    ...chartOptions(true),
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { labelVisible: true },
      horzLine: { labelVisible: true },
    },
  });
  const volumeChart = LightweightCharts.createChart(volumeElement, chartOptions(false));
  const openInterestChart = LightweightCharts.createChart(openInterestElement, chartOptions(true));
  const priceSeries = priceChart.addCandlestickSeries({
    upColor: "#2fc28b", downColor: "#ed6a6a", borderVisible: false,
    wickUpColor: "#2fc28b", wickDownColor: "#ed6a6a",
    lastValueVisible: false, priceLineVisible: false,
  });
  const customMarkerShapes = new Set([
    "diamond", "triangleUp", "triangleDown", "star", "target", "flag",
  ]);

  class CustomMarkerRenderer {
    constructor(coordinates) {
      this.coordinates = coordinates;
    }

    draw(target) {
      const points = this.coordinates();
      if (!points.length) return;
      target.useMediaCoordinateSpace(({ context }) => {
        for (const point of points) {
          // Match Lightweight Charts' native circle surrogate: it draws at
          // 80% of the marker layout size with a one-pixel centered inset.
          const halfSize = (point.visualSize - 1) / 2;
          context.fillStyle = point.color;
          context.beginPath();
          if (point.shape === "triangleUp") {
            context.moveTo(point.x, point.y - halfSize);
            context.lineTo(point.x + halfSize, point.y + halfSize);
            context.lineTo(point.x - halfSize, point.y + halfSize);
          } else if (point.shape === "triangleDown") {
            context.moveTo(point.x, point.y + halfSize);
            context.lineTo(point.x + halfSize, point.y - halfSize);
            context.lineTo(point.x - halfSize, point.y - halfSize);
          } else if (point.shape === "star") {
            for (let index = 0; index < 10; index += 1) {
              const radius = index % 2 === 0 ? halfSize : halfSize * 0.45;
              const angle = -Math.PI / 2 + index * Math.PI / 5;
              const x = point.x + Math.cos(angle) * radius;
              const y = point.y + Math.sin(angle) * radius;
              if (index === 0) context.moveTo(x, y);
              else context.lineTo(x, y);
            }
          } else if (point.shape === "target") {
            const ringRadius = Math.max(halfSize - 1, 1);
            context.strokeStyle = point.color;
            context.lineWidth = Math.max(1, point.visualSize * 0.12);
            context.arc(point.x, point.y, ringRadius, 0, Math.PI * 2);
            context.stroke();
            context.beginPath();
            context.arc(point.x, point.y, Math.max(1.5, halfSize * 0.32), 0, Math.PI * 2);
          } else if (point.shape === "flag") {
            const poleX = point.x - halfSize * 0.55;
            context.strokeStyle = point.color;
            context.lineWidth = Math.max(1, point.visualSize * 0.13);
            context.moveTo(poleX, point.y + halfSize);
            context.lineTo(poleX, point.y - halfSize);
            context.stroke();
            context.beginPath();
            context.moveTo(poleX, point.y - halfSize);
            context.lineTo(point.x + halfSize, point.y - halfSize * 0.7);
            context.lineTo(point.x + halfSize * 0.42, point.y);
            context.lineTo(poleX, point.y + halfSize * 0.08);
          } else {
            context.moveTo(point.x, point.y - halfSize);
            context.lineTo(point.x + halfSize, point.y);
            context.lineTo(point.x, point.y + halfSize);
            context.lineTo(point.x - halfSize, point.y);
          }
          context.closePath();
          context.fill();
          if (point.text) {
            context.font = `${point.fontSize}px ${point.fontFamily}`;
            context.textAlign = "center";
            context.textBaseline = "middle";
            context.fillText(point.text, point.x, point.textY);
          }
        }
      });
    }
  }

  class CustomMarkerPrimitive {
    constructor(coordinates) {
      this.renderer = new CustomMarkerRenderer(coordinates);
      this.paneView = { renderer: () => this.renderer, zOrder: () => "top" };
    }

    paneViews() {
      return [this.paneView];
    }
  }

  const customMarkerPrimitive = new CustomMarkerPrimitive(
    () => customMarkerCoordinates(),
  );
  priceSeries.attachPrimitive(customMarkerPrimitive);
  let currentMarketPriceLine = null;

  function updateCurrentMarketPrice(price) {
    if (!Number.isFinite(price)) {
      if (currentMarketPriceLine) priceSeries.removePriceLine(currentMarketPriceLine);
      currentMarketPriceLine = null;
      return;
    }
    if (currentMarketPriceLine) {
      currentMarketPriceLine.applyOptions({ price });
      return;
    }
    currentMarketPriceLine = priceSeries.createPriceLine({
      price, color: "#2fc28b", lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
    });
  }

  window.addEventListener("dashboard-current-market-price", (event) => {
    const { symbol, price } = event.detail ?? {};
    if (symbol !== chartElement.dataset.symbol) return;
    updateCurrentMarketPrice(price);
  });
  const volumeSeries = volumeChart.addHistogramSeries({ priceFormat: { type: "volume" } });
  const openInterestSeries = openInterestChart.addLineSeries({ color: "#8ec5ff", lineWidth: 2 });
  const openInterestUnavailable = document.createElement("p");
  openInterestUnavailable.className = "open-interest-unavailable";
  openInterestUnavailable.hidden = true;
  openInterestUnavailable.setAttribute("role", "status");
  openInterestElement.append(openInterestUnavailable);

  const timeframeDefinitions = [
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1D", "1W", "1M",
  ];
  const timeframeSeconds = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1D": 86400, "1W": 604800, "1M": 2592000,
  };
  const supportedTimeframes = new Set(timeframeDefinitions);
  const hyperliquidUnsupportedTimeframes = new Set(["6h"]);
  const defaultVisibleTimeframes = ["1m", "5m", "15m", "1h", "4h", "1D"];
  const timeframeStorageKey = "signal-dashboard-timeframe";
  const visibleTimeframesStorageKey = "signal-dashboard-visible-timeframes";
  const chartPanelVisibilityStorageKey = "signal-dashboard-chart-panel-visibility";
  const localFilterStorageKey = "signal-dashboard-local-signal-filter-overrides";
  const marketDataSourceStorageKey = "signal-dashboard-market-data-source";
  const detailedHistoryBoundary = Date.UTC(2026, 5, 1) / 1000;
  const dateJumpWindowBars = 750;
  const latestViewportBars = 120;
  const latestViewportRightMarginBars = 6;
  let activeTimeframe;
  let activeMarketDataSource;
  let candles = [];
  let volume = [];
  let openInterest = [];
  let openInterestStatus = null;
  let markers = [];
  let renderedMarkerGroups = [];
  let markerGroupsById = new Map();
  let highlightedMarkerGroupId = null;
  let hoveredMarkerGroupId = null;
  let historyBoundary = null;
  let hasOlder = false;
  let hasNewer = false;
  let loadingOlder = false;
  let loadingNewer = false;
  let lazyLoadingEnabled = false;
  let chartLoadVersion = 0;
  let historicalWindow = false;
  let retryChartRequest = null;
  let refreshing = false;
  let synchronizingRange = false;
  let synchronizingCrosshair = false;
  let suppressCrosshairSynchronization = false;
  let viewportFollowsLive = true;
  let chartPointerDownAt = null;
  let rulerActive = false;
  let rulerDragging = false;
  let rulerMeasurement = null;
  let rulerPreviousPressedMouseMove;
  let rulerScrollOverrideActive = false;
  const markerEventList = document.createElement("div");
  markerEventList.className = "marker-event-list";
  markerEventList.hidden = true;
  chartElement.closest(".chart-panel")?.append(markerEventList);
  const markerHoverTooltip = document.createElement("div");
  markerHoverTooltip.className = "marker-hover-tooltip";
  markerHoverTooltip.hidden = true;
  chartElement.closest(".chart-panel")?.append(markerHoverTooltip);
  const selectedMarkerHighlight = document.createElement("div");
  selectedMarkerHighlight.className = "selected-marker-highlight";
  selectedMarkerHighlight.hidden = true;
  chartElement.append(selectedMarkerHighlight);
  const historyBoundaryElement = document.createElement("div");
  historyBoundaryElement.className = "history-boundary";
  historyBoundaryElement.hidden = true;
  const historyBoundaryLabel = document.createElement("span");
  historyBoundaryElement.append(historyBoundaryLabel);
  chartElement.append(historyBoundaryElement);
  const rulerOverlay = document.createElement("div");
  rulerOverlay.className = "measurement-ruler";
  rulerOverlay.hidden = true;
  const rulerRange = document.createElement("div");
  rulerRange.className = "measurement-ruler-range";
  const rulerLine = document.createElement("div");
  rulerLine.className = "measurement-ruler-line";
  const rulerLabel = document.createElement("div");
  rulerLabel.className = "measurement-ruler-label";
  rulerOverlay.append(rulerRange, rulerLine, rulerLabel);
  chartElement.append(rulerOverlay);

  function localFilterOverrides() {
    try {
      const stored = JSON.parse(localStorage.getItem(localFilterStorageKey) ?? "{}");
      return stored && typeof stored === "object" ? stored : {};
    } catch (_) {
      return {};
    }
  }

  function savedMarketDataSource() {
    const saved = sessionStorage.getItem(marketDataSourceStorageKey);
    const option = [...marketDataSource.options].find(({ value }) => value === saved);
    if (option && !option.disabled) return saved;
    const defaultOption = [...marketDataSource.options].find(
      ({ value }) => value === marketDataSource.dataset.defaultSource,
    );
    if (defaultOption && !defaultOption.disabled) return defaultOption.value;
    return [...marketDataSource.options].find(({ disabled }) => !disabled)?.value;
  }

  function localFilterOverride() {
    const filter = localFilterOverrides()[chartElement.dataset.symbol];
    return typeof filter === "string" ? filter : null;
  }

  function eventTypesForFilter(filter) {
    if (filter === "all") return new Set(signalTypes.map(({ event_type }) => event_type));
    const keys = new Set(filter.split(","));
    return new Set(
      signalTypes.filter(({ filter_key }) => keys.has(filter_key)).map(
        ({ event_type }) => event_type,
      ),
    );
  }

  function localEventTypes() {
    return new Set(
      [...eventTypesForFilter(localFilterOverride() ?? activeFilter())].filter(
        (eventType) => availableEventTypes.has(eventType),
      ),
    );
  }

  function localFilterValue() {
    return localFilterOverride() ?? activeFilter();
  }

  function setLocalFilterOverride(filter) {
    const overrides = localFilterOverrides();
    overrides[chartElement.dataset.symbol] = filter;
    localStorage.setItem(localFilterStorageKey, JSON.stringify(overrides));
  }

  function clearLocalFilterOverride() {
    const overrides = localFilterOverrides();
    delete overrides[chartElement.dataset.symbol];
    localStorage.setItem(localFilterStorageKey, JSON.stringify(overrides));
  }

  function normalizeLocalFilterOverride() {
    const override = localFilterOverride();
    if (override === null || override === "all") return false;
    const normalized = filterValueFor(localEventTypes());
    if (normalized === override) return false;
    setLocalFilterOverride(normalized);
    return true;
  }

  function renderLocalFilterButtons() {
    hideSignalFilterTooltip();
    const eventTypes = localEventTypes();
    const inherited = localFilterOverride() === null;
    const fragment = document.createDocumentFragment();
    const reset = document.createElement("button");
    reset.className = "local-filter-reset";
    reset.type = "button";
    reset.dataset.action = "use-global";
    reset.disabled = inherited;
    reset.textContent = "Use global";
    fragment.append(reset);
    for (const signalType of signalTypes) {
      if (!availableEventTypes.has(signalType.event_type)) continue;
      const button = document.createElement("button");
      button.className = `local-filter${eventTypes.has(signalType.event_type) ? " is-active" : ""}`;
      button.type = "button";
      button.dataset.eventType = signalType.event_type;
      button.textContent = signalType.short_label;
      attachSignalFilterTooltip(button, signalType.display_name);
      fragment.append(button);
    }
    localFilterContainer.replaceChildren(fragment);
  }

  function savedTimeframe() {
    const saved = localStorage.getItem(timeframeStorageKey);
    return visibleTimeframes().includes(saved) ? saved : visibleTimeframes()[0];
  }

  function isTimeframeSupportedByMarketSource(timeframe, source = activeMarketDataSource) {
    return source !== "hyperliquid_api" || !hyperliquidUnsupportedTimeframes.has(timeframe);
  }

  function timeframeForMarketSource(timeframe, source = activeMarketDataSource) {
    return isTimeframeSupportedByMarketSource(timeframe, source) ? timeframe : "1h";
  }

  function visibleTimeframesForMarketSource() {
    const visible = visibleTimeframes().filter((timeframe) => (
      isTimeframeSupportedByMarketSource(timeframe)
    ));
    return visible.length ? visible : ["1h"];
  }

  function visibleTimeframes() {
    try {
      const saved = JSON.parse(localStorage.getItem(visibleTimeframesStorageKey) ?? "null");
      const visible = Array.isArray(saved)
        ? timeframeDefinitions.filter((timeframe) => saved.includes(timeframe))
        : defaultVisibleTimeframes;
      return visible.length ? visible : defaultVisibleTimeframes;
    } catch (_) {
      return defaultVisibleTimeframes;
    }
  }

  function chartPanelVisibility() {
    try {
      const saved = JSON.parse(localStorage.getItem(chartPanelVisibilityStorageKey) ?? "{}");
      return {
        volume: saved?.volume !== false,
        openInterest: saved?.openInterest !== false,
      };
    } catch (_) {
      return { volume: true, openInterest: true };
    }
  }

  function applyChartPanelVisibility() {
    const visibility = chartPanelVisibility();
    volumeElement.hidden = !visibility.volume;
    openInterestElement.hidden = !visibility.openInterest;
    chartStack.classList.toggle("is-volume-hidden", !visibility.volume);
    chartStack.classList.toggle("is-open-interest-hidden", !visibility.openInterest);
  }

  function renderOpenInterestAvailability() {
    const unavailable = openInterestStatus?.status === "unavailable";
    openInterestUnavailable.hidden = !unavailable;
    openInterestUnavailable.textContent = unavailable
      ? "Open Interest unavailable for this source and timeframe."
      : "";
  }

  function renderTimeframeControls() {
    const visible = visibleTimeframesForMarketSource();
    const buttons = document.createDocumentFragment();
    for (const timeframe of visible) {
      const button = document.createElement("button");
      button.className = `timeframe${timeframe === activeTimeframe ? " is-active" : ""}`;
      button.type = "button";
      button.dataset.timeframe = timeframe;
      button.textContent = timeframe;
      buttons.append(button);
    }
    timeframeButtons.replaceChildren(buttons);
    const options = document.createDocumentFragment();
    for (const timeframe of timeframeDefinitions) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = timeframe;
      input.checked = visibleTimeframes().includes(timeframe);
      input.disabled = !isTimeframeSupportedByMarketSource(timeframe)
        || (input.checked && visibleTimeframes().length === 1);
      label.append(input, document.createTextNode(timeframe));
      options.append(label);
    }
    const visibility = chartPanelVisibility();
    const panelControls = document.createElement("div");
    panelControls.className = "chart-panel-visibility-controls";
    for (const [panel, labelText, accessibleName] of [
      ["volume", "VOL", "Volume"],
      ["openInterest", "OI", "Open Interest"],
    ]) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.chartPanel = panel;
      input.setAttribute("aria-label", accessibleName);
      input.checked = visibility[panel];
      label.append(input, document.createTextNode(labelText));
      panelControls.append(label);
    }
    options.append(panelControls);
    timeframeSettingsPanel.replaceChildren(options);
  }

  function formatDisplayChartTime(time) {
    const value = dateParts(new Date(time * 1000), displayTimezone(), {
      day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
    return `${value.day} ${value.month} '${value.year.slice(-2)} ${value.hour}:${value.minute}`;
  }

  function formatDisplayTickMark(time, tickMarkType) {
    const date = new Date(time * 1000);
    const value = dateParts(date, displayTimezone(), {
      day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
    if (tickMarkType === LightweightCharts.TickMarkType.Year) return value.year;
    if (tickMarkType === LightweightCharts.TickMarkType.Month) {
      return `${value.month} ${value.year}`;
    }
    if (tickMarkType === LightweightCharts.TickMarkType.DayOfMonth) {
      return `${value.day} ${value.month}`;
    }
    return `${value.hour}:${value.minute}`;
  }

  function marker(event) {
    const definition = signalTypeFor(event.eventType ?? event.type);
    const chartMarker = definition?.chart_marker ?? {
      position: "inBar", color: "#aeb9cc", shape: "circle",
    };
    return { time: event.time, color: definition?.color ?? chartMarker.color, ...chartMarker };
  }

  function canonicalEventType(event) {
    const eventType = event.eventType ?? event.type;
    return signalTypeFor(eventType)?.event_type ?? String(eventType ?? "unknown");
  }

  function markerTypeOrder(eventType) {
    const index = signalTypes.findIndex(
      (signalType) => signalType.event_type === eventType,
    );
    return index === -1 ? signalTypes.length : index;
  }

  function compareMarkerIdentities(left, right) {
    const leftNumber = Number(left);
    const rightNumber = Number(right);
    if (Number.isSafeInteger(leftNumber) && Number.isSafeInteger(rightNumber)) {
      return leftNumber - rightNumber;
    }
    return left < right ? -1 : left > right ? 1 : 0;
  }

  function markerGroupsByType(events) {
    const groups = new Map();
    for (const event of events) {
      const eventType = canonicalEventType(event);
      const groupKey = `${event.time}\u0000${eventType}`;
      const group = groups.get(groupKey) ?? {
        time: event.time,
        eventType,
        events: [],
        eventIds: [],
      };
      group.events.push(event);
      group.eventIds.push(markerIdentity(event));
      groups.set(groupKey, group);
    }
    return [...groups.values()]
      .map((group) => ({
        ...group,
        events: [...group.events].sort((left, right) => (
          compareMarkerIdentities(markerIdentity(left), markerIdentity(right))
        )),
        eventIds: [...group.eventIds].sort(compareMarkerIdentities),
      }))
      .sort((left, right) => (
        left.time - right.time
        || markerTypeOrder(left.eventType) - markerTypeOrder(right.eventType)
        || compareMarkerIdentities(left.eventType, right.eventType)
      ));
  }

  function markerForGroup(group) {
    const markerData = marker({ ...group.events[0], eventType: group.eventType });
    const isCustomShape = customMarkerShapes.has(markerData.shape);
    return {
      ...markerData,
      // The transparent native marker intentionally retains its geometry, text
      // and stable ID so v4.2 continues to provide hoveredObjectId.
      ...(isCustomShape ? { color: "rgba(0, 0, 0, 0)", shape: "circle" } : {}),
      // v4.2 stacks same-time, same-side markers in the order supplied here.
      id: markerIdForGroup(group),
      ...(group.events.length > 1 ? { text: `×${group.events.length}` } : {}),
    };
  }

  function markerIdForGroup(group) {
    return `event-group:${group.time}:${group.eventType}:${group.eventIds.join(",")}`;
  }

  function ceiledOdd(value) {
    const rounded = Math.ceil(value);
    return rounded % 2 === 0 ? rounded + 1 : rounded;
  }

  function ceiledEven(value) {
    const rounded = Math.ceil(value);
    return rounded % 2 === 0 ? rounded : rounded + 1;
  }

  function markerCoordinatesForGroup(group) {
    const candle = candles.find((item) => item.time === group.time);
    const x = priceChart.timeScale().timeToCoordinate(group.time);
    if (!candle || x === null) return;

    // Mirror Lightweight Charts 4.2's marker stack using the exact groups and
    // order sent to setMarkers(). Its public API exposes chart coordinates but
    // not rendered marker coordinates, so a timestamp-only anchor cannot tell
    // vertically stacked markers apart.
    const barSpacing = priceChart.timeScale().options().barSpacing;
    const baseSize = ceiledOdd(Math.min(Math.max(barSpacing, 12), 30));
    const shapeSize = ceiledEven(baseSize);
    const shapeMargin = Math.max(ceiledOdd(Math.min(Math.max(barSpacing, 12), 30) * 0.1), 3);
    const textHeight = priceChart.options().layout.fontSize;
    const markerId = markerIdForGroup(group);
    let aboveOffset = shapeMargin;
    let belowOffset = shapeMargin;

    for (const candidate of renderedMarkerGroups) {
      if (candidate.time !== group.time) continue;
      const markerData = markerForGroup(candidate);
      const sizeMultiplier = Number.isFinite(markerData.size)
        ? Math.max(markerData.size, 0) : 1;
      const candidateShapeSize = shapeSize * sizeMultiplier;
      const halfSize = candidateShapeSize / 2;
      const position = markerData.position;
      const markerPrice = position === "aboveBar" ? candle.high
        : position === "belowBar" ? candle.low : candle.close;
      const markerY = priceSeries.priceToCoordinate(markerPrice);
      if (markerY === null) return;

      let y = markerY;
      if (position === "aboveBar") {
        y -= halfSize + aboveOffset;
        if (markerData.text) aboveOffset += textHeight * 1.2;
        aboveOffset += candidateShapeSize + shapeMargin;
      } else if (position === "belowBar") {
        y += halfSize + belowOffset;
        if (markerData.text) belowOffset += textHeight * 1.2;
        belowOffset += candidateShapeSize + shapeMargin;
      }
      if (markerIdForGroup(candidate) === markerId) return { x, y, position };
    }
  }

  function customMarkerCoordinates() {
    const candleByTime = new Map(candles.map((candle) => [candle.time, candle]));
    const barSpacing = priceChart.timeScale().options().barSpacing;
    const baseSize = ceiledOdd(Math.min(Math.max(barSpacing, 12), 30));
    const fontSize = priceChart.options().layout.fontSize;
    const shapeMargin = Math.max(ceiledOdd(baseSize * 0.1), 3);
    const points = [];
    let currentTime = null;
    let aboveOffset = shapeMargin;
    let belowOffset = shapeMargin;
    for (const group of renderedMarkerGroups) {
      if (group.time !== currentTime) {
        currentTime = group.time;
        aboveOffset = shapeMargin;
        belowOffset = shapeMargin;
      }
      const markerData = markerForGroup(group);
      const candle = candleByTime.get(group.time);
      const x = priceChart.timeScale().timeToCoordinate(group.time);
      if (!candle || x === null) continue;
      const sizeMultiplier = Number.isFinite(markerData.size)
        ? Math.max(markerData.size, 0) : 1;
      const size = ceiledEven(baseSize) * sizeMultiplier;
      const visualSize = ceiledOdd(size * 0.8);
      const halfSize = size / 2;
      const position = markerData.position;
      const markerPrice = position === "aboveBar" ? candle.high
        : position === "belowBar" ? candle.low : candle.close;
      const markerY = priceSeries.priceToCoordinate(markerPrice);
      if (markerY === null) continue;
      let y = markerY;
      if (position === "aboveBar") {
        y -= halfSize + aboveOffset;
        if (markerData.text) aboveOffset += fontSize * 1.2;
        aboveOffset += size + shapeMargin;
      } else if (position === "belowBar") {
        y += halfSize + belowOffset;
        if (markerData.text) belowOffset += fontSize * 1.2;
        belowOffset += size + shapeMargin;
      }
      const customMarker = marker({ ...group.events[0], eventType: group.eventType });
      if (!customMarkerShapes.has(customMarker.shape)) continue;
      points.push({
        x,
        y,
        shape: customMarker.shape,
        color: customMarker.color,
        visualSize,
        text: group.events.length > 1 ? `×${group.events.length}` : null,
        textY: position === "aboveBar"
          ? y - halfSize - fontSize * 0.6
          : y + halfSize + shapeMargin + fontSize * 0.6,
        fontSize,
        fontFamily: priceChart.options().layout.fontFamily,
      });
    }
    return points;
  }

  function closeMarkerEventList() {
    markerEventList.hidden = true;
  }

  function closeMarkerHoverTooltip() {
    markerHoverTooltip.hidden = true;
    hoveredMarkerGroupId = null;
  }

  function positionMarkerPopover(popover, group) {
    const coordinates = markerCoordinatesForGroup(group);
    if (!coordinates) return false;

    popover.hidden = false;
    const chartBounds = chartElement.getBoundingClientRect();
    const panelBounds = popover.offsetParent.getBoundingClientRect();
    const left = Math.min(
      Math.max(coordinates.x - popover.offsetWidth / 2, 0),
      chartBounds.width - popover.offsetWidth,
    );
    const gap = 8;
    const anchorY = chartBounds.top - panelBounds.top + coordinates.y;
    const above = anchorY - popover.offsetHeight - gap;
    const below = anchorY + gap;
    const prefersAbove = coordinates.position === "aboveBar";
    const fitsAbove = above >= chartBounds.top - panelBounds.top;
    const fitsBelow = below + popover.offsetHeight <= chartBounds.bottom - panelBounds.top;
    const naturalTop = prefersAbove ? above : below;
    const fallbackTop = prefersAbove ? below : above;
    const top = (prefersAbove ? fitsAbove : fitsBelow) || !(prefersAbove ? fitsBelow : fitsAbove)
      ? naturalTop : fallbackTop;
    popover.style.left = `${chartBounds.left - panelBounds.left + left}px`;
    popover.style.top = `${Math.max(
      chartBounds.top - panelBounds.top,
      Math.min(top, chartBounds.bottom - panelBounds.top - popover.offsetHeight),
    )}px`;
    return true;
  }

  function showMarkerHoverTooltip(groupId) {
    const group = markerGroupsById.get(groupId);
    if (!group) {
      closeMarkerHoverTooltip();
      return;
    }
    if (hoveredMarkerGroupId !== groupId) {
      const signalType = signalTypeFor(group.eventType);
      const title = document.createElement("strong");
      title.textContent = `${signalType?.short_label ?? group.eventType}${
        group.events.length > 1 ? ` ×${group.events.length}` : ""
      }`;
      if (signalType?.color) title.style.setProperty("--signal-type-color", signalType.color);
      const time = document.createElement("time");
      time.textContent = displayTimestamp(group.events[0]?.eventAt ?? group.time * 1000);
      markerHoverTooltip.replaceChildren(title, time);
      hoveredMarkerGroupId = groupId;
    }
    if (!positionMarkerPopover(markerHoverTooltip, group)) closeMarkerHoverTooltip();
  }

  function positionMarkerEventList(group) {
    return positionMarkerPopover(markerEventList, group);
  }

  function renderSelectedMarkerHighlight() {
    const group = markerGroupsById.get(highlightedMarkerGroupId);
    if (!group) {
      selectedMarkerHighlight.hidden = true;
      return;
    }
    const coordinates = markerCoordinatesForGroup(group);
    if (!coordinates) {
      selectedMarkerHighlight.hidden = true;
      return;
    }
    selectedMarkerHighlight.style.left = `${coordinates.x}px`;
    selectedMarkerHighlight.style.top = `${coordinates.y}px`;
    selectedMarkerHighlight.hidden = false;
  }

  function markerGroupIdForEvent(eventId) {
    for (const [groupId, group] of markerGroupsById) {
      if (group.events.some((event) => String(event.eventId) === String(eventId))) {
        return groupId;
      }
    }
    return null;
  }

  function highlightMarkerGroup(groupId) {
    highlightedMarkerGroupId = groupId;
    renderSelectedMarkerHighlight();
  }

  function updateMarkerEventListSelection() {
    for (const item of markerEventList.querySelectorAll(".marker-event-list-item")) {
      item.classList.toggle("is-selected", item.dataset.eventId === selectedEventId);
    }
  }

  function setEventCardExpanded(card, expanded) {
    const eventId = String(card.dataset.eventId);
    card.classList.toggle("is-expanded", expanded);
    const toggle = card.querySelector(".event-card-toggle");
    toggle?.setAttribute("aria-expanded", String(expanded));
    toggle?.setAttribute(
      "aria-label",
      expanded ? "Collapse event details" : "Expand event details",
    );
    if (expanded) expandedEventIds.add(eventId);
    else expandedEventIds.delete(eventId);
  }

  function selectEventById(
    eventId,
    markerGroupId = markerGroupIdForEvent(eventId),
    { expandCard = false, highlight = true } = {},
  ) {
    if (eventId == null) return false;
    const matchingEvent = eventRows.find(
      (event) => String(event.event_id ?? event.eventId) === String(eventId),
    );
    if (!matchingEvent) return false;
    showCompactEventList();
    selectedEventId = String(eventId);
    if (markerGroupId && highlight) highlightMarkerGroup(markerGroupId);
    let selectedCard = null;
    for (const card of eventCards.querySelectorAll(".event-card")) {
      const selected = card.dataset.eventId === selectedEventId;
      card.classList.toggle("is-focused", selected);
      if (selected) {
        if (expandCard) setEventCardExpanded(card, true);
        selectedCard = card;
      }
    }
    selectedCard?.scrollIntoView({ block: "nearest" });
    updateMarkerEventListSelection();
    return selectedCard !== null;
  }

  function selectMarkerGroupEvent(groupId) {
    const group = markerGroupsById.get(groupId);
    if (!group || group.events.length !== 1) return false;
    return selectEventById(group.events[0].eventId, groupId);
  }

  function openMarkerEventList(groupId) {
    const group = markerGroupsById.get(groupId);
    if (!group) return;
    const fragment = document.createDocumentFragment();
    for (const event of group.events) {
      if (event.eventId == null) continue;
      const item = document.createElement("button");
      item.className = "marker-event-list-item";
      item.type = "button";
      item.dataset.eventId = String(event.eventId);
      item.dataset.markerGroupId = groupId;
      const signalType = signalTypeFor(event.eventType);
      const header = document.createElement("div");
      header.className = "marker-event-list-header";
      const time = document.createElement("time");
      time.textContent = displayTimestamp(event.eventAt);
      const type = document.createElement("span");
      type.className = `event-type event-type-${signalType?.event_card.tone ?? "default"}`;
      type.textContent = signalType?.short_label ?? event.type;
      header.append(time, type);
      const price = document.createElement("div");
      price.className = "marker-event-list-price";
      const priceLabel = document.createElement("span");
      priceLabel.textContent = "Price";
      const priceValue = document.createElement("strong");
      priceValue.textContent = event.eventPrice ?? "—";
      price.append(priceLabel, priceValue);
      item.append(header, price);
      fragment.append(item);
    }
    markerEventList.replaceChildren(fragment);
    updateMarkerEventListSelection();
    if (!markerEventList.childElementCount || !positionMarkerEventList(group)) {
      closeMarkerEventList();
    }
  }

  function rulerPointAt(x, y) {
    const time = priceChart.timeScale().coordinateToTime(x);
    const logical = priceChart.timeScale().coordinateToLogical(x);
    const price = priceSeries.coordinateToPrice(y);
    if (time === null || logical === null || price === null) return null;
    return { time, logical, price };
  }

  function rulerDuration(seconds) {
    const minutes = Math.floor(seconds / 60);
    const days = Math.floor(minutes / (24 * 60));
    const hours = Math.floor((minutes % (24 * 60)) / 60);
    const remainingMinutes = minutes % 60;
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${remainingMinutes}m`;
    return `${Math.max(1, remainingMinutes)}m`;
  }

  function renderRuler() {
    if (!rulerMeasurement) {
      rulerOverlay.hidden = true;
      return;
    }
    const { start, end } = rulerMeasurement;
    const startX = priceChart.timeScale().timeToCoordinate(start.time);
    const endX = priceChart.timeScale().timeToCoordinate(end.time);
    const startY = priceSeries.priceToCoordinate(start.price);
    const endY = priceSeries.priceToCoordinate(end.price);
    if (startX === null || endX === null || startY === null || endY === null) {
      rulerOverlay.hidden = true;
      return;
    }
    const priceChange = end.price - start.price;
    const percentage = start.price ? (priceChange / start.price) * 100 : 0;
    const direction = priceChange > 0 ? "up" : priceChange < 0 ? "down" : "flat";
    const width = Math.abs(endX - startX);
    const height = Math.abs(endY - startY);
    const lineLength = Math.hypot(endX - startX, endY - startY);
    const angle = Math.atan2(endY - startY, endX - startX) * 180 / Math.PI;
    const signedPrice = `${priceChange >= 0 ? "+" : ""}${priceSeries.priceFormatter().format(priceChange)}`;
    const signedPercentage = `${percentage >= 0 ? "+" : ""}${percentage.toFixed(2)}%`;
    const elapsed = Math.abs(Number(end.time) - Number(start.time));

    rulerOverlay.className = `measurement-ruler is-${direction}`;
    rulerRange.style.left = `${Math.min(startX, endX)}px`;
    rulerRange.style.top = `${Math.min(startY, endY)}px`;
    rulerRange.style.width = `${width}px`;
    rulerRange.style.height = `${height}px`;
    rulerLine.style.left = `${startX}px`;
    rulerLine.style.top = `${startY}px`;
    rulerLine.style.width = `${lineLength}px`;
    rulerLine.style.transform = `rotate(${angle}deg)`;
    rulerLabel.textContent = `${signedPercentage} · ${signedPrice} · ${rulerDuration(elapsed)}`;
    rulerOverlay.hidden = false;
    rulerLabel.style.left = `${Math.min(Math.max(endX + 10, 4), chartElement.clientWidth - rulerLabel.offsetWidth - 4)}px`;
    rulerLabel.style.top = `${Math.min(Math.max(endY - 12, 4), chartElement.clientHeight - rulerLabel.offsetHeight - 4)}px`;
  }

  function clearRulerMeasurement() {
    stopRulerDragging();
    rulerMeasurement = null;
    renderRuler();
  }

  function setRulerActive(active) {
    rulerActive = active;
    measurementRuler.classList.toggle("is-active", active);
    measurementRuler.setAttribute("aria-pressed", String(active));
    chartElement.classList.toggle("is-measuring", active);
    if (!active) clearRulerMeasurement();
  }

  function updateRulerFromMouseEvent(event) {
    const bounds = chartElement.getBoundingClientRect();
    const point = rulerPointAt(event.clientX - bounds.left, event.clientY - bounds.top);
    if (!point || !rulerMeasurement) return;
    rulerMeasurement.end = point;
    renderRuler();
  }

  function stopRulerDragging() {
    rulerDragging = false;
    if (!rulerScrollOverrideActive) return;
    priceChart.applyOptions({
      handleScroll: { pressedMouseMove: rulerPreviousPressedMouseMove },
    });
    rulerScrollOverrideActive = false;
    rulerPreviousPressedMouseMove = undefined;
  }

  chartElement.addEventListener("mousedown", (event) => {
    if (!rulerActive || event.button !== 0) return;
    const bounds = chartElement.getBoundingClientRect();
    const start = rulerPointAt(event.clientX - bounds.left, event.clientY - bounds.top);
    if (!start) return;
    event.preventDefault();
    event.stopPropagation();
    rulerPreviousPressedMouseMove = priceChart.options().handleScroll.pressedMouseMove;
    rulerScrollOverrideActive = true;
    priceChart.applyOptions({ handleScroll: { pressedMouseMove: false } });
    preserveUserViewport();
    rulerDragging = true;
    rulerMeasurement = { start, end: start };
    renderRuler();
  });
  chartElement.addEventListener("mousemove", (event) => {
    if (!rulerDragging) return;
    event.preventDefault();
    updateRulerFromMouseEvent(event);
  });
  window.addEventListener("mousemove", (event) => {
    if (rulerDragging) updateRulerFromMouseEvent(event);
  });
  window.addEventListener("mouseup", () => {
    stopRulerDragging();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && (rulerActive || rulerMeasurement)) {
      setRulerActive(false);
    }
  });

  function renderChart() {
    const candlesByTime = new Map(candles.map((candle) => [candle.time, candle]));
    const openInterestByTime = new Map(openInterest.map((point) => [point.time, point]));
    synchronizingRange = true;
    try {
      volumeSeries.setData(volume.map((point) => ({
        ...point,
        color: candlesByTime.get(point.time)?.close >= candlesByTime.get(point.time)?.open
          ? "#2fc28b" : "#ed6a6a",
      })));
      openInterestSeries.setData(candles.map(
        (candle) => openInterestByTime.get(candle.time) ?? { time: candle.time },
      ));
      renderOpenInterestAvailability();
      priceSeries.applyOptions({ priceFormat: priceFormatFor(candles) });
      priceSeries.setData(candles);
      renderMarkers();
    } finally {
      synchronizingRange = false;
    }
    synchronizeRange(priceChart, priceChart.timeScale().getVisibleLogicalRange());
    renderHistoryBoundary();
    renderRuler();
  }

  function renderMarkers() {
    const markerGroups = markerGroupsByType(markers);
    renderedMarkerGroups = markerGroups;
    markerGroupsById = new Map(
      markerGroups.map((group) => [markerIdForGroup(group), group]),
    );
    priceSeries.setMarkers(markerGroups.map(markerForGroup));
    renderSelectedMarkerHighlight();
  }

  function renderHistoryBoundary() {
    if (!historyBoundary) {
      historyBoundaryElement.hidden = true;
      return;
    }
    const boundaryIndex = candles.findIndex(
      (candle) => candle.time === historyBoundary.time,
    );
    const visibleRange = priceChart.timeScale().getVisibleLogicalRange();
    if (
      boundaryIndex === -1 || !visibleRange
      || boundaryIndex < Math.floor(visibleRange.from)
      || boundaryIndex > Math.ceil(visibleRange.to)
    ) {
      historyBoundaryElement.hidden = true;
      return;
    }
    const coordinate = priceChart.timeScale().timeToCoordinate(historyBoundary.time);
    if (coordinate === null) {
      historyBoundaryElement.hidden = true;
      return;
    }
    // A candle on the first visible bar can have a slightly negative center.
    // Keep the marker in the chart instead of hiding it at that important edge.
    const x = Math.min(Math.max(coordinate, 0), chartElement.clientWidth - 1);
    historyBoundaryLabel.textContent = historyBoundary.label;
    historyBoundaryElement.style.left = `${x}px`;
    if (x > chartElement.clientWidth / 2) {
      historyBoundaryLabel.style.left = "auto";
      historyBoundaryLabel.style.right = "5px";
    } else {
      historyBoundaryLabel.style.left = "5px";
      historyBoundaryLabel.style.right = "auto";
    }
    historyBoundaryElement.hidden = false;
  }

  function priceFormatFor(chartCandles) {
    const prices = chartCandles.flatMap((candle) => [
      candle.open, candle.high, candle.low, candle.close,
    ]).filter((price) => price > 0);
    const referencePrice = prices.length ? Math.max(...prices) : 1;
    const precision = Math.max(
      0,
      Math.min(8, 3 - Math.floor(Math.log10(referencePrice))),
    );
    return { type: "price", precision, minMove: 10 ** -precision };
  }

  async function fetchChart(timeframe, before, after) {
    const url = new URL("/api/chart", window.location.origin);
    url.searchParams.set("symbol", chartElement.dataset.symbol);
    url.searchParams.set("timeframe", timeframe);
    url.searchParams.set("filter", localFilterValue());
    url.searchParams.set("market_source", activeMarketDataSource);
    if (before !== undefined) url.searchParams.set("before", before);
    if (after !== undefined) url.searchParams.set("after", after);
    try {
      const response = await fetch(url);
      if (!response.ok) {
        let message = `Chart data is unavailable (HTTP ${response.status}).`;
        try {
          const payload = await response.json();
          if (typeof payload.detail === "string") message = payload.detail;
        } catch (_) {
          // Keep the HTTP status message when the server response is not JSON.
        }
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return response.json();
    } catch (error) {
      if (error instanceof Error) throw error;
      throw new Error("Chart data is unavailable.");
    }
  }

  function hideChartError() {
    chartError.hidden = true;
    chartError.classList.remove("is-nonblocking");
    chartErrorMessage.textContent = "";
    retryChartRequest = null;
  }

  function showChartError(error, retryRequest, nonBlocking = false) {
    chartErrorMessage.textContent = error.message;
    retryChartRequest = retryRequest;
    chartError.classList.toggle("is-nonblocking", nonBlocking);
    chartError.hidden = false;
  }

  function isHyperliquidHistoryUnavailable(error, timeframe) {
    return error?.status === 410
      && activeMarketDataSource === "hyperliquid_api"
      && activeTimeframe === timeframe;
  }

  function showHyperliquidHistoryUnavailable(timeframe) {
    hideChartError();
    hyperliquidHistoryMessage.textContent = `Hyperliquid does not provide ${timeframe} candles for the selected date. Try a higher timeframe.`;
    if (!hyperliquidHistoryDialog.open) hyperliquidHistoryDialog.showModal();
  }

  function dismissHyperliquidHistoryUnavailable() {
    if (hyperliquidHistoryDialog.open) hyperliquidHistoryDialog.close();
  }

  function clearChartData() {
    suppressCrosshairSynchronization = true;
    priceChart.clearCrosshairPosition();
    volumeChart.clearCrosshairPosition();
    openInterestChart.clearCrosshairPosition();
    candles = [];
    volume = [];
    openInterest = [];
    openInterestStatus = null;
    markers = [];
    historyBoundary = null;
    renderChart();
    emptyChart.hidden = true;
  }

  function preserveUserViewport() {
    viewportFollowsLive = false;
  }

  function positionLatestCandleViewport() {
    const latest = candles[candles.length - 1];
    const seconds = timeframeSeconds[activeTimeframe];
    if (!latest || !Number.isFinite(seconds)) return;
    priceChart.timeScale().setVisibleRange({
      from: latest.time - seconds * latestViewportBars,
      to: latest.time + seconds * latestViewportRightMarginBars,
    });
  }

  chartElement.addEventListener("wheel", preserveUserViewport, { passive: true });
  chartElement.addEventListener("pointerdown", (event) => {
    if (rulerActive) return;
    if (event.button === 0) {
      chartPointerDownAt = { x: event.clientX, y: event.clientY };
    }
  });
  chartElement.addEventListener("pointermove", (event) => {
    if (rulerActive) return;
    if (!chartPointerDownAt) return;
    if (Math.hypot(
      event.clientX - chartPointerDownAt.x,
      event.clientY - chartPointerDownAt.y,
    ) >= 3) {
      preserveUserViewport();
      chartPointerDownAt = null;
    }
  });
  window.addEventListener("pointerup", () => {
    chartPointerDownAt = null;
  });

  async function loadChart(timeframe, before, focusTime, visibleRange, options = {}) {
    const { preserveExisting = false, requiredDate } = options;
    const loadVersion = ++chartLoadVersion;
    activeTimeframe = timeframe;
    renderTimeframeControls();
    lazyLoadingEnabled = false;
    hasOlder = false;
    hasNewer = false;
    loadingOlder = false;
    loadingNewer = false;
    historicalWindow = before !== undefined;
    if (visibleRange === undefined && focusTime === undefined) {
      viewportFollowsLive = true;
    }
    const retryRequest = { operation: "load", timeframe, before, focusTime, visibleRange };
    hideChartError();
    let data;
    try {
      data = await fetchChart(timeframe, before);
    } catch (error) {
      if (chartLoadVersion === loadVersion) {
        if (isHyperliquidHistoryUnavailable(error, timeframe)) {
          showHyperliquidHistoryUnavailable(timeframe);
        } else if (!preserveExisting) {
          clearChartData();
          showChartError(error, retryRequest);
        }
      }
      if (isHyperliquidHistoryUnavailable(error, timeframe)) return "retention";
      return preserveExisting ? "error" : false;
    }
    if (!data || activeTimeframe !== timeframe || chartLoadVersion !== loadVersion) {
      return preserveExisting ? "error" : false;
    }
    if (
      requiredDate !== undefined
      && !data.candles.some((candle) => (
        candle.time >= requiredDate && candle.time < requiredDate + 86400
      ))
    ) return "empty";
    candles = data.candles;
    volume = data.volume;
    openInterest = data.openInterest;
    openInterestStatus = data.openInterestStatus ?? null;
    markers = data.markers;
    historyBoundary = data.historyBoundary;
    hasOlder = data.hasOlder;
    hasNewer = data.hasNewer;
    hideChartError();
    renderChart();
    suppressCrosshairSynchronization = false;
    emptyChart.hidden = candles.length !== 0;
    priceChart.timeScale().fitContent();
    if (visibleRange === undefined && focusTime === undefined) {
      priceChart.timeScale().scrollToRealTime();
      positionLatestCandleViewport();
    }
    restoreVisibleTimeRange(visibleRange, focusTime);
    window.setTimeout(() => {
      if (chartLoadVersion === loadVersion) lazyLoadingEnabled = true;
    }, 0);
    return true;
  }

  async function refreshChart() {
    const timeframe = activeTimeframe;
    const loadVersion = chartLoadVersion;
    if (historicalWindow) return;
    const timeScale = priceChart.timeScale();
    const visibleLogicalRange = timeScale.getVisibleLogicalRange();
    const wasAtLiveEdge = Math.abs(timeScale.scrollPosition()) < 0.5;
    const candleCount = candles.length;
    let data;
    try {
      data = await fetchChart(timeframe);
    } catch (error) {
      if (activeTimeframe === timeframe && chartLoadVersion === loadVersion) {
        clearChartData();
        showChartError(error, { operation: "refresh", timeframe });
      }
      return;
    }
    if (
      !data || activeTimeframe !== timeframe || chartLoadVersion !== loadVersion
      || historicalWindow
    ) return;
    const candlesByTime = new Map(candles.map((candle) => [candle.time, candle]));
    for (const candle of data.candles) candlesByTime.set(candle.time, candle);
    candles = [...candlesByTime.values()].sort((left, right) => left.time - right.time);
    volume = mergeByTime(volume, data.volume);
    openInterest = data.openInterestStatus?.status === "unavailable"
      ? []
      : mergeByTime(openInterest, data.openInterest);
    openInterestStatus = data.openInterestStatus ?? openInterestStatus;
    const markersByEvent = new Map(markers.map((event) => [markerIdentity(event), event]));
    for (const event of data.markers) markersByEvent.set(markerIdentity(event), event);
    markers = [...markersByEvent.values()].sort((left, right) => left.time - right.time);
    hasOlder ||= data.hasOlder;
    hasNewer = data.hasNewer;
    historyBoundary = data.historyBoundary ?? historyBoundary;
    hideChartError();
    renderChart();
    suppressCrosshairSynchronization = false;
    emptyChart.hidden = candles.length !== 0;
    if (!visibleLogicalRange) return;

    const appendedCandles = candles.length - candleCount;
    if (wasAtLiveEdge && viewportFollowsLive) {
      timeScale.setVisibleLogicalRange({
        from: visibleLogicalRange.from + appendedCandles,
        to: visibleLogicalRange.to + appendedCandles,
      });
      return;
    }
    timeScale.setVisibleLogicalRange(visibleLogicalRange);
  }

  function eventCardModel(event) {
    const receivedType = event.event_type ?? event.eventType;
    const signalType = signalTypeFor(receivedType);
    const eventType = signalType?.event_type ?? receivedType;
    const timestamp = event.event_timestamp ?? event.timestamp;
    const eventId = event.event_id ?? event.eventId ?? timestamp;
    return {
      symbol: event.symbol ?? chartElement.dataset.symbol,
      eventId,
      timestamp,
      eventType,
      displayType: event.type ?? signalType?.short_label ?? eventType,
      price: event.event_price ?? event.price,
      eventAt: displayTimestamp(timestamp),
      source_interval: event.source_interval ?? event.sourceInterval,
      alert_level: event.alert_level ?? event.alertLevel,
      direction: event.direction,
      exchange: event.exchange,
      primarySignalId: event.primary_signal_id ?? event.primarySignalId,
      metadata: event.metadata ?? {},
      compactFields: event.compact_fields ?? event.compactFields ?? [],
      detailFields: event.detail_fields ?? event.detailFields ?? [],
      displayName: signalType?.display_name ?? event.type ?? signalType?.short_label ?? eventType,
      color: signalType?.color,
      definition: signalType ? {
        tone: signalType.event_card.tone,
        collapsedFields: signalType.event_card.collapsed_fields
          ?? defaultCollapsedEventCardFields(eventType),
      } : {
        tone: event.tone ?? "default",
        collapsedFields: defaultCollapsedEventCardFields(eventType),
      },
    };
  }

  function compactLayoutField(model, descriptor) {
    const [namespace, name] = descriptor.split(/:(.*)/s);
    const standard = {
      event_price: model.price,
      exchange: model.exchange,
      symbol: model.symbol,
      primary_signal_id: model.primarySignalId,
      source_interval: model.source_interval,
      alert_level: model.alert_level,
      direction: model.direction,
    };
    const value = namespace === "metadata" ? model.metadata[name] : standard[name];
    return [name.replaceAll("_", " "), value];
  }

  function compactLayoutValue(value) {
    if (value === undefined || value === null || String(value).trim() === "") return "—";
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }

  function appendLayoutFields(container, model, descriptors) {
    for (const descriptor of descriptors) {
      const [labelText, rawValue] = compactLayoutField(model, descriptor);
      const field = document.createElement("div");
      const label = document.createElement("dt");
      label.textContent = labelText;
      label.title = labelText;
      const value = document.createElement("dd");
      const valueText = compactLayoutValue(rawValue);
      value.textContent = valueText;
      value.title = valueText;
      field.append(label, value);
      container.append(field);
    }
  }

  function createConfiguredCompactCard(card, model, header, time, type) {
    card.classList.add("event-card-layout-configured");
    header.classList.add("event-card-configured-header");
    type.textContent = model.displayName;
    if (model.color) type.style.color = model.color;
    header.append(time, type);
    const detailButton = document.createElement("button");
    detailButton.className = "event-card-detail-open";
    detailButton.type = "button";
    detailButton.setAttribute("aria-label", `Open ${model.displayName} details`);
    detailButton.textContent = "›";
    header.append(detailButton);
    card.append(header);
    if (!model.compactFields.length) return card;
    const fields = document.createElement("dl");
    fields.className = "event-card-fields event-card-configured-fields";
    appendLayoutFields(fields, model, model.compactFields);
    card.append(fields);
    return card;
  }

  function createEventCard(event) {
    const model = eventCardModel(event);
    const card = document.createElement("article");
    card.className = `event-card event-card-${model.definition.tone}`;
    card.setAttribute("role", "listitem");
    card.tabIndex = 0;
    card.dataset.eventId = model.eventId;
    card.dataset.eventAt = model.timestamp;
    card.dataset.eventType = model.eventType;
    card.classList.toggle("is-focused", String(model.eventId) === selectedEventId);
    setEventCardExpanded(card, expandedEventIds.has(String(model.eventId)));

    const header = document.createElement("div");
    header.className = "event-card-header";
    const time = document.createElement("time");
    time.textContent = model.eventAt ?? "—";
    const type = document.createElement("span");
    type.className = `event-type event-type-${model.definition.tone}`;
    type.textContent = model.displayType;
    return createConfiguredCompactCard(card, model, header, time, type);
  }

  refreshEventCards([...eventCards.querySelectorAll(".event-card")].map((card) => ({
    event_id: card.dataset.eventId,
    event_type: card.dataset.eventType,
    event_timestamp: card.dataset.eventAt,
    event_at: card.dataset.eventDisplayAt,
    event_price: card.dataset.eventPrice,
    source_interval: card.dataset.sourceInterval,
    alert_level: card.dataset.alertLevel,
    direction: card.dataset.direction,
    exchange: card.dataset.exchange,
    primary_signal_id: card.dataset.primarySignalId,
    metadata: JSON.parse(card.dataset.metadata || "{}"),
    compact_fields: JSON.parse(card.dataset.compactFields || "null"),
    detail_fields: JSON.parse(card.dataset.detailFields || "null"),
  })));

  function renderEventList({ restoreScroll = false } = {}) {
    eventCards.setAttribute("role", "list");
    const fragment = document.createDocumentFragment();
    for (const event of eventRows) fragment.append(createEventCard(event));
    eventCards.replaceChildren(fragment);
    if (restoreScroll) eventCards.scrollTop = eventListScrollTop;
  }

  function eventFieldDescriptors(model) {
    const standard = {
      event_price: model.price,
      exchange: model.exchange,
      symbol: model.symbol,
      primary_signal_id: model.primarySignalId,
      source_interval: model.source_interval,
      alert_level: model.alert_level,
      direction: model.direction,
    };
    return [
      ...Object.entries(standard)
        .filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "")
        .map(([name]) => `standard:${name}`),
      ...Object.keys(model.metadata).map((name) => `metadata:${name}`),
    ];
  }

  function createDetailSection(model, descriptors) {
    const section = document.createElement("section");
    const fields = document.createElement("dl");
    fields.className = "event-card-fields event-card-configured-fields";
    appendLayoutFields(fields, model, descriptors);
    section.append(fields);
    return section;
  }

  function renderEventDetail(event) {
    const model = eventCardModel(event);
    eventCards.removeAttribute("role");
    const detail = document.createElement("article");
    detail.className = "event-card-detail";
    const header = document.createElement("header");
    const back = document.createElement("button");
    back.className = "event-card-detail-back";
    back.type = "button";
    back.textContent = "← Back";
    const title = document.createElement("h3");
    title.textContent = model.displayName;
    title.title = model.displayName;
    if (model.color) title.style.color = model.color;
    const time = document.createElement("time");
    time.textContent = model.eventAt ?? "—";
    const detailSummary = document.createElement("div");
    detailSummary.className = "event-card-detail-summary";
    detailSummary.append(title, time);
    header.append(back, detailSummary);
    detail.append(header);
    if (model.compactFields.length) {
      detail.append(createDetailSection(model, model.compactFields));
    }
    if (model.detailFields.length) {
      if (model.compactFields.length) {
        const divider = document.createElement("div");
        divider.className = "event-card-detail-divider";
        detail.append(divider);
      }
      detail.append(createDetailSection(model, model.detailFields));
    }
    const selected = new Set([...model.compactFields, ...model.detailFields]);
    const technical = eventFieldDescriptors(model).filter((field) => !selected.has(field));
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Technical details";
    details.append(summary);
    if (technical.length) {
      const fields = document.createElement("dl");
      fields.className = "event-card-fields event-card-configured-fields";
      appendLayoutFields(fields, model, technical);
      details.append(fields);
    } else {
      const empty = document.createElement("p");
      empty.textContent = "No remaining event fields.";
      details.append(empty);
    }
    detail.append(details);
    eventCards.replaceChildren(detail);
  }

  function openEventDetail(event) {
    eventListScrollTop = eventCards.scrollTop;
    detailedEventId = String(event.event_id ?? event.eventId);
    renderEventDetail(event);
  }

  function closeEventDetail() {
    detailedEventId = null;
    renderEventList({ restoreScroll: true });
  }

  function showCompactEventList() {
    if (detailedEventId === null) return;
    detailedEventId = null;
    renderEventList();
  }

  function refreshEventCards(events) {
    eventRows = events;
    if (detailedEventId !== null) {
      const detailEvent = events.find((event) => String(event.event_id ?? event.eventId) === detailedEventId);
      if (detailEvent) renderEventDetail(detailEvent);
      else closeEventDetail();
      return;
    }
    renderEventList();
  }

  function markersForEvents(events) {
    const seconds = timeframeSeconds[activeTimeframe];
    const candleTimes = new Set(candles.map((candle) => candle.time));
    return events.flatMap((event) => {
      const timestamp = Math.floor(new Date(event.event_timestamp).getTime() / 1000);
      const time = Math.floor(timestamp / seconds) * seconds;
      if (!candleTimes.has(time)) return [];
      return [{
        time,
        type: event.type,
        eventId: event.event_id,
        eventType: event.event_type,
        eventAt: event.event_timestamp,
        eventPrice: event.event_price,
        sourceInterval: event.source_interval,
        alertLevel: event.alert_level,
      }];
    });
  }

  async function refreshLocalEvents() {
    const url = new URL("/api/dashboard", window.location.origin);
    url.searchParams.set("symbol", chartElement.dataset.symbol);
    url.searchParams.set("filter", activeFilter());
    url.searchParams.set("local_filter", localFilterValue());
    const response = await fetch(url);
    if (!response.ok) return;
    const data = await response.json();
    refreshEventCards(data.events);
    return data.events;
  }

  async function applyLocalFilter() {
    renderLocalFilterButtons();
    const events = await refreshLocalEvents();
    if (!events) return;
    markers = markersForEvents(events);
    renderMarkers();
    closeMarkerEventList();
  }

  async function refreshDashboard() {
    if (refreshing) return;
    refreshing = true;
    const sidebarScrollTop = symbolList.scrollTop;
    const mainScrollTop = main.scrollTop;
    try {
      const url = new URL("/api/dashboard", window.location.origin);
      url.searchParams.set("symbol", chartElement.dataset.symbol);
      url.searchParams.set("filter", activeFilter());
      url.searchParams.set("local_filter", localFilterValue());
      const response = await fetch(url);
      if (!response.ok) return;
      const data = await response.json();
      updateAvailableFilters(data);
      const localFilterChanged = normalizeLocalFilterOverride();
      renderLocalFilterButtons();
      refreshSidebar(data.symbols, chartElement.dataset.symbol, sidebarScrollTop);
      const localEvents = localFilterChanged
        ? await refreshLocalEvents() ?? data.events
        : data.events;
      refreshEventCards(localEvents);
      main.scrollTop = mainScrollTop;
      await refreshChart();
    } catch (_) {
      // Keep the current UI intact and retry on the next polling cycle.
    } finally {
      refreshing = false;
    }
  }

  async function loadOlderCandles(before = candles[0]?.time) {
    if (loadingOlder || !hasOlder || candles.length === 0) return;
    loadingOlder = true;
    const timeframe = activeTimeframe;
    const loadVersion = chartLoadVersion;
    const oldest = before;
    const visibleLogicalRange = priceChart.timeScale().getVisibleLogicalRange();
    const candleCount = candles.length;
    try {
      const data = await fetchChart(timeframe, oldest);
      if (
        !data || activeTimeframe !== timeframe || chartLoadVersion !== loadVersion
      ) return;
      candles = mergeByTime(data.candles, candles);
      volume = mergeByTime(data.volume, volume);
      openInterest = data.openInterestStatus?.status === "unavailable"
        ? []
        : mergeByTime(data.openInterest, openInterest);
      openInterestStatus = data.openInterestStatus ?? openInterestStatus;
      markers = mergeMarkers(data.markers, markers);
      historyBoundary = data.historyBoundary ?? historyBoundary;
      hasOlder = data.hasOlder;
      hideChartError();
      renderChart();
      suppressCrosshairSynchronization = false;
      if (visibleLogicalRange) {
        const prependedCandles = candles.length - candleCount;
        priceChart.timeScale().setVisibleLogicalRange({
          from: visibleLogicalRange.from + prependedCandles,
          to: visibleLogicalRange.to + prependedCandles,
        });
      }
    } catch (error) {
      if (chartLoadVersion === loadVersion) {
        if (isHyperliquidHistoryUnavailable(error, timeframe)) {
          showHyperliquidHistoryUnavailable(timeframe);
        } else {
          showChartError(
            error, { operation: "older", timeframe, before: oldest }, true,
          );
        }
      }
    } finally {
      if (chartLoadVersion === loadVersion) loadingOlder = false;
    }
  }

  async function loadNewerCandles(after = candles[candles.length - 1]?.time) {
    if (loadingNewer || !hasNewer || candles.length === 0) return;
    loadingNewer = true;
    const timeframe = activeTimeframe;
    const loadVersion = chartLoadVersion;
    const newest = after;
    const visibleLogicalRange = priceChart.timeScale().getVisibleLogicalRange();
    const candleCount = candles.length;
    try {
      const data = await fetchChart(timeframe, undefined, newest);
      if (
        !data || activeTimeframe !== timeframe || chartLoadVersion !== loadVersion
      ) return;
      candles = mergeByTime(candles, data.candles);
      volume = mergeByTime(volume, data.volume);
      openInterest = mergeByTime(openInterest, data.openInterest);
      markers = mergeMarkers(markers, data.markers);
      historyBoundary = data.historyBoundary ?? historyBoundary;
      hasOlder ||= data.hasOlder;
      // An empty page must terminate this cursor even if a sparse source
      // advertised a later raw observation outside the bounded page.
      hasNewer = candles.length > candleCount && data.hasNewer;
      hideChartError();
      renderChart();
      suppressCrosshairSynchronization = false;
      if (visibleLogicalRange) {
        priceChart.timeScale().setVisibleLogicalRange(visibleLogicalRange);
      }
    } catch (error) {
      if (chartLoadVersion === loadVersion) {
        if (isHyperliquidHistoryUnavailable(error, timeframe)) {
          showHyperliquidHistoryUnavailable(timeframe);
        } else {
          showChartError(
            error, { operation: "newer", timeframe, after: newest }, true,
          );
        }
      }
    } finally {
      if (chartLoadVersion === loadVersion) loadingNewer = false;
    }
  }

  function focusNearestCandle(timestamp) {
    if (candles.length === 0) return;
    preserveUserViewport();
    const nearest = candles.reduce((closest, candle) => (
      Math.abs(candle.time - timestamp) < Math.abs(closest.time - timestamp)
        ? candle
        : closest
    ));
    const seconds = timeframeSeconds[activeTimeframe];
    priceChart.timeScale().setVisibleRange({
      from: nearest.time - seconds * 24,
      to: nearest.time + seconds * 24,
    });
  }

  function visibleTimeRange() {
    const range = priceChart.timeScale().getVisibleRange();
    return range && Number.isFinite(range.from) && Number.isFinite(range.to)
      ? range
      : undefined;
  }

  function restoreVisibleTimeRange(visibleRange, fallbackTime) {
    if (candles.length === 0) return;
    if (visibleRange) {
      const first = candles[0].time;
      const last = candles[candles.length - 1].time;
      const duration = Math.min(visibleRange.to - visibleRange.from, last - first);
      if (Number.isFinite(duration) && duration > 0) {
        const from = Math.min(Math.max(visibleRange.from, first), last - duration);
        const to = from + duration;
        priceChart.timeScale().setVisibleRange({ from, to });
        return;
      }
    }
    if (fallbackTime !== undefined) focusNearestCandle(fallbackTime);
  }

  function parseDateJump(value) {
    const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value);
    if (!match) return;
    const [, dayText, monthText, yearText] = match;
    const year = Number(yearText);
    const month = Number(monthText);
    const day = Number(dayText);
    if (year < 1 || month < 1 || month > 12 || day < 1) return;
    const date = new Date(0);
    date.setUTCFullYear(year, month - 1, day);
    date.setUTCHours(0, 0, 0, 0);
    if (
      date.getUTCFullYear() !== year ||
      date.getUTCMonth() !== month - 1 ||
      date.getUTCDate() !== day
    ) return;
    return {
      timestamp: date.getTime() / 1000,
      pickerValue: `${yearText}-${monthText}-${dayText}`,
    };
  }

  const dateSegments = [chartDateDay, chartDateMonth, chartDateYear];

  function dateSegmentsValue() {
    return `${chartDateDay.value}.${chartDateMonth.value}.${chartDateYear.value}`;
  }

  function setDateSegments(value) {
    const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value);
    if (!match) return false;
    const [, day, month, year] = match;
    chartDateDay.value = day;
    chartDateMonth.value = month;
    chartDateYear.value = year;
    return true;
  }

  function normalizeDateSegment(input) {
    input.value = input.value.replace(/\D/g, "").slice(0, Number(input.maxLength));
  }

  function dateJumpState() {
    return {
      hasOlder,
      hasNewer,
      historicalWindow,
      lazyLoadingEnabled,
      visibleRange: visibleTimeRange(),
      viewportFollowsLive,
    };
  }

  function restoreDateJumpState(state) {
    hasOlder = state.hasOlder;
    hasNewer = state.hasNewer;
    historicalWindow = state.historicalWindow;
    lazyLoadingEnabled = state.lazyLoadingEnabled;
    viewportFollowsLive = state.viewportFollowsLive;
    if (state.visibleRange) priceChart.timeScale().setVisibleRange(state.visibleRange);
  }

  function targetForTimeframe(timeframe, target) {
    if (["1m", "3m", "5m", "15m", "30m"].includes(timeframe)) {
      // Intraday charts are deliberately limited to detailed minute history.
      return Math.max(target, detailedHistoryBoundary);
    }
    return target;
  }

  function beforeForTimeframe(timeframe, target) {
    const seconds = timeframeSeconds[timeframe];
    const now = Date.now() / 1000;
    return target <= now && now < target + 86400
      ? undefined
      : target + seconds * dateJumpWindowBars;
  }

  function visibleTimeAnchor(range = visibleTimeRange()) {
    if (!range || candles.length === 0) return;
    const midpoint = (range.from + range.to) / 2;
    return candles.reduce((closest, candle) => (
      Math.abs(candle.time - midpoint) < Math.abs(closest.time - midpoint)
        ? candle
        : closest
    )).time;
  }

  function isViewingLatestCandles() {
    if (candles.length === 0) return false;
    const range = visibleTimeRange();
    if (!range) return false;
    const latest = candles[candles.length - 1].time;
    return range.to >= latest - timeframeSeconds[activeTimeframe];
  }

  function loadTimeframeAtVisibleRange(timeframe) {
    const range = visibleTimeRange();
    if (isViewingLatestCandles()) {
      loadChart(timeframe);
      return;
    }
    const anchor = visibleTimeAnchor(range);
    if (anchor === undefined) {
      loadChart(timeframe);
      return;
    }
    preserveUserViewport();
    const target = targetForTimeframe(timeframe, anchor);
    loadChart(timeframe, beforeForTimeframe(timeframe, target), target, range);
  }

  async function jumpToDate(parsedDate) {
    if (parsedDate === undefined) return false;
    const state = dateJumpState();
    preserveUserViewport();
    const target = targetForTimeframe(activeTimeframe, parsedDate.timestamp);
    const result = await loadChart(
      activeTimeframe,
      beforeForTimeframe(activeTimeframe, target),
      target,
      undefined,
      { preserveExisting: true, requiredDate: parsedDate.timestamp },
    );
    if (result === true) return true;
    restoreDateJumpState(state);
    if (result !== "retention") {
      setDateJumpFeedback(
        result === "empty"
          ? "No candles available for this date."
          : "Could not load candles. Try again.",
      );
    }
    return false;
  }

  function setDateJumpFeedback(message = "", invalid = false) {
    chartDateStatus.textContent = message;
    chartDateStatus.hidden = !message;
    chartDateStatus.classList.toggle("is-invalid", invalid);
    chartDatePickerElement.classList.toggle("is-invalid", invalid);
    for (const input of dateSegments) {
      input.toggleAttribute("aria-invalid", invalid);
    }
  }

  const flatpickrMonths = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];

  function updateFlatpickrNavigator(instance) {
    const navigator = instance.dateJumpNavigator;
    if (!navigator) return;
    navigator.month.value = String(instance.currentMonth);
    navigator.year.value = String(instance.currentYear);
  }

  function addFlatpickrNavigator(instance) {
    const controls = document.createElement("div");
    controls.className = "flatpickr-date-jump-nav";
    const month = document.createElement("select");
    month.setAttribute("aria-label", "Calendar month");
    flatpickrMonths.forEach((name, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = name;
      month.append(option);
    });
    const year = document.createElement("input");
    year.type = "number";
    year.min = "1";
    year.max = "9999";
    year.step = "1";
    year.inputMode = "numeric";
    year.setAttribute("aria-label", "Calendar year");
    const jump = () => {
      const nextYear = Number(year.value);
      const nextMonth = Number(month.value);
      if (!Number.isInteger(nextYear) || nextYear < 1 || nextYear > 9999) return;
      instance.jumpToDate(new Date(nextYear, nextMonth, 1));
    };
    month.addEventListener("change", jump);
    year.addEventListener("change", jump);
    year.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      jump();
    });
    controls.append(month, year);
    instance.calendarContainer.prepend(controls);
    instance.dateJumpNavigator = { month, year };
    updateFlatpickrNavigator(instance);
  }

  const chartDatePicker = flatpickr("#chart-date-picker", {
    allowInput: false,
    dateFormat: "d.m.Y",
    disableMobile: true,
    locale: "default",
    monthSelectorType: "static",
    onChange: (_, value) => {
      if (setDateSegments(value)) setDateJumpFeedback();
    },
    onMonthChange: (_, __, instance) => updateFlatpickrNavigator(instance),
    onReady: (_, __, instance) => addFlatpickrNavigator(instance),
    onYearChange: (_, __, instance) => updateFlatpickrNavigator(instance),
    position: "auto right",
    positionElement: chartDatePickerElement,
    wrap: true,
  });

  async function submitDateJump() {
    const value = dateSegmentsValue();
    const parsedDate = parseDateJump(value);
    if (parsedDate === undefined) {
      setDateJumpFeedback("Enter a valid date as DD.MM.YYYY.", true);
      return;
    }
    setDateJumpFeedback();
    chartDatePicker.setDate(value, false, "d.m.Y");
    if (await jumpToDate(parsedDate)) setDateJumpFeedback();
  }

  function mergeByTime(existing, incoming) {
    const points = new Map(existing.map((point) => [point.time, point]));
    for (const point of incoming) points.set(point.time, point);
    return [...points.values()].sort((left, right) => left.time - right.time);
  }

  function mergeMarkers(existing, incoming) {
    const points = new Map(existing.map((point) => [markerIdentity(point), point]));
    for (const point of incoming) points.set(markerIdentity(point), point);
    return [...points.values()].sort((left, right) => left.time - right.time);
  }

  function markerIdentity(event) {
    return String(event.eventId ?? event.eventAt);
  }

  function synchronizeRange(source, range) {
    if (synchronizingRange || !range) return;
    // The price chart is the shared viewport authority. Lightweight Charts can
    // publish independent provisional ranges for companion panels after data
    // or scale changes; bring those panels back to price instead of moving it.
    const priceRange = priceChart.timeScale().getVisibleLogicalRange();
    const authoritativeRange = source === priceChart ? range : priceRange;
    if (!authoritativeRange) return;
    synchronizingRange = true;
    try {
      for (const chart of [priceChart, volumeChart, openInterestChart]) {
        if (chart !== priceChart) {
          chart.timeScale().setVisibleLogicalRange(authoritativeRange);
        }
      }
    } finally {
      synchronizingRange = false;
    }
  }

  function loadOlderWhenAtLeft() {
    const range = priceChart.timeScale().getVisibleLogicalRange();
    if (lazyLoadingEnabled && range && range.from < 30) loadOlderCandles();
  }

  function loadCandlesWhenNearEdge() {
    loadOlderWhenAtLeft();
    const range = priceChart.timeScale().getVisibleLogicalRange();
    if (
      lazyLoadingEnabled && range
      && range.to > candles.length - 30
    ) loadNewerCandles();
  }

  priceChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    synchronizeRange(priceChart, range);
    renderHistoryBoundary();
    renderRuler();
    renderSelectedMarkerHighlight();
    loadCandlesWhenNearEdge();
  });
  volumeChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    synchronizeRange(volumeChart, range);
    loadCandlesWhenNearEdge();
  });
  openInterestChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    synchronizeRange(openInterestChart, range);
    loadCandlesWhenNearEdge();
  });

  function synchronizeCrosshair(source, param) {
    if (synchronizingCrosshair || suppressCrosshairSynchronization) return;
    synchronizingCrosshair = true;
    for (const [chart, series, points] of [
      [priceChart, priceSeries, new Map(candles.map((candle) => [candle.time, candle.close]))],
      [volumeChart, volumeSeries, new Map(volume.map((point) => [point.time, point.value]))],
      [openInterestChart, openInterestSeries, new Map(openInterest.map((point) => [point.time, point.value]))],
    ]) {
      if (chart === source) continue;
      const value = points.get(param.time);
      if (!param.point || param.time == null || !Number.isFinite(value)) {
        chart.clearCrosshairPosition();
      } else {
        chart.setCrosshairPosition(value, param.time, series);
      }
    }
    synchronizingCrosshair = false;
  }

  priceChart.subscribeCrosshairMove((param) => {
    synchronizeCrosshair(priceChart, param);
    if (markerGroupsById.has(param.hoveredObjectId)) {
      showMarkerHoverTooltip(param.hoveredObjectId);
    } else {
      closeMarkerHoverTooltip();
    }
  });
  priceChart.subscribeClick((param) => {
    const groupId = param.hoveredObjectId;
    if (markerGroupsById.has(groupId)) {
      showCompactEventList();
      highlightMarkerGroup(groupId);
      if (selectMarkerGroupEvent(groupId)) closeMarkerEventList();
      else openMarkerEventList(groupId);
    }
    else closeMarkerEventList();
  });
  volumeChart.subscribeCrosshairMove((param) => synchronizeCrosshair(volumeChart, param));
  openInterestChart.subscribeCrosshairMove((param) => synchronizeCrosshair(openInterestChart, param));

  async function focusEvent(eventId, eventAt) {
    selectEventById(eventId, undefined, { highlight: false });
    highlightedMarkerGroupId = null;
    selectedMarkerHighlight.hidden = true;
    const timestamp = Math.floor(new Date(eventAt).getTime() / 1000);
    const seconds = timeframeSeconds[activeTimeframe];
    const candleTime = Math.floor(timestamp / seconds) * seconds;
    preserveUserViewport();
    while (candles.length && candleTime < candles[0].time && hasOlder) {
      await loadOlderCandles();
    }
    if (candles.some((candle) => candle.time === candleTime)) {
      priceChart.timeScale().setVisibleRange({ from: candleTime - seconds * 12, to: candleTime + seconds * 12 });
      window.requestAnimationFrame(() => {
        if (selectedEventId !== String(eventId)) return;
        const markerGroupId = markerGroupIdForEvent(eventId);
        if (markerGroupId) highlightMarkerGroup(markerGroupId);
      });
    }
  }

  eventCards.addEventListener("click", (event) => {
    const detailButton = event.target.closest(".event-card-detail-open");
    if (detailButton) {
      const card = detailButton.closest(".event-card");
      if (card) {
        const selected = eventRows.find(
          (item) => String(item.event_id ?? item.eventId) === card.dataset.eventId,
        );
        if (selected) openEventDetail(selected);
      }
      event.stopPropagation();
      return;
    }
    if (event.target.closest(".event-card-detail-back")) {
      closeEventDetail();
      event.stopPropagation();
      return;
    }
    const card = event.target.closest(".event-card");
    if (!card) return;
    const toggle = event.target.closest(".event-card-toggle");
    if (toggle) {
      event.stopPropagation();
      setEventCardExpanded(card, !card.classList.contains("is-expanded"));
      return;
    }
    focusEvent(card.dataset.eventId, card.dataset.eventAt);
  });
  eventCards.addEventListener("keydown", (event) => {
    const card = event.target.closest(".event-card");
    if (event.target.closest(".event-card-toggle, .event-card-detail-open")) return;
    if (card && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      focusEvent(card.dataset.eventId, card.dataset.eventAt);
    }
  });
  markerEventList.addEventListener("click", (event) => {
    const item = event.target.closest(".marker-event-list-item");
    if (!item) return;
    selectEventById(item.dataset.eventId, item.dataset.markerGroupId, { expandCard: true });
  });
  document.addEventListener("pointerdown", (event) => {
    if (!markerEventList.hidden && !markerEventList.contains(event.target)) {
      closeMarkerEventList();
    }
  });

  localFilterContainer.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    if (button.dataset.action === "use-global") {
      clearLocalFilterOverride();
      applyLocalFilter();
      return;
    }
    const eventTypes = localEventTypes();
    if (eventTypes.has(button.dataset.eventType)) {
      eventTypes.delete(button.dataset.eventType);
    } else {
      eventTypes.add(button.dataset.eventType);
    }
    setLocalFilterOverride(filterValueFor(eventTypes));
    applyLocalFilter();
  });

  timeframeButtons.addEventListener("click", (event) => {
    const button = event.target.closest(".timeframe");
    if (!button) return;
    const timeframe = button.dataset.timeframe;
    localStorage.setItem(timeframeStorageKey, timeframe);
    loadTimeframeAtVisibleRange(timeframe);
  });
  marketDataSource.addEventListener("change", () => {
    const source = marketDataSource.value;
    if (source === activeMarketDataSource) return;
    activeMarketDataSource = source;
    sessionStorage.setItem(marketDataSourceStorageKey, source);
    startMarketSummaryForChartSource(chartElement.dataset.symbol, source);
    loadChart(timeframeForMarketSource(activeTimeframe, source));
  });
  function closeTimeframeSettings() {
    timeframeSettingsPanel.hidden = true;
    timeframeSettings.setAttribute("aria-expanded", "false");
  }
  timeframeSettings.addEventListener("click", () => {
    if (timeframeSettingsPanel.hidden) {
      timeframeSettingsPanel.hidden = false;
      timeframeSettings.setAttribute("aria-expanded", "true");
      return;
    }
    closeTimeframeSettings();
  });
  document.addEventListener("pointerdown", (event) => {
    if (
      !timeframeSettingsPanel.hidden
      && !timeframeSettingsPanel.contains(event.target)
      && !timeframeSettings.contains(event.target)
    ) closeTimeframeSettings();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !timeframeSettingsPanel.hidden) {
      closeTimeframeSettings();
    }
  });
  measurementRuler.addEventListener("click", () => {
    setRulerActive(!rulerActive);
  });
  timeframeSettingsPanel.addEventListener("change", (event) => {
    const input = event.target.closest('input[type="checkbox"]');
    if (!input) return;
    if (input.dataset.chartPanel) {
      const visibility = chartPanelVisibility();
      visibility[input.dataset.chartPanel] = input.checked;
      localStorage.setItem(chartPanelVisibilityStorageKey, JSON.stringify(visibility));
      applyChartPanelVisibility();
      return;
    }
    const visible = new Set(visibleTimeframes());
    if (input.checked) visible.add(input.value);
    else visible.delete(input.value);
    if (visible.size === 0) return;
    const next = timeframeDefinitions.filter((timeframe) => visible.has(timeframe));
    localStorage.setItem(visibleTimeframesStorageKey, JSON.stringify(next));
    if (!visible.has(activeTimeframe)) {
      localStorage.setItem(timeframeStorageKey, next[0]);
      renderTimeframeControls();
      loadTimeframeAtVisibleRange(next[0]);
      return;
    }
    renderTimeframeControls();
  });
  function closeTimezoneMenu() {
    timezoneMenu.hidden = true;
    timezoneButton.setAttribute("aria-expanded", "false");
  }
  function renderTimezoneControl() {
    const now = new Date();
    const timezone = displayTimezone();
    timezoneButton.textContent = `${displayTimestamp(now, { seconds: true }).slice(-8)} ${displayTimezoneOffset(timezone, now)}`;
    const selectedChoice = displayTimezoneChoices.find(
      ({ id }) => id === selectedDisplayTimezone,
    );
    timezoneButton.title = `Display timezone: ${displayTimezoneChoiceLabel(selectedChoice, now)}`;
    const options = document.createDocumentFragment();
    for (const choice of displayTimezoneChoices) {
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "menuitemradio");
      option.setAttribute("aria-checked", String(choice.id === selectedDisplayTimezone));
      option.dataset.timezone = choice.id;
      option.textContent = `${choice.id === selectedDisplayTimezone ? "✓ " : "  "}${displayTimezoneChoiceLabel(choice, now)}`;
      options.append(option);
    }
    timezoneMenu.replaceChildren(options);
  }
  function applyDisplayTimezone() {
    const options = {
      timeScale: { tickMarkFormatter: formatDisplayTickMark },
      localization: { locale: "en-GB", timeFormatter: formatDisplayChartTime },
    };
    for (const chart of [priceChart, volumeChart, openInterestChart]) {
      chart.applyOptions(options);
    }
    refreshSidebarTimestamps();
    refreshEventCards(eventRows);
    renderTimezoneControl();
  }
  timezoneButton.addEventListener("click", () => {
    timezoneMenu.hidden = !timezoneMenu.hidden;
    timezoneButton.setAttribute("aria-expanded", String(!timezoneMenu.hidden));
  });
  timezoneMenu.addEventListener("click", (event) => {
    const option = event.target.closest("button[data-timezone]");
    if (!option) return;
    selectedDisplayTimezone = option.dataset.timezone;
    localStorage.setItem(displayTimezoneStorageKey, selectedDisplayTimezone);
    closeTimezoneMenu();
    applyDisplayTimezone();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!timezoneMenu.hidden && !timezoneControl.contains(event.target)) closeTimezoneMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !timezoneMenu.hidden) closeTimezoneMenu();
  });
  renderTimezoneControl();
  const timezoneClock = window.setInterval(renderTimezoneControl, 1_000);
  window.addEventListener("pagehide", () => window.clearInterval(timezoneClock), { once: true });
  normalizeLocalFilterOverride();
  renderLocalFilterButtons();
  for (const [index, input] of dateSegments.entries()) {
    input.addEventListener("input", () => {
      normalizeDateSegment(input);
      setDateJumpFeedback();
      if (input.value.length === Number(input.maxLength) && index < dateSegments.length - 1) {
        dateSegments[index + 1].focus();
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitDateJump();
        return;
      }
      if (
        event.key === "Backspace" && input.value.length === 0 && index > 0
      ) {
        event.preventDefault();
        const previous = dateSegments[index - 1];
        previous.focus();
        previous.setSelectionRange(previous.value.length, previous.value.length);
        return;
      }
      if (event.key === "ArrowLeft" && input.selectionStart === 0 && index > 0) {
        event.preventDefault();
        dateSegments[index - 1].focus();
        return;
      }
      if (
        event.key === "ArrowRight" && input.selectionStart === input.value.length
        && index < dateSegments.length - 1
      ) {
        event.preventDefault();
        dateSegments[index + 1].focus();
      }
    });
    input.addEventListener("paste", (event) => {
      const pasted = event.clipboardData?.getData("text").trim() ?? "";
      if (!setDateSegments(pasted)) return;
      event.preventDefault();
      setDateJumpFeedback();
      chartDateYear.focus();
    });
  }
  chartDateGo.addEventListener("click", submitDateJump);
  chartRetry.addEventListener("click", () => {
    if (!retryChartRequest) return;
    const request = retryChartRequest;
    if (request.operation === "load") {
      loadChart(request.timeframe, request.before, request.focusTime, request.visibleRange);
    } else if (request.operation === "refresh") {
      refreshChart();
    } else if (request.operation === "older") {
      loadOlderCandles(request.before);
    } else if (request.operation === "newer") {
      loadNewerCandles(request.after);
    }
  });
  hyperliquidHistoryOkay.addEventListener("click", dismissHyperliquidHistoryUnavailable);
  activeMarketDataSource = savedMarketDataSource();
  const timeframe = timeframeForMarketSource(savedTimeframe());
  activeTimeframe = timeframe;
  marketDataSource.value = activeMarketDataSource;
  startMarketSummaryForChartSource(
    chartElement.dataset.symbol, activeMarketDataSource,
  );
  applyChartPanelVisibility();
  renderTimeframeControls();
  loadChart(timeframe);
  if (localFilterOverride() !== null) refreshLocalEvents();
  window.setInterval(refreshDashboard, refreshIntervalMilliseconds);
} else {
  let refreshing = false;
  window.setInterval(async () => {
    if (refreshing) return;
    refreshing = true;
    const sidebarScrollTop = symbolList.scrollTop;
    try {
      const url = new URL("/api/dashboard", window.location.origin);
      url.searchParams.set("filter", activeFilter());
      const response = await fetch(url);
      if (!response.ok) return;
      const data = await response.json();
      updateAvailableFilters(data);
      refreshSidebar(data.symbols, null, sidebarScrollTop);
    } catch (_) {
      // Keep the existing sidebar visible and retry on the next polling cycle.
    } finally {
      refreshing = false;
    }
  }, refreshIntervalMilliseconds);
}
