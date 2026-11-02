// The arrivals log and its filters. This module only renders what getState() says and asks setState to
// change the view; app.js's setState is the only place `state` itself changes, so the URL, the chips, the log and
// the drawer never disagree. Built with createElement/textContent only: callsigns and other API strings never go
// through innerHTML.
import { arrivalToRefresh, arrivalsQuery, attemptChip, attemptReferenceText, attentionTitle, chipMode, cycleChip,
  gate1000Text, parseState, rowTime, rowsInWindow, upsertRow } from "./logic.js";
import { flagChip, pathChip } from "./dom.js";

const REFRESH_DEBOUNCE_MS = 250;
const EXPIRE_INTERVAL_MS = 60000;          // how often rows that have left the time window are dropped

const runwayKey = (icao, ident) => `${icao}/${ident}`;

export function createLog(elements, { getState, setState, onOpen, fetchJson }) {
  let config = null;
  let rows = [];
  let nextCursor = null;
  let reqNum = 0;
  let refreshToken = 0;             // bumped only by reload(): a reload supersedes any in-flight refresh outright
                                     // (a full requery is on its way), but loadMore() must not touch this -- paging
                                     // appends rows, it does not invalidate the ones already on screen
  let rowsEpoch = 0;                // bumped only by a refresh that actually writes to `rows`: tells a reload in
                                     // flight that fresher data has landed since its own fetch was issued, so its
                                     // (now provably stale) snapshot must not overwrite it
  const pendingRefresh = new Set();
  let flushTimer = null;
  const views = new Map();          // arrival_id -> that row's elements, kept in place across a refresh

  function setConfig(cfg) {
    config = cfg;
    elements.attention.closest("label").title = attentionTitle(cfg.attention);
    for (const group of elements.filters.querySelectorAll(".filter-group")) {
      const key = group.dataset.key;
      const chips = group.querySelector(".chips");
      chips.replaceChildren(...valuesFor(key).map(([value, label]) => chipButton(key, value, label)));
    }
  }

  function valuesFor(key) {
    if (key === "airport") return config.airports.map((a) => [a.icao, a.icao]);
    if (key === "runway") {
      return config.airports.flatMap((a) => a.runways.map((r) => [runwayKey(a.icao, r.ident), `${a.icao} ${r.ident}`]));
    }
    if (key === "outcome") return config.outcomes.map((o) => [o, o.replace(/_/g, " ")]);
    if (key === "flags") return config.flags.map((f) => [f, f]);
    return [];
  }

  function chipButton(key, value, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.dataset.key = key;
    button.dataset.value = value;
    button.textContent = label;
    button.addEventListener("click", () => setState(cycleChip(getState(), key, value)));
    return button;
  }

  function renderFilters() {
    const state = getState();
    for (const button of elements.filters.querySelectorAll(".chip")) {
      button.dataset.mode = chipMode(state, button.dataset.key, button.dataset.value);
    }
    elements.windowSelect.value = state.window;
    elements.attention.checked = state.attention;
    elements.rotorcraft.checked = state.rotorcraft;
    elements.ga.checked = state.ga;
    elements.approachKind.value = state.approachKind;
  }

  elements.windowSelect.addEventListener("change", () => {
    setState({ ...getState(), window: elements.windowSelect.value });
  });
  elements.attention.addEventListener("change", () => {
    setState({ ...getState(), attention: elements.attention.checked });
  });
  elements.rotorcraft.addEventListener("change", () => {
    setState({ ...getState(), rotorcraft: elements.rotorcraft.checked });
  });
  elements.ga.addEventListener("change", () => {
    setState({ ...getState(), ga: elements.ga.checked });
  });
  elements.approachKind.addEventListener("change", () => {
    setState({ ...getState(), approachKind: elements.approachKind.value });
  });
  elements.clear.addEventListener("click", () => setState({ ...parseState(""), approach: getState().approach }));
  elements.more.addEventListener("click", loadMore);
  elements.table.addEventListener("click", onTableClick);
  setInterval(dropExpiredRows, EXPIRE_INTERVAL_MS);

  async function reload() {
    const myReq = ++reqNum;
    refreshToken += 1;                              // supersede any refresh already in flight: a full requery is coming
    rows = [];
    nextCursor = null;
    render();
    await fetchArrivals(myReq);
  }

  /** reload()'s own GET, split out so it can retry itself: if a refresh writes to `rows` (rowsEpoch moves on) while
   * this fetch is in flight, applying it regardless would silently revert that row to older data -- this fetch was
   * issued before the refresh's, so it can only be describing an earlier state. Refetching once more, instead of
   * either discarding the response or applying it, is what keeps a refresh applied after a reload was issued from
   * being silently discarded: the retry's own query is fresh enough to include whatever the refresh just did.
   *
   * It retries at most once. An unbounded retry could livelock while events keep arriving -- each attempt racing the
   * next refresh -- and `rows` is blank for the whole of it, because reload() emptied it before this was called, so
   * the livelock the user would see is an empty log. Losing the race twice in a row instead leaves one row showing
   * reload's slightly older copy of it, which the next event for that arrival corrects. */
  async function fetchArrivals(myReq, retried = false) {
    const myEpoch = rowsEpoch;
    try {
      const body = await fetchJson(`api/arrivals?${arrivalsQuery(getState(), Date.now() / 1000, { limit: 50 })}`);
      if (myReq !== reqNum) return;                 // a later reload or filter change has already moved on
      if (myEpoch !== rowsEpoch && !retried) {        // a refresh landed while this fetch was in flight: redo it, once
        await fetchArrivals(myReq, true);
        return;
      }
      rows = body.arrivals;
      nextCursor = body.next_cursor;
      render();
    } catch (err) {
      if (myReq !== reqNum) return;
      showLoadError(err);
    }
  }

  async function loadMore() {
    if (nextCursor === null) return;
    const myReq = ++reqNum;
    try {
      const query = arrivalsQuery(getState(), Date.now() / 1000, { limit: 50, cursor: nextCursor });
      const body = await fetchJson(`api/arrivals?${query}`);
      if (myReq !== reqNum) return;
      rows = [...rows, ...body.arrivals];
      nextCursor = body.next_cursor;
      render();
    } catch (err) {
      if (myReq !== reqNum) return;
      showLoadMoreError(err);
    }
  }

  /** Time moves rows out of the window, but the log fetches only rows that change: without this, a page left open
   * keeps every arrival since it loaded. */
  function dropExpiredRows() {
    const kept = rowsInWindow(rows, getState(), Date.now() / 1000);
    if (kept.length === rows.length) return;
    rows = kept;
    render();
  }

  function onEvent(event) {
    const id = arrivalToRefresh(event);
    if (id === null) return;
    pendingRefresh.add(id);
    if (flushTimer === null) flushTimer = setTimeout(flushRefresh, REFRESH_DEBOUNCE_MS);
  }

  /** A "Load more" is not a reason to abandon a refresh: it appends rows, it does not invalidate the ones already on
   * screen. So this checks refreshToken, not reqNum -- loadMore() never touches refreshToken, only reload() does. */
  async function flushRefresh() {
    flushTimer = null;
    const ids = [...pendingRefresh];
    pendingRefresh.clear();
    const now = Date.now() / 1000;
    const myRefresh = refreshToken;
    for (const id of ids) {
      let body;
      try {
        body = await fetchJson(`api/arrivals?${arrivalsQuery(getState(), now, { arrival_id: id, limit: 1 })}`);
      } catch {
        continue;                                    // a transient failure refreshing one row: a later event retries it
      }
      if (myRefresh !== refreshToken) return;         // a reload happened meanwhile; its own fetch is authoritative
      rows = upsertRow(rows, id, body.arrivals[0] ?? null, nextCursor !== null);
      rowsEpoch += 1;                                 // tell any reload in flight that fresher data has landed
    }
    if (myRefresh === refreshToken) render();
  }

  function onTableClick(event) {
    const tr = event.target.closest("tr[data-arrival]");
    if (!tr) return;
    const button = event.target.closest(".attempt, .path-chip");
    if (button) {
      onOpen(Number(button.dataset.approach));
      return;
    }
    const arrival = rows.find((r) => String(r.arrival_id) === tr.dataset.arrival);
    if (!arrival) return;
    onOpen(arrival.attempts[arrival.attempts.length - 1].approach_id);
  }

  /** A refresh (flushRefresh, dropExpiredRows) or a reload rebuilds `rows` and calls this, most often for arrivals
   * this view never touches: a row keeps its element and is updated in place, keyed by arrival_id, the way live.js's
   * cards are keyed by hex. Rebuilding every row from scratch, as this used to, took keyboard focus off a tabbed-to
   * .attempt button on every refresh (the log polls on a 250 ms debounce) and could drop a click pressed mid-rebuild.
   * A row moves only when its place in the order changes, and a focused element that moved gets its focus back. */
  function render() {
    const focused = elements.table.contains(document.activeElement) ? document.activeElement : null;
    const ids = new Set(rows.map((r) => r.arrival_id));
    for (const [id, view] of views) {
      if (!ids.has(id)) {
        view.tr.remove();
        views.delete(id);
      }
    }
    const tbody = elements.table.tBodies[0];
    rows.forEach((arrival, i) => {
      const view = views.get(arrival.arrival_id) ?? rowView(arrival.arrival_id);
      fillRow(view, arrival);
      if (tbody.children[i] !== view.tr) tbody.insertBefore(view.tr, tbody.children[i] ?? null);
    });
    if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    elements.empty.hidden = rows.length > 0;
    if (rows.length === 0) elements.empty.textContent = "No arrivals match these filters.";
    elements.more.hidden = nextCursor === null;
    elements.more.textContent = "Load more";
  }

  function showLoadError(err) {
    elements.empty.hidden = false;
    elements.empty.textContent = `Could not load arrivals: ${err.message}`;
    elements.more.hidden = true;
  }

  /** A failed "Load more": unlike showLoadError, the button stays up (rows are already showing, and nextCursor is
   * still the one that failed) so a click retries the same page. */
  function showLoadMoreError(err) {
    elements.more.hidden = false;
    elements.more.textContent = `Could not load more: ${err.message} — tap to retry`;
  }

  function rowView(id) {
    const tr = document.createElement("tr");
    tr.dataset.arrival = String(id);
    const time = document.createElement("td");
    const flight = document.createElement("td");
    const type = document.createElement("td");
    const airport = document.createElement("td");
    const attempts = document.createElement("td");
    const gate = document.createElement("td");
    const flags = document.createElement("td");
    tr.append(time, flight, type, airport, attempts, gate, flags);
    const view = { tr, time, flight, type, airport, attempts, gate, flags, attemptButtons: new Map(),
      attemptChips: new Map(), attemptOrder: [] };
    views.set(id, view);
    return view;
  }

  function fillRow(view, arrival) {
    view.tr.toggleAttribute("data-attention", !!arrival.attention);
    view.tr.toggleAttribute("data-emergency", !!arrival.emergency);      // red wins over attention's yellow
    setText(view.time, rowTime(arrival.started_at));
    setText(view.flight, arrival.callsign || arrival.hex);
    setText(view.type, arrival.type_code || "—");
    setText(view.airport, arrival.airport);
    setText(view.gate, gate1000Text(arrival));
    fillAttempts(view, arrival.attempts);
    fillFlags(view, arrival.flags);
  }

  /** The attempt chips, keyed by approach_id the way a row itself is keyed by arrival_id: a button is reused, not
   * recreated, for as long as its attempt stays in the chain, so a click or keyboard focus on it survives another
   * attempt in the same row opening or closing. The cell's children are only rewritten when the chain's own order or
   * membership changes -- not on every refresh, so an unaffected row's buttons are never touched at all.
   *
   * After each button, its attempt's path chip ("Quiet Bridge", "Quiet Bridge → ILS": attemptReferenceText), kept
   * the same way and hidden while it has nothing to say, so a reference that changes between refreshes only changes
   * the chip's text. */
  function fillAttempts(view, attempts) {
    const ids = attempts.map((a) => String(a.approach_id));
    for (const map of [view.attemptButtons, view.attemptChips]) {
      for (const id of [...map.keys()]) {
        if (!ids.includes(id)) map.delete(id);
      }
    }
    const pairs = attempts.map((attempt) => {
      const id = String(attempt.approach_id);
      let button = view.attemptButtons.get(id);
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "attempt";
        button.dataset.approach = id;
        view.attemptButtons.set(id, button);
      }
      setText(button, attemptChip(attempt));
      button.toggleAttribute("data-open", attempt.outcome === null);
      let chip = view.attemptChips.get(id);
      if (!chip) {
        chip = pathChip(attempt);
        view.attemptChips.set(id, chip);
      }
      const text = attemptReferenceText(attempt);
      setText(chip, text);
      chip.hidden = text === "";
      return [button, chip];
    });
    const sameOrder = ids.length === view.attemptOrder.length && ids.every((id, i) => id === view.attemptOrder[i]);
    if (!sameOrder) {
      const nodes = [];
      pairs.forEach(([button, chip], i) => {
        if (i > 0) nodes.push(" → ");
        nodes.push(button, chip);
      });
      view.attempts.replaceChildren(...nodes);
      view.attemptOrder = ids;
    }
  }

  // Flags carry no focusable or user-manipulable element, so unlike the attempt chips they are simply rebuilt --
  // cheaper than diffing, and with nothing for a rebuild to cost here.
  function fillFlags(view, flags) {
    view.flags.replaceChildren(...flags.map(flagChip));
  }

  return { setConfig, renderFilters, reload, onEvent };
}

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}
