// Unit tests for searchlogic.js: the search in the URL and in the request, the bar down a row's left edge, the wording
// of the "where" chip, the match counts, the emergency and heard lines, and the facts list. The times use
// `new Date(y, m, d)` so they hold in any time zone. In the style of logic.test.mjs.
import assert from "node:assert/strict";
import { test } from "node:test";

import * as L from "../../adsb_approach/web/static/logic.js";
import * as S from "../../adsb_approach/web/static/searchlogic.js";

const localSeconds = (y, m, d) => new Date(y, m, d).getTime() / 1000;

test("parseSearchState reads the text and window, and an unknown window is All", () => {
  assert.deepEqual(S.parseSearchState(""), { q: "", window: "all", from: null, to: null });
  assert.deepEqual(S.parseSearchState("?q=VIR19&window=6h"), { q: "VIR19", window: "6h", from: null, to: null });
  assert.equal(S.parseSearchState("?q=VIR19&window=fortnight").window, "all");
  assert.equal(S.parseSearchState("?q=VIR19&window=toString").window, "all");       // not an own key of the windows
  assert.equal(S.parseSearchState("?q=%20%20vir19%20").q, "vir19");                  // trimmed, otherwise as typed
  for (const window of ["1h", "6h", "24h", "7d", "all", "custom"]) {
    assert.equal(S.parseSearchState(`?q=VIR19&window=${window}`).window, window);
  }
});

test("parseSearchState keeps a custom range's days only when they are real YYYY-MM-DD dates", () => {
  const custom = S.parseSearchState("?q=N00210&window=custom&from=2026-10-01&to=2026-10-05");
  assert.deepEqual(custom, { q: "N00210", window: "custom", from: "2026-10-01", to: "2026-10-05" });
  for (const bad of ["yesterday", "2026-13-01", "2026-02-30", "2026-1-5", "20261001", ""]) {
    const state = S.parseSearchState(`?q=N00210&window=custom&from=${bad}&to=${bad}`);
    assert.deepEqual([state.from, state.to], [null, null], bad);
  }
  assert.equal(S.parseSearchState("?q=N00210&window=custom&from=2028-02-29").from, "2028-02-29");     // a leap day
});

test("toSearchString writes the search, with the days only for a custom window", () => {
  assert.equal(S.toSearchString({ q: "VIR19", window: "all", from: null, to: null }), "?q=VIR19&window=all");
  assert.equal(S.toSearchString({ q: "VIR19", window: "6h", from: "2026-10-01", to: "2026-10-05" }),
    "?q=VIR19&window=6h");                                      // days left over from an earlier choice are not kept
  assert.equal(S.toSearchString({ q: "VIR19", window: "custom", from: "2026-10-01", to: "2026-10-05" }),
    "?q=VIR19&window=custom&from=2026-10-01&to=2026-10-05");
  assert.equal(S.toSearchString({ q: "VIR19", window: "custom", from: "2026-10-01", to: null }),
    "?q=VIR19&window=custom&from=2026-10-01");
});

test("parseSearchState and toSearchString round-trip, including text that needs escaping", () => {
  for (const state of [
    { q: "VIR19", window: "all", from: null, to: null },
    { q: "N-00210", window: "24h", from: null, to: null },
    { q: "A&B=C #1", window: "7d", from: null, to: null },
    { q: "407699", window: "custom", from: "2026-10-01", to: "2026-10-05" },
    { q: "407699", window: "custom", from: null, to: "2026-10-05" },
  ]) {
    assert.deepEqual(S.parseSearchState(S.toSearchString(state)), state);
  }
});

test("searchParams sends the text and a rolling window as since alone", () => {
  const now = 1_792_000_000.75;
  const spans = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800 };
  for (const [window, span] of Object.entries(spans)) {
    const p = S.searchParams({ q: "VIR19", window, from: null, to: null }, now);
    assert.equal(p.get("q"), "VIR19");
    assert.equal(p.get("since"), String(Math.floor(now - span)), window);
    assert.equal(p.has("until"), false, window);
    assert.deepEqual([...p.keys()], ["q", "since"], window);
  }
  const all = S.searchParams({ q: "VIR19", window: "all", from: null, to: null }, now);
  assert.deepEqual([...all.keys()], ["q"]);
  assert.equal(S.searchParams({ q: "  vir19 ", window: "all", from: null, to: null }, now).get("q"), "vir19");
});

