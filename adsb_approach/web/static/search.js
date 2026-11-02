// The Find a flight page: the search held in the URL, the results table in the Arrivals
// layout with grey rows for flights that have no approach logged, and the two drawers, the approach drawer an
// arrival opens and a chart-free one for a grey row. A search runs on submit, not as you type: it reads a track file
// for every grey row on the page. Built with createElement/textContent only: callsigns, registrations and labels
// are API strings and never go through innerHTML.
import { attemptChip, clockSecondsText, clockText, externalGlobeLabel, gate1000Text, rowTime } from "./logic.js";
import { flagChip, pathChip } from "./dom.js";
import { countsText, emergencyText, factsRows, heardText, parseSearchState, rowBar, searchParams, toSearchString, whereText } from "./searchlogic.js";
import { createDrawer } from "./drawer.js";

const PAGE_SIZE = 50;

const $ = (id) => document.getElementById(id);
const form = $("search-form");
const queryInput = $("q");
const windowSelect = $("window");
const fromInput = $("from");
const toInput = $("to");
const countsEl = $("counts");
const table = $("results");
const emptyEl = $("results-empty");
const moreButton = $("more");
const approachDrawer = $("drawer");
const flightDrawer = $("flight-drawer");

let state = parseSearchState(location.search);
let rows = [];
let nextCursor = null;
let reqNum = 0;                  // a later search supersedes any still in flight, and a "Load more" begun before it
let loadingMore = false;
let flightOpener = null;         // focus goes back here when the grey-row drawer closes, if it is still in the page

/** `seconds` as the viewer reads it at `nowS` (default: now): with its date unless that is `nowS`'s day. A span's end
 * is asked for at the span's start, so that it repeats the date only when it falls on another day. */
function clock(seconds, nowS = Date.now() / 1000) {
  return clockText(seconds, nowS);
}

/** `clock` with the seconds, for the emergency note: a run can start and end inside one minute. */
function clockSeconds(seconds, nowS = Date.now() / 1000) {
  return clockSecondsText(seconds, nowS);
}

async function fetchJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  const body = await r.json().catch(() => ({}));
  if (r.status !== 200) {
    const err = new Error(body.detail || `${url}: ${r.status}`);
    err.status = r.status;                    // so a caller can tell a 404 from other failures (e.g. the drawer)
    throw err;
  }
  return body;
}

// Only one drawer is open at a time. The page opens no event stream, so an approach opened here is a snapshot: it
// does not follow a live attempt the way the Approaches page's drawer does (drawer.onEvent is never called).
const drawer = createDrawer(
  { drawer: approachDrawer, title: $("drawer-title"), emergency: $("drawer-emergency"), attempts: $("drawer-attempts"),
    tar1090: $("drawer-tar1090"), globe: $("drawer-globe"), external: $("drawer-external"), norm: $("drawer-norm"),
    path: $("drawer-path"), pathChip: $("drawer-path-chip"), pathSource: $("drawer-path-source"),
    height: $("chart-height"), cross: $("chart-cross"), crossTitle: $("chart-cross-title"), speed: $("chart-speed"),
    gates: $("drawer-gates"), metar: $("drawer-metar") },
  { onOpen: openApproach, fetchJson, clock },
);

// Moving from one drawer to the other must return focus to what opened each, not to the element the first drawer's
// closing would focus: the element the person used is taken before that drawer closes, and that drawer closes without
// restoring focus.
function openApproach(id) {
  const opener = document.activeElement;
  closeFlight({ restoreFocus: false });
  drawer.open(id, opener);
}

function closeApproach({ restoreFocus = true } = {}) {
  if (!approachDrawer.hidden) drawer.close({ restoreFocus });
}

/** Fill the grey-row drawer from the row object itself: a flight with no approach has no detail endpoint, and the
 * API sent everything the drawer shows. */
