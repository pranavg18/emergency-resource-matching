"use strict";

// Map client for the emergency matching system. Every call goes to this page's
// own origin (/api/...); client/web.py forwards it to the API or coordinator.

const TYPES = ["water", "food", "medicine", "transportation", "shelter"];
const REQUEST_COLORS = {
  PENDING: "--pending", MATCHED: "--matched", IN_PROGRESS: "--progress",
  COMPLETED: "--completed", UNMATCHED: "--unmatched", CANCELLED: "--cancelled",
};
const RESOURCE_COLORS = { AVAILABLE: "--available", BUSY: "--busy", OFFLINE: "--offline" };
const NODE_COLORS = { UP: "--up", SYNCING: "--syncing", DOWN: "--down" };
const CLOSED = new Set(["COMPLETED", "CANCELLED"]);
const POLL_MS = 2000;
const DEFAULT_VIEW = { center: [28.61, 77.2], zoom: 12 };

const state = {
  requests: new Map(),
  resources: new Map(),
  selected: null, // {kind: "request" | "resource", id}
  requestTotals: { total: 0, open: 0, closedOmitted: 0 }, // across all stored requests, not just those listed
  maxRadiusKm: 50,
  shownSelection: "", // what the selection panel currently shows; redraw only when it changes
};

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (n) => Number(n).toFixed(6);

// --- API (api() itself is in api.js) ----------------------------------------

function errorText(result) {
  const error = result.data && result.data.error;
  if (error && typeof error === "object") return `${error.code}: ${error.message}`;
  return `HTTP ${result.status}`;
}

function logActivity(text, ok) {
  const item = document.createElement("li");
  item.className = ok ? "ok" : "err";
  item.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
  $("activity").prepend(item);
  while ($("activity").children.length > 50) $("activity").lastChild.remove();
}

async function act(label, method, path, body) {
  const result = await api(method, path, body);
  logActivity(`${label} → ${result.ok ? result.status : errorText(result)}`, result.ok);
  if (result.ok) await refresh();
  return result;
}

// --- Map --------------------------------------------------------------------

const map = L.map("map").setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const linksLayer = L.layerGroup().addTo(map);
const resourcesLayer = L.layerGroup().addTo(map);
const requestsLayer = L.layerGroup().addTo(map);
const markers = { request: new Map(), resource: new Map() };
let radiusCircle = null;

