// Wires the page together: state from the URL, the health banner, live cards, the arrivals log and its
// filters, the detail drawer, and the reconnecting event stream.
import { EventStream, bannerFor, clockText, liveQuery, liveRunChanged, liveRunId, parseState, viewChange } from "./logic.js";
import { createDrawer } from "./drawer.js";
import { createLive } from "./live.js";
import { createLog } from "./log.js";

const $ = (id) => document.getElementById(id);
const bannerEl = $("banner");
const streamStateEl = $("stream-state");

let state = parseState(location.search);
let streamState = "connecting";
let live = null;
let lastRunId = null;             // the collector run last seen in a health report; liveRunChanged watches it for a restart

function clock(seconds) {
  return clockText(seconds, Date.now() / 1000);
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

function showBanner(banner) {
  bannerEl.hidden = banner === null;
  if (banner === null) return;
  bannerEl.textContent = banner.text;
  bannerEl.dataset.level = banner.level;
}

let healthReq = 0;      // the interval and a stream-state change can each have a request in flight at once; an
                         // older one must not paint the banner after a newer one already has

async function refreshHealth() {
  const myReq = ++healthReq;
  let health;
  try {
    health = await fetchJson("api/health");
  } catch {
    health = null;
  }
  if (myReq !== healthReq) return;         // a newer refreshHealth() has already started; its answer wins
  showBanner(bannerFor(health, streamState, clock));
  // The collector's start-up sweep (store/db.py) closes a run's open approaches without writing events, so a page
  // left open through a restart would otherwise keep their cards forever: notice the new run and reload them.
  const runId = liveRunId(health?.collector);
  if (liveRunChanged(lastRunId, runId)) {
    fetchJson(`api/live?${liveQuery(state)}`).then((body) => live.load(body.cards)).catch(() => {});
  }
  if (runId !== null) lastRunId = runId;
}

function onState(name, delay) {
  streamState = name;
  streamStateEl.dataset.state = name;
  streamStateEl.textContent = name === "open" ? "live"
    : name === "retrying" ? `reconnecting in ${Math.round(delay / 1000)} s`
    : name;
  refreshHealth();
}

function onOpen(id) {
  setState({ ...state, approach: id });
}

const drawer = createDrawer(
  { drawer: $("drawer"), title: $("drawer-title"), emergency: $("drawer-emergency"), attempts: $("drawer-attempts"),
    tar1090: $("drawer-tar1090"), globe: $("drawer-globe"), external: $("drawer-external"), norm: $("drawer-norm"),
    path: $("drawer-path"), pathChip: $("drawer-path-chip"), pathSource: $("drawer-path-source"),
    height: $("chart-height"), cross: $("chart-cross"), crossTitle: $("chart-cross-title"), speed: $("chart-speed"),
    gates: $("drawer-gates"), metar: $("drawer-metar") },
  { onOpen, fetchJson, clock },
);

$("drawer-close").addEventListener("click", () => setState({ ...state, approach: null }));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.approach !== null) setState({ ...state, approach: null });
});

const log = createLog(
  { table: $("log"), empty: $("log-empty"), more: $("more"), filters: $("filters"),
    windowSelect: $("window"), attention: $("attention"), rotorcraft: $("rotorcraft"), ga: $("ga"),
    approachKind: $("approach-kind"), clear: $("clear-filters") },
  { getState: () => state, setState, onOpen, fetchJson },
);

/** The only place `state` changes: the URL, the filter chips, the log, the live cards and the drawer are kept from
 * disagreeing. */
function setState(next) {
  const change = viewChange(state, next);
  state = next;
  history.replaceState(null, "", change.search || location.pathname);
  log.renderFilters();
  if (change.reloadLog) log.reload();
  if (change.reloadLive) fetchJson(`api/live?${liveQuery(state)}`).then((body) => live.load(body.cards)).catch(() => {});
  if (change.drawer === "open") drawer.open(state.approach);
  if (change.drawer === "close") drawer.close();
}

function onEvent(event) {
  live.apply(event);
  log.onEvent(event);
  drawer.onEvent(event);
}

async function start() {
  let config, liveBody;
  try {
    [config, liveBody] = await Promise.all([fetchJson("api/config"), fetchJson(`api/live?${liveQuery(state)}`)]);
  } catch {
    showBanner(bannerFor(null, streamState, clock));
    setTimeout(start, 5000);
    return;
  }
  live = createLive({ list: $("live"), empty: $("live-empty"), getState: () => state, onOpen });
  live.setConfig(config);
  live.load(liveBody.cards);
  log.setConfig(config);
  log.renderFilters();
  log.reload();
  if (state.approach !== null) drawer.open(state.approach);
  const stream = new EventStream(liveBody.last_event_id, { onEvent, onReload: () => location.reload(), onState });
  stream.start();               // EventStream.open() calls onState("connecting") at once, which itself calls
                                 // refreshHealth() -- a second explicit call here would just be the same request twice
  setInterval(refreshHealth, 30000);
}

start();