test("searchParams bounds a custom window by local midnight, the end day included", () => {
  const now = 1_792_000_000;
  const p = S.searchParams({ q: "VIR19", window: "custom", from: "2026-10-01", to: "2026-10-05" }, now);
  assert.equal(p.get("since"), String(localSeconds(2026, 9, 1)));          // the start of the first day
  assert.equal(p.get("until"), String(localSeconds(2026, 9, 6)));          // the start of the day after the last
  const oneDay = S.searchParams({ q: "x1", window: "custom", from: "2026-10-05", to: "2026-10-05" }, now);
  assert.equal(Number(oneDay.get("until")) - Number(oneDay.get("since")), localSeconds(2026, 9, 6) - localSeconds(2026, 9, 5));
  const monthEnd = S.searchParams({ q: "x1", window: "custom", from: "2026-12-31", to: "2026-12-31" }, now);
  assert.equal(monthEnd.get("until"), String(localSeconds(2027, 0, 1)));   // across a month and a year
  const fromOnly = S.searchParams({ q: "x1", window: "custom", from: "2026-10-01", to: null }, now);
  assert.deepEqual([...fromOnly.keys()], ["q", "since"]);
  const toOnly = S.searchParams({ q: "x1", window: "custom", from: null, to: "2026-10-05" }, now);
  assert.deepEqual([...toOnly.keys()], ["q", "until"]);
  const neither = S.searchParams({ q: "x1", window: "custom", from: null, to: null }, now);
  assert.deepEqual([...neither.keys()], ["q"]);
  const stale = S.searchParams({ q: "x1", window: "6h", from: "2026-10-01", to: "2026-10-05" }, now);
  assert.equal(stale.get("since"), String(Math.floor(now - 21600)));       // days from an earlier choice are ignored
  assert.equal(stale.has("until"), false);
});

test("searchParams reads a custom range entered backwards as the days between them", () => {
  const now = 1_792_000_000;
  const forwards = S.searchParams({ q: "VIR19", window: "custom", from: "2026-10-01", to: "2026-10-05" }, now);
  const backwards = S.searchParams({ q: "VIR19", window: "custom", from: "2026-10-05", to: "2026-10-01" }, now);
  assert.equal(backwards.get("since"), forwards.get("since"));
  assert.equal(backwards.get("until"), forwards.get("until"));
  assert.ok(Number(backwards.get("since")) < Number(backwards.get("until")));
  assert.equal(backwards.get("since"), String(localSeconds(2026, 9, 1)));
  assert.equal(backwards.get("until"), String(localSeconds(2026, 9, 6)));
  const across = S.searchParams({ q: "x1", window: "custom", from: "2027-01-02", to: "2026-12-30" }, now);   // across a year
  assert.equal(across.get("since"), String(localSeconds(2026, 11, 30)));
  assert.equal(across.get("until"), String(localSeconds(2027, 0, 3)));
});

test("rowBar gives an arrival's emergency or attention, and a flight's emergency or the grey of a heard flight", () => {
  assert.equal(S.rowBar({ kind: "arrival", emergency: true, attention: true }), "emergency");   // red wins over yellow
  assert.equal(S.rowBar({ kind: "arrival", emergency: false, attention: true }), "attention");
  assert.equal(S.rowBar({ kind: "arrival", emergency: false, attention: false }), null);
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: ["EMERGENCY_NORDO"] }), "emergency");
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: ["EMERGENCY_NORDO", "EMERGENCY_GENERAL"] }), "emergency");
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: [] }), "seen");
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: [], emergency_runs: [] }), "seen");   // a list is not a flag
});

test("rowBar draws a flight whose only flag is MINIMUM_FUEL as attention, like an approach row, and red for any emergency", () => {
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: ["MINIMUM_FUEL"] }), "attention");
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: ["EMERGENCY_NORDO", "MINIMUM_FUEL"] }), "emergency");
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: ["MINIMUM_FUEL", "EMERGENCY_NORDO"] }), "emergency");
  assert.equal(S.rowBar({ kind: "flight", emergency_flags: ["EMERGENCY_DOWNED"] }), "emergency");
});

