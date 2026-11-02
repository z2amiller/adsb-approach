// The Find a flight page's logic without its DOM: the search held in the URL and sent to
// /api/search, the bar down a row's left edge, and the wording of a row's chip, the match counts and the grey-row
// drawer. Times from the API are UNIX seconds.
import { WINDOWS, fmt, isEmergencyFlag } from "./logic.js";

const WINDOW_NAMES = [...Object.keys(WINDOWS), "custom"];
const DAY = /^\d{4}-\d{2}-\d{2}$/;

// What the squawks and the ADS-B emergency states mean, for the drawer's note (emergency.py's SQUAWK_FLAGS and
// STATE_FLAGS).
const SQUAWK_WORDS = { 7700: "general emergency", 7600: "radio failure", 7500: "unlawful interference" };
const STATE_WORDS = { general: "general emergency", nordo: "radio failure", unlawful: "unlawful interference",
  downed: "aircraft downed", minfuel: "minimum fuel" };

/** `text` if it is a real YYYY-MM-DD calendar day, else null: 2026-02-30 is not one. */
function dayOrNull(text) {
  if (text === null || !DAY.test(text)) return null;
  const [y, m, d] = text.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d ? text : null;
}

/** The search held in the URL: the text, the window (one of the Arrivals page's, or "custom") and, for a custom
 * window, its first and last day. The window defaults to All: users often won't know the day. Anything
 * missing or malformed takes its default. */
export function parseSearchState(search) {
  const p = new URLSearchParams(search);
  const window = p.get("window");
  return { q: (p.get("q") ?? "").trim(), window: WINDOW_NAMES.includes(window) ? window : "all",
    from: dayOrNull(p.get("from")), to: dayOrNull(p.get("to")) };
}

/** The URL query for a search, "?q=VIR19&window=all": the days only for a custom window, so one search has one URL. */
export function toSearchString(state) {
  const p = new URLSearchParams({ q: state.q, window: state.window });
  if (state.window === "custom") {
    if (state.from) p.set("from", state.from);
    if (state.to) p.set("to", state.to);
  }
  return `?${p}`;
}

/** Local midnight at the start of `day` (YYYY-MM-DD) plus `plusDays` days, in UNIX seconds. Built from the calendar
 * date so that a day with a daylight-saving change is its own length, not 24 hours. */
function localMidnight(day, plusDays) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d + plusDays).getTime() / 1000;
}

/** The /api/search query for a search at nowS: the text, and `since` and `until` in UNIX seconds. A rolling window
 * (1 hour to 7 days) is `since` alone, as on the Arrivals page; a custom one runs from local midnight at the start of
 * its first day to local midnight after its last, so the last day is included; All sets neither. Two days entered
 * the wrong way round (YYYY-MM-DD sorts as text) are the days between them, not an empty range. */
export function searchParams(state, nowS) {
  const p = new URLSearchParams({ q: state.q.trim() });
  if (state.window === "custom") {
    const reversed = state.from && state.to && state.from > state.to;
    const [first, last] = reversed ? [state.to, state.from] : [state.from, state.to];
    if (first) p.set("since", String(localMidnight(first, 0)));
    if (last) p.set("until", String(localMidnight(last, 1)));
  } else if (WINDOWS[state.window]) {
    p.set("since", String(Math.floor(nowS - WINDOWS[state.window])));
  }
  return p;
}

/** The bar down a row's left edge, as the API's row kinds give it: "emergency" (red) for an arrival the collector
 * flagged or a flight whose track latched an EMERGENCY_ flag (isEmergencyFlag), "attention" (yellow) for an arrival
 * that needs a look or a flight whose only latched flag is MINIMUM_FUEL, as an approach row draws it, "seen" (grey)
 * for any other flight heard with no approach logged, and null for the rest. Red wins over the others. */
