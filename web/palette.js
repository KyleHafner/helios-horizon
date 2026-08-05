const byId = (id) => document.getElementById(id);
const dialog = byId("command-palette");
const search = byId("palette-search");
const results = byId("palette-list");
const trigger = byId("palette-trigger");
const closeButton = byId("palette-close");
let commands = [];
let filtered = [];
let selected = 0;
let returnFocus = null;

function normalize(value) {
  return String(value || "").toLocaleLowerCase().trim();
}

function fuzzyScore(query, command) {
  const haystack = normalize(`${command.label} ${command.keywords || ""}`);
  if (!query) return 0;
  const terms = query.split(/\s+/).filter(Boolean);
  let score = 0;
  for (const term of terms) {
    const exact = haystack.indexOf(term);
    if (exact >= 0) {
      score += 120 - Math.min(80, exact);
      continue;
    }
    let cursor = 0;
    let spanStart = -1;
    for (const character of term) {
      const found = haystack.indexOf(character, cursor);
      if (found < 0) return null;
      if (spanStart < 0) spanStart = found;
      cursor = found + 1;
    }
    score += Math.max(1, 60 - (cursor - spanStart) * 2);
  }
  return score;
}

function refreshCommands() {
  commands = window.HORIZON_PALETTE?.getCommands?.() || [];
}

function getFocusable() {
  return [...dialog.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])")]
    .filter((node) => !node.disabled && node.offsetParent !== null);
}

function updateSelection(next) {
  if (!filtered.length) {
    selected = 0;
    search.removeAttribute("aria-activedescendant");
    return;
  }
  selected = (next + filtered.length) % filtered.length;
  [...results.querySelectorAll("[role=option]")].forEach((node, index) => {
    const active = index === selected;
    node.classList.toggle("is-selected", active);
    node.setAttribute("aria-selected", String(active));
  });
  search.setAttribute("aria-activedescendant", `palette-option-${selected}`);
}

function render() {
  const query = normalize(search.value);
  filtered = commands
    .map((command, index) => ({ command, index, score: fuzzyScore(query, command) }))
    .filter((item) => item.score !== null)
    .sort((a, b) => b.score - a.score || a.command.label.localeCompare(b.command.label))
    .map((item) => item.command);
  results.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("p");
    empty.className = "palette-empty";
    empty.textContent = "No matching commands.";
    results.append(empty);
    updateSelection(0);
    return;
  }
  let group = "";
  filtered.forEach((command, index) => {
    if (command.group !== group) {
      group = command.group;
      const heading = document.createElement("p");
      heading.className = "palette-group";
      heading.textContent = group;
      results.append(heading);
    }
    const option = document.createElement("button");
    option.type = "button";
    option.id = `palette-option-${index}`;
    option.className = "palette-option";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.dataset.index = String(index);
    const label = document.createElement("span");
    label.className = "palette-option-label";
    label.textContent = command.label;
    const groupLabel = document.createElement("small");
    groupLabel.textContent = command.group;
    option.append(label, groupLabel);
    option.addEventListener("click", () => run(index));
    results.append(option);
  });
  updateSelection(Math.min(selected, filtered.length - 1));
}

function close() {
  if (dialog.open) dialog.close("cancel");
  const focus = returnFocus;
  returnFocus = null;
  if (focus && document.contains(focus)) focus.focus();
}

function run(index = selected) {
  const command = filtered[index];
  if (!command) return;
  const opener = returnFocus;
  close();
  command.run?.({ trigger: opener });
}

function open(opener = document.activeElement) {
  if (!dialog || dialog.open || [...document.querySelectorAll("dialog[open]")].some((node) => node !== dialog)) return;
  refreshCommands();
  returnFocus = opener && opener !== document.body ? opener : trigger;
  search.value = "";
  selected = 0;
  render();
  dialog.showModal();
  search.focus();
}

function onKeydown(event) {
  if (!dialog.open) return;
  if (event.key === "Escape") {
    event.preventDefault();
    close();
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    updateSelection(selected + (event.key === "ArrowDown" ? 1 : -1));
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    const option = event.target.closest?.("[role=option]");
    run(option ? Number(option.dataset.index) : selected);
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = getFocusable();
  if (!focusable.length) return;
  const index = focusable.indexOf(document.activeElement);
  if (event.shiftKey && index <= 0) {
    event.preventDefault();
    focusable.at(-1).focus();
  } else if (!event.shiftKey && (index < 0 || index === focusable.length - 1)) {
    event.preventDefault();
    focusable[0].focus();
  }
}

search.addEventListener("input", render);
dialog.addEventListener("keydown", onKeydown);
dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
closeButton.addEventListener("click", close);
trigger.addEventListener("click", (event) => open(event.currentTarget));
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    open(document.activeElement);
  }
});
