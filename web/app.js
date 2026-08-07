const PROFILE_FALLBACK = [
  ["minecraft", "Minecraft", "crafty"],
  ["pz-rising", "Project Zomboid", "systemd"],
  ["terraria-vanilla", "Terraria Vanilla", "systemd"],
  ["terraria-tmod", "Terraria tModLoader", "systemd"],
];
const PROFILE_FAMILY_RULES = [
  { adapter: "crafty", prefix: "minecraft", key: "minecraft", label: "Minecraft" },
  { adapter: "systemd", prefix: "terraria", key: "terraria", label: "Terraria" },
  { adapter: "systemd", prefix: "pz-", key: "project-zomboid", label: "Project Zomboid" },
];
const THEMES = ["ember", "frost", "moss", "aurora", "paper"];
const SAMPLE_LIMIT = 90;
const NOISE_PATTERNS = [
  /Closing TcpSocket/i,
  /\(Anonymous\)\] (Connecting|Closing)/i,
  /Tried to send data to a client after losing connection/i,
  /Thread RCON Client \/127\.0\.0\.1 (?:started|shutting down)/i,
];
const isNoise = (line) => NOISE_PATTERNS.some((pattern) => pattern.test(line.message || ""));

function markPerformance(name) {
  try { window.performance?.mark(name); } catch {}
}

function measurePerformance(name, startMark, endMark) {
  try {
    window.performance?.measure(name, startMark, endMark);
    window.performance?.clearMarks(startMark);
    window.performance?.clearMarks(endMark);
  } catch {}
}

markPerformance("horizon-load-start");

const state = {
  actor: null,
  csrf: null,
  profiles: new Map(),
  statuses: new Map(),
  selectedProfile: null,
  dialog: null,
  returnFocus: null,
  drawerReturnFocus: null,
  forceProfile: null,
  restoreProfile: null,
  logs: new Map(),
  cpuSamples: new Map(),
  metricSamples: new Map(),
  detail: { id: null, tab: "console", backups: [], statsTimer: null, statsRequest: 0 },
  configRestartRequired: new Map(),
  lastGeneration: 0,
  loadFailed: false,
  perf: { firstStatusPaint: false, pendingMutations: new Map() },
};
let aggregateBackupRequest = 0;
let reauthenticating = false;
const stream = { source: null, lastEventAt: 0, retryMs: 3000, watchdog: null, pollTimer: null, reconnectTimer: null, reconnectStartedAt: null };

const byId = (id) => document.getElementById(id);
const cards = byId("profile-cards");
const announcer = byId("status-announcer");
const profileLabel = (id) => state.profiles.get(id)?.display_name || id;
const titleCase = (value) => String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
const statusLabel = (value) => {
  const label = titleCase(value);
  return ["starting", "stopping"].includes(value) ? `${label}…` : label;
};
const slotOwnerId = () => [...state.statuses.values()].find((status) => status?.slot_owner)?.slot_owner || null;

function applyTheme(value, persist = false) {
  const theme = THEMES.includes(value) ? value : "ember";
  document.documentElement.dataset.theme = theme;
  const colorScheme = byId("color-scheme-meta") || document.querySelector('meta[name="color-scheme"]');
  if (colorScheme) colorScheme.setAttribute("content", theme === "paper" ? "light" : "dark");
  if (persist) {
    try { localStorage.setItem("helios-theme", theme); } catch {}
  }
  const picker = byId("theme-picker");
  if (picker && picker.value !== theme) picker.value = theme;
}

function loadTheme() {
  let stored = null;
  try { stored = localStorage.getItem("helios-theme"); } catch {}
  applyTheme(stored, false);
}

function updateServerNav() {
  const nav = byId("server-nav");
  const owner = [...state.statuses.values()].find((status) => status?.slot_owner)?.slot_owner || null;
  state.profiles.forEach((profile, id) => {
    let item = nav.querySelector(`[data-profile-nav="${CSS.escape(id)}"]`);
    if (!item) {
      item = document.createElement("a");
      item.className = "nav-item nav-server";
      item.href = `#/servers/${encodeURIComponent(id)}/console`;
      item.dataset.profileNav = id;
      item.innerHTML = '<span class="server-dot" aria-hidden="true"></span><span data-profile-label></span>';
      nav.append(item);
    }
    const label = item.querySelector("[data-profile-label]");
    if (label) label.textContent = profile?.display_name || id;
    const ownerStatus = owner === id ? state.statuses.get(id) : null;
    const dot = item.querySelector(".server-dot");
    dot?.classList.toggle("is-owner", owner === id);
    dot?.classList.toggle("is-transitional", ["starting", "stopping"].includes(ownerStatus?.state));
  });
}

function setDrawer(open, returnFocus = null) {
  const sidebar = byId("sidebar");
  const overlay = byId("drawer-overlay");
  const burger = byId("menu-toggle");
  const close = byId("drawer-close");
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  const isOpen = Boolean(open && mobile);
  const visible = mobile ? isOpen : true;
  if (isOpen && returnFocus) state.drawerReturnFocus = returnFocus;
  sidebar.setAttribute("aria-hidden", String(!visible));
  sidebar.inert = mobile && !isOpen;
  overlay.hidden = !isOpen;
  burger.setAttribute("aria-expanded", String(isOpen));
  document.body.classList.toggle("drawer-open", isOpen);
  if (isOpen) {
    const first = close || sidebar.querySelector("a, button, input, select, textarea, [tabindex]:not([tabindex='-1'])");
    window.requestAnimationFrame(() => first?.focus());
  } else {
    const focus = state.drawerReturnFocus || returnFocus || burger;
    if (focus && document.contains(focus)) focus.focus();
  }
  if (!isOpen) state.drawerReturnFocus = null;
}

function showView(view) {
  const dashboard = byId("dashboard-view");
  const settings = byId("settings-view");
  const detail = byId("detail-view");
  const backups = byId("backups-view");
  const events = byId("events-view");
  const audit = byId("audit-view");
  const settingsActive = view === "settings";
  const detailActive = view === "detail";
  const backupsActive = view === "backups";
  const eventsActive = view === "events";
  const auditActive = view === "audit";
  settings.hidden = !settingsActive;
  detail.hidden = !detailActive;
  backups.hidden = !backupsActive;
  events.hidden = !eventsActive;
  audit.hidden = !auditActive;
  dashboard.hidden = settingsActive || detailActive || backupsActive || eventsActive || auditActive;
  document.querySelectorAll("[data-view]").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view || (!settingsActive && !detailActive && !backupsActive && !eventsActive && !auditActive && item.dataset.view === "dashboard")));
  document.querySelectorAll("[data-profile-nav]").forEach((item) => item.classList.toggle("is-active", detailActive && item.dataset.profileNav === state.detail.id));
}

function setupShell() {
  loadTheme();
  const sidebar = byId("sidebar");
  const media = window.matchMedia("(max-width: 760px)");
  const overlay = byId("drawer-overlay");
  const burger = byId("menu-toggle");
  const syncBreakpoint = () => {
    const mobile = media.matches;
    sidebar.inert = mobile;
    sidebar.setAttribute("aria-hidden", String(mobile));
    overlay.hidden = true;
    burger.setAttribute("aria-expanded", "false");
    document.body.classList.remove("drawer-open");
    state.drawerReturnFocus = null;
  };
  syncBreakpoint();
  if (media.addEventListener) media.addEventListener("change", syncBreakpoint);
  else media.addListener?.(syncBreakpoint);
  window.addEventListener("resize", syncBreakpoint);
  byId("menu-toggle").addEventListener("click", (event) => setDrawer(true, event.currentTarget));
  byId("drawer-close").addEventListener("click", () => setDrawer(false));
  byId("drawer-overlay").addEventListener("click", () => setDrawer(false));
  byId("servers-toggle").addEventListener("click", (event) => {
    const expanded = event.currentTarget.getAttribute("aria-expanded") !== "true";
    event.currentTarget.setAttribute("aria-expanded", String(expanded));
    byId("server-nav").hidden = !expanded;
  });
  document.querySelectorAll("[data-view]").forEach((item) => item.addEventListener("click", () => {
    const view = ["settings", "backups", "events", "audit"].includes(item.dataset.view) ? item.dataset.view : "dashboard";
    if (view === "settings") window.location.hash = "#/settings";
    else if (view === "backups") window.location.hash = "#/backups";
    else if (view === "events") window.location.hash = "#/events";
    else if (view === "audit") window.location.hash = "#/audit";
    else window.location.hash = "#/";
    showView(view);
    if (media.matches) setDrawer(false);
  }));
  byId("server-nav").addEventListener("click", (event) => {
    if (!event.target.closest("[data-profile-nav]")) return;
    const id = event.target.closest("[data-profile-nav]").dataset.profileNav;
    window.location.hash = `#/servers/${encodeURIComponent(id)}/console`;
    if (media.matches) setDrawer(false);
  });
  byId("theme-picker").addEventListener("change", (event) => applyTheme(event.target.value, true));
  byId("notification-profile").addEventListener("change", (event) => loadNotifications(event.target.value));
  document.querySelectorAll("[data-notification-test]").forEach((button) => button.addEventListener("click", () => testNotification(button.dataset.notificationTest)));
  byId("idle-stop-form").addEventListener("submit", saveIdleStop);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !sidebar.inert && window.matchMedia("(max-width: 760px)").matches) setDrawer(false);
  });
}

function notify(message) {
  announcer.textContent = message;
  const toast = byId("toast-region");
  toast.textContent = message;
  window.setTimeout(() => { if (toast.textContent === message) toast.textContent = ""; }, 5000);
}

function stateClass(value) {
  return `state-${String(value || "unknown").replace(/[^a-z0-9-]/g, "")}`;
}