export function rowBar(row) {
  if (row.kind === "flight") {
    if (row.emergency_flags.some(isEmergencyFlag)) return "emergency";
    return row.emergency_flags.length ? "attention" : "seen";
  }
  if (row.emergency) return "emergency";
  return row.attention ? "attention" : null;
}

/** A grey row's chip: its label, "arrival KSQL", with "· no approach logged" when an arrival at an airport the
 * analyser scores has no approach row, which is a matcher miss. */
export function whereText(row) {
  return row.no_approach_logged ? `${row.label} · no approach logged` : row.label;
}

/** The line above the results: "8 flights match VIR19: 6 with an approach, 2 without." `counts` is the response's
 * {flights, with_approach}, over every match and not just the rows on the page. */
export function countsText(counts, q) {
  const { flights, with_approach: withApproach } = counts;
  if (flights === 0) return `No flights match ${q}.`;
  return `${flights} ${flights === 1 ? "flight matches" : "flights match"} ${q}: ${withApproach} with an approach, `
    + `${flights - withApproach} without.`;
}

/** An altitude in feet with thousands separators, "2,600 ft", or an em dash when it is missing. The separator is
 * fixed to en-US rather than the browser's locale, which the page's other numbers do not follow either. */
function feet(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value).toLocaleString("en-US")} ft`;
}

/** The drawer's red note for one emergency run (tracks.py's EmergencyRun): "Squawked 7600 (radio failure) from Oct 16,
 * 07:14:49 PM to 07:17:02 PM, starting at 2,600 ft." A run's `code` is the squawk when the squawk latched it and the
 * ADS-B state ("nordo") otherwise, so a four-digit code reads as a squawk and anything else as a state. `clock(seconds,
 * nowS)` formats epoch seconds for a viewer at `nowS` (default: now), as logic.js's clockSecondsText does (the seconds
 * matter: the 30 s latch is the whole test for red, so a run can sit inside one minute), so the end is asked for
 * relative to the start and shows its date only when it falls on another local day. A code with no words
 * here is shown bare; a run with no altitude drops the "starting at". */
export function emergencyText(run, clock) {
  const squawk = Object.hasOwn(SQUAWK_WORDS, run.code) || /^\d{4}$/.test(run.code);
  const words = squawk ? SQUAWK_WORDS : STATE_WORDS;
  const meaning = Object.hasOwn(words, run.code) ? ` (${words[run.code]})` : "";
  const what = squawk ? `Squawked ${run.code}` : `ADS-B emergency state ${run.code}`;
  const start = typeof run.alt_ft === "number" ? `, starting at ${feet(run.alt_ft)}` : "";
  return `${what}${meaning} from ${clock(run.start)} to ${clock(run.end, run.start)}${start}.`;
}

/** The span a flight was heard, "Heard Oct 21, 02:56 PM – 03:41 PM." The end carries its date only when it falls on
 * another local day than the start (`clock` as in emergencyText). */
export function heardText(row, clock) {
  return `Heard ${clock(row.heard_from)} – ${clock(row.heard_to, row.heard_from)}.`;
}

/** The grey-row drawer's facts as [label, value] pairs: the aircraft (registration, type, class and hex, whichever
 * are known), where (the label), the lowest and highest altitudes heard with how far the lowest was from the nearest
 * airport, and the squawks. What the track did not give is an em dash. */
export function factsRows(row) {
  const from = row.label_airport ? ` from ${row.label_airport}` : "";
  const near = row.lowest_nm === null ? "" : `, ${fmt(row.lowest_nm, 1)} nm${from}`;
  return [
    ["Aircraft", [row.registration, row.type_code, row.traffic_class, row.hex].filter(Boolean).join(" · ")],
    ["Where", row.label],
    ["Lowest", row.lowest_ft === null ? "—" : `${feet(row.lowest_ft)}${near}`],
    ["Highest", feet(row.highest_ft)],
    ["Squawk", row.squawks.length ? row.squawks.join(", ") : "—"],
  ];
}