function openFlight(row) {
  const opener = document.activeElement;
  closeApproach({ restoreFocus: false });
  if (flightDrawer.hidden) flightOpener = opener;
  $("flight-title").textContent = `${row.callsign || row.hex} · ${row.label}`;
  $("flight-heard").textContent = `${heardText(row, clock)} No approach was logged for this flight, `
    + "so there are no charts." + (row.no_approach_logged ? " It looks like an arrival the matcher missed." : "");
  const emergency = $("flight-emergency");
  emergency.replaceChildren(...row.emergency_runs.map((run) => paragraph(emergencyText(run, clockSeconds))));
  emergency.hidden = row.emergency_runs.length === 0;
  $("flight-tar1090").href = row.tar1090_url;
  const globe = $("flight-globe");
  globe.hidden = row.external_globe_url === null;
  if (row.external_globe_url === null) {
    globe.removeAttribute("href");
    globe.textContent = "";
  } else {
    globe.href = row.external_globe_url;
    globe.textContent = `Open on ${externalGlobeLabel(row.external_globe_url)}`;
  }
  const facts = factsRows(row).flatMap(([label, value]) => [element("dt", label), element("dd", value)]);
  $("flight-facts").replaceChildren(...facts);
  flightDrawer.hidden = false;
  $("flight-title").focus();
}

function closeFlight({ restoreFocus = true } = {}) {
  if (flightDrawer.hidden) return;
  flightDrawer.hidden = true;
  if (restoreFocus && flightOpener?.isConnected) flightOpener.focus();
  flightOpener = null;
}

$("drawer-close").addEventListener("click", () => closeApproach());
$("flight-close").addEventListener("click", () => closeFlight());
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  closeApproach();
  closeFlight();
});

function element(tag, text) {
  const el = document.createElement(tag);
  el.textContent = text;
  return el;
}

const paragraph = (text) => element("p", text);

function cell(...children) {
  const td = document.createElement("td");
  td.append(...children);
  return td;
}

/** The Flight cell: the callsign (the hex when it has none), with the registration beside it in muted text because
 * news reports often give only the tail number. A registration that is the callsign, as it is for most private
 * aircraft, is not shown twice. */
function flightCell(row) {
  const td = cell(row.callsign || row.hex);
  if (row.registration && row.registration !== row.callsign) {
    const reg = element("span", row.registration);
    reg.className = "reg";
    td.append(" ", reg);
  }
  return td;
}

function arrivalCells(arrival) {
  const attempts = [];
  arrival.attempts.forEach((attempt, i) => {
    if (i > 0) attempts.push(" → ");
    const button = element("button", attemptChip(attempt));
    button.type = "button";
    button.className = "attempt";
    button.dataset.approach = String(attempt.approach_id);
    button.toggleAttribute("data-open", attempt.outcome === null);
    attempts.push(button);
    const chip = pathChip(attempt);
    if (!chip.hidden) attempts.push(chip);
  });
  return [cell(rowTime(arrival.started_at)), flightCell(arrival), cell(arrival.type_code || "—"),
    cell(arrival.airport), cell(...attempts), cell(gate1000Text(arrival)), cell(...arrival.flags.map(flagChip))];
}

function flightCells(flight) {
  const where = element("span", whereText(flight));
  where.className = "where";
  return [cell(rowTime(flight.heard_from)), flightCell(flight), cell(flight.type_code || "—"),
    cell(flight.label_airport || "—"), cell(where), cell("—"), cell(...flight.emergency_flags.map(flagChip))];
}

/** One result row. `index` is the row's place in `rows`, which a click looks it up by. The bar down its left edge
 * is data-attention, data-emergency or data-seen, from rowBar: attention and emergency keep the selectors the
 * Arrivals log uses, and the grey bar is the page's own. */
function rowElement(row, index) {
  const tr = document.createElement("tr");
  tr.dataset.row = String(index);
  const bar = rowBar(row);
  if (bar !== null) tr.dataset.bar = bar;
  tr.toggleAttribute("data-attention", bar === "attention");
  tr.toggleAttribute("data-emergency", bar === "emergency");
  tr.toggleAttribute("data-seen", bar === "seen");
  if (row.kind === "flight") {
    tr.tabIndex = 0;                  // an arrival's chips are buttons; a grey row has none, so the row takes focus
    tr.append(...flightCells(row));
  } else {
    tr.append(...arrivalCells(row));
  }
  return tr;
}