const pickedMarker = L.marker(DEFAULT_VIEW.center, {
  draggable: true,
  icon: L.divIcon({ className: "", html: '<div class="picked-icon"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
  title: "Picked location",
});

function pick(lat, lon) {
  $("lat").value = fmt(lat);
  $("lon").value = fmt(lon);
  pickedMarker.setLatLng([lat, lon]);
  if (!map.hasLayer(pickedMarker)) pickedMarker.addTo(map);
}

map.on("click", (e) => pick(e.latlng.lat, e.latlng.lng));
pickedMarker.on("dragend", () => {
  const { lat, lng } = pickedMarker.getLatLng();
  pick(lat, lng);
});
for (const id of ["lat", "lon"]) {
  $(id).addEventListener("change", () => {
    const lat = parseFloat($("lat").value), lon = parseFloat($("lon").value);
    if (Number.isFinite(lat) && Number.isFinite(lon)) pick(lat, lon);
  });
}

function pickedLocation() {
  const lat = parseFloat($("lat").value), lon = parseFloat($("lon").value);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const location = { lat, lon };
  const label = $("label").value.trim();
  if (label) location.label = label;
  return location;
}

// --- Rendering --------------------------------------------------------------

function visible(kind, value) {
  const type = $("type-filter").value;
  if (type && value.type !== type) return false;
  if (kind === "request") return $("show-closed").checked || !CLOSED.has(value.status);
  return $("show-offline").checked || value.status !== "OFFLINE";
}

function tooltip(kind, v) {
  const where = v.location.label ? ` · ${v.location.label}` : "";
  return kind === "request"
    ? `Request · ${v.type} · ${v.status}${where}`
    : `Resource · ${v.type} · ${v.status} · ${v.availability} left${where}`;
}

// Leaflet treats a string tooltip as HTML, and labels come from stored records,
// so the tooltip is always a DOM node holding plain text.
function textNode(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

function setTooltip(marker, text) {
  if (marker.tooltipText === text) return;
  marker.tooltipText = text;
  if (marker.getTooltip()) marker.setTooltipContent(textNode(text)); else marker.bindTooltip(textNode(text));
}

// Records can share a spot (a request placed right at its resource), and a
// resource marker would always cover the request beneath it. A click therefore
// looks for every visible record drawn within a few pixels; with more than one
// it offers a list to choose from, so each keeps its exact position.
const OVERLAP_PX = 14;

function markerClicked(kind, id) {
  const marker = markers[kind].get(id);
  const here = map.latLngToContainerPoint(marker.getLatLng());
  const nearby = [];
  for (const [otherKind, store] of [["request", state.requests], ["resource", state.resources]]) {
    for (const [otherId, other] of markers[otherKind]) {
      const value = store.get(otherId);
      if (value && map.latLngToContainerPoint(other.getLatLng()).distanceTo(here) <= OVERLAP_PX) {
        nearby.push({ kind: otherKind, id: otherId, value });
      }
    }
  }
  if (nearby.length <= 1) return select(kind, id);

  const popup = L.popup({ maxWidth: 340 });
  const box = document.createElement("div");
  box.className = "picker";
  const list = document.createElement("div");
  list.className = "picker-list";
  for (const item of nearby) {
    list.append(pickerRow(item, () => { map.closePopup(popup); select(item.kind, item.id); }));
  }
  box.append(textNode(`${nearby.length} records here:`), list);
  popup.setLatLng(marker.getLatLng()).setContent(box).openOn(map);
}

// One row per record: a short ID and status, then its details or label, so
// records of the same type and status can still be told apart.
function pickerRow({ kind, id, value }, onClick) {
  const row = button("", onClick);
  const head = document.createElement("strong");
  head.textContent = kind === "request"
    ? `Request ${id.slice(0, 8)} · ${value.type} · ${value.status}`
    : `Resource ${id.slice(0, 8)} · ${value.type} · ${value.status} · ${value.availability} left`;
  const note = document.createElement("span");
  note.className = "muted";
  const text = (kind === "request" ? value.details : "") || value.location.label || "";
  note.textContent = text.length > 60 ? `${text.slice(0, 59)}…` : text;
  row.append(head, note);
  return row;
}

function requestMarker(v) {
  const color = css(REQUEST_COLORS[v.status] || "--muted");
  const style = { radius: 8, color: "#fff", weight: 2, fillColor: color, fillOpacity: 0.95 };
  let marker = markers.request.get(v.id);
  if (!marker) {
    marker = L.circleMarker([v.location.lat, v.location.lon], style).addTo(requestsLayer);
    marker.on("click", (e) => { L.DomEvent.stopPropagation(e); markerClicked("request", v.id); });
    markers.request.set(v.id, marker);
  }
  marker.setLatLng([v.location.lat, v.location.lon]).setStyle(style);
  setTooltip(marker, tooltip("request", v));
}

function resourceMarker(v) {
  const color = css(RESOURCE_COLORS[v.status] || "--muted");
  const html = `<div class="resource-icon" style="background:${color}">${Math.min(v.availability, 999)}</div>`;
  const icon = L.divIcon({ className: "", html, iconSize: [22, 22], iconAnchor: [11, 11] });
  let marker = markers.resource.get(v.id);
  if (!marker) {
    marker = L.marker([v.location.lat, v.location.lon], { icon }).addTo(resourcesLayer);
    marker.on("click", (e) => { L.DomEvent.stopPropagation(e); markerClicked("resource", v.id); });
    markers.resource.set(v.id, marker);
  }
  marker.setLatLng([v.location.lat, v.location.lon]).setIcon(icon);
  setTooltip(marker, tooltip("resource", v));
}

function renderMarkers() {
  for (const [kind, store, draw] of [["request", state.requests, requestMarker], ["resource", state.resources, resourceMarker]]) {
    for (const [id, marker] of markers[kind]) {
      const value = store.get(id);
      if (!value || !visible(kind, value)) {
        marker.remove();
        markers[kind].delete(id);
      }
    }
    for (const value of store.values()) if (visible(kind, value)) draw(value);
  }

  linksLayer.clearLayers();
  for (const v of state.requests.values()) {
    const resource = v.assigned_resource_id && state.resources.get(v.assigned_resource_id);
    if (resource && visible("request", v) && (v.status === "MATCHED" || v.status === "IN_PROGRESS")) {
      L.polyline([[v.location.lat, v.location.lon], [resource.location.lat, resource.location.lon]],
                 { color: css(REQUEST_COLORS[v.status]), weight: 2, dashArray: "4 4", interactive: false })
        .addTo(linksLayer);
    }
  }

  const { total, open, closedOmitted } = state.requestTotals;
  const omitted = closedOmitted ? ` · ${closedOmitted} oldest closed not shown` : "";
  $("counts").textContent = `${total} requests (${open} open)${omitted} · ${state.resources.size} resources`;
  if (closedOmitted) {
    $("counts").title = "Every open request is on the map; only the oldest completed or cancelled ones are left off.";
  } else {
    $("counts").removeAttribute("title");
  }
}

function badge(text, colorVar) {
  const span = document.createElement("span");
  span.className = "badge";
  span.style.background = css(colorVar);
  span.textContent = text;
  return span;
}

function renderCluster(data) {
  const box = $("cluster");
  box.replaceChildren();
  for (const shard of data.shards) {
    const card = document.createElement("div");
    card.className = "shard";
    const head = document.createElement("div");
    head.className = "shard-head";
    head.append(`Shard ${shard.shard}`);
    const info = document.createElement("span");
    info.className = "muted";
    info.textContent = shard.primary ? `primary ${shard.primary}` : "no primary";
    head.append(info);
    card.append(head);
    if (shard.nodes.length === 0) card.append("no nodes registered");
    for (const node of shard.nodes) {
      const row = document.createElement("div");
      row.className = "node";
      const name = document.createElement("span");
      const inSync = shard.in_sync.includes(node.node_id) ? "" : " · not in sync";
      name.textContent = `${node.node_id}`;
      const role = document.createElement("span");
      role.className = "role";
      role.textContent = `${node.role}${inSync} · ${node.keys ?? "–"} keys`;
      row.append(name, role, badge(node.state, NODE_COLORS[node.state] || "--muted"));
      card.append(row);
    }
    box.append(card);
  }
}

// --- Selection and actions --------------------------------------------------

function select(kind, id) {
  state.selected = { kind, id };
  renderSelection();
}

function addDetail(dl, term, value) {
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  if (value instanceof Node) dd.append(value); else dd.textContent = value;
  dl.append(dt, dd);
}

function button(text, onClick, enabled = true) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  b.disabled = !enabled;
  b.addEventListener("click", onClick);
  return b;
}

function renderSelection() {
  const section = $("selection");
  const sel = state.selected;
  const value = sel && (sel.kind === "request" ? state.requests : state.resources).get(sel.id);
  // Polling calls this every 2 s. Rebuilding unchanged buttons would swallow
  // clicks and wipe what is being typed, so redraw only on a real change.
  const assigned = value && value.assigned_resource_id && state.resources.get(value.assigned_resource_id);
  const shown = JSON.stringify([sel, value, assigned && [assigned.type, assigned.location.label]]);
  if (shown === state.shownSelection) return;
  state.shownSelection = shown;
  if (radiusCircle) { radiusCircle.remove(); radiusCircle = null; }
  if (!value) { section.hidden = true; return; }
  section.hidden = false;

  const dl = $("selection-details");
  const actions = $("selection-actions");
  dl.replaceChildren();
  actions.replaceChildren();
  addDetail(dl, "ID", value.id);
  addDetail(dl, "Type", value.type);
  addDetail(dl, "Location", `${fmt(value.location.lat)}, ${fmt(value.location.lon)}${value.location.label ? ` (${value.location.label})` : ""}`);

  if (sel.kind === "request") {
    $("selection-title").textContent = "Selected request";
    addDetail(dl, "Status", badge(value.status, REQUEST_COLORS[value.status] || "--muted"));
    addDetail(dl, "Details", value.details);
    const assigned = value.assigned_resource_id && state.resources.get(value.assigned_resource_id);
    addDetail(dl, "Assigned to", value.assigned_resource_id
      ? `${value.assigned_resource_id.slice(0, 8)}…${assigned ? ` (${assigned.type}, ${assigned.location.label || "resource"})` : ""}`
      : "—");
    addDetail(dl, "Created", new Date(value.created_at).toLocaleString());

    const s = value.status;
    const statusPath = `requests/${value.id}/status`;
    actions.append(
      button("Match now", () => act("Match request", "POST", `match/${value.id}`), s === "PENDING" || s === "UNMATCHED"),
      button("Start (in progress)", () => act("Start request", "PUT", statusPath, { status: "IN_PROGRESS" }), s === "MATCHED"),
      button("Complete", () => act("Complete request", "PUT", statusPath, { status: "COMPLETED" }), s === "IN_PROGRESS"),
      button("Cancel", () => act("Cancel request", "PUT", statusPath, { status: "CANCELLED" }), !CLOSED.has(s)),
    );
    if (!CLOSED.has(s)) {
      radiusCircle = L.circle([value.location.lat, value.location.lon], {
        radius: state.maxRadiusKm * 1000, color: css("--accent"), weight: 1, dashArray: "6 6", fillOpacity: 0.04,
        interactive: false,
      }).addTo(map);
    }
  } else {
    $("selection-title").textContent = "Selected resource";
    addDetail(dl, "Status", badge(value.status, RESOURCE_COLORS[value.status] || "--muted"));
    addDetail(dl, "Availability", String(value.availability));
    addDetail(dl, "Serving", String(Object.keys(value.assigned_request_ids || {}).length));

    const statusPath = `resources/${value.id}/status`;
    const offline = value.status === "OFFLINE";
    actions.append(
      button(offline ? "Bring online" : "Take offline",
             () => act(offline ? "Bring resource online" : "Take resource offline", "PUT", statusPath,
                       { status: offline ? "AVAILABLE" : "OFFLINE" })),
    );
    const amount = document.createElement("input");
    amount.type = "number";
    amount.min = "0";
    amount.value = String(value.availability);
    amount.style.width = "80px";
    amount.setAttribute("aria-label", "New availability");
    actions.append(amount, button("Set availability", () => {
      const n = parseInt(amount.value, 10);
      if (Number.isInteger(n) && n >= 0) act("Set availability", "PUT", statusPath, { availability: n });
    }));
  }
}

// --- Forms -------------------------------------------------------------------

for (const dropdown of document.querySelectorAll(".type-select")) {
  for (const t of TYPES) dropdown.add(new Option(t, t));
}
for (const t of TYPES) $("type-filter").add(new Option(t, t));

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab")) other.classList.toggle("active", other === tab);
    for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== tab.dataset.tab;
  });
}