test("whereText is the label, and says so when an arrival at a scored airport has no approach", () => {
  assert.equal(S.whereText({ label: "arrival KSFO", no_approach_logged: true }), "arrival KSFO · no approach logged");
  assert.equal(S.whereText({ label: "arrival KSQL", no_approach_logged: false }), "arrival KSQL");
  assert.equal(S.whereText({ label: "overflight FL350", no_approach_logged: false }), "overflight FL350");
});

test("countsText counts the flights, with and without an approach", () => {
  assert.equal(S.countsText({ flights: 8, with_approach: 6 }, "VIR19"), "8 flights match VIR19: 6 with an approach, 2 without.");
  assert.equal(S.countsText({ flights: 1, with_approach: 0 }, "N00210"), "1 flight matches N00210: 0 with an approach, 1 without.");
  assert.equal(S.countsText({ flights: 1, with_approach: 1 }, "UAL2177"), "1 flight matches UAL2177: 1 with an approach, 0 without.");
  assert.equal(S.countsText({ flights: 0, with_approach: 0 }, "XYZ"), "No flights match XYZ.");
});

test("emergencyText reads a squawk run and a state run, with the altitude when it is known", () => {
  const clock = (s) => String(s);
  const squawk = { flag: "EMERGENCY_NORDO", code: "7600", start: 100, end: 200, alt_ft: 2600 };
  assert.equal(S.emergencyText(squawk, clock), "Squawked 7600 (radio failure) from 100 to 200, starting at 2,600 ft.");
  assert.equal(S.emergencyText({ ...squawk, code: "7700", flag: "EMERGENCY_GENERAL" }, clock),
    "Squawked 7700 (general emergency) from 100 to 200, starting at 2,600 ft.");
  assert.equal(S.emergencyText({ ...squawk, code: "7500", flag: "EMERGENCY_UNLAWFUL" }, clock),
    "Squawked 7500 (unlawful interference) from 100 to 200, starting at 2,600 ft.");
  const state = { ...squawk, code: "nordo" };
  assert.equal(S.emergencyText(state, clock), "ADS-B emergency state nordo (radio failure) from 100 to 200, starting at 2,600 ft.");
  const words = { general: "general emergency", nordo: "radio failure", unlawful: "unlawful interference",
    downed: "aircraft downed", minfuel: "minimum fuel" };
  for (const [code, text] of Object.entries(words)) {
    assert.equal(S.emergencyText({ ...squawk, code }, clock), `ADS-B emergency state ${code} (${text}) from 100 to 200, starting at 2,600 ft.`);
  }
  assert.equal(S.emergencyText({ ...squawk, alt_ft: null }, clock), "Squawked 7600 (radio failure) from 100 to 200.");
  assert.equal(S.emergencyText({ ...state, alt_ft: null }, clock), "ADS-B emergency state nordo (radio failure) from 100 to 200.");
  assert.equal(S.emergencyText({ ...squawk, alt_ft: 0 }, clock), "Squawked 7600 (radio failure) from 100 to 200, starting at 0 ft.");
  assert.equal(S.emergencyText({ ...squawk, code: "7000" }, clock), "Squawked 7000 from 100 to 200, starting at 2,600 ft.");
  assert.equal(S.emergencyText({ ...state, code: "lifeguard" }, clock),
    "ADS-B emergency state lifeguard from 100 to 200, starting at 2,600 ft.");
});

// A clock in the shape the page's is: `clock(seconds, nowS)` reads `seconds` as a viewer at `nowS` would, with its day
// ("D1", for seconds 100 to 199) unless that is nowS's day. With no nowS the viewer is at second 0, on day 0.
const dayClock = (s, nowS = 0) => (Math.floor(s / 100) === Math.floor(nowS / 100) ? "" : `D${Math.floor(s / 100)} `) + `t${s}`;

test("heardText gives the span a flight was heard, with the end's day only when it differs from the start's", () => {
  assert.equal(S.heardText({ heard_from: 150, heard_to: 180 }, dayClock), "Heard D1 t150 – t180.");
  assert.equal(S.heardText({ heard_from: 150, heard_to: 250 }, dayClock), "Heard D1 t150 – D2 t250.");
  assert.equal(S.heardText({ heard_from: 100, heard_to: 200 }, (s) => `t${s}`), "Heard t100 – t200.");
});