function onRowActivate(event) {
  const tr = event.target.closest("tr[data-row]");
  if (!tr) return;
  const row = rows[Number(tr.dataset.row)];
  if (row.kind === "flight") {
    openFlight(row);
    return;
  }
  const button = event.target.closest(".attempt, .path-chip");
  openApproach(button ? Number(button.dataset.approach) : row.attempts[row.attempts.length - 1].approach_id);
}

table.addEventListener("click", onRowActivate);
table.addEventListener("keydown", (event) => {
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("tr[data-row]")) {
    event.preventDefault();
    onRowActivate(event);
  }
});

function addRows(added) {
  const first = rows.length;
  rows = [...rows, ...added];
  table.tBodies[0].append(...added.map((row, i) => rowElement(row, first + i)));
  table.hidden = rows.length === 0;
  moreButton.hidden = nextCursor === null;
  moreButton.textContent = "Load more";
}

function showMessage(text) {
  emptyEl.hidden = text === null;
  emptyEl.textContent = text ?? "";
}

function clearResults() {
  rows = [];
  nextCursor = null;
  table.tBodies[0].replaceChildren();
  table.hidden = true;
  moreButton.hidden = true;
  showMessage(null);
}

function query(extra = {}) {
  const params = searchParams(state, Date.now() / 1000);
  params.set("limit", String(PAGE_SIZE));
  for (const [key, value] of Object.entries(extra)) params.set(key, value);
  return `api/search?${params}`;
}

/** Run the search the state describes, from the first page. */
async function runSearch() {
  const myReq = ++reqNum;
  loadingMore = false;
  const focused = document.activeElement;      // closing a drawer moves focus back to the row that opened it
  closeApproach();
  closeFlight();
  clearResults();
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  if (state.q === "") {
    countsEl.textContent = "";
    showMessage("Type a callsign, registration or hex, then press Search.");
    return;
  }
  countsEl.textContent = "Searching…";
  try {
    const body = await fetchJson(query());
    if (myReq !== reqNum) return;
    nextCursor = body.next_cursor;
    countsEl.textContent = countsText(body.counts, state.q);
    addRows(body.rows);
  } catch (err) {
    if (myReq !== reqNum) return;
    countsEl.textContent = "";
    showMessage(`Could not search: ${err.message}`);
  }
}

async function loadMore() {
  if (nextCursor === null || loadingMore) return;
  const myReq = reqNum;
  loadingMore = true;
  try {
    const body = await fetchJson(query({ cursor: nextCursor }));
    if (myReq !== reqNum) return;
    nextCursor = body.next_cursor;
    addRows(body.rows);
  } catch (err) {
    if (myReq !== reqNum) return;
    moreButton.hidden = false;
    moreButton.textContent = `Could not load more: ${err.message} — tap to retry`;   // the same page is retried
  } finally {
    if (myReq === reqNum) loadingMore = false;
  }
}

moreButton.addEventListener("click", loadMore);

/** The custom window's two days show only while Custom is chosen. */
function showRange() {
  for (const input of [fromInput, toInput]) input.closest("label").hidden = windowSelect.value !== "custom";
}

function fillForm() {
  queryInput.value = state.q;
  windowSelect.value = state.window;
  fromInput.value = state.from ?? "";
  toInput.value = state.to ?? "";
  showRange();
}

windowSelect.addEventListener("change", showRange);

// Each submitted search is a history entry, so Back steps through past searches. Submitting the search
// already showing refreshes it without adding an entry.
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const next = { q: queryInput.value.trim(), window: windowSelect.value, from: fromInput.value || null,
    to: toInput.value || null };
  if (next.q === "") {
    queryInput.value = "";
    queryInput.focus();
    return;
  }
  state = next;
  const search = toSearchString(state);
  if (search === location.search) history.replaceState(null, "", search);
  else history.pushState(null, "", search);
  queryInput.value = state.q;
  runSearch();
});

window.addEventListener("popstate", () => {
  state = parseSearchState(location.search);
  fillForm();
  runSearch();
});

fillForm();
runSearch();