$("request-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const location = pickedLocation();
  if (!location) return logActivity("Pick a location on the map first", false);
  const form = e.target;
  const result = await act("Create request", "POST", "requests",
                           { type: form.type.value, details: form.details.value.trim(), location });
  if (result.ok) { form.details.value = ""; $("label").value = ""; select("request", result.data.id); }
});

$("resource-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const location = pickedLocation();
  if (!location) return logActivity("Pick a location on the map first", false);
  const form = e.target;
  const result = await act("Register resource", "POST", "resources",
                           { type: form.type.value, availability: parseInt(form.availability.value, 10), location });
  if (result.ok) { $("label").value = ""; select("resource", result.data.id); }
});

for (const id of ["show-closed", "show-offline", "type-filter"]) {
  $(id).addEventListener("change", () => { renderMarkers(); renderSelection(); });
}

$("fit").addEventListener("click", () => {
  const points = [...markers.request.values(), ...markers.resource.values()].map((m) => m.getLatLng());
  if (points.length) map.fitBounds(L.latLngBounds(points).pad(0.2), { maxZoom: 15 });
});

// --- Polling -----------------------------------------------------------------

let refreshing = false;

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [reqs, res, cluster] = await Promise.all([api("GET", "all-requests"), api("GET", "resources"), api("GET", "cluster")]);
    const ok = reqs.ok && res.ok && cluster.ok;
    const pill = $("connection");
    pill.className = `pill ${ok ? "ok" : "bad"}`;
    pill.textContent = ok ? "connected" : `problem: ${errorText([reqs, res, cluster].find((r) => !r.ok))}`;
    if (reqs.ok) {
      state.requests = new Map(reqs.data.requests.map((v) => [v.id, v]));
      state.requestTotals = { total: reqs.data.count, open: reqs.data.open, closedOmitted: reqs.data.closed_omitted };
    }
    if (res.ok) state.resources = new Map(res.data.resources.map((v) => [v.id, v]));
    if (cluster.ok) renderCluster(cluster.data);
    renderMarkers();
    renderSelection();
  } finally {
    refreshing = false;
  }
}

(async () => {
  const config = await api("GET", "config");
  if (config.ok) state.maxRadiusKm = config.data.max_radius_km;
  await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
})();