function uptime(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const total = Math.max(0, Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function formatBytes(bytes) {
  if (bytes == null || bytes === "" || typeof bytes === "boolean") return "—";
  if (!["number", "string"].includes(typeof bytes)) return "—";
  const value = typeof bytes === "string" && !bytes.trim() ? NaN : Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "—";
  return `${(value / 1073741824).toFixed(1)} GB`;
}

function formatRate(value) {
  if (value == null || !Number.isFinite(Number(value))) return "Unavailable";
  const units = ["B/s", "KiB/s", "MiB/s", "GiB/s"];
  let amount = Math.max(0, Number(value));
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount >= 100 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatVersion(version) {
  if (version == null) return "—";
  const value = String(version).trim();
  const sentinel = ["", "false", "none", "null", "undefined", "unknown", "n/a", "na", "unavailable", "not available"];
  return sentinel.includes(value.toLowerCase()) ? "—" : value;
}

function createCard(id) {
  const card = document.createElement("article");
  card.className = "profile-card";
  card.dataset.profileId = id;
  card.innerHTML = `
    <div class="card-topline"><span class="profile-kicker"></span><span class="status-badge"><span class="status-dot" aria-hidden="true"></span><span class="status-text"></span></span></div>
    <h3 class="profile-name"></h3>
    <p class="profile-description"></p>
    <svg class="cpu-sparkline" viewBox="0 0 120 38" role="img" aria-label="CPU usage unavailable">
      <title>CPU usage</title><polyline class="cpu-sparkline-line" points="0,36 120,36"></polyline>
    </svg>
    <dl class="card-metrics">
      <div><dt>Players</dt><dd class="metric-players">—</dd></div>
      <div><dt>CPU</dt><dd class="metric-cpu">—</dd></div>
      <div><dt>Memory</dt><dd class="metric-memory">—</dd></div>
      <div><dt>Version</dt><dd class="metric-version">—</dd></div>
    </dl>
    <p class="card-reason" role="note"></p>
    <div class="card-actions">
      <button class="button button-small action-start" type="button"></button>
      <button class="button button-small button-quiet action-stop" type="button"></button>
      <button class="button button-small button-quiet action-restart" type="button"></button>
      <button class="button button-small button-quiet action-switch" data-action="switch" type="button">Switch</button>
      <a class="button button-small button-quiet manage-link" href="#server">Manage →</a>
    </div>`;
  cards.append(card);
  card.querySelector(".action-start").addEventListener("click", () => mutate(id, "start"));
  card.querySelector(".action-stop").addEventListener("click", () => mutate(id, "stop"));
  card.querySelector(".action-restart").addEventListener("click", () => mutate(id, "restart"));
  card.querySelector(".action-switch").addEventListener("click", (event) => openSwitchDialog(id, event.currentTarget));
  return card;
}

function profileOrder() {
  const known = PROFILE_FALLBACK.map(([id]) => id);
  const future = [...state.profiles.keys()].filter((id) => !known.includes(id));
  return [...known.filter((id) => state.profiles.has(id)), ...future];
}

function profileFamily(profileOrId) {
  const id = String(profileOrId?.id ?? profileOrId ?? "");
  const adapter = String(profileOrId?.adapter?.value ?? profileOrId?.adapter ?? "");
  return PROFILE_FAMILY_RULES.find((rule) => adapter === rule.adapter && id.startsWith(rule.prefix)) || {
    key: "other",
    label: "Other",
  };
}

function createFamilySection(family) {
  const section = document.createElement("section");
  section.className = "profile-family";
  section.dataset.profileFamily = family.key;
  const headingId = `profile-family-${family.key}-title`;
  section.innerHTML = `
    <div class="profile-family-header">
      <div class="profile-family-heading"><h3 id="${headingId}" class="profile-family-name"></h3><span class="profile-family-count" data-family-count></span></div>
      <span class="profile-family-owner" data-family-owner></span>
    </div>
    <div class="cards profile-family-cards" aria-labelledby="${headingId}"></div>`;
  section.querySelector(".profile-family-name").textContent = family.label;
  return section;
}

function patchFamilyHeaders() {
  const owner = slotOwnerId();
  cards.querySelectorAll("[data-profile-family]").forEach((section) => {
    const members = [...section.querySelectorAll("[data-profile-id]")];
    const ownerMember = members.find((card) => card.dataset.profileId === owner);
    section.classList.toggle("is-singleton", members.length === 1);
    section.querySelector("[data-family-count]").textContent = `${members.length} member${members.length === 1 ? "" : "s"}`;
    section.querySelector("[data-family-owner]").textContent = ownerMember ? `Slot: ${profileLabel(owner)}` : "Slot: —";
  });
}

function renderCards() {
  cards.querySelectorAll("[data-skeleton]").forEach((placeholder) => placeholder.remove());
  const sections = new Map();
  profileOrder().forEach((id) => {
    const family = profileFamily(state.profiles.get(id) || id);
    let section = sections.get(family.key);
    if (!section) {
      section = cards.querySelector(`[data-profile-family="${CSS.escape(family.key)}"]`) || createFamilySection(family);
      sections.set(family.key, section);
    }
    const card = cards.querySelector(`[data-profile-id="${CSS.escape(id)}"]`) || createCard(id);
    section.querySelector(".profile-family-cards").append(card);
    patchCard(id);
  });
  const orderedSections = PROFILE_FAMILY_RULES.map((rule) => sections.get(rule.key)).filter(Boolean);
  if (sections.has("other")) orderedSections.push(sections.get("other"));
  cards.replaceChildren(...orderedSections);
  patchFamilyHeaders();
}

function appendCpuSample(id, status) {
  const cpu = Number(status?.cpu_percent);
  if (!Number.isFinite(cpu) || cpu < 0 || ["stopped", "unknown"].includes(status?.state)) {
    state.cpuSamples.set(id, [0]);
    return;
  }
  const samples = state.cpuSamples.get(id) || [];
  samples.push(cpu);
  if (samples.length > SAMPLE_LIMIT) samples.splice(0, samples.length - SAMPLE_LIMIT);
  state.cpuSamples.set(id, samples);
}

function patchSparkline(card, id, status) {
  const samples = state.cpuSamples.get(id) || [0];
  const max = Math.max(100, ...samples);
  const width = 120;
  const height = 38;
  const points = samples.map((sample, index) => {
    const x = samples.length === 1 ? 0 : (index / (samples.length - 1)) * width;
    const y = height - 2 - (Math.min(max, Math.max(0, sample)) / max) * (height - 4);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).concat(samples.length === 1 ? [`${width.toFixed(2)},${(height - 2).toFixed(2)}`] : []).join(" ");
  const line = card.querySelector(".cpu-sparkline-line");
  line.setAttribute("points", points);
  const current = Number(status?.cpu_percent);
  card.querySelector(".cpu-sparkline").setAttribute(
    "aria-label",
    Number.isFinite(current) && current >= 0 ? `CPU usage ${current.toFixed(1)} percent` : "CPU usage 0 percent",
  );
}

function patchCard(id) {
  const card = cards.querySelector(`[data-profile-id="${CSS.escape(id)}"]`) || createCard(id);
  const profile = state.profiles.get(id) || { id, display_name: id, operations: [] };
  const status = state.statuses.get(id) || { profile_id: id, state: "unknown", health: "unknown" };
  const display = profile.display_name || id;
  const current = status.state || "unknown";
  const operationSet = new Set(profile.operations || ["start", "stop", "restart"]);
  card.classList.toggle("is-active", status.slot_owner === id);
  card.classList.remove("state-running", "state-starting", "state-stopping", "state-stopped", "state-blocked", "state-failed", "state-unknown");
  card.classList.add(stateClass(current));
  card.querySelector(".profile-kicker").textContent = id;
  card.querySelector(".profile-name").textContent = display;
  card.querySelector(".profile-description").textContent = profile.public_endpoint?.host || "private";
  card.querySelector(".status-badge").className = `status-badge ${stateClass(current)}`;
  card.querySelector(".status-text").textContent = statusLabel(current);
  card.querySelector(".metric-players").textContent = status.players_online == null ? "—" : `${status.players_online} player${status.players_online === 1 ? "" : "s"}`;
  card.querySelector(".metric-cpu").textContent = status.cpu_percent == null ? "—" : `${Number(status.cpu_percent).toFixed(1)}%`;
  card.querySelector(".metric-memory").textContent = formatBytes(status.rss_bytes);
  card.querySelector(".metric-version").textContent = formatVersion(status.installed_version);
  patchSparkline(card, id, status);
  const readiness = status.required_ports_ready ? "ready" : "process accepted; waiting for required ports";
  const reason = current === "blocked" ? "Blocked: another server owns the active slot. Switch active server…" :
    current === "failed" ? "Previous health check failed; review details before starting." :
      current === "starting" ? "Starting: actions are paused until the server is ready." :
        current === "stopping" ? "Stopping: actions are paused until shutdown completes." : "";
  card.querySelector(".card-reason").textContent = reason || (current === "starting" ? `Start accepted; ${readiness}.` : "");
  card.querySelector(".card-reason").textContent = reason;
  const buttons = [
    [".action-start", "Start", "start", !["stopped", "failed", "blocked", "unknown"].includes(current) || !operationSet.has("start") || current === "blocked"],
    [".action-stop", "Stop", "stop", !["running", "starting"].includes(current) || !operationSet.has("stop")],
    [".action-restart", "Restart", "restart", current !== "running" || !operationSet.has("restart")],
  ];
  buttons.forEach(([selector, label, operation, disabled]) => {
    const button = card.querySelector(selector);
    button.textContent = label;
    button.disabled = Boolean(disabled);
    button.hidden = operation === "start"
      ? !["stopped", "failed", "blocked", "unknown"].includes(current)
      : !["running", "starting"].includes(current);
    const why = reason ? ` (${reason})` : "";
    button.setAttribute("aria-label", `${label} ${display}`);
    button.title = button.disabled ? (why || "Action is unavailable in this state.") : "";
  });
  const switchButton = card.querySelector(".action-switch");
  switchButton.disabled = status.slot_owner === id;
  switchButton.setAttribute("aria-label", `Switch to ${display}`);
  switchButton.title = switchButton.disabled ? "This server already owns the active slot." : "Preselect this server in the switch dialog.";
  const manage = card.querySelector(".manage-link");
  manage.href = `#/servers/${encodeURIComponent(id)}/console`;
  manage.setAttribute("aria-label", `Manage ${display}`);
  updateServerNav();
}

function patchActiveSlot() {
  const ownerId = slotOwnerId();
  const active = ownerId ? state.statuses.get(ownerId) : null;
  const slot = byId("active-slot");
  const title = byId("active-slot-title");
  const manage = byId("active-manage");
  if (!active) {
    title.textContent = "Nothing is running";
    byId("active-slot-summary").textContent = "No server currently owns the active slot.";
    byId("active-players").textContent = "—";
    byId("active-uptime").textContent = "—";
    byId("active-health").textContent = "—";
    slot.classList.add("is-empty");
    slot.classList.remove("is-transitional");
    manage.hidden = true;
    return;
  }
  const name = profileLabel(active.profile_id);
  title.textContent = name;
  byId("active-slot-summary").textContent = "";
  byId("active-players").textContent = active.players_online == null ? "—" : String(active.players_online);
  byId("active-uptime").textContent = uptime(active.uptime_seconds);
  byId("active-health").textContent = titleCase(active.health);
  slot.classList.remove("is-empty");
  slot.classList.toggle("is-transitional", ["starting", "stopping"].includes(active.state));
  manage.hidden = false;
  manage.href = `#/servers/${encodeURIComponent(active.profile_id)}/console`;
  manage.setAttribute("aria-label", `Manage ${name}`);
}

function applyStatus(snapshot) {
  if (!snapshot || !Array.isArray(snapshot.profiles)) return;
  if (Number.isFinite(Number(snapshot.generation))) state.lastGeneration = Number(snapshot.generation);
  snapshot.profiles.forEach((item) => {
    if (!item?.profile_id) return;
    const previous = state.statuses.get(item.profile_id) || {};
    state.statuses.set(item.profile_id, { ...previous, ...item });
    markPerformance("horizon-card-reflect");
    if (state.perf.pendingMutations.has(item.profile_id)) {
      measurePerformance("horizon-mutation-click-to-card-reflect", "horizon-mutation-click", "horizon-card-reflect");
      state.perf.pendingMutations.delete(item.profile_id);
    }
    appendCpuSample(item.profile_id, item);
    const samples = state.metricSamples.get(item.profile_id) || { cpu: [], memory: [], players: [] };
    const sampleTime = Date.now();
    if (Number.isFinite(Number(item.cpu_percent))) samples.cpu.push({ t: sampleTime, v: Number(item.cpu_percent) });
    if (Number.isFinite(Number(item.rss_bytes))) samples.memory.push({ t: sampleTime, v: Number(item.rss_bytes) / 1073741824 });
    if (Number.isFinite(Number(item.players_online))) samples.players.push({ t: sampleTime, v: Number(item.players_online) });
    ["cpu", "memory", "players"].forEach((key) => { samples[key] = samples[key].slice(-SAMPLE_LIMIT); });
    state.metricSamples.set(item.profile_id, samples);
    patchCard(item.profile_id);
  });
  patchActiveSlot();
  patchFamilyHeaders();
  if (!state.perf.firstStatusPaint) {
    markPerformance("horizon-first-status-paint");
    measurePerformance("horizon-load-to-first-status-paint", "horizon-load-start", "horizon-first-status-paint");
    state.perf.firstStatusPaint = true;
  }
  byId("profile-cards").setAttribute("aria-busy", "false");
  if (snapshot.observed_at) {
    const observed = new Date(snapshot.observed_at);
    const hours = String(observed.getHours()).padStart(2, "0");
    const minutes = String(observed.getMinutes()).padStart(2, "0");
    byId("last-updated").textContent = `Updated ${hours}:${minutes}`;
  } else {
    byId("last-updated").textContent = `Generation ${state.lastGeneration}`;
  }
  if (state.detail.id) patchDetail(state.detail.id);
}

async function api(path, options = {}) {
  const attempt = async () => {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body) headers.set("Content-Type", "application/json");
    if (state.csrf && options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
    return fetch(path, { credentials: "same-origin", ...options, headers, redirect: "manual" });
  };
  let response;
  try {
    response = await attempt();
  } catch {
    if (!options.method || options.method === "GET") {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try { response = await attempt(); } catch { throw new Error("Connection lost. Retrying in the background…"); }
    } else {
      throw new Error("Connection lost — action not sent. Check the connection pill and retry.");
    }
  }
  if (response.type === "opaqueredirect") {
    if (!reauthenticating) {
      reauthenticating = true;
      window.location.assign("/");
    }
    throw new Error("Session expired. Redirecting to sign in.");
  }
  let csrfFailure = false;
  if (response.status === 403) {
    try {
      const body = await response.clone().json();
      const detail = body?.detail || body?.error?.message || "";
      csrfFailure = String(detail).toLowerCase().includes("csrf validation failed");
    } catch {}
  }
  if ((response.status === 401 || csrfFailure) && !options._retried && path !== "/api/v1/session") {
    try {
      const session = await fetch("/api/v1/session", { credentials: "same-origin", headers: { Accept: "application/json" }, redirect: "manual" });
      if (session.ok) {
        const body = await session.json();
        state.csrf = body.csrf_token || state.csrf;
        reauthenticating = false;
        return api(path, { ...options, _retried: true });
      }
    } catch {}
  }
  if (!response.ok) {
    let detail = "Request failed.";
    try { detail = (await response.json())?.error?.message || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}

async function load() {
  try {
    const session = await api("/api/v1/session");
    state.actor = session.actor;
    state.csrf = session.csrf_token || null;
    byId("session-note").textContent = state.actor ? `Signed in as ${state.actor}` : "Signed-in operator";
    const [profileList, status] = await Promise.all([api("/api/v1/profiles"), api("/api/v1/status")]);
    (Array.isArray(profileList) ? profileList : PROFILE_FALLBACK.map(([id, display_name, adapter]) => ({ id, display_name, adapter }))).forEach((profile) => {
      const id = profile.id || profile.profile_id;
      if (id) state.profiles.set(id, { ...profile, id });
    });
    PROFILE_FALLBACK.forEach(([id, display_name]) => { if (!state.profiles.has(id)) state.profiles.set(id, { id, display_name }); });
    populateNotificationProfiles();
    renderCards();
    applyStatus(status);
    populateTargets();
    populateNotificationProfiles();
    connectStream();
  } catch (error) {
    state.loadFailed = true;
    byId("session-note").textContent = "Status unavailable";
    notify(error.message || "Status unavailable.");
    PROFILE_FALLBACK.forEach(([id, display_name, adapter]) => {
      state.profiles.set(id, { id, display_name, adapter });
      state.statuses.set(id, { profile_id: id, state: "unknown", health: "unknown" });
    });
    renderCards();
    patchActiveSlot();
    populateTargets();
    byId("retry-load")?.removeAttribute("hidden");
  }
}

function populateNotificationProfiles() {
  const select = byId("notification-profile");
  if (!select) return;
  const prior = select.value;
  select.replaceChildren();
  state.profiles.forEach((profile, id) => {
    const option = document.createElement("option"); option.value = id; option.textContent = profile.display_name || id; select.append(option);
  });
  select.value = [...select.options].some((option) => option.value === prior) ? prior : select.options[0]?.value || "";
}

function performanceMs(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} ms` : "—";
}

function renderPerformance(snapshot) {
  const routes = byId("performance-routes");
  const backend = byId("performance-slotd");
  const marks = byId("performance-marks");
  if (!routes || !backend || !marks) return;
  routes.replaceChildren();
  Object.entries(snapshot || {})
    .filter(([key, value]) => !["rpc", "sse", "slotd"].includes(key) && value && typeof value === "object")
    .forEach(([route, value]) => {
      const row = document.createElement("li");
      row.className = "performance-row";
      const label = document.createElement("strong"); label.textContent = route;
      const detail = document.createElement("span"); detail.textContent = `p50 ${performanceMs(value.p50_ms)} · p95 ${performanceMs(value.p95_ms)} · max ${performanceMs(value.max_ms)}`;
      row.append(label, detail); routes.append(row);
    });
  if (!routes.children.length) {
    const empty = document.createElement("li"); empty.className = "empty-state"; empty.textContent = "No route samples recorded yet."; routes.append(empty);
  }
  const cycle = snapshot?.slotd?.cycle;
  const rpc = snapshot?.slotd?.rpc || snapshot?.rpc;
  backend.textContent = cycle
    ? `Status cycle p95 ${performanceMs(cycle.p95_ms)} · RPC p95 ${performanceMs(rpc?.p95_ms)}`
    : `Web RPC p95 ${performanceMs(rpc?.p95_ms)}`;
  marks.replaceChildren();
  const entries = window.performance?.getEntriesByType("measure") || [];
  ["horizon-load-to-first-status-paint", "horizon-mutation-click-to-optimistic-reflect", "horizon-mutation-click-to-card-reflect", "horizon-sse-reconnect-gap"].forEach((name) => {
    const entry = [...entries].reverse().find((item) => item.name === name);
    const row = document.createElement("li"); row.className = "performance-row";
    const label = document.createElement("strong"); label.textContent = name.replace("horizon-", "").replaceAll("-", " ");
    const detail = document.createElement("span"); detail.textContent = entry ? performanceMs(entry.duration) : "Not measured yet";
    row.append(label, detail); marks.append(row);
  });
}

async function loadPerformance() {
  try {
    renderPerformance(await api("/api/v1/perf"));
  } catch (error) {
    renderPerformance({});
    const panel = byId("performance-panel");
    if (panel) panel.dataset.error = error.message || "Performance unavailable";
  }
}

async function loadNotifications(id) {
  if (!id) return;
  const rules = byId("notification-rules");
  try {
    const config = await api(`/api/v1/profiles/${encodeURIComponent(id)}/notifications`);
    rules.replaceChildren();
    Object.entries(config.rules || {}).forEach(([event, enabled]) => {
      const label = document.createElement("label"); label.className = "check-row notification-rule";
      const input = document.createElement("input"); input.type = "checkbox"; input.checked = Boolean(enabled); input.dataset.event = event;
      input.addEventListener("change", async () => {
        input.disabled = true;
        try { await api(`/api/v1/profiles/${encodeURIComponent(id)}/notifications/rule`, { method: "POST", body: JSON.stringify({ event, enabled: input.checked }) }); setNotificationStatus("Rule saved."); }
        catch (error) { input.checked = !input.checked; setNotificationStatus(error.message || "Rule update failed."); }
        finally { input.disabled = false; }
      });
      label.append(input, document.createTextNode(titleCase(event))); rules.append(label);
    });
    setNotificationStatus("Notification settings loaded.");
  } catch (error) { rules.replaceChildren(); const empty = document.createElement("p"); empty.className = "empty-state"; empty.textContent = error.message || "Notifications unavailable."; rules.append(empty); }
}

function setNotificationStatus(message) { const node = byId("notification-status"); if (node) node.textContent = message; }

async function testNotification(channel) {
  const id = byId("notification-profile")?.value;
  if (!id) return;
  try { await api(`/api/v1/profiles/${encodeURIComponent(id)}/notifications/test`, { method: "POST", body: JSON.stringify({ channel }) }); setNotificationStatus(`${titleCase(channel)} test requested.`); }
  catch (error) { setNotificationStatus(error.message || `${titleCase(channel)} test failed.`); }
}

function renderActivity(listId, items, kind) {
  const list = byId(listId); list.replaceChildren();
  (Array.isArray(items) ? items : []).forEach((item) => {
    const row = document.createElement("li"); row.className = "activity-row";
    const heading = document.createElement("strong"); heading.textContent = kind === "audit" ? `${item.actor || "unknown"} · ${item.action || "—"}` : `${item.code || "event"}${item.profile_id ? ` · ${profileLabel(item.profile_id)}` : ""}`;
    const time = document.createElement("time"); time.textContent = item.timestamp ? new Date(item.timestamp).toLocaleString() : "—";
    const detail = document.createElement("span"); detail.textContent = kind === "audit" ? `${item.result || "—"}${item.error_code ? ` · ${item.error_code}` : ""} · ${item.detail || ""}` : (item.message || "");
    row.append(heading, time, detail); list.append(row);
  });
  if (!items?.length) { const empty = document.createElement("li"); empty.className = "empty-state"; empty.textContent = `No ${kind} records loaded.`; list.append(empty); }
}

async function loadActivity(kind) {
  try { const page = await api(`/api/v1/${kind}?limit=200`); renderActivity(`${kind}-list`, page.items, kind); }
  catch (error) { renderActivity(`${kind}-list`, [] , kind); notify(error.message || `${titleCase(kind)} unavailable.`); }
}

function connectStream() {
  if (!window.EventSource) { startFallbackPolling(); return; }
  if (stream.source) { stream.source.close(); stream.source = null; }
  const source = new EventSource("/api/v1/stream");
  stream.source = source;
  const alive = () => {
    if (stream.reconnectStartedAt !== null) {
      markPerformance("horizon-sse-reconnect-end");
      measurePerformance("horizon-sse-reconnect-gap", "horizon-sse-reconnect-start", "horizon-sse-reconnect-end");
      stream.reconnectStartedAt = null;
    }
    stream.lastEventAt = Date.now(); stream.retryMs = 3000; setConnState("live"); stopFallbackPolling();
  };
  source.onopen = alive;
  source.addEventListener("heartbeat", alive);
  source.addEventListener("status", (event) => { alive(); try { applyStatus(JSON.parse(event.data)); } catch {} });
  source.onmessage = (event) => { alive(); try { applyStatus(JSON.parse(event.data)); } catch {} };
  source.onerror = () => {
    setConnState("reconnecting");
    startFallbackPolling();
    if (source.readyState === EventSource.CLOSED) scheduleReconnect();
  };
  if (!stream.watchdog) stream.watchdog = window.setInterval(() => {
    if (stream.source && Date.now() - stream.lastEventAt > 45000) { setConnState("reconnecting"); scheduleReconnect(); }
  }, 10000);
}

function setConnState(mode) {
  const pill = byId("conn-state");
  if (!pill) return;
  pill.dataset.state = mode;
  pill.textContent = mode === "live" ? "Live" : mode === "reconnecting" ? "Reconnecting…" : "Offline";
}

function scheduleReconnect() {
  if (stream.reconnectTimer) return;
  if (stream.source) { stream.source.close(); stream.source = null; }
  if (stream.reconnectStartedAt === null) {
    stream.reconnectStartedAt = performance.now();
    markPerformance("horizon-sse-reconnect-start");
  }
  const delay = stream.retryMs + Math.random() * 1000;
  stream.retryMs = Math.min(stream.retryMs * 2, 60000);
  stream.reconnectTimer = window.setTimeout(async () => {
    stream.reconnectTimer = null;
    try {
      const session = await api("/api/v1/session");
      state.csrf = session.csrf_token || state.csrf;
    } catch {}
    connectStream();
  }, delay);
}

function startFallbackPolling() {
  if (stream.pollTimer) return;
  stream.pollTimer = window.setInterval(async () => {
    try { applyStatus(await api("/api/v1/status")); } catch { setConnState("offline"); }
  }, 10000);
}

function stopFallbackPolling() {
  if (stream.pollTimer) { window.clearInterval(stream.pollTimer); stream.pollTimer = null; }
}

function populateTargets(preferredTarget = null) {
  const current = [...state.statuses.values()].find((item) => item.slot_owner || item.state === "running")?.profile_id;
  const target = byId("switch-target");
  const prior = preferredTarget || target.value;
  target.replaceChildren();
  state.profiles.forEach((profile, id) => {
    if (id === current) return;
    const option = document.createElement("option");
    option.value = id;
    option.textContent = profile.display_name || id;
    target.append(option);
  });
  if ([...target.options].some((option) => option.value === prior)) target.value = prior;
  byId("switch-current").textContent = current ? profileLabel(current) : "No active server";
  byId("switch-target-summary").textContent = target.selectedOptions[0]?.textContent || "Choose a target";
  byId("switch-timeout").textContent = "Up to 5 minutes";
}

function openSwitchDialog(targetId, opener) {
  const switchDialog = byId("switch-dialog");
  populateTargets(targetId);
  byId("switch-confirm-text").value = "";
  setupDialog(switchDialog, opener);
  const expected = byId("switch-target").selectedOptions[0]?.textContent || "";
  byId("switch-target-summary").textContent = expected || "Choose a target";
  byId("switch-confirm").disabled = true;
}

async function mutate(id, operation) {
  const owner = [...state.statuses.values()].find((status) => status?.slot_owner)?.slot_owner;
  if (operation === "start" && owner && owner !== id) {
    notify(`${profileLabel(id)} cannot start while ${profileLabel(owner)} owns the active slot. Switch active server…`);
    return;
  }
  if (state.perf.pendingMutations.has(id)) return;
  const previous = { ...(state.statuses.get(id) || { profile_id: id, state: "unknown", health: "unknown" }) };
  const optimistic = {
    ...previous,
    profile_id: id,
    state: operation === "start" ? "starting" : "stopping",
    slot_owner: id,
    health: operation === "start" ? "unknown" : previous.health,
    required_ports_ready: operation === "start" ? false : previous.required_ports_ready,
  };
  const pending = { operation, previous };
  state.perf.pendingMutations.set(id, pending);
  state.statuses.set(id, optimistic);
  markPerformance("horizon-mutation-click");
  markPerformance("horizon-mutation-optimistic-click");
  patchCard(id);
  patchActiveSlot();
  patchFamilyHeaders();
  if (state.detail.id === id) patchDetail(id);
  markPerformance("horizon-mutation-optimistic-reflect");
  measurePerformance("horizon-mutation-click-to-optimistic-reflect", "horizon-mutation-optimistic-click", "horizon-mutation-optimistic-reflect");
  try {
    await api(`/api/v1/profiles/${encodeURIComponent(id)}/${operation}`, { method: "POST", body: JSON.stringify({}) });
    notify(`${titleCase(operation)} requested for ${profileLabel(id)}.`);
  } catch (error) {
    if (state.perf.pendingMutations.get(id) === pending) {
      state.statuses.set(id, previous);
      state.perf.pendingMutations.delete(id);
      patchCard(id);
      patchActiveSlot();
      patchFamilyHeaders();
      if (state.detail.id === id) patchDetail(id);
    }
    notify(error.message || `${titleCase(operation)} failed.`);
  }
}

function setupDialog(dialog, opener) {
  state.dialog = dialog;
  state.returnFocus = opener || document.activeElement;
  dialog.showModal();
  const focusable = dialog.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])");
  focusable[0]?.focus();
}

function closeDialog(dialog) {
  if (dialog.open) dialog.close("cancel");
  const focus = state.returnFocus;
  state.dialog = null;
  state.returnFocus = null;
  if (focus && document.contains(focus)) focus.focus();
}

document.addEventListener("keydown", (event) => {
  const dialog = state.dialog;
  if (!dialog?.open) return;
  if (event.key === "Escape") { event.preventDefault(); closeDialog(dialog); return; }
  if (event.key !== "Tab") return;
  const focusable = [...dialog.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])")].filter((node) => !node.disabled);
  if (!focusable.length) return;
  const index = focusable.indexOf(document.activeElement);
  if (event.shiftKey && (index <= 0)) { event.preventDefault(); focusable.at(-1).focus(); }
  else if (!event.shiftKey && (index === focusable.length - 1)) { event.preventDefault(); focusable[0].focus(); }
});

function wireDialogForms() {
  const switchDialog = byId("switch-dialog");
  const switchTarget = byId("switch-target");
  const switchText = byId("switch-confirm-text");
  const switchButton = byId("switch-confirm");
  const validateSwitch = () => {
    const expected = switchTarget.selectedOptions[0]?.textContent || "";
    byId("switch-target-summary").textContent = expected || "Choose a target";
    switchButton.disabled = !expected || switchText.value.trim().toLowerCase() !== expected.trim().toLowerCase();
  };
  byId("switch-active").addEventListener("click", (event) => openSwitchDialog(null, event.currentTarget));
  switchTarget.addEventListener("change", validateSwitch);
  switchText.addEventListener("input", validateSwitch);
  byId("switch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (switchButton.disabled) return;
    const current = slotOwnerId() || [...state.statuses.values()].find((item) => item.state === "running")?.profile_id;
    const target = switchTarget.value;
    try {
      const confirmation = await api("/api/v1/switch/prepare", { method: "POST", body: JSON.stringify({ current_profile_id: current, target_profile_id: target, create_backup: byId("switch-backup-option").checked, force_after_timeout: byId("switch-force-option").checked, rollback_on_failure: byId("switch-rollback-option").checked }) });
      await api("/api/v1/switch/confirm", { method: "POST", body: JSON.stringify({ confirmation_id: confirmation.confirmation_id }) });
      closeDialog(switchDialog); notify(`Switch to ${profileLabel(target)} requested.`);
    } catch (error) { notify(error.message || "Switch was not accepted."); }
  });
  [switchDialog, byId("force-dialog"), byId("logs-dialog"), byId("restore-dialog"), byId("console-save-as-dialog")].forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(dialog); });
    dialog.addEventListener("close", () => {
      if (state.dialog !== dialog) return;
      const focus = state.returnFocus;
      state.dialog = null;
      state.returnFocus = null;
      if (focus && document.contains(focus)) focus.focus();
    });
  });

  const forceDialog = byId("force-dialog");
  byId("force-confirm-text").addEventListener("input", () => { byId("force-confirm").disabled = byId("force-confirm-text").value.trim().toLowerCase() !== profileLabel(state.forceProfile).toLowerCase(); });
  byId("force-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = state.forceProfile;
    try {
      const confirmation = await api(`/api/v1/profiles/${encodeURIComponent(id)}/force-stop/prepare`, { method: "POST", body: JSON.stringify({}) });
      await api("/api/v1/force-stop/confirm", { method: "POST", body: JSON.stringify({ confirmation_id: confirmation.confirmation_id }) });
      closeDialog(forceDialog); notify(`Force stop requested for ${profileLabel(id)}.`);
    } catch (error) { notify(error.message || "Force stop was not accepted."); }
  });

  byId("log-query").addEventListener("input", () => { const item = state.logs.get(state.selectedProfile); if (item) { item.query = byId("log-query").value; renderLogs(item); } });
  byId("log-severity").addEventListener("change", () => { const item = state.logs.get(state.selectedProfile); if (item) { item.severity = byId("log-severity").value; renderLogs(item); } });
  byId("log-pause").addEventListener("click", () => { const item = state.logs.get(state.selectedProfile); if (item) { item.paused = !item.paused; byId("log-pause").textContent = item.paused ? "Resume live logs" : "Pause live logs"; } });

  byId("restore-confirm-text").addEventListener("input", () => { byId("restore-confirm").disabled = byId("restore-confirm-text").value.trim().toLowerCase() !== profileLabel(state.restoreProfile).toLowerCase() || !byId("restore-backup-id").value.trim(); });
  byId("restore-backup-id").addEventListener("input", () => { byId("restore-confirm-text").dispatchEvent(new Event("input")); });
  byId("restore-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = state.restoreProfile;
    try {
      const confirmation = await api(`/api/v1/profiles/${encodeURIComponent(id)}/restore/prepare`, { method: "POST", body: JSON.stringify({ backup_id: byId("restore-backup-id").value.trim() }) });
      await api("/api/v1/restore/confirm", { method: "POST", body: JSON.stringify({ confirmation_id: confirmation.confirmation_id }) });
      closeDialog(byId("restore-dialog")); notify(`Restore requested for ${profileLabel(id)}.`);
    } catch (error) { notify(error.message || "Restore was not accepted."); }
  });
}

function openForce(id, opener) {
  state.forceProfile = id;
  byId("force-description").textContent = `This interrupts ${profileLabel(id)} without waiting for a clean shutdown.`;
  byId("force-confirm-text").value = "";
  byId("force-confirm").disabled = true;
  setupDialog(byId("force-dialog"), opener);
}

function openRestore(id, opener, backupId = "") {
  state.restoreProfile = id;
  byId("restore-description").textContent = `Restoring replaces ${profileLabel(id)} world data.`;
  byId("restore-backup-id").value = backupId;
  byId("restore-confirm-text").value = "";
  byId("restore-confirm").disabled = !backupId;
  setupDialog(byId("restore-dialog"), opener);
}

async function openLogs(id, opener) {
  state.selectedProfile = id;
  const item = state.logs.get(id) || { query: "", severity: "all", paused: false, lines: [], hideNoise: true };
  state.logs.set(id, item);
  byId("logs-dialog-title").textContent = `Logs for ${profileLabel(id)}`;
  byId("log-query").value = item.query;
  byId("log-severity").value = item.severity;
  byId("log-pause").textContent = item.paused ? "Resume live logs" : "Pause live logs";
  setupDialog(byId("logs-dialog"), opener);
  try {
    const page = await api(`/api/v1/profiles/${encodeURIComponent(id)}/logs?limit=200&severity=all`);
    if (!item.paused && Array.isArray(page.items)) item.lines = page.items;
    renderLogs(item);
  } catch (error) { notify(error.message || "Logs unavailable."); }
}

function renderLogs(item) {
  const query = item.query.trim().toLowerCase();
  const lines = item.lines.filter((line) => (!query || String(line.message || "").toLowerCase().includes(query)) && (item.severity === "all" || line.severity === item.severity));
  const list = byId("log-list");
  list.replaceChildren();
  if (!lines.length) { const empty = document.createElement("li"); empty.className = "empty-state"; empty.textContent = "No matching log lines."; list.append(empty); return; }
  lines.forEach((line) => {
    const row = document.createElement("li");
    row.className = `log-line severity-${line.severity}`;
    const time = document.createElement("time");
    time.textContent = line.timestamp ? new Date(line.timestamp).toLocaleTimeString() : "—";
    const badge = document.createElement("span"); badge.className = "log-severity"; badge.textContent = line.severity;
    const message = document.createElement("span"); message.textContent = line.message || "";
    row.append(time, badge, message); list.append(row);
  });
}

function routeFromHash() {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash || hash === "/" || hash === "/dashboard" || hash === "dashboard-view") return { view: "dashboard" };
  if (hash === "/settings" || hash === "settings-view") return { view: "settings" };
  if (hash === "/backups" || hash === "backups") return { view: "backups" };
  if (hash === "/events" || hash === "events") return { view: "events" };
  if (hash === "/audit" || hash === "audit") return { view: "audit" };
  const match = hash.match(/^\/servers\/([^/]+)(?:\/([^/]+))?$/) || hash.match(/^server\/([^/]+)(?:\/([^/]+))?$/);
  if (match) {
    try {
      return { view: "detail", id: decodeURIComponent(match[1]), tab: match[2] || "console" };
    } catch {
      return { view: "dashboard" };
    }
  }
  return { view: "dashboard" };
}

function sparklinePoints(samples, width = 180, height = 90) {
  const values = samples.length ? samples.map((sample) => typeof sample === "object" ? sample.v : sample) : [0];
  const max = Math.max(1, ...values);
  return values.map((sample, index) => {
    const x = values.length === 1 ? 0 : index / (values.length - 1) * width;
    const y = height - 4 - Math.min(max, Math.max(0, Number(sample) || 0)) / max * (height - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).concat(values.length === 1 ? [`${width.toFixed(1)},${(height - 4).toFixed(1)}`] : []).join(" ");
}

function renderMetricChart(svgId, samples, { unit = "", formatValue = (value) => String(value) } = {}) {
  const svg = byId(svgId);
  if (!svg) return;
  const plot = { x0: 30, x1: 215, y0: 8, y1: 90 };
  const values = samples.map((sample) => Number(sample.v)).filter(Number.isFinite);
  const max = Math.max(1, ...values);
  const points = samples.map((sample, index) => {
    const x = samples.length === 1 ? plot.x0 : plot.x0 + (index / (samples.length - 1)) * (plot.x1 - plot.x0);
    const y = plot.y1 - (Math.min(max, Math.max(0, Number(sample.v) || 0)) / max) * (plot.y1 - plot.y0);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  svg.querySelector(".chart-line").setAttribute("points", points || `${plot.x0},${plot.y1} ${plot.x1},${plot.y1}`);
  svg.querySelector(".chart-ymax").textContent = formatValue(max) + unit;
  svg.querySelector(".chart-ymid").textContent = formatValue(max / 2) + unit;
  const fmt = (time) => new Date(time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  svg.querySelector(".chart-x0").textContent = samples.length ? fmt(samples[0].t) : "—";
  svg.querySelector(".chart-x1").textContent = samples.length ? fmt(samples[samples.length - 1].t) : "—";
}

const STATS_PROFILES = new Set(["minecraft", "terraria-vanilla", "terraria-tmod", "pz-rising"]);
const STATS_WINDOWS = { "1h": 1, "6h": 6, "24h": 24 };
const HEATMAP_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function clearStatsTimer() {
  if (state.detail.statsTimer) window.clearInterval(state.detail.statsTimer);
  state.detail.statsTimer = null;
}

function renderStatsSummary(summary) {
  const countOnly = summary?.player_tracking === "count";
  byId("stats-summary").hidden = countOnly;
  byId("stats-leaderboard").closest(".stats-block").hidden = countOnly;
  byId("stats-heatmap").closest(".stats-block").hidden = countOnly;
  byId("stats-occupancy-block").hidden = !countOnly;
  const total = Number(summary?.total_hours);
  const unique = Number(summary?.unique_players);
  byId("stats-total-hours").textContent = Number.isFinite(total) ? total.toFixed(2) : "—";
  byId("stats-unique-players").textContent = Number.isFinite(unique) ? String(unique) : "—";
  byId("stats-leaderboard-meta").textContent = Number.isFinite(unique) ? `${unique} player${unique === 1 ? "" : "s"}` : "—";
  const latest = Number(summary?.occupancy?.latest);
  byId("stats-occupancy-current").textContent = Number.isFinite(latest) ? `${latest} online` : "Unavailable";
}

function renderStatsLeaderboard(rows) {
  const body = byId("stats-leaderboard");
  body.replaceChildren();
  if (!Array.isArray(rows) || !rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "empty-state";
    cell.colSpan = 4;
    cell.textContent = "No sessions recorded yet.";
    row.append(cell);
    body.append(row);
    return;
  }
  rows.forEach((item) => {
    const row = document.createElement("tr");
    const player = document.createElement("th"); player.scope = "row"; player.textContent = item.player || "Unknown player";
    const hours = document.createElement("td"); hours.textContent = Number.isFinite(Number(item.hours)) ? Number(item.hours).toFixed(2) : "—";
    const sessions = document.createElement("td"); sessions.textContent = Number.isFinite(Number(item.sessions)) ? String(item.sessions) : "—";
    const lastSeen = document.createElement("td");
    const parsed = item.last_seen ? new Date(item.last_seen) : null;
    lastSeen.textContent = parsed && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString() : "—";
    row.append(player, hours, sessions, lastSeen);
    body.append(row);
  });
}

function renderStatsHeatmap(result) {
  const grid = byId("stats-heatmap");
  grid.replaceChildren();
  const buckets = Array.isArray(result?.buckets) ? result.buckets : [];
  const values = buckets.flatMap((row) => Array.isArray(row) ? row.map(Number) : []).filter(Number.isFinite);
  const maximum = Math.max(0, ...values);
  HEATMAP_LABELS.forEach((label, day) => {
    const row = document.createElement("div");
    row.className = "heatmap-row";
    const heading = document.createElement("span");
    heading.className = "heatmap-label";
    heading.textContent = label;
    row.append(heading);
    for (let hour = 0; hour < 24; hour += 1) {
      const value = Number(buckets[day]?.[hour]) || 0;
      const cell = document.createElement("span");
      cell.className = "heatmap-cell";
      cell.style.setProperty("--heat", maximum ? String(Math.min(1, value / maximum)) : "0");
      cell.title = `${label} ${String(hour).padStart(2, "0")}:00 UTC · ${value.toFixed(2)} player-hours`;
      cell.setAttribute("aria-label", cell.title);
      row.append(cell);
    }
    grid.append(row);
  });
}

function renderStatsTpsUnavailable(message) {
  byId("stats-tps-note").textContent = message;
  byId("stats-tps-current").textContent = "—";
  byId("stats-mspt-current").textContent = "—";
  const lines = byId("stats-tps-chart")?.querySelector(".tps-chart-lines");
  if (lines) {
    lines.replaceChildren();
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.classList.add("chart-line"); line.setAttribute("points", "42,150 510,150"); lines.append(line);
  }
}

function renderStatsUnavailable(message) {
  byId("stats-summary").hidden = false;
  byId("stats-leaderboard").closest(".stats-block").hidden = false;
  byId("stats-heatmap").closest(".stats-block").hidden = false;
  byId("stats-occupancy-block").hidden = true;
  renderStatsSummary({});
  renderStatsLeaderboard([]);
  renderStatsHeatmap({ buckets: [] });
  renderStatsTpsUnavailable(message);
}

function renderStatsTps(result) {
  const note = byId("stats-tps-note");
  const samples = (Array.isArray(result?.samples) ? result.samples : [])
    .map((sample) => ({ ...sample, time: Date.parse(sample.ts), tps: Number(sample.tps), mspt: Number(sample.mspt) }))
    .filter((sample) => Number.isFinite(sample.time) && Number.isFinite(sample.tps) && Number.isFinite(sample.mspt))
    .sort((a, b) => a.time - b.time);
  const latest = samples.at(-1);
  byId("stats-tps-current").textContent = latest ? `${Math.min(20, latest.tps).toFixed(2)} TPS` : "—";
  byId("stats-mspt-current").textContent = latest ? `${latest.mspt.toFixed(2)} ms/tick` : "—";
  note.textContent = samples.length ? `${samples.length} samples · ${result.window || "selected window"}` : "No tick samples recorded yet.";
  const svg = byId("stats-tps-chart");
  const group = svg?.querySelector(".tps-chart-lines");
  if (!svg || !group) return;
  group.replaceChildren();
  svg.querySelector(".chart-ymax").textContent = "20 TPS";
  svg.querySelector(".chart-ymid").textContent = "10 TPS";
  if (!samples.length) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.classList.add("chart-line"); line.setAttribute("points", "42,150 510,150"); group.append(line);
    svg.querySelector(".chart-x0").textContent = "—"; svg.querySelector(".chart-x1").textContent = "—";
    return;
  }
  const hours = STATS_WINDOWS[byId("stats-window").value] || 24;
  const end = Date.now();
  const start = end - hours * 60 * 60 * 1000;
  const inWindow = samples.filter((sample) => sample.time >= start && sample.time <= end);
  const visible = inWindow.length ? inWindow : samples;
  const domainStart = start;
  const domainEnd = end;
  const intervals = visible.slice(1).map((sample, index) => sample.time - visible[index].time).filter((value) => value > 0).sort((a, b) => a - b);
  const median = intervals.length ? intervals[Math.floor(intervals.length / 2)] : 60_000;
  const gapLimit = Math.max(1, median * 2);
  const segments = [];
  let segment = [];
  visible.forEach((sample, index) => {
    if (index && sample.time - visible[index - 1].time > gapLimit) { if (segment.length) segments.push(segment); segment = []; }
    segment.push(sample);
  });
  if (segment.length) segments.push(segment);
  const x = (time) => 42 + Math.min(1, Math.max(0, (time - domainStart) / (domainEnd - domainStart))) * 468;
  const y = (value) => 150 - Math.min(20, Math.max(0, value)) / 20 * 138;
  segments.forEach((items) => {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.classList.add("chart-line");
    line.setAttribute("points", items.map((sample) => `${x(sample.time).toFixed(1)},${y(sample.tps).toFixed(1)}`).join(" "));
    group.append(line);
  });
  const fmt = (time) => new Date(time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  svg.querySelector(".chart-x0").textContent = fmt(domainStart);
  svg.querySelector(".chart-x1").textContent = fmt(domainEnd);
}

async function loadStats(id) {
  if (!id || state.detail.tab !== "stats") return;
  const request = ++state.detail.statsRequest;
  if (!STATS_PROFILES.has(id)) { renderStatsUnavailable("Player stats not available for this game."); return; }
  const base = `/api/v1/profiles/${encodeURIComponent(id)}/stats`;
  const window = byId("stats-window")?.value || "24h";
  try {
    const requests = [api(`${base}/summary?days=90`), api(`${base}/heatmap?days=90`)];
    if (id === "minecraft") requests.push(api(`${base}/tps?window=${encodeURIComponent(window)}`));
    const [summary, heatmap, tps] = await Promise.all(requests);
    if (request !== state.detail.statsRequest || state.detail.id !== id) return;
    renderStatsSummary(summary); renderStatsLeaderboard(summary.leaderboard); renderStatsHeatmap(heatmap);
    const tpsBlock = byId("stats-tps-title").closest(".stats-tps-block");
    tpsBlock.hidden = id !== "minecraft";
    if (id === "minecraft") renderStatsTps(tps);
  } catch (error) {
    if (request !== state.detail.statsRequest) return;
    renderStatsUnavailable(error.message || "Stats unavailable.");
    notify(error.message || "Stats unavailable.");
  }
}

function startStatsRefresh(id) {
  clearStatsTimer();
  loadStats(id);
  state.detail.statsTimer = window.setInterval(() => loadStats(id), 60000);
}

function detailProfile(id) {
  return state.profiles.get(id) || { id, display_name: id, operations: [] };
}

function detailStatus(id) {
  return state.statuses.get(id) || { profile_id: id, state: "unknown", health: "unknown" };
}

function detailTabUrl(id, tab) {
  return `#/servers/${encodeURIComponent(id)}/${encodeURIComponent(tab)}`;
}

function commandCatalog(id) {
  const source = window.HORIZON_COMMANDS?.[id];
  const commands = typeof source === "string" ? window.HORIZON_COMMANDS?.[source] : source;
  const result = Array.isArray(commands) ? [...commands] : [];
  if (id === "terraria-tmod") result.push({ cmd: "modlist", args: "", help: "List loaded mods" });
  return result;
}

function renderCommandCatalog(id) {
  const commands = commandCatalog(id);
  const datalist = byId("command-suggestions");
  const catalog = byId("command-catalog");
  datalist.replaceChildren();
  catalog.replaceChildren();
  commands.forEach((entry) => {
    const option = document.createElement("option");
    option.value = entry.cmd;
    option.label = entry.help;
    datalist.append(option);
    const row = document.createElement("button");
    row.type = "button";
    row.className = "command-catalog-row";
    row.innerHTML = `<code></code><span></span><small></small>`;
    row.querySelector("code").textContent = entry.cmd;
    row.querySelector("span").textContent = entry.args;
    row.querySelector("small").textContent = entry.help;
    row.addEventListener("click", () => {
      const input = byId("command-input");
      input.value = `${entry.cmd}${entry.args ? " " : ""}`;
      input.focus();
    });
    catalog.append(row);
  });
}

function setDetailTab(tab) {
  const allowed = ["console", "metrics", "stats", "logs", "backups", "config"];
  const next = allowed.includes(tab) ? tab : "console";
  clearStatsTimer();
  state.detail.tab = next;
  document.querySelectorAll("[data-detail-tab]").forEach((button) => {
    const selected = button.dataset.detailTab === next;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll("#detail-view [role=tabpanel]").forEach((panel) => { panel.hidden = panel.id !== `panel-${next}`; });
  if (next === "logs" || next === "console") loadDetailLogs(state.detail.id);
  if (next === "backups") loadBackups(state.detail.id);
  if (next === "config") renderConfig(state.detail.id);
  if (next === "stats") {
    const tpsBlock = byId("stats-tps-title")?.closest(".stats-tps-block");
    if (tpsBlock) tpsBlock.hidden = state.detail.id !== "minecraft";
    startStatsRefresh(state.detail.id);
  }
}

function patchDetail(id) {
  if (!id) return;
  const profile = detailProfile(id);
  const status = detailStatus(id);
  renderCommandCatalog(id);
  byId("detail-breadcrumb-name").textContent = ` / ${profile.display_name || id}`;
  byId("detail-title").textContent = profile.display_name || id;
  byId("detail-subtitle").textContent = profile.public_endpoint?.host || "private";
  const current = status.state || "unknown";
  const metricAvailability = {
    cpu: status.cpu_percent != null,
    memory: status.rss_bytes != null,
    players: status.players_online != null,
    uptime: status.uptime_seconds != null,
    disk: status.disk_free_bytes != null,
    "disk-io": status.disk_read_bps != null || status.disk_write_bps != null,
  };
  document.querySelectorAll("#panel-metrics [data-metric]").forEach((tile) => {
    tile.hidden = metricAvailability[tile.dataset.metric] === false;
  });
  const badge = byId("detail-status");
  badge.className = `status-badge ${stateClass(current)}`;
  badge.querySelector(".status-text").textContent = statusLabel(current);
  const operationSet = new Set(profile.operations || []);
  const running = current === "running";
  const transitional = ["starting", "stopping"].includes(current);
  byId("detail-start").hidden = running || transitional;
  byId("detail-stop").hidden = !running && current !== "starting";
  byId("detail-restart").hidden = !running;
  byId("detail-force").hidden = !running;
  byId("detail-start").disabled = !["stopped", "failed", "blocked", "unknown"].includes(current) || !operationSet.has("start");
  byId("detail-stop").disabled = !operationSet.has("stop");
  byId("detail-restart").disabled = !operationSet.has("restart");
  byId("detail-force").disabled = !operationSet.has("stop");
  const commandEnabled = running && operationSet.has("command");
  byId("command-input").disabled = !commandEnabled;
  byId("command-send").disabled = !commandEnabled;
  byId("command-input").placeholder = operationSet.has("command")
    ? (running ? "Enter a server command" : "Start this server to use its console")
    : "Console input is unavailable for this profile";
  byId("command-note").textContent = !operationSet.has("command")
    ? "Console input is not available for this profile."
    : running
      ? "Command input is available for this running profile."
      : "Start this server to use its console.";
  byId("rail-cpu").textContent = status.cpu_percent == null ? "—" : `${Number(status.cpu_percent).toFixed(1)}%`;
  byId("rail-memory").textContent = formatBytes(status.rss_bytes);
  byId("rail-players").textContent = status.players_online == null ? "Unavailable" : String(status.players_online);
  byId("rail-players-note").textContent = status.players_online == null ? "Player count unavailable" : "Players observed";
  byId("rail-version").textContent = formatVersion(status.installed_version);
  const configRestart = state.configRestartRequired.get(id) || [];
  byId("rail-version-note").textContent = configRestart.length ? "Config changed · restart required" : status.restart_required ? "Update available · restart required" : status.required_ports_ready ? "Ready on required ports" : "Accepted; waiting for readiness";
  const samples = state.metricSamples.get(id) || { cpu: [], memory: [], players: [] };
  byId("rail-cpu-line").setAttribute("points", sparklinePoints(samples.cpu, 120, 32));
  byId("rail-memory-line").setAttribute("points", sparklinePoints(samples.memory, 120, 32));
  byId("metric-cpu-current").textContent = status.cpu_percent == null ? "Unavailable" : `${Number(status.cpu_percent).toFixed(1)}%`;
  byId("metric-memory-current").textContent = formatBytes(status.rss_bytes);
  byId("metric-players-current").textContent = status.players_online == null ? "Unavailable" : String(status.players_online);
  renderMetricChart("metric-cpu-chart", samples.cpu, { unit: "%", formatValue: (value) => value.toFixed(0) });
  renderMetricChart("metric-memory-chart", samples.memory, { unit: " GiB", formatValue: (value) => value.toFixed(1) });
  renderMetricChart("metric-players-chart", samples.players, { formatValue: (value) => value.toFixed(0) });
  byId("metric-uptime").textContent = uptime(status.uptime_seconds);
  byId("metric-disk-free").textContent = formatBytes(status.disk_free_bytes);
  byId("metric-disk-free-note").textContent = profile.mutable_root || "Profile mutable root";
  byId("metric-disk-io").textContent = status.disk_read_bps == null && status.disk_write_bps == null
    ? "Unavailable"
    : `R ${formatRate(status.disk_read_bps)} · W ${formatRate(status.disk_write_bps)}`;
  const item = state.logs.get(id);
  const lines = (item?.lines || [])
    .filter((line) => !item?.clearedAt || Date.parse(line.timestamp || 0) > item.clearedAt)
    .filter((line) => item?.hideNoise === false || !isNoise(line));
  const hidden = (item?.lines || []).filter((line) => !item?.clearedAt || Date.parse(line.timestamp || 0) > item.clearedAt).filter(isNoise).length;
  const consoleOutput = byId("console-output");
  if (consoleOutput && !consoleOutput.matches(":focus-within") && !item?.loading) {
    consoleOutput.replaceChildren();
    lines.slice(-200).forEach((line) => {
      const row = document.createElement("div");
      row.className = `console-line severity-${line.severity || "info"}`;
      const time = document.createElement("time");
      time.textContent = line.timestamp ? new Date(line.timestamp).toLocaleTimeString() : "—";
      const message = document.createElement("span");
      message.textContent = line.message || "";
      row.append(time, message);
      consoleOutput.append(row);
    });
    if (!item || item.autoScroll !== false) consoleOutput.scrollTop = consoleOutput.scrollHeight;
    byId("console-jump").hidden = !item || item.autoScroll !== false;
  }
  const noiseToggle = byId("console-noise-toggle");
  if (noiseToggle) noiseToggle.checked = item?.hideNoise !== false;
  byId("console-noise-label").textContent = `Hide network noise · ${hidden} hidden`;
}

function configChanges() {
  const changes = {};
  document.querySelectorAll("#config-panel [data-config-key]").forEach((input) => {
    if (input.type === "password" && !input.value) return;
    const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    if (JSON.stringify(value) !== input.dataset.initial) changes[input.dataset.configKey] = value;
  });
  return changes;
}

function updateConfigDiff() {
  const keys = Object.keys(configChanges());
  byId("config-diff").textContent = keys.length ? `${keys.length} change${keys.length === 1 ? "" : "s"} — apply? (${keys.join(", ")})` : "No pending changes.";
  byId("config-apply").disabled = !keys.length;
}

async function renderConfig(id) {
  const profile = state.profiles.get(id) || { id };
  const summary = byId("config-summary");
  summary.replaceChildren();
  const values = [
    ["Profile ID", profile.id],
    ["Display name", profile.display_name],
    ["Operations", Array.isArray(profile.operations) ? profile.operations.join(", ") : "Unavailable"],
    ["Public endpoint", profile.public_endpoint ? `${profile.public_endpoint.protocol || "game"}://${profile.public_endpoint.host || "—"}:${profile.public_endpoint.port || "—"}` : "Private"],
    ["Auto-stop", Number(profile.idle_stop_minutes) > 0 ? `${profile.idle_stop_minutes}m idle` : "off"],
  ];
  values.forEach(([key, value]) => {
    const wrap = document.createElement("div");
    const term = document.createElement("dt"); term.textContent = key;
    const detail = document.createElement("dd"); detail.textContent = value || "Unavailable";
    wrap.append(term, detail); summary.append(wrap);
  });
  const minutes = Number(profile.idle_stop_minutes) || 0;
  byId("idle-stop-enabled").checked = minutes > 0;
  byId("idle-stop-minutes").value = minutes > 0 ? String(minutes) : "30";
  byId("idle-stop-status").textContent = minutes > 0 ? `Auto-stop: ${minutes}m idle` : "Auto-stop: off";
  const list = byId("config-panel");
  list.replaceChildren();
  byId("config-diff").textContent = "Loading editable settings…";
  try {
    const response = await api(`/api/v1/profiles/${encodeURIComponent(id)}/config`);
    state.detail.config = response;
    (Array.isArray(response.settings) ? response.settings : []).forEach((setting) => {
      const row = document.createElement("div");
      const label = document.createElement("label"); label.textContent = setting.key;
      const input = setting.type === "enum" ? document.createElement("select") : document.createElement("input");
      if (setting.type === "enum") (setting.bounds?.choices || []).forEach((choice) => { const option = document.createElement("option"); option.value = choice; option.textContent = choice; input.append(option); });
      else input.type = setting.type === "bool" ? "checkbox" : setting.secret ? "password" : setting.type === "int" ? "number" : "text";
      input.dataset.configKey = setting.key;
      if (input.type === "checkbox") input.checked = Boolean(setting.value); else if (setting.value != null) input.value = String(setting.value);
      if (setting.type === "int") { if (setting.bounds?.min != null) input.min = setting.bounds.min; if (setting.bounds?.max != null) input.max = setting.bounds.max; }
      if (setting.bounds?.max_length != null) input.maxLength = setting.bounds.max_length;
      if (setting.secret) input.placeholder = setting.configured ? "Set · enter to replace" : "Not set · write only";
      input.dataset.initial = JSON.stringify(setting.secret ? "" : setting.value);
      input.addEventListener("input", updateConfigDiff); input.addEventListener("change", updateConfigDiff);
      row.append(label, input); list.append(row);
    });
    updateConfigDiff();
  } catch (error) { byId("config-diff").textContent = error.message || "Config unavailable."; }
}

async function applyConfig(event) {
  event.preventDefault();
  const id = state.detail.id;
  const changes = configChanges();
  const keys = Object.keys(changes);
  if (!id || !keys.length || !window.confirm(`${keys.length} changes — apply?`)) return;
  byId("config-apply").disabled = true;
  try {
    const response = await api(`/api/v1/profiles/${encodeURIComponent(id)}/config`, { method: "POST", body: JSON.stringify({ changes }) });
    const restart = Array.isArray(response.restart_required) ? response.restart_required : [];
    state.configRestartRequired.set(id, restart);
    byId("config-status").textContent = restart.length ? `Applied. Restart required to take effect: ${restart.join(", ")}.` : "Applied. No restart required.";
    patchDetail(id);
    await renderConfig(id);
  } catch (error) { byId("config-status").textContent = error.message || "Config update failed."; updateConfigDiff(); }
}

// SetIdleStop RPC route: config changes stay behind the authenticated mutation path.
async function saveIdleStop(event) {
  event.preventDefault();
  const id = state.detail.id;
  if (!id) return;
  const enabled = byId("idle-stop-enabled").checked;
  const minutes = enabled ? Number(byId("idle-stop-minutes").value) : 0;
  if (enabled && (!Number.isInteger(minutes) || minutes < 5 || minutes > 1440)) {
    byId("idle-stop-status").textContent = "Choose 5–1440 minutes, or turn auto-stop off.";
    return;
  }
  const save = byId("idle-stop-save");
  save.disabled = true;
  try {
    const result = await api(`/api/v1/profiles/${encodeURIComponent(id)}/idle-stop`, { method: "PATCH", body: JSON.stringify({ minutes }) });
    const profile = state.profiles.get(id) || { id };
    profile.idle_stop_minutes = Number(result?.idle_stop_minutes ?? minutes);
    state.profiles.set(id, profile);
    renderConfig(id);
    notify(`${profileLabel(id)} auto-stop saved.`);
  } catch (error) {
    byId("idle-stop-status").textContent = error.message || "Auto-stop update failed.";
  } finally {
    save.disabled = false;
  }
}

async function loadDetailLogs(id) {
  if (!id) return;
  const item = state.logs.get(id) || { query: "", severity: "all", paused: false, lines: [], autoScroll: true, clearedAt: null, hideNoise: true };
  item.loading = true;
  state.logs.set(id, item);
  byId("detail-log-query").value = item.query;
  byId("detail-log-severity").value = item.severity;
  byId("detail-log-pause").textContent = item.paused ? "Resume live logs" : "Pause live logs";
  try {
    const page = await api(`/api/v1/profiles/${encodeURIComponent(id)}/logs?limit=200&severity=all`);
    if (!item.paused && Array.isArray(page.items)) item.lines = page.items;
  } catch (error) { notify(error.message || "Logs unavailable."); }
  item.loading = false;
  renderDetailLogs(item);
  patchDetail(id);
}

function consoleLineText(line) {
  const date = line.timestamp ? new Date(line.timestamp) : null;
  const timestamp = date && !Number.isNaN(date.getTime())
    ? `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}.${String(date.getMilliseconds()).padStart(3, "0")}`
    : "--:--:--.---";
  return `[${timestamp}] [${line.severity || "info"}] ${line.message || ""}`;
}

function downloadConsole(id, lines, includeTimestamps = true) {
  const body = lines.map((line) => includeTimestamps ? consoleLineText(line) : (line.message || "")).join("\n");
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-T:]/g, "");
  const blob = new Blob([body ? `${body}\n` : ""], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${id}-console-${stamp}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function exportStamp(value, fallback = "now") {
  return value ? value.replace(/[-:]/g, "").replace("T", "-") : fallback;
}

async function saveConsoleAs() {
  const id = state.detail.id;
  if (!id) return;
  const from = byId("console-export-from").value;
  const to = byId("console-export-to").value;
  const severity = byId("console-export-severity").value;
  const includeTimestamps = byId("console-export-timestamps").checked;
  const rangeSelected = Boolean(from || to);
  let lines = visibleConsoleLines(id);
  if (rangeSelected) {
    const params = new URLSearchParams({ limit: "5000", severity: "all" });
    if (from) params.set("since", new Date(from).toISOString());
    if (to) params.set("until", new Date(to).toISOString());
    try {
      const page = await api(`/api/v1/profiles/${encodeURIComponent(id)}/logs?${params}`);
      lines = Array.isArray(page.items) ? page.items : [];
    } catch (error) { notify(error.message || "Log export failed."); return; }
  }
  if (severity !== "all") lines = lines.filter((line) => line.severity === severity);
  const body = lines.map((line) => includeTimestamps ? consoleLineText(line) : (line.message || "")).join("\n");
  const blob = new Blob([body ? `${body}\n` : ""], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${id}-console-${exportStamp(from)}-${exportStamp(to)}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
  closeDialog(byId("console-save-as-dialog"));
}

function visibleConsoleLines(id) {
  const item = state.logs.get(id);
  return (item?.lines || []).filter((line) => !item?.clearedAt || Date.parse(line.timestamp || 0) > item.clearedAt).slice(-200);
}

async function sendConsoleCommand() {
  const id = state.detail.id;
  const input = byId("command-input");
  const command = input.value;
  if (!id || !command.trim()) return;
  try {
    await api(`/api/v1/profiles/${encodeURIComponent(id)}/command`, {
      method: "POST",
      body: JSON.stringify({ command }),
    });
    input.value = "";
    notify("Command sent.");
    await loadDetailLogs(id);
  } catch (error) {
    notify(error.message || "Command was not accepted.");
  }
}

async function createBackup(id, protectedBackup = false) {
  try {
    await api(`/api/v1/profiles/${encodeURIComponent(id)}/backups`, { method: "POST", body: JSON.stringify({ protected: protectedBackup }) });
    notify(`${protectedBackup ? "Protected " : ""}backup requested for ${profileLabel(id)}.`);
    await loadBackups(id);
  } catch (error) { notify(error.message || "Backup request failed."); }
}

async function checkForUpdate(id) {
  try {
    const status = await api(`/api/v1/profiles/${encodeURIComponent(id)}/update`);
    if (!status.apply_supported || !status.available_version || status.available_version === status.installed_version) {
      notify(`No update available for ${profileLabel(id)}.`);
      return;
    }
    const prepared = await api(`/api/v1/profiles/${encodeURIComponent(id)}/update/prepare`, { method: "POST", body: "{}" });
    await api("/api/v1/update/confirm", { method: "POST", body: JSON.stringify({ confirmation_id: prepared.confirmation_id }) });
    notify(`Update requested for ${profileLabel(id)}.`);
  } catch (error) { notify(error.message || "Update check failed."); }
}

function renderDetailLogs(item) {
  const query = item.query.trim().toLowerCase();
  const matching = item.lines.filter((line) => (!query || String(line.message || "").toLowerCase().includes(query)) && (item.severity === "all" || line.severity === item.severity));
  const hidden = matching.filter(isNoise).length;
  const lines = matching.filter((line) => item.hideNoise === false || !isNoise(line));
  const list = byId("detail-log-list");
  list.replaceChildren();
  lines.forEach((line) => {
    const row = document.createElement("li"); row.className = `log-line severity-${line.severity}`;
    const time = document.createElement("time"); time.textContent = line.timestamp ? new Date(line.timestamp).toLocaleTimeString() : "—";
    const severity = document.createElement("span"); severity.className = "log-severity"; severity.textContent = line.severity;
    const message = document.createElement("span"); message.textContent = line.message || "";
    row.append(time, severity, message); list.append(row);
  });
  if (!lines.length) { const empty = document.createElement("li"); empty.className = "empty-state"; empty.textContent = "No matching log lines."; list.append(empty); }
  byId("detail-log-footer").textContent = `${lines.length} lines · ${hidden} network-noise lines hidden · secrets redacted`;
  byId("detail-noise-toggle").checked = item.hideNoise !== false;
  byId("detail-noise-label").textContent = `Hide network noise · ${hidden} hidden`;
}

async function loadBackups(id) {
  if (!id) return;
  const list = byId("backup-list");
  try {
    const page = await api(`/api/v1/profiles/${encodeURIComponent(id)}/backups?limit=200`);
    state.detail.backups = Array.isArray(page.items) ? page.items : [];
    const latest = state.detail.backups[0];
    byId("switch-backup").textContent = latest?.created_at ? new Date(latest.created_at).toLocaleString() : "No recent backup recorded";
  } catch { state.detail.backups = []; }
  list.replaceChildren();
  state.detail.backups.forEach((backup) => {
    const row = document.createElement("li"); row.className = "backup-row";
    const idNode = document.createElement("strong"); idNode.textContent = backup.id || "—";
    const time = document.createElement("time"); time.textContent = backup.created_at ? new Date(backup.created_at).toLocaleString() : "—";
    const size = document.createElement("span"); size.textContent = formatBytes(backup.size_bytes);
    const restore = document.createElement("button"); restore.type = "button"; restore.className = "button button-small button-quiet"; restore.textContent = `Restore ${backup.id || ""}`.trim();
    restore.addEventListener("click", (event) => openRestore(id, event.currentTarget, backup.id));
    row.append(idNode, time, size, restore); list.append(row);
  });
  if (!state.detail.backups.length) { const empty = document.createElement("li"); empty.className = "empty-state"; empty.textContent = "No backups loaded."; list.append(empty); }
}

async function renderAggregateBackups() {
  const list = byId("aggregate-backup-list");
  const requestId = ++aggregateBackupRequest;
  list.replaceChildren();
  const profileItems = await Promise.all([...state.profiles].map(async ([id, profile]) => {
    try {
      const page = await api(`/api/v1/profiles/${encodeURIComponent(id)}/backups?limit=200`);
      return (Array.isArray(page.items) ? page.items : []).map((backup) => ({ ...backup, profile_id: id, profile_name: profile.display_name || id }));
    } catch { return []; }
  }));
  if (requestId !== aggregateBackupRequest) return;
  const items = profileItems.flat();
  const uniqueItems = [...new Map(items.map((backup) => [`${backup.profile_id}:${backup.id || ""}`, backup])).values()];
  uniqueItems.forEach((backup) => {
    const row = document.createElement("li"); row.className = "backup-row";
    const label = document.createElement("strong"); label.textContent = `${backup.profile_name} · ${backup.id || "—"}`;
    const time = document.createElement("time"); time.textContent = backup.created_at ? new Date(backup.created_at).toLocaleString() : "—";
    const size = document.createElement("span"); size.textContent = formatBytes(backup.size_bytes);
    const link = document.createElement("a"); link.className = "button button-small button-quiet"; link.href = detailTabUrl(backup.profile_id, "backups"); link.textContent = "View backups";
    row.append(label, time, size, link); list.append(row);
  });
  if (!uniqueItems.length) { const empty = document.createElement("li"); empty.className = "empty-state"; empty.textContent = "No backups loaded."; list.append(empty); }
}

function route() {
  const parsed = routeFromHash();
  if (parsed.view === "detail") {
    if (!state.profiles.has(parsed.id)) {
      state.detail.id = null;
      showView("dashboard");
      return;
    }
    state.detail.id = parsed.id;
    showView("detail"); patchDetail(state.detail.id); setDetailTab(parsed.tab);
  } else {
    clearStatsTimer();
    state.detail.id = null;
    showView(parsed.view);
    if (parsed.view === "backups") renderAggregateBackups();
    if (parsed.view === "events") loadActivity("events");
    if (parsed.view === "audit") loadActivity("audit");
    if (parsed.view === "settings") {
      loadNotifications(byId("notification-profile")?.value || [...state.profiles.keys()][0]);
      loadPerformance();
    }
  }
}

function setupDetail() {
  document.querySelectorAll("[data-detail-tab]").forEach((button) => {
    button.addEventListener("click", () => { if (state.detail.id) window.location.hash = detailTabUrl(state.detail.id, button.dataset.detailTab).slice(1); });
    button.addEventListener("keydown", (event) => {
      if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("[data-detail-tab]")];
      const index = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key.includes("Right") || event.key.includes("Down") ? 1 : -1) + tabs.length) % tabs.length;
      tabs[next].focus(); window.location.hash = detailTabUrl(state.detail.id, tabs[next].dataset.detailTab).slice(1);
    });
  });
  ["start", "stop", "restart"].forEach((operation) => byId(`detail-${operation}`).addEventListener("click", () => mutate(state.detail.id, operation)));
  byId("detail-force").addEventListener("click", (event) => openForce(state.detail.id, event.currentTarget));
  byId("create-backup")?.addEventListener("click", () => createBackup(state.detail.id, false));
  byId("create-protected-backup")?.addEventListener("click", () => createBackup(state.detail.id, true));
  byId("check-update")?.addEventListener("click", () => checkForUpdate(state.detail.id));
  byId("command-send").addEventListener("click", sendConsoleCommand);
  byId("console-output").addEventListener("scroll", (event) => {
    const id = state.detail.id;
    const item = id ? state.logs.get(id) : null;
    if (!item) return;
    const output = event.currentTarget;
    item.autoScroll = output.scrollTop + output.clientHeight >= output.scrollHeight - 8;
    byId("console-jump").hidden = item.autoScroll;
  });
  byId("console-jump").addEventListener("click", () => {
    const item = state.logs.get(state.detail.id);
    if (!item) return;
    item.autoScroll = true;
    const output = byId("console-output");
    output.scrollTop = output.scrollHeight;
    byId("console-jump").hidden = true;
  });
  byId("console-clear").addEventListener("click", () => {
    const item = state.logs.get(state.detail.id);
    if (!item || !item.lines.length) return;
    const last = item.lines[item.lines.length - 1];
    item.clearedAt = Date.parse(last.timestamp || "") || Date.now();
    patchDetail(state.detail.id);
  });
  byId("console-save").addEventListener("click", () => {
    if (state.detail.id) downloadConsole(state.detail.id, visibleConsoleLines(state.detail.id));
  });
  byId("console-noise-toggle").addEventListener("change", (event) => {
    const item = state.logs.get(state.detail.id);
    if (item) { item.hideNoise = event.currentTarget.checked; patchDetail(state.detail.id); }
  });
  byId("console-save-as").addEventListener("click", (event) => {
    byId("console-export-severity").value = state.logs.get(state.detail.id)?.severity || "all";
    setupDialog(byId("console-save-as-dialog"), event.currentTarget);
  });
  byId("console-save-as-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveConsoleAs();
  });
  byId("command-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendConsoleCommand();
    }
  });
  byId("detail-log-query").addEventListener("input", () => { const item = state.logs.get(state.detail.id); if (item) { item.query = byId("detail-log-query").value; renderDetailLogs(item); } });
  byId("detail-log-severity").addEventListener("change", () => { const item = state.logs.get(state.detail.id); if (item) { item.severity = byId("detail-log-severity").value; renderDetailLogs(item); } });
  byId("detail-log-pause").addEventListener("click", () => { const item = state.logs.get(state.detail.id); if (item) { item.paused = !item.paused; byId("detail-log-pause").textContent = item.paused ? "Resume live logs" : "Pause live logs"; } });
  byId("stats-window").addEventListener("change", () => { if (state.detail.tab === "stats") loadStats(state.detail.id); });
  byId("config-form")?.addEventListener("submit", applyConfig);
  byId("detail-noise-toggle").addEventListener("change", (event) => { const item = state.logs.get(state.detail.id); if (item) { item.hideNoise = event.currentTarget.checked; renderDetailLogs(item); patchDetail(state.detail.id); } });
  window.setInterval(() => {
    const id = state.detail.id;
    const item = id ? state.logs.get(id) : null;
    if (id && item && !item.paused && ["console", "logs"].includes(state.detail.tab)) loadDetailLogs(id);
  }, 3000);
  byId("retry-load")?.addEventListener("click", () => load().finally(route));
  byId("logout")?.addEventListener("click", async () => { try { await api("/api/v1/session/revoke", { method: "POST", body: "{}" }); location.reload(); } catch (error) { notify(error.message || "Logout failed."); } });
}

function paletteRoute(id, tab = "console") {
  return `#/servers/${encodeURIComponent(id)}/${encodeURIComponent(tab)}`;
}

function paletteCommands() {
  const commands = [];
  const addNavigation = (id, label, hash, keywords = "") => commands.push({
    id,
    label,
    group: "Navigate",
    keywords: `${label} ${keywords}`,
    run: () => { window.location.hash = hash; },
  });
  addNavigation("dashboard", "Go to Dashboard", "#/", "home overview");
  addNavigation("backups", "Go to Backups", "#/backups", "archives snapshots");
  addNavigation("events", "Go to Events", "#/events", "activity history");
  addNavigation("audit", "Go to Audit", "#/audit", "log review security");
  addNavigation("settings", "Go to Settings", "#/settings", "preferences notifications performance");

  const tabLabels = { console: "Console", metrics: "Metrics", stats: "Stats", logs: "Logs", backups: "Backups", config: "Config" };
  const owner = slotOwnerId();
  profileOrder().forEach((id) => {
    const profile = detailProfile(id);
    const display = profile.display_name || id;
    Object.entries(tabLabels).forEach(([tab, tabLabel]) => addNavigation(
      `${id}-${tab}`,
      `${display} / ${tabLabel}`,
      paletteRoute(id, tab),
      `${id} ${display} ${tab} ${tabLabel}`,
    ));

    const status = detailStatus(id);
    const current = status.state || "unknown";
    const operations = new Set(profile.operations || []);
    const addMutation = (operation, states) => {
      if (!operations.has(operation) || !states.includes(current) || (operation === "start" && owner && owner !== id)) return;
      commands.push({
        id: `${id}-${operation}`,
        label: `${operation[0].toUpperCase()}${operation.slice(1)} ${display}`,
        group: "Server actions",
        keywords: `${id} ${display} ${operation} server action`,
        run: () => mutate(id, operation),
      });
    };
    addMutation("start", ["stopped", "failed", "blocked", "unknown"]);
    addMutation("stop", ["running", "starting"]);
    addMutation("restart", ["running"]);
    if (id !== owner && state.profiles.has(id)) {
      commands.push({
        id: `${id}-switch`,
        label: `Switch to ${display}`,
        group: "Server actions",
        keywords: `${id} ${display} switch active server`,
        run: () => {
          byId("switch-active")?.click();
          const target = byId("switch-target");
          if (!target || ![...target.options].some((option) => option.value === id)) return;
          target.value = id;
          target.dispatchEvent(new Event("change", { bubbles: true }));
        },
      });
    }
  });

  THEMES.forEach((theme) => commands.push({
    id: `theme-${theme}`,
    label: `Switch theme to ${titleCase(theme)}`,
    group: "Appearance",
    keywords: `theme ${theme} color appearance`,
    run: () => applyTheme(theme, true),
  }));
  return commands;
}

window.HORIZON_PALETTE = { getCommands: paletteCommands };

window.addEventListener("game-control-status", (event) => applyStatus(event.data || event.detail));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  api("/api/v1/status").then(applyStatus).catch(() => {});
  if (!stream.source || stream.source.readyState === window.EventSource?.CLOSED || Date.now() - stream.lastEventAt > 45000) connectStream();
});
window.addEventListener("online", () => connectStream());
window.__horizonOpenLogs = openLogs;
window.addEventListener("hashchange", route);
setupShell();
wireDialogForms();
setupDetail();
load().finally(route);
