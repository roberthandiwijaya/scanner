const filters = document.querySelector("#historyFilters");
const rows = document.querySelector("#historyRows");
const status = document.querySelector("#historyStatus");
const pagination = document.querySelector("#historyPagination");
const previous = document.querySelector("#historyPrevious");
const next = document.querySelector("#historyNext");
const themeButton = document.querySelector("#themeToggle");
let page = 1;
let totalPages = 1;
let requestNumber = 0;
let searchTimer;

const lookupLabels = { success: "Found", not_found: "Not found", error: "Error", pending: "Pending", not_requested: "Not requested" };
const videoLabels = { available: "Available", not_recorded: "Not recorded", deleted: "Deleted", missing: "Missing file", archived: "Archived" };

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function cell(row, text) {
  const td = element("td", text);
  row.appendChild(td);
  return td;
}

function formatSize(bytes) {
  if (bytes == null) return "";
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function renderRow(scan) {
  const row = element("tr");
  const timeCell = cell(row);
  const date = new Date(scan.submitted_at);
  const time = element("time", Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString());
  time.dateTime = scan.submitted_at;
  timeCell.appendChild(time);
  if (scan.timestamp_basis === "saved_at") timeCell.appendChild(element("small", "Imported · save time", "history-secondary"));
  cell(row, scan.tracking_number);
  cell(row, scan.mode === "return" ? "Return unboxing" : "Packing");
  const order = cell(row, scan.order_sn || "—");
  if (scan.package_number) order.appendChild(element("small", `Package: ${scan.package_number}`, "history-secondary"));
  cell(row, scan.shipping_carrier || "—");
  const products = cell(row);
  const items = scan.items || [];
  if (items.length) {
    const details = element("details");
    details.appendChild(element("summary", `${items.length} product${items.length === 1 ? "" : "s"} · ${items[0].item_name || "Details"}`));
    const list = element("ul", undefined, "history-products");
    for (const item of items) {
      const li = element("li");
      li.appendChild(element("strong", item.item_name || "Unnamed product"));
      li.appendChild(element("span", [item.model_name, `Qty: ${item.model_quantity_purchased ?? 0}`, item.model_sku ? `SKU: ${item.model_sku}` : ""].filter(Boolean).join(" · "), "history-secondary"));
      list.appendChild(li);
    }
    details.appendChild(list);
    products.appendChild(details);
  } else {
    products.textContent = "—";
  }
  const lookup = cell(row);
  lookup.appendChild(element("span", lookupLabels[scan.lookup_status] || scan.lookup_status, `history-badge history-${scan.lookup_status}`));
  if (scan.lookup_error) lookup.appendChild(element("small", scan.lookup_error, "history-secondary"));
  const video = cell(row);
  video.appendChild(element("span", videoLabels[scan.video_status] || "Not recorded", `history-badge history-${scan.video_status}`));
  const size = scan.recording?.size_bytes ?? scan.recording_size_bytes;
  if (size != null) video.appendChild(element("small", formatSize(size), "history-secondary"));
  if (scan.backup_verified_label) video.appendChild(element("small", `Backup verified: ${scan.backup_verified_label} (last verification; rechecked before removal)`, "history-secondary"));
  if (scan.archive_location) video.appendChild(element("small", scan.archive_location, "history-secondary"));
  if (scan.archived_at) video.appendChild(element("small", `Archived ${new Date(scan.archived_at).toLocaleString()}`, "history-secondary"));
  const actions = cell(row);
  if (scan.video_status === "available") {
    for (const [label, url] of [["View", scan.video_url || scan.recording?.video_url], ["Download", scan.download_url || scan.recording?.download_url]]) {
      // Only app-local video routes may become links.
      if (!url || !url.startsWith("/videos/")) continue;
      const link = element("a", label);
      link.href = url;
      if (label === "View") { link.target = "_blank"; link.rel = "noopener"; }
      actions.appendChild(link);
    }
  } else {
    actions.textContent = "—";
  }
  return row;
}

async function loadHistory() {
  const request = ++requestNumber;
  const query = new URLSearchParams(new FormData(filters));
  query.set("page", String(page));
  query.set("per_page", "25");
  status.textContent = "Loading scan history…";
  status.classList.remove("error");
  rows.replaceChildren();
  pagination.hidden = true;
  try {
    const response = await fetch(`/api/scan-events?${query}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load scan history.");
    if (request !== requestNumber) return;
    totalPages = payload.total_pages;
    if (page > totalPages) { page = totalPages; return loadHistory(); }
    rows.replaceChildren(...payload.items.map(renderRow));
    status.textContent = payload.total ? `${payload.total.toLocaleString()} submission${payload.total === 1 ? "" : "s"} · newest first` : "No scanned labels match these filters.";
    pagination.hidden = !payload.total;
    previous.disabled = page <= 1;
    next.disabled = page >= totalPages;
    document.querySelector("#historyPageInfo").textContent = `Page ${page} of ${totalPages}`;
  } catch (error) {
    if (request !== requestNumber) return;
    status.textContent = `${error.message} Use Refresh to retry.`;
    status.classList.add("error");
  }
}

function reloadFilters() { clearTimeout(searchTimer); page = 1; loadHistory(); }
filters.addEventListener("submit", (event) => { event.preventDefault(); reloadFilters(); });
filters.addEventListener("change", reloadFilters);
document.querySelector("#historySearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(reloadFilters, 300); });
document.querySelector("#clearHistoryFilters").addEventListener("click", () => { filters.reset(); reloadFilters(); });
document.querySelector("#refreshHistory").addEventListener("click", () => { clearTimeout(searchTimer); loadHistory(); });
previous.addEventListener("click", () => { if (page > 1) { page--; loadHistory(); } });
next.addEventListener("click", () => { if (page < totalPages) { page++; loadHistory(); } });
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("scanner-theme", theme);
  themeButton.textContent = theme === "dark" ? "Light mode" : "Dark mode";
}
themeButton.addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
applyTheme(localStorage.getItem("scanner-theme") === "dark" ? "dark" : "light");
loadHistory();