test("emergencyText shows its end's day only when it differs from its start's", () => {
  const run = { flag: "EMERGENCY_NORDO", code: "7600", start: 150, end: 180, alt_ft: 2600 };
  assert.equal(S.emergencyText(run, dayClock), "Squawked 7600 (radio failure) from D1 t150 to t180, starting at 2,600 ft.");
  assert.equal(S.emergencyText({ ...run, end: 250 }, dayClock),
    "Squawked 7600 (radio failure) from D1 t150 to D2 t250, starting at 2,600 ft.");
  assert.equal(S.emergencyText({ ...run, code: "nordo", alt_ft: null }, dayClock),
    "ADS-B emergency state nordo (radio failure) from D1 t150 to t180.");
});

test("with logic.js's clockText, an end on the start's local day reads as a time alone, in any time zone", () => {
  const at = (day, hour, minute) => new Date(2026, 9, day, hour, minute).getTime() / 1000;      // local time
  const now = at(23, 12, 0);
  const clock = (s, nowS = now) => L.clockText(s, nowS);
  const [from, same, other] = [at(17, 15, 18), at(17, 15, 36), at(18, 0, 10)];
  assert.notEqual(clock(same, same), clock(same));                  // the time alone and with its date differ here
  assert.equal(S.heardText({ heard_from: from, heard_to: same }, clock), `Heard ${clock(from)} – ${clock(same, same)}.`);
  assert.equal(S.heardText({ heard_from: from, heard_to: other }, clock), `Heard ${clock(from)} – ${clock(other)}.`);
  const run = { flag: "EMERGENCY_NORDO", code: "7600", start: from, end: same, alt_ft: 2600 };
  assert.equal(S.emergencyText(run, clock),
    `Squawked 7600 (radio failure) from ${clock(from)} to ${clock(same, same)}, starting at 2,600 ft.`);
  assert.equal(S.emergencyText({ ...run, end: other }, clock),
    `Squawked 7600 (radio failure) from ${clock(from)} to ${clock(other)}, starting at 2,600 ft.`);
});

test("with logic.js's clockSecondsText, a run inside one minute reads with two different times", () => {
  const at = (day, hour, minute, second) => new Date(2026, 9, day, hour, minute, second).getTime() / 1000;   // local time
  const now = at(23, 12, 0, 0);
  const clock = (s, nowS = now) => L.clockSecondsText(s, nowS);
  const [from, same, other] = [at(16, 19, 14, 20), at(16, 19, 14, 55), at(17, 0, 10, 3)];
  const run = { flag: "EMERGENCY_NORDO", code: "7600", start: from, end: same, alt_ft: 2600 };
  assert.equal(S.emergencyText(run, clock), "Squawked 7600 (radio failure) from Oct 16, 07:14:20 PM to 07:14:55 PM, starting at 2,600 ft.");
  assert.equal(S.emergencyText({ ...run, end: other }, clock),
    "Squawked 7600 (radio failure) from Oct 16, 07:14:20 PM to Oct 17, 12:10:03 AM, starting at 2,600 ft.");
});

test("factsRows lists the aircraft, where, lowest, highest and squawks, with a dash for what the track lacks", () => {
  const row = { registration: "N00210", type_code: "PA24", traffic_class: "ga", hex: "0010fd", label: "arrival KSQL",
    label_airport: "KSQL", lowest_ft: 600, lowest_nm: 1.8, highest_ft: 3000, squawks: ["1200", "7600"] };
  assert.deepEqual(S.factsRows(row), [
    ["Aircraft", "N00210 · PA24 · ga · 0010fd"],
    ["Where", "arrival KSQL"],
    ["Lowest", "600 ft, 1.8 nm from KSQL"],
    ["Highest", "3,000 ft"],
    ["Squawk", "1200, 7600"],
  ]);
  const overflight = { registration: null, type_code: null, traffic_class: "airline", hex: "ac7686",
    label: "overflight FL337", label_airport: null, lowest_ft: 10025, lowest_nm: null, highest_ft: 33700, squawks: [] };
  assert.deepEqual(S.factsRows(overflight), [
    ["Aircraft", "airline · ac7686"],
    ["Where", "overflight FL337"],
    ["Lowest", "10,025 ft"],
    ["Highest", "33,700 ft"],
    ["Squawk", "—"],
  ]);
  const noTrack = { ...overflight, label: "no track on disk", lowest_ft: null, highest_ft: null };
  assert.deepEqual(S.factsRows(noTrack).slice(2, 4), [["Lowest", "—"], ["Highest", "—"]]);
});
