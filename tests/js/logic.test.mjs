// Unit tests for logic.js: the page's pure logic, apart from its DOM. Every function logic.js exports is covered,
// including the URL state round trip with its defaults (24 hours) and malformed values, a card that follows the newer
// attempt through a runway change, where a new log row goes in the API's order, chart data in ascending order with
// nulls left as gaps, and the modes of the filter chips. Adapted from the standalone checks that were run against
// logic.js while the page was written, updated for the 24-hour default window (those checks assumed "all" was the
// default).
import assert from "node:assert/strict";
import { test } from "node:test";

import * as L from "../../adsb_approach/web/static/logic.js";

test("parseState reads lists, the window, attention and the open approach", () => {
  const s = L.parseState("?airport=KSFO,KOAK,KSFO&runway_not=KSFO%2F28L&window=6h&preset=attention&approach=54&bogus=1");
  assert.deepEqual(s.airport, ["KSFO", "KOAK"]);          // de-duplicated, in first-seen order
  assert.deepEqual(s.runway_not, ["KSFO/28L"]);
  assert.equal(s.window, "6h");
  assert.equal(s.attention, true);
  assert.equal(s.approach, 54);
  assert.deepEqual(s.outcome, []);                        // an absent list key defaults to empty
});

test("parseState defaults to the last 24 hours, and malformed values fall back to their defaults", () => {
  assert.equal(L.parseState("").window, "24h");
  assert.equal(L.parseState("?window=toString").window, "24h");     // not a key of WINDOWS
  assert.equal(L.parseState("?window=all").window, "all");
  assert.equal(L.parseState("?approach=-3").approach, null);
  assert.equal(L.parseState("?approach=abc").approach, null);
  assert.equal(L.parseState("?approach=1.5").approach, null);
});

test("parseState and toSearch round-trip, and the default view has an empty query", () => {
  const s = L.parseState("?airport=KSFO,KOAK&runway_not=KSFO%2F28L&window=6h&preset=attention&approach=54");
  assert.deepEqual(L.parseState(L.toSearch(s)), s);
  assert.equal(L.toSearch(L.parseState("")), "");
});

test("parseState and toSearch carry the rotorcraft filter, off by default", () => {
  assert.equal(L.parseState("").rotorcraft, false);
  assert.equal(L.parseState("?rotorcraft=1").rotorcraft, true);
  assert.equal(L.parseState("?rotorcraft=0").rotorcraft, false);     // anything but "1" is off
  assert.equal(L.toSearch({ ...L.parseState(""), rotorcraft: true }), "?rotorcraft=1");
  assert.equal(L.toSearch({ ...L.parseState(""), rotorcraft: false }), "");
});

test("parseState and toSearch carry the GA filter, off by default", () => {
  assert.equal(L.parseState("").ga, false);
  assert.equal(L.parseState("?ga=1").ga, true);
  assert.equal(L.parseState("?ga=0").ga, false);                     // anything but "1" is off
  assert.equal(L.toSearch({ ...L.parseState(""), ga: true }), "?ga=1");
  assert.equal(L.toSearch({ ...L.parseState(""), ga: false }), "");
  // both flags round-trip together, in a stable order
  const s = L.parseState("?rotorcraft=1&ga=1");
  assert.deepEqual([s.rotorcraft, s.ga], [true, true]);
  assert.equal(L.toSearch(s), "?rotorcraft=1&ga=1");
  assert.deepEqual(L.parseState(L.toSearch(s)), s);
});

test("toSearch writes window=all for All and omits window=24h, the default", () => {
  const base = L.parseState("");
  assert.equal(L.toSearch({ ...base, window: "all" }), "?window=all");
  assert.equal(L.toSearch({ ...base, window: "24h" }), "");
  assert.equal(L.toSearch({ ...base, window: "1h" }), "?window=1h");
});

test("arrivalsQuery turns the window into since, keeps the preset and rotorcraft, and folds in extras", () => {
  const s = L.parseState("?airport=KSFO,KOAK&runway_not=KSFO%2F28L&window=6h&preset=attention");
  assert.equal(L.arrivalsQuery(s, 1000000.7, { limit: 50, cursor: null }),
    "airport=KSFO%2CKOAK&runway_not=KSFO%2F28L&since=978400&preset=attention&limit=50");
  assert.equal(L.arrivalsQuery({ ...L.parseState(""), window: "all" }, 5, {}), "");     // All: no since at all
  assert.equal(L.arrivalsQuery(L.parseState(""), 100000, {}), "since=13600");           // the 24h default
  assert.equal(L.arrivalsQuery({ ...L.parseState(""), rotorcraft: true }, 100000, {}), "since=13600&rotorcraft=1");
});

test("arrivalsQuery also keeps the GA filter, beside rotorcraft", () => {
  assert.equal(L.arrivalsQuery({ ...L.parseState(""), ga: true }, 100000, {}), "since=13600&ga=1");
  assert.equal(L.arrivalsQuery({ ...L.parseState(""), rotorcraft: true, ga: true }, 100000, {}),
    "since=13600&rotorcraft=1&ga=1");
});

test("liveQuery is rotorcraft=1 and/or ga=1 for whichever checkboxes are on, else empty", () => {
  assert.equal(L.liveQuery(L.parseState("")), "");
  assert.equal(L.liveQuery({ ...L.parseState(""), rotorcraft: true }), "rotorcraft=1");
  assert.equal(L.liveQuery({ ...L.parseState(""), ga: true }), "ga=1");
  assert.equal(L.liveQuery({ ...L.parseState(""), rotorcraft: true, ga: true }), "rotorcraft=1&ga=1");
});

test("viewChange reports the URL, whether the log and live cards reload, and the drawer transition", () => {
  const view = L.parseState("?outcome=go_around");
  assert.deepEqual(L.viewChange(view, { ...view, approach: 7 }),
    { search: "?outcome=go_around&approach=7", reloadLog: false, reloadLive: false, drawer: "open" });
  assert.deepEqual(L.viewChange({ ...view, approach: 7 }, view),
    { search: "?outcome=go_around", reloadLog: false, reloadLive: false, drawer: "close" });
  assert.deepEqual(L.viewChange(view, L.cycleChip(view, "outcome", "go_around")),
    { search: "?outcome_not=go_around", reloadLog: true, reloadLive: false, drawer: null });
  assert.equal(L.viewChange({ ...view, approach: 7 }, { ...view, approach: 9 }).drawer, "open");
  assert.equal(L.viewChange(view, { ...view, window: "1h" }).reloadLog, true);          // changing the window reloads
  assert.equal(L.viewChange(view, { ...view }).reloadLog, false);
  assert.equal(L.viewChange(view, { ...view, window: "1h" }).reloadLive, false);        // ...but not the live cards
  assert.equal(L.viewChange(view, { ...view, rotorcraft: true }).reloadLive, true);     // rotorcraft reloads them...
  assert.equal(L.viewChange(view, { ...view, ga: true }).reloadLive, true);             // ...and so does GA
});

test("chipMode cycles off, include and exclude", () => {
  let st = L.parseState("");
  assert.equal(L.chipMode(st, "outcome", "go_around"), "off");
  st = L.cycleChip(st, "outcome", "go_around");
  assert.equal(L.chipMode(st, "outcome", "go_around"), "include");
  st = L.cycleChip(st, "outcome", "go_around");
  assert.equal(L.chipMode(st, "outcome", "go_around"), "exclude");
  assert.deepEqual([st.outcome, st.outcome_not], [[], ["go_around"]]);
  st = L.cycleChip(st, "outcome", "go_around");
  assert.equal(L.chipMode(st, "outcome", "go_around"), "off");
  assert.deepEqual([st.outcome, st.outcome_not], [[], []]);
  st = L.cycleChip(st, "flags", "SIDESTEP");
  assert.deepEqual(st.flags_any, ["SIDESTEP"]);
  assert.equal(L.chipMode(st, "flags", "SIDESTEP"), "include");
});

test("attentionTitle words /api/config's attention set, or falls back without one", () => {
  const full = { outcomes: ["go_around", "touch_and_go", "abandoned"],
    flags: ["FAST", "SLOW", "UNSTABLE_1000", "UNSTABLE_500", "STEEP", "SHALLOW", "LATE_ESTABLISHED", "EMERGENCY_GENERAL",
      "EMERGENCY_NORDO", "EMERGENCY_UNLAWFUL", "EMERGENCY_DOWNED", "MINIMUM_FUEL"] };      // as test_web_app.py pins it
  const fallback = "Attention only shows go-arounds, touch-and-goes, abandoned approaches, and fast, slow, "
    + "unstable, steep, shallow, late-established, emergency or minimum-fuel approaches.";
  assert.equal(L.attentionTitle(full), fallback);              // today's config words the same as the fixed fallback
  assert.equal(L.attentionTitle({ outcomes: ["go_around"], flags: ["STEEP"] }),
    "Attention only shows go-arounds, and steep approaches.");
  assert.equal(L.attentionTitle(undefined), fallback);          // no /api/config yet
  assert.equal(L.attentionTitle({ outcomes: [], flags: [] }), fallback);      // an older server without the field
});

test("attemptChip words a go-around by its height, and attemptChain joins the chain", () => {
  assert.equal(L.attemptChip({ outcome: "touch_and_go", runway: "30", ga_initiated_hat_ft: 40, lowest_hat_ft: 0 }),
    "touch-and-go @ 40 ft");
  assert.equal(L.attemptChip({ outcome: "go_around", runway: "30", ga_initiated_hat_ft: null, lowest_hat_ft: null }),
    "GA 30");
  assert.equal(L.attemptChip({ outcome: null, runway: "28R" }), "on final 28R");
  assert.equal(L.attemptChip({ outcome: "lost_on_final", runway: "28R" }), "lost on final 28R");
  assert.equal(L.attemptChain({ attempts: [
    { outcome: "go_around", runway: "28L", ga_initiated_hat_ft: null, lowest_hat_ft: 812.4 },
    { outcome: "landed", runway: "28L" }] }), "GA @ 812 ft → landed 28L");
});

test("attemptChip appends · adsb.lol when EXTERNAL_OUTCOME marks the attempt, after a landing", () => {
  assert.equal(L.attemptChip({ outcome: "landed", runway: "28L", flags: ["EXTERNAL_OUTCOME"] }),
    "landed 28L · adsb.lol");
});

test("attemptChip's · adsb.lol follows a go-around's height too", () => {
  assert.equal(L.attemptChip({ outcome: "go_around", runway: "30", ga_initiated_hat_ft: 400, lowest_hat_ft: null,
    flags: ["EXTERNAL_OUTCOME"] }), "GA @ 400 ft · adsb.lol");
});

test("attemptChip's · adsb.lol follows a touch-and-go's height too", () => {
  assert.equal(L.attemptChip({ outcome: "touch_and_go", runway: "12", ga_initiated_hat_ft: 20, lowest_hat_ft: null,
    flags: ["EXTERNAL_OUTCOME"] }), "touch-and-go @ 20 ft · adsb.lol");
});

test("attemptChip is not labelled by landed_confirmed_by or EXTERNAL_TAIL alone", () => {
  assert.equal(L.attemptChip({ outcome: "landed", runway: "28L", landed_confirmed_by: "adsb.lol", flags: [] }),
    "landed 28L");
  assert.equal(L.attemptChip({ outcome: "landed", runway: "28L", flags: ["EXTERNAL_TAIL"] }), "landed 28L");
});

test("attemptButtonText numbers the attempt and words its outcome, open or not", () => {
  assert.equal(L.attemptButtonText({ attempt_no: 1, runway: "28R", outcome: null }), "#1 28R on final");
  assert.equal(L.attemptButtonText({ attempt_no: 2, runway: "28L", outcome: "go_around" }), "#2 28L go around");
  assert.equal(L.attemptButtonText({ attempt_no: 3, runway: "28L", outcome: "lost_on_final" }),
    "#3 28L lost on final");
});

test("attemptButtonText appends · adsb.lol when EXTERNAL_OUTCOME marks the attempt, not for other flags", () => {
  assert.equal(L.attemptButtonText({ attempt_no: 2, runway: "28L", outcome: "landed", flags: ["EXTERNAL_OUTCOME"] }),
    "#2 28L landed · adsb.lol");
  assert.equal(L.attemptButtonText({ attempt_no: 1, runway: "28L", outcome: "landed", flags: ["EXTERNAL_TAIL"] }),
    "#1 28L landed");
  assert.equal(L.attemptButtonText({ attempt_no: 1, runway: "28L", outcome: "landed", flags: [] }), "#1 28L landed");
});

test("applyLiveEvent keeps a card on its aircraft's newest attempt through a runway change", () => {
  const cards = new Map();
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 10, t: 1,
    payload: { hex: "a1", runway: "28L", airport: "KSFO", callsign: "UAL1", along_nm: 6, hat_ft: 1900 } }), true);
  L.applyLiveEvent(cards, { kind: "approach.updated", approach_id: 10, t: 2, payload: { hex: "a1", along_nm: 5, hat_ft: 1600 } });
  assert.equal(cards.get("a1").hat_ft, 1600);

  // the runway change: attempt 11 opens on 28R while 10 is still open; the card follows the newer attempt at once
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 11, t: 3,
    payload: { hex: "a1", runway: "28R", airport: "KSFO", callsign: "UAL1", along_nm: 4.8, sidestep: true } }), true);
  assert.deepEqual([cards.get("a1").runway, cards.get("a1").hat_ft, cards.get("a1").callsign], ["28R", null, "UAL1"]);

  // stale events for the superseded attempt, or a repeat of one already applied, change nothing
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.updated", approach_id: 10, t: 4, payload: { hex: "a1", along_nm: 4.7 } }), false);
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.completed", approach_id: 10, t: 4, payload: { hex: "a1" } }), false);
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 11, t: 3, payload: { hex: "a1", runway: "28R" } }), false);

  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 12, t: 5, payload: { hex: "b2", runway: "30", airport: "KOAK", along_nm: 2 } });
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 13, t: 5, payload: { hex: "c3", runway: "30", airport: "KOAK" } });
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.completed", approach_id: 11, t: 6, payload: { hex: "a1" } }), true);
  assert.deepEqual([...cards.keys()], ["b2", "c3"]);
});

test("applyLiveEvent creates no card for a rotorcraft approach.started unless shown", () => {
  const cards = new Map();
  const started = { kind: "approach.started", approach_id: 20, t: 1,
    payload: { hex: "h1", runway: "28L", airport: "KSFO", callsign: "JOLLY93", traffic_class: "rotorcraft" } };
  assert.equal(L.applyLiveEvent(cards, started), false);
  assert.equal(cards.size, 0);
  assert.equal(L.applyLiveEvent(cards, { ...started, payload: { ...started.payload } }, true), true);
  assert.equal(cards.get("h1").callsign, "JOLLY93");
  // an airline attempt is never affected by the flag
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 21, t: 1,
    payload: { hex: "h2", runway: "28R", airport: "KSFO", traffic_class: "airline" } }), true);
});

test("applyLiveEvent creates no card for a GA approach.started unless shown", () => {
  const cards = new Map();
  const gaTypes = new Set(["C172", "C152"]);
  const started = { kind: "approach.started", approach_id: 30, t: 1,
    payload: { hex: "h3", runway: "28L", airport: "KSFO", callsign: "N0152AB", type_code: "C172" } };
  assert.equal(L.applyLiveEvent(cards, started, false, false, gaTypes), false);
  assert.equal(cards.size, 0);
  assert.equal(L.applyLiveEvent(cards, { ...started, payload: { ...started.payload } }, false, true, gaTypes), true);
  assert.equal(cards.get("h3").callsign, "N0152AB");
  // a type not in gaTypes is never affected by the flag
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 31, t: 1,
    payload: { hex: "h4", runway: "28R", airport: "KSFO", type_code: "B738" } }, false, false, gaTypes), true);
  // no type_code at all (the NULL trap, client-side): never hidden as GA, whatever gaTypes holds
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 32, t: 1,
    payload: { hex: "h5", runway: "28L", airport: "KSFO" } }, false, false, gaTypes), true);
});

test("applyLiveEvent's approach.started bypasses the rotorcraft and GA filters when emergency is non-empty", () => {
  // A medevac helicopter or a GA aircraft already declaring on establishment must not be hidden
  const cards = new Map();
  const gaTypes = new Set(["C172"]);
  const heli = { kind: "approach.started", approach_id: 40, t: 1, payload: { hex: "h6", runway: "28L",
    airport: "KSFO", traffic_class: "rotorcraft", emergency: ["EMERGENCY_GENERAL"] } };
  assert.equal(L.applyLiveEvent(cards, heli, false, false, gaTypes), true);
  assert.deepEqual(cards.get("h6").emergency, ["EMERGENCY_GENERAL"]);
  const cessna = { kind: "approach.started", approach_id: 41, t: 1, payload: { hex: "h7", runway: "28R",
    airport: "KSFO", type_code: "C172", emergency: ["EMERGENCY_NORDO"] } };
  assert.equal(L.applyLiveEvent(cards, cessna, false, false, gaTypes), true);
  // MINIMUM_FUEL alone is not EMERGENCY_MASK: it must not bypass either filter
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 42, t: 1,
    payload: { hex: "h8", runway: "28L", airport: "KSFO", traffic_class: "rotorcraft", emergency: [] } },
    false, false, gaTypes), false);
  // an ordinary approach.started (no emergency at all) keeps its card, with an empty emergency list
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 43, t: 1,
    payload: { hex: "h9", runway: "28L", airport: "KSFO" } }, false, false, gaTypes);
  assert.deepEqual(cards.get("h9").emergency, []);
});

test("applyLiveEvent's approach.emergency builds a card for a hidden aircraft, updates one showing, and ignores in_progress: false", () => {
  const cards = new Map();
  const gaTypes = new Set(["C172"]);
  // hidden by the rotorcraft filter when it started; latches an emergency mid-approach and must now appear
  const started = { kind: "approach.started", approach_id: 50, t: 1, payload: { hex: "h10", runway: "28L",
    airport: "KSFO", traffic_class: "rotorcraft", callsign: "JOLLY93", emergency: [] } };
  assert.equal(L.applyLiveEvent(cards, started, false, false, gaTypes), false);
  assert.equal(cards.size, 0);
  const declared = { kind: "approach.emergency", approach_id: 50, t: 5, payload: { hex: "h10", runway: "28L",
    airport: "KSFO", traffic_class: "rotorcraft", callsign: "JOLLY93", established_at: 1, in_progress: true,
    emergency: ["EMERGENCY_GENERAL"] } };
  assert.equal(L.applyLiveEvent(cards, declared, false, false, gaTypes), true);
  const card = cards.get("h10");
  assert.equal(card.approach_id, 50);
  assert.equal(card.established_at, 1);           // the attempt's own establishment, not this event's time
  assert.deepEqual(card.emergency, ["EMERGENCY_GENERAL"]);

  // a second approach.emergency for the same attempt updates the card already showing, in place
  assert.equal(L.applyLiveEvent(cards, { ...declared, t: 6,
    payload: { ...declared.payload, emergency: ["EMERGENCY_GENERAL", "EMERGENCY_NORDO"] } }, false, false, gaTypes), true);
  assert.deepEqual(cards.get("h10").emergency, ["EMERGENCY_GENERAL", "EMERGENCY_NORDO"]);
  assert.equal(cards.size, 1);

  // in_progress: false (path 4, just after close) changes no card -- the log row is what refreshes for that
  const closed = { kind: "approach.emergency", approach_id: 60, t: 9, payload: { hex: "h11", runway: "28R",
    airport: "KSFO", established_at: 2, in_progress: false, emergency: ["EMERGENCY_UNLAWFUL"] } };
  assert.equal(L.applyLiveEvent(cards, closed, false, false, gaTypes), false);
  assert.equal(cards.has("h11"), false);

  // a stale approach.emergency for an attempt older than the one already showing is ignored
  const stale = { kind: "approach.emergency", approach_id: 49, t: 10,
    payload: { hex: "h10", runway: "28L", airport: "KSFO", established_at: 1, in_progress: true, emergency: ["EMERGENCY_GENERAL"] } };
  assert.equal(L.applyLiveEvent(cards, stale, false, false, gaTypes), false);
  assert.equal(cards.get("h10").approach_id, 50);
});

test("approach.emergency for the attempt a card already shows keeps the values approach.updated filled in", () => {
  const cards = new Map();
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 70, t: 100, payload: { hex: "h20", runway: "28R",
    airport: "KSFO", callsign: "UAL1", along_nm: 5, hat_ft: 1600, emergency: [] } });
  L.applyLiveEvent(cards, { kind: "approach.updated", approach_id: 70, t: 130, payload: { hex: "h20", along_nm: 4,
    hat_ft: 1280, gp_dev_ft: -48, gp_dots: -0.3, loc_dev_ft: 20, loc_dots: 0.1, gs: 150, gs_corr: 140, sink_fpm: 800 } });
  assert.equal(L.applyLiveEvent(cards, { kind: "approach.emergency", approach_id: 70, t: 130.5, payload: { hex: "h20",
    runway: "28R", airport: "KSFO", callsign: "UAL1", along_nm: 3.98, hat_ft: 1275, attempt_no: 1, arrival_id: 70,
    established_at: 100, in_progress: true, emergency: ["EMERGENCY_GENERAL"] } }), true);
  const card = cards.get("h20");
  assert.deepEqual(card.emergency, ["EMERGENCY_GENERAL"]);
  assert.deepEqual([card.gp_dev_ft, card.gp_dots, card.loc_dev_ft, card.loc_dots, card.gs, card.gs_corr, card.sink_fpm],
    [-48, -0.3, 20, 0.1, 150, 140, 800]);
});

test("isEmergencyFlag names the four EMERGENCY_MASK flags by their prefix, and MINIMUM_FUEL is not one", () => {
  assert.equal(L.isEmergencyFlag("EMERGENCY_GENERAL"), true);
  assert.equal(L.isEmergencyFlag("EMERGENCY_NORDO"), true);
  assert.equal(L.isEmergencyFlag("EMERGENCY_UNLAWFUL"), true);
  assert.equal(L.isEmergencyFlag("EMERGENCY_DOWNED"), true);
  assert.equal(L.isEmergencyFlag("MINIMUM_FUEL"), false);
  assert.equal(L.isEmergencyFlag("SIDESTEP"), false);
  assert.equal(L.isEmergencyFlag(""), false);
});

test("emergencyNote words each EMERGENCY_MASK flag", () => {
  assert.equal(L.emergencyNote(["EMERGENCY_GENERAL"]), "General emergency · 7700");
  assert.equal(L.emergencyNote(["EMERGENCY_NORDO"]), "Radio failure · 7600");
  assert.equal(L.emergencyNote(["EMERGENCY_UNLAWFUL"]), "Unlawful interference · 7500");
  assert.equal(L.emergencyNote(["EMERGENCY_DOWNED"]), "Downed");
});

test("emergencyNote joins several flags in EMERGENCY_GENERAL/NORDO/UNLAWFUL/DOWNED order, not flagNames' own", () => {
  assert.equal(L.emergencyNote(["EMERGENCY_DOWNED", "EMERGENCY_GENERAL"]), "General emergency · 7700 · Downed");
  assert.equal(L.emergencyNote(["EMERGENCY_UNLAWFUL", "EMERGENCY_NORDO", "EMERGENCY_GENERAL"]),
    "General emergency · 7700 · Radio failure · 7600 · Unlawful interference · 7500");
});

test("emergencyNote ignores MINIMUM_FUEL and every other flag, and is null with none or nothing to say", () => {
  assert.equal(L.emergencyNote(["MINIMUM_FUEL", "SIDESTEP", "LATE_ESTABLISHED"]), null);
  assert.equal(L.emergencyNote([]), null);
  assert.equal(L.emergencyNote(["MINIMUM_FUEL", "EMERGENCY_NORDO"]), "Radio failure · 7600");
});

test("sortedCards orders by distance, nearest first, with the distanceless last", () => {
  const cards = new Map();
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 12, t: 5, payload: { hex: "b2", runway: "30", airport: "KOAK", along_nm: 2 } });
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 10, t: 1, payload: { hex: "a1", runway: "28L", airport: "KSFO", along_nm: 6 } });
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 13, t: 5, payload: { hex: "c3", runway: "30", airport: "KOAK" } });
  assert.deepEqual(L.sortedCards(cards).map((c) => c.hex), ["b2", "a1", "c3"]);
});

test("arrivalToRefresh names the arrival a log row must be refetched for, or null", () => {
  assert.equal(L.arrivalToRefresh({ kind: "approach.updated", payload: { arrival_id: 4 } }), null);
  assert.equal(L.arrivalToRefresh({ kind: "approach.completed", payload: { arrival_id: 4 } }), 4);
  assert.equal(L.arrivalToRefresh({ kind: "approach.reclassified", payload: { arrival_id: 4 } }), 4);
  assert.equal(L.arrivalToRefresh({ kind: "approach.confirmed", payload: { arrival_id: 4 } }), 4);
  assert.equal(L.arrivalToRefresh({ kind: "approach.extended", payload: { arrival_id: 4 } }), 4);
  assert.equal(L.arrivalToRefresh({ kind: "approach.started", payload: { arrival_id: 4, attempt_no: 1 } }), null);
  assert.equal(L.arrivalToRefresh({ kind: "approach.started", payload: { arrival_id: 4, attempt_no: 2 } }), 4);
  assert.equal(L.arrivalToRefresh({ kind: "approach.completed", payload: {} }), null);
  // unconditional on in_progress -- an open attempt's row shows "emergency" too (arrivals.py), and a just-closed one
  // (the matcher's stamping path 4) is the log's only way to learn about it
  assert.equal(L.arrivalToRefresh({ kind: "approach.emergency", payload: { arrival_id: 4, in_progress: true } }), 4);
  assert.equal(L.arrivalToRefresh({ kind: "approach.emergency", payload: { arrival_id: 4, in_progress: false } }), 4);
});

test("openApproachRefresh says whether and how urgently an event should refetch the drawer's open approach", () => {
  assert.equal(L.openApproachRefresh(null, { kind: "approach.updated", approach_id: 5 }), null);   // no drawer open
  assert.equal(L.openApproachRefresh(5, { kind: "approach.updated", approach_id: 6 }), null);       // a different approach
  assert.equal(L.openApproachRefresh(5, { kind: "approach.updated", approach_id: 5 }), "throttled");
  assert.equal(L.openApproachRefresh(5, { kind: "approach.completed", approach_id: 5 }), "immediate");
  assert.equal(L.openApproachRefresh(5, { kind: "approach.go_around", approach_id: 5 }), "immediate");
  assert.equal(L.openApproachRefresh(5, { kind: "approach.runway_change", approach_id: 5 }), "immediate");
  // a splice reclassifies, confirms or merely extends an approach with adsb.lol points
  assert.equal(L.openApproachRefresh(5, { kind: "approach.reclassified", approach_id: 5 }), "immediate");
  assert.equal(L.openApproachRefresh(5, { kind: "approach.confirmed", approach_id: 5 }), "immediate");
  assert.equal(L.openApproachRefresh(5, { kind: "approach.extended", approach_id: 5 }), "immediate");
  // immediate whether the attempt is still open (stamping path 2) or was just closed (path 4) -- both stamp flags the
  // open drawer must show without waiting on the throttle
  assert.equal(L.openApproachRefresh(5, { kind: "approach.emergency", approach_id: 5 }), "immediate");
  // approach.started never changes what the drawer shows for its own approach
  assert.equal(L.openApproachRefresh(5, { kind: "approach.started", approach_id: 5 }), null);
});

test("upsertRow places a new row where the API's own order would put it", () => {
  const row = (id, t, extra = {}) => ({ arrival_id: id, started_at: t, ...extra });
  const rows = [row(9, 90), row(7, 70), row(5, 50)];
  const ids = (list) => list.map((r) => r.arrival_id);
  assert.deepEqual(ids(L.upsertRow(rows, 8, row(8, 80), true)), [9, 8, 7, 5]);          // between two rows
  assert.deepEqual(ids(L.upsertRow(rows, 10, row(10, 95), true)), [10, 9, 7, 5]);       // newest: goes first
  assert.deepEqual(ids(L.upsertRow(rows, 3, row(3, 30), true)), [9, 7, 5]);             // past the last row, more pages: dropped
  assert.deepEqual(ids(L.upsertRow(rows, 3, row(3, 30), false)), [9, 7, 5, 3]);         // ...but appended on the last page
  assert.equal(L.upsertRow(rows, 5, row(5, 50, { callsign: "X" }), true)[2].callsign, "X");   // replaced in place
  assert.deepEqual(ids(L.upsertRow(rows, 7, null, true)), [9, 5]);                      // removed: no longer passes the filters
  assert.deepEqual(ids(L.upsertRow(rows, 4, null, true)), [9, 7, 5]);                   // null for an unlisted row: no-op
});

test("rowsInWindow drops rows whose closed attempts all ended before the window, and All keeps every row", () => {
  const row = (id, ...ends) => ({ arrival_id: id,
    attempts: ends.map((t) => (t === null ? { outcome: null, ended_at: null } : { outcome: "landed", ended_at: t })) });
  const rows = [row(3, 99000), row(2, 13599, null), row(1, 500, 13600)];
  const ids = (list) => list.map((r) => r.arrival_id);
  const day = L.parseState("");                        // 24 h at 100000: since is 13600, as arrivalsQuery sends it
  assert.deepEqual(ids(L.rowsInWindow(rows, day, 100000)), [3, 1]);      // 2's only closed attempt ended before it
  assert.deepEqual(ids(L.rowsInWindow(rows, { ...day, window: "1h" }, 100000)), [3]);
  assert.equal(L.rowsInWindow(rows, { ...day, window: "all" }, 100000), rows);
});

test("translucent turns a #rrggbb custom property into rgba at the given alpha", () => {
  assert.equal(L.translucent("#2563eb", 0.5), "rgba(37, 99, 235, 0.5)");
  assert.equal(L.translucent("  #2563eb  ", 0.5), "rgba(37, 99, 235, 0.5)");    // getPropertyValue returns whitespace
  assert.equal(L.translucent("not-a-colour", 0.5), "not-a-colour");
  assert.equal(L.translucent("", 0.5), "");
});

test("chartData sorts ascending by distance, drops duplicate distances, and leaves gaps as null", () => {
  const ref = { glide_path_deg: 3, tch_ft: 50, ft_per_nm: 6076.115 };
  const pts = [{ along_nm: 2, hat_ft: 700, cross_ft: 10, gs: 140, gs_corr: 150 },
    { along_nm: 1, hat_ft: 370, cross_ft: 5, gs: 135, gs_corr: null }, { along_nm: 1, hat_ft: 999 }, { along_nm: null }];
  const d = L.chartData(pts, ref);
  assert.deepEqual(d.height[0], [0, 1, 2]);                    // ascending, with the threshold added at the front
  assert.deepEqual(d.height[1], [null, 370, 700]);             // the second along_nm=1 point was dropped, not the first
  assert.equal(d.height[2][0], 50);                            // the glide path starts at tch_ft
  assert.ok(Math.abs(d.height[2][2] - (50 + 2 * 6076.115 * Math.tan(Math.PI / 60))) < 1e-9);
  assert.deepEqual(d.cross, [[0, 1, 2], [null, 5, 10], [0, 0, 0]]);
  assert.deepEqual(d.speed, [[0, 1, 2], [null, 135, 140], [null, null, 150]]);
  assert.equal(pts[0].along_nm, 2);                            // the caller's points are not reordered in place

  const past = L.chartData([{ along_nm: -0.5, hat_ft: 100 }, { along_nm: 0.5, hat_ft: 200 }], ref);
  assert.deepEqual(past.height[0], [-0.5, 0.5]);               // already reaches the threshold: nothing added
  assert.equal(past.height[2][0], null);                       // the glide path is not drawn before the threshold

  assert.deepEqual(L.chartData([], null), { height: [[], [], []], cross: [[], [], []], speed: [[], [], []] });
  assert.deepEqual(L.chartData([], ref).height, [[0], [null], [50]]);
});

test("chartData feeds the cross-track chart a constant zero series, the same length as the rest", () => {
  const d = L.chartData([{ along_nm: 2, cross_ft: 40 }, { along_nm: 1, cross_ft: -20 }], null);
  assert.deepEqual(d.cross, [[1, 2], [-20, 40], [0, 0]]);
  const withRef = L.chartData([{ along_nm: 1, cross_ft: -20 }], { glide_path_deg: 3, tch_ft: 50, ft_per_nm: 6076.115 });
  assert.deepEqual(withRef.cross, [[0, 1], [null, -20], [0, 0]]);      // the threshold point added at the front, too
});

test("chartData splits into local and adsb.lol series once a point's origin is adsb.lol, joined at the seam", () => {
  const pts = [
    { along_nm: 8, hat_ft: 800, cross_ft: 80, gs: 150, gs_corr: 160 },
    { along_nm: 5, hat_ft: 500, cross_ft: 50, gs: 140, gs_corr: 150 },
    { along_nm: 2.7, hat_ft: 270, cross_ft: 27, gs: 135, gs_corr: 145 },
    { along_nm: 2, hat_ft: 200, cross_ft: 20, gs: 130, gs_corr: 140, origin: "adsb.lol" },
    { along_nm: 1, hat_ft: 100, cross_ft: 10, gs: 120, gs_corr: 130, origin: "adsb.lol" },
    { along_nm: -0.5, hat_ft: 0, cross_ft: -5, gs: 100, gs_corr: 110, origin: "adsb.lol" },
  ];
  const d = L.chartData(pts, null);
  assert.equal(d.external, true);
  assert.deepEqual(d.height[0], [-0.5, 1, 2, 2.7, 5, 8]);
  assert.deepEqual(d.height[1], [null, null, null, 270, 500, 800]);            // local: null wherever the point is external
  assert.deepEqual(d.height[2], [0, 100, 200, 270, null, null]);               // adsb.lol, joined by the 2.7 nm local point
  assert.equal(d.height.length, 4);                                           // x, local, adsb.lol, glide path
  assert.deepEqual(d.cross[1], [null, null, null, 27, 50, 80]);
  assert.deepEqual(d.cross[2], [-5, 10, 20, 27, null, null]);
  assert.deepEqual(d.speed[1], [null, null, null, 135, 140, 150]);
  assert.deepEqual(d.speed[2], [100, 120, 130, 135, null, null]);
  assert.deepEqual(d.speed[3], [null, null, null, 145, 150, 160]);
  assert.deepEqual(d.speed[4], [110, 130, 140, 145, null, null]);
});

test("chartData's split leaves an approach without adsb.lol points exactly as before", () => {
  const pts = [{ along_nm: 2, hat_ft: 700, cross_ft: 10, gs: 140, gs_corr: 150, origin: "local" },
    { along_nm: 1, hat_ft: 370, cross_ft: 5, gs: 135, gs_corr: null }];        // a point without origin counts as local
  const d = L.chartData(pts, null);
  assert.deepEqual(Object.keys(d).sort(), ["cross", "height", "speed"]);       // no `external` key at all
  assert.equal(d.height.length, 3);
});

test("fmtDelta signs a delta and gives an em dash for a missing one", () => {
  assert.equal(L.fmtDelta(6.2, " ft"), "+6 ft");
  assert.equal(L.fmtDelta(-3.4, " kt"), "−3 kt");             // U+2212, as signed writes it
  assert.equal(L.fmtDelta(-0.2, " ft"), "+0 ft");             // the sign follows the rounded value
  assert.equal(L.fmtDelta(null, " kt"), "—");
  assert.equal(L.fmtDelta(undefined, " ft"), "—");
});

test("fleetBandSeries leaves a gap where the key has no bin and holds a lone bin's value across its own span", () => {
  // 1.0 and 2.0 are not adjacent bins (bin_width_nm 0.5), so each is flat across its own span: no neighbour to ramp to.
  const fleet = { bin_width_nm: 0.5, bins: [
    { bin_nm: 1.0, gs_p25: 100, gs_p75: 110 },
    { bin_nm: 2.0, gs_p25: 200, gs_p75: 210 },
  ] };
  const x = [0.75, 1.0, 1.4, 1.6, 2.2, 3.0];
  const { speed } = L.fleetBandSeries(x, fleet, null);
  assert.deepEqual(speed, [
    [null, 100, 100, null, 200, null],
    [null, 110, 110, null, 210, null],
  ]);
  assert.equal(speed[0].length, x.length);
  assert.equal(speed[1].length, x.length);
});

test("fleetBandSeries puts the height band around the published path and the speed band at its own values", () => {
  const fleet = { bin_width_nm: 0.5, bins: [{ bin_nm: 1.0, gp_p25: -40, gp_p75: 20, gs_p25: 135, gs_p75: 150 }] };
  const reference = { glide_path_deg: 3, tch_ft: 55, threshold_elev_ft: 13, ft_per_nm: 6076.11548556, reference_source: "cifp:test" };
  const path = 55 + 1.2 * 6076.11548556 * Math.tan((3 * Math.PI) / 180);
  const { height, speed } = L.fleetBandSeries([1.2], fleet, reference);
  assert.ok(Math.abs(height[0][0] - (path - 40)) < 0.001);
  assert.ok(Math.abs(height[1][0] - (path + 20)) < 0.001);
  assert.deepEqual(speed, [[135], [150]]);
});

test("fleetBandSeries gives no band without a norm, and no height band without a reference", () => {
  const reference = { glide_path_deg: 3, tch_ft: 55, threshold_elev_ft: 13, ft_per_nm: 6076.11548556, reference_source: "cifp:test" };
  assert.deepEqual(L.fleetBandSeries([1.0], null, reference), { height: null, speed: null });
  assert.deepEqual(L.fleetBandSeries([1.0], undefined, reference), { height: null, speed: null });

  const fleet = { bin_width_nm: 0.5, bins: [{ bin_nm: 1.0, gp_p25: -40, gp_p75: 20, gs_p25: 135, gs_p75: 150 }] };
  const noRef = L.fleetBandSeries([1.0], fleet, null);
  assert.equal(noRef.height, null);
  assert.deepEqual(noRef.speed, [[135], [150]]);

  // gs_p25 missing makes the bin unusable for speed only; its gp_p25/gp_p75 still serve the height band.
  const thin = { bin_width_nm: 0.5, bins: [{ bin_nm: 1.0, gp_p25: -40, gp_p75: 20, gs_p25: null, gs_p75: 150 }] };
  const withGap = L.fleetBandSeries([1.0], thin, reference);
  assert.deepEqual(withGap.speed, [[null], [null]]);
  assert.equal(withGap.height[0][0] === null || withGap.height[1][0] === null, false);
});

test("fleetBandSeries ramps between the centres of two adjacent bins", () => {
  const fleet = { bin_width_nm: 0.5, bins: [
    { bin_nm: 1.0, gs_p25: 130, gs_p75: 140 },
    { bin_nm: 1.5, gs_p25: 140, gs_p75: 150 },
  ] };
  const { speed } = L.fleetBandSeries([1.25, 1.5, 1.75], fleet, null);
  const close = (a, b) => Math.abs(a - b) < 0.001;
  assert.ok(speed[0].every((v, i) => close(v, [130, 135, 140][i])));
  assert.ok(speed[1].every((v, i) => close(v, [140, 145, 150][i])));
});

test("normLabel names the type, the class or all aircraft, on the approach's runway", () => {
  assert.equal(L.normLabel({ key_kind: "type", key: "B738", n: 142 }, "28R"), "norm: B738 on 28R, 142 approaches");
  assert.equal(L.normLabel({ key_kind: "class", key: "airline", n: 96 }, "28R"), "norm: airline on 28R, 96 approaches");
  assert.equal(L.normLabel({ key_kind: "*", key: "*", n: 310 }, "30"), "norm: all aircraft on 30, 310 approaches");
  assert.equal(L.normLabel({ key_kind: "type", key: "C172", n: 1 }, "28R"), "norm: C172 on 28R, 1 approach");
  assert.equal(L.normLabel(null, "28R"), null);
  assert.equal(L.normLabel(undefined, "28R"), null);
});

test("crossTickLabel words a cross-track axis tick as L, R or 0, and blanks uPlot's unlabelled minor ticks", () => {
  assert.equal(L.crossTickLabel(0), "0");
  assert.equal(L.crossTickLabel(-0.4), "0");                   // rounds to zero
  assert.equal(L.crossTickLabel(100), "100 R");
  assert.equal(L.crossTickLabel(-1000), "1000 L");
  assert.equal(L.crossTickLabel(null), "");    // uPlot's axis ticks array carries null where it wants no label drawn
});

test("crossTickFilter labels 0 and powers of ten from 100 on both sides of the centreline", () => {
  assert.deepEqual(L.crossTickFilter([-1000, -900, -200, -100, -50, 0, 50, 100, 300, 1000]),
                   [-1000, null, null, -100, null, 0, null, 100, null, 1000]);
  assert.deepEqual(L.crossTickFilter([null, Number.NaN]), [null, null]);
});

test("fmt formats numbers, tidies negative zero, and blanks missing values", () => {
  assert.equal(L.fmt(-48.2, 0, " ft"), "-48 ft");
  assert.equal(L.fmt(-0.2, 0), "0");
  assert.equal(L.fmt(-0.004, 2), "0.00");
  assert.equal(L.fmt(null), "—");
  assert.equal(L.fmt(undefined), "—");
  assert.equal(L.fmt(NaN), "—");
  assert.equal(L.fmt(0.383, 2), "0.38");
});

test("fmtLateral reads a lateral deviation as L/R, with the dots unsigned", () => {
  assert.equal(L.fmtLateral(-45, -0.1), "45 ft L (0.1)");     // cross-track/localizer positive is right
  assert.equal(L.fmtLateral(45, 0.1), "45 ft R (0.1)");
  assert.equal(L.fmtLateral(0.4, 0.02), "0 ft");               // rounds to 0 ft: no side, no dots
  assert.equal(L.fmtLateral(-0.3, -0.01), "0 ft");
  assert.equal(L.fmtLateral(null, null), "—");
  assert.equal(L.fmtLateral(undefined, undefined), "—");
  assert.equal(L.fmtLateral(-12), "12 ft L");                  // no dots given: the cross-track chart has none per point
});

test("bannerFor covers every banner state, and is null when all is well", () => {
  const clock = (t) => `t${t}`;
  const fine = { db: { ok: true }, collector: { state: "running" }, feed: { state: "ok" } };
  assert.equal(L.bannerFor(fine, "open", clock), null);
  assert.equal(L.bannerFor(null, "open", clock).level, "error");
  assert.equal(L.bannerFor(fine, "retrying", clock).level, "warning");
  assert.equal(L.bannerFor({ ...fine, feed: { state: "silent", quiet_s: 2400 } }, "open", clock).text,
    "Nothing recorded for 40 min: the feed may be down, or no aircraft are landing.");
  assert.equal(L.bannerFor({ ...fine, collector: { state: "stopped", ended_at: 5 } }, "open", clock).text,
    "The collector stopped at t5.");
  assert.equal(L.bannerFor({ ...fine, collector: { state: "failed", ended_at: 5, note: "[failed: interrupted]" } }, "open", clock).text,
    "The collector stopped with an error at t5: [failed: interrupted]");
  assert.equal(L.bannerFor({ ...fine, collector: { state: "none" } }, "open", clock).level, "info");
  assert.equal(L.bannerFor({ db: { ok: false, error: "gone" }, collector: { state: "unknown" }, feed: { state: "unknown" } },
    "open", clock).text, "The database cannot be read: gone");
});

test("bannerFor's clock shows the date only when the stop wasn't today, in the viewer's local time", () => {
  const nowS = new Date(2026, 8, 13, 9, 0, 0).getTime() / 1000;             // 2026-09-13, 09:00 local
  const clock = (t) => L.clockText(t, nowS);
  const stopped = (endedAt) => ({ db: { ok: true }, collector: { state: "stopped", ended_at: endedAt }, feed: { state: "ok" } });
  const today = new Date(2026, 8, 13, 17, 3, 0).getTime() / 1000;          // same local day as nowS
  const yesterday = new Date(2026, 8, 12, 17, 3, 0).getTime() / 1000;
  assert.equal(L.bannerFor(stopped(today), "open", clock).text, "The collector stopped at 05:03 PM.");
  assert.equal(L.bannerFor(stopped(yesterday), "open", clock).text, "The collector stopped at Sep 12, 05:03 PM.");
});

test("clockSecondsText is clockText with seconds: the date only when it isn't nowS's local day", () => {
  const at = (day, hour, minute, second) => new Date(2026, 9, day, hour, minute, second).getTime() / 1000;   // local time
  const nowS = at(23, 12, 0, 0);
  const seconds = at(16, 19, 14, 49);
  assert.equal(L.clockSecondsText(seconds, nowS), "Oct 16, 07:14:49 PM");
  assert.equal(L.clockSecondsText(seconds, seconds + 60), "07:14:49 PM");                  // the same local day: the time alone
  assert.equal(L.clockSecondsText(seconds, nowS),
    new Date(seconds * 1000).toLocaleString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", month: "short", day: "numeric" }));
  assert.notEqual(L.clockSecondsText(at(16, 19, 14, 49), nowS), L.clockSecondsText(at(16, 19, 14, 20), nowS));    // two times in one minute differ
  assert.equal(L.clockSecondsText(at(23, 0, 0, 5), nowS), "12:00:05 AM");
});

test("rowTime is the arrivals log's Time cell: the date and 12-hour clock in the viewer's local time", () => {
  const local = new Date(2026, 9, 16, 19, 14, 0);                           // built from local parts: any time zone gives the same text
  const seconds = local.getTime() / 1000;
  assert.equal(L.rowTime(seconds), "Oct 16, 07:14 PM");
  assert.equal(L.rowTime(seconds), local.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }));
  assert.equal(L.rowTime(seconds + 86400), "Oct 17, 07:14 PM");              // always with the date, unlike clockText
});

test("externalGlobeLabel names the configured host, with a leading globe. removed", () => {
  assert.equal(L.externalGlobeLabel("https://globe.adsb.lol/?icao=a75fbf&showTrace=2026-09-14"), "adsb.lol");
  assert.equal(L.externalGlobeLabel("https://globe.example.net/?icao=a75fbf"), "example.net");     // another host
  assert.equal(L.externalGlobeLabel("https://tracker.example.org/?icao=a75fbf"), "tracker.example.org");  // no globe.
});

test("mergedPointCount counts the points adsb.lol supplied, and a point with no origin is the receiver's", () => {
  assert.equal(L.mergedPointCount([]), 0);
  assert.equal(L.mergedPointCount([{ origin: "local" }, { along_nm: 1 }, { origin: null }]), 0);
  assert.equal(L.mergedPointCount([{ origin: "local" }, { origin: "adsb.lol" }, { along_nm: 1 }, { origin: "adsb.lol" }]), 2);
  assert.equal(L.mergedPointCount([{ origin: "adsb.lol" }]), 1);
});

test("externalNote is null without a lookup or merged positions", () => {
  assert.equal(L.externalNote(null, (t) => `t${t}`), null);
  assert.equal(L.externalNote(null, (t) => `t${t}`, 0), null);
});

test("externalNote counts the positions merged live when there is no lookup, singular for one", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote(null, clock, 12), "adsb.lol: 12 positions merged live");
  assert.equal(L.externalNote(null, clock, 1), "adsb.lol: 1 position merged live");
  assert.equal(L.externalNote(null, clock, 0), null);
  assert.equal(L.externalNote(undefined, clock, 3), "adsb.lol: 3 positions merged live");
});

test("externalNote reads a lookup row as before, whatever the merged count", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "pending", due_at: 100 }, clock, 12), "adsb.lol: lookup at t100");
  assert.equal(L.externalNote({ status: "expired" }, clock, 12), "adsb.lol: expired unfetched");
});

test("externalNote gives the due time while pending or retrying", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "pending", due_at: 100 }, clock), "adsb.lol: lookup at t100");
  assert.equal(L.externalNote({ status: "retry", due_at: 200 }, clock), "adsb.lol: lookup at t200");
});

test("externalNote counts points added, singular for one, and nothing for zero", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "done", points_added: 1, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: added 1 point");
  assert.equal(L.externalNote({ status: "done", points_added: 5, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: added 5 points");
  assert.equal(L.externalNote({ status: "done", points_added: 0, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: nothing to add");
});

test("externalNote appends the seam offsets, signed, when both were measured", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "done", points_added: 42, offset_s: 0.83, offset_ft: -25.4 }, clock),
    "adsb.lol: added 42 points · +0.8 s, −25 ft against your receiver");
  assert.equal(L.externalNote({ status: "done", points_added: 3, offset_s: -1.2, offset_ft: 10 }, clock),
    "adsb.lol: added 3 points · −1.2 s, +10 ft against your receiver");
  // only one of the two offsets measured: neither is shown
  assert.equal(L.externalNote({ status: "done", points_added: 2, offset_s: null, offset_ft: 5 }, clock),
    "adsb.lol: added 2 points");
  assert.equal(L.externalNote({ status: "done", points_added: 2, offset_s: 0.1, offset_ft: null }, clock),
    "adsb.lol: added 2 points");
});

test("externalNote's signed offsets round first, so a value that rounds to zero still reads +", () => {
  const clock = (t) => `t${t}`;
  // −0.004 s rounds to 0.0 at 1 digit: picking the sign before rounding would read −0.0 s
  assert.equal(L.externalNote({ status: "done", points_added: 1, offset_s: -0.004, offset_ft: 0 }, clock),
    "adsb.lol: added 1 point · +0.0 s, +0 ft against your receiver");
  // −0.4 ft rounds to 0 at 0 digits: same trap, the other field and digit count
  assert.equal(L.externalNote({ status: "done", points_added: 1, offset_s: 0, offset_ft: -0.4 }, clock),
    "adsb.lol: added 1 point · +0.0 s, +0 ft against your receiver");
});

test("externalNote's failed note depends on the HTTP status", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "failed", http_status: 404 }, clock), "adsb.lol: no trace");
  assert.equal(L.externalNote({ status: "failed", http_status: 503 }, clock), "adsb.lol: failed (HTTP 503)");
  assert.equal(L.externalNote({ status: "failed", http_status: 429 }, clock), "adsb.lol: failed (HTTP 429)");
  assert.equal(L.externalNote({ status: "failed", http_status: null }, clock), "adsb.lol: failed");    // e.g. a timeout
  assert.equal(L.externalNote({ status: "failed", http_status: 200 }, clock), "adsb.lol: failed");     // an unreadable body
});

test("externalNote marks an expired lookup", () => {
  assert.equal(L.externalNote({ status: "expired" }, (t) => `t${t}`), "adsb.lol: expired unfetched");
});

test("externalNote lists the tail's points and the filled gaps, and names matching points as the offset's source", () => {
  const clock = (t) => `t${t}`;
  const lookup = { status: "done", points_added: 104, gaps_filled: 9, gap_points_added: 18, offset_source: "matches",
    match_n: 14, applied_offset_s: 0.1, offset_s: 0.102, offset_ft: 0 };
  assert.equal(L.externalNote(lookup, clock),
    "adsb.lol: added 104 points, filled 9 gaps (+18) · +0.1 s from 14 matching points");
});

test("externalNote names the filled gaps' points when no tail was added, singular for one", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "done", points_added: 0, gaps_filled: 9, gap_points_added: 18,
    offset_source: "matches", match_n: 14, applied_offset_s: 0.099, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: filled 9 gaps (+18 points) · +0.1 s from 14 matching points");
  assert.equal(L.externalNote({ status: "done", points_added: 0, gaps_filled: 1, gap_points_added: 1,
    offset_source: "matches", match_n: 3, applied_offset_s: 0.094, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: filled 1 gap (+1 point) · +0.1 s from 3 matching points");
  assert.equal(L.externalNote({ status: "done", points_added: 1, gaps_filled: 1, gap_points_added: 1,
    offset_source: "matches", match_n: 3, applied_offset_s: 0.094, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: added 1 point, filled 1 gap (+1) · +0.1 s from 3 matching points");
});

test("externalNote gives the along-track offset and height when the seam chose the offset", () => {
  const clock = (t) => `t${t}`;
  const lookup = { status: "done", points_added: 42, gaps_filled: 2, gap_points_added: 3, offset_source: "along_track",
    match_n: 1, applied_offset_s: 0.104, offset_s: 0.104, offset_ft: -25.4 };
  assert.equal(L.externalNote(lookup, clock),
    "adsb.lol: added 42 points, filled 2 gaps (+3) · +0.1 s, −25 ft against your receiver");
});

test("externalNote gives no offset when the default was applied", () => {
  const clock = (t) => `t${t}`;
  const lookup = { status: "done", points_added: 5, gaps_filled: 1, gap_points_added: 2, offset_source: "default",
    match_n: 0, applied_offset_s: 0.1, offset_s: null, offset_ft: null };
  assert.equal(L.externalNote(lookup, clock), "adsb.lol: added 5 points, filled 1 gap (+2)");
});

test("externalNote leaves out the offset when a matches row carries no offset to show", () => {
  const clock = (t) => `t${t}`;
  const lookup = { status: "done", points_added: 4, gaps_filled: 0, gap_points_added: 0, offset_source: "matches",
    match_n: null, applied_offset_s: null, offset_s: null, offset_ft: null };
  assert.equal(L.externalNote(lookup, clock), "adsb.lol: added 4 points");
});

test("externalNote says nothing to add, with the offset, when neither the tail nor the gaps added a point", () => {
  const clock = (t) => `t${t}`;
  assert.equal(L.externalNote({ status: "done", points_added: 0, gaps_filled: 0, gap_points_added: 0,
    offset_source: "matches", match_n: 12, applied_offset_s: 0.1, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: nothing to add · +0.1 s from 12 matching points");
  assert.equal(L.externalNote({ status: "done", points_added: 0, gaps_filled: 0, gap_points_added: 0,
    offset_source: "default", match_n: 0, applied_offset_s: 0.1, offset_s: null, offset_ft: null }, clock),
    "adsb.lol: nothing to add");
});

test("externalNote reads a lookup finished before gap filling as before", () => {
  const clock = (t) => `t${t}`;
  const lookup = { status: "done", points_added: 42, gaps_filled: 0, gap_points_added: 0, offset_source: null,
    match_n: null, applied_offset_s: null, offset_s: 0.83, offset_ft: -25.4 };
  assert.equal(L.externalNote(lookup, clock), "adsb.lol: added 42 points · +0.8 s, −25 ft against your receiver");
});

test("bannerFor warns while adsb.lol polling is paused", () => {
  const clock = (t) => `t${t}`;
  const fine = { db: { ok: true }, collector: { state: "running" }, feed: { state: "ok" },
    external: { enabled: true, paused_until: 500, paused_status: 429, paused_error: "HTTP 429" } };
  assert.deepEqual(L.bannerFor(fine, "open", clock),
    { level: "warning", text: "adsb.lol polling paused until t500 after HTTP 429." });
});

test("bannerFor's pause banner leaves out the HTTP status without a numeric one", () => {
  const clock = (t) => `t${t}`;
  const fine = { db: { ok: true }, collector: { state: "running" }, feed: { state: "ok" },
    external: { enabled: true, paused_until: 500, paused_status: null, paused_error: null } };
  assert.deepEqual(L.bannerFor(fine, "open", clock), { level: "warning", text: "adsb.lol polling paused until t500." });
});

test("bannerFor's pause banner says when the key file can't be read, and an unrelated error adds nothing", () => {
  const clock = (t) => `t${t}`;
  const paused = (paused_error) => ({ db: { ok: true }, collector: { state: "running" }, feed: { state: "ok" },
    external: { enabled: true, paused_until: 500, paused_status: null, paused_error } });
  assert.deepEqual(L.bannerFor(paused("api_key_file: FileNotFoundError: gone"), "open", clock),
    { level: "warning", text: "adsb.lol polling paused until t500: the key file can't be read." });
  assert.deepEqual(L.bannerFor(paused("TimeoutError: timed out"), "open", clock),
    { level: "warning", text: "adsb.lol polling paused until t500." });
});

test("bannerFor's stream-retrying warning wins over a paused poller", () => {
  const clock = (t) => `t${t}`;
  const fine = { db: { ok: true }, collector: { state: "running" }, feed: { state: "ok" },
    external: { enabled: true, paused_until: 500, paused_status: 429 } };
  assert.equal(L.bannerFor(fine, "retrying", clock).text, "Live updates lost: reconnecting.");
});

test("bannerFor's stopped-collector warning wins over a paused poller", () => {
  const clock = (t) => `t${t}`;
  const stopped = { db: { ok: true }, collector: { state: "stopped", ended_at: 5 }, feed: { state: "ok" },
    external: { enabled: true, paused_until: 500, paused_status: 429 } };
  assert.equal(L.bannerFor(stopped, "open", clock).text, "The collector stopped at t5.");
});

test("bannerFor gives no adsb.lol banner without a health report's external, or while it isn't paused", () => {
  const clock = (t) => `t${t}`;
  const noExternal = { db: { ok: true }, collector: { state: "running" }, feed: { state: "ok" } };
  assert.equal(L.bannerFor(noExternal, "open", clock), null);
  const notPaused = { ...noExternal, external: { enabled: true, paused_until: null, paused_status: null } };
  assert.equal(L.bannerFor(notPaused, "open", clock), null);
});

test("liveRunId reads a health report's collector run id, or null without one", () => {
  assert.equal(L.liveRunId({ state: "running", run_id: 5 }), 5);
  assert.equal(L.liveRunId({ state: "none" }), null);          // no live run has ever written to this database
  assert.equal(L.liveRunId(undefined), null);                  // the health report itself failed
});

test("liveRunChanged is true only once a later run replaces one already seen", () => {
  assert.equal(L.liveRunChanged(null, 5), false);        // the first run observed: start() already loaded its cards
  assert.equal(L.liveRunChanged(5, 5), false);
  assert.equal(L.liveRunChanged(5, 6), true);            // the collector's start-up sweep left the old run's cards stale
  assert.equal(L.liveRunChanged(5, null), false);        // an unreadable report or a database with no run: nothing to load
});

test("backoffMs doubles from 1s with a cap at 30s, plus up to 20% jitter", () => {
  assert.equal(L.backoffMs(0, () => 0), 1000);
  assert.equal(L.backoffMs(3, () => 0), 8000);
  assert.equal(L.backoffMs(9, () => 1), 36000);          // 1000 * 2**9 = 512000, capped to 30000, +20% jitter
  assert.equal(L.backoffMs(0, () => 1), 1200);
});

test("EventStream backs off, resumes from the last event, ignores stale sources, and reload stops it", () => {
  class FakeSource {
    static all = [];
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; FakeSource.all.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.closed = true; }
  }
  const timers = [];
  const seen = [];
  const states = [];
  let reloads = 0;
  const stream = new L.EventStream(100, { onEvent: (e) => seen.push(e.id), onReload: () => { reloads += 1; },
    onState: (state, delay) => states.push(delay === undefined ? state : `${state} ${delay}`), EventSourceImpl: FakeSource,
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimer: () => {}, random: () => 0 });
  stream.start();
  const [s1] = FakeSource.all;
  assert.equal(s1.url, "api/events?after=100");
  s1.onopen();
  s1.onmessage({ data: JSON.stringify({ id: 101, kind: "approach.updated", payload: {} }) });
  s1.onmessage({ data: JSON.stringify({ id: 101, kind: "approach.updated", payload: {} }) });   // a repeat: ignored
  s1.onerror();
  assert.ok(s1.closed);
  assert.equal(timers[0].ms, 1000);
  timers[0].fn();
  const s2 = FakeSource.all[1];
  assert.equal(s2.url, "api/events?after=101");           // resumed from the last event actually received
  s2.onerror();
  assert.equal(timers[1].ms, 2000);                       // backing off further
  s1.onerror();                                           // s1 is stale (superseded by s2): ignored
  s1.onmessage({ data: JSON.stringify({ id: 150, kind: "approach.updated", payload: {} }) });
  assert.equal(timers.length, 2);
  timers[1].fn();
  const s3 = FakeSource.all[2];
  s3.onopen();
  s3.listeners.reload();
  assert.equal(reloads, 1);
  assert.ok(s3.closed);
  s3.onerror();                                           // stopped by reload: no further reconnect
  assert.equal(timers.length, 2);
  assert.deepEqual(seen, [101]);
  assert.deepEqual(states, ["connecting", "open", "retrying 1000", "connecting", "retrying 2000", "connecting", "open"]);

  const fresh = new L.EventStream(null, { EventSourceImpl: FakeSource });
  fresh.start();
  assert.equal(FakeSource.all[3].url, "api/events");      // no `after`: from whatever comes next
  fresh.stop();
  assert.ok(FakeSource.all[3].closed);
});

test("gate1000Text reads the last closed attempt's observed gate, and an em dash without one", () => {
  // VIR19Z, 2026-10-22: the gate as the collector stored it.
  const gate = { observed: true, gp_dev_ft: -1.46, gp_dots: -0.0258, loc_dev_ft: -2.56, loc_dots: -0.0070, gs: 137.1,
    sink_fpm: 768 };
  const landed = { outcome: "landed", gate1000: gate };
  assert.equal(L.gate1000Text({ attempts: [landed] }), "-1 ft (0.0) · 3 ft L (0.0) · 137 kt · 768 fpm");
  const goAround = { outcome: "go_around", gate1000: { ...gate, gp_dev_ft: 48.2, gp_dots: 0.4, loc_dev_ft: 43.4,
    loc_dots: 0.09, gs: 150.4, sink_fpm: null } };
  assert.equal(L.gate1000Text({ attempts: [goAround, landed] }), "-1 ft (0.0) · 3 ft L (0.0) · 137 kt · 768 fpm");
  assert.equal(L.gate1000Text({ attempts: [landed, { outcome: null, gate1000: { ...gate, gs: 99 } }] }),
    "-1 ft (0.0) · 3 ft L (0.0) · 137 kt · 768 fpm");              // an attempt still open is not the one it reads
  assert.equal(L.gate1000Text({ attempts: [goAround] }), "48 ft (0.4) · 43 ft R (0.1) · 150 kt · —");
  assert.equal(L.gate1000Text({ attempts: [{ outcome: "landed", gate1000: { observed: false } }] }), "—");
  assert.equal(L.gate1000Text({ attempts: [{ outcome: "landed", gate1000: null }] }), "—");
  assert.equal(L.gate1000Text({ attempts: [{ outcome: null, gate1000: gate }] }), "—");      // no closed attempt yet
});

// --- Procedure paths: labels, the approach filter and the drawer's path note --------------------------------------

const QB = { path: "quiet-bridge", path_label: "Quiet Bridge",
  path_source: "SFO VOR R-095 inbound (292.0T, declination 17E); turn at 5.0 nm and join at 1.1 nm" };
const NO_PATH = { path: null, path_label: null, path_source: null };

test("referenceText joins an attempt's reference labels with an arrow, and is empty for the centreline alone", () => {
  assert.equal(L.referenceText([{ name: "quiet-bridge", label: "Quiet Bridge" }, { name: null, label: "ILS" }]),
    "Quiet Bridge → ILS");
  assert.equal(L.referenceText([{ name: "quiet-bridge", label: "Quiet Bridge" }]), "Quiet Bridge");
  assert.equal(L.referenceText([{ name: null, label: "ILS" }]), "");
  assert.equal(L.referenceText([{ name: null, label: "straight-in" }]), "");
  assert.equal(L.referenceText([{ name: null, label: "ILS" }, { name: "quiet-bridge", label: "Quiet Bridge" }]),
    "ILS → Quiet Bridge");
  assert.equal(L.referenceText([]), "");
  assert.equal(L.referenceText(undefined), "");              // an older server's attempt has no `references`
});

test("attemptReferenceText is the log chip: the references, else the path's label when there are none", () => {
  const chain = [{ name: "quiet-bridge", label: "Quiet Bridge" }, { name: null, label: "ILS" }];
  assert.equal(L.attemptReferenceText({ path: "quiet-bridge", path_label: "Quiet Bridge", references: chain }),
    "Quiet Bridge → ILS");
  assert.equal(L.attemptReferenceText({ path: null, path_label: null, references: [{ name: null, label: "ILS" }] }), "");
  // no references: the path's label, only while the attempt has a path
  assert.equal(L.attemptReferenceText({ path: "quiet-bridge", path_label: "Quiet Bridge", references: [] }), "Quiet Bridge");
  assert.equal(L.attemptReferenceText({ path: "quiet-bridge", path_label: null, references: [] }), "quiet-bridge");
  assert.equal(L.attemptReferenceText({ path: null, path_label: "ILS", references: [] }), "");
  assert.equal(L.attemptReferenceText({ path: null, path_label: "ILS" }), "");
  assert.equal(L.attemptReferenceText({}), "");              // an attempt from before procedure paths
});

test("parseState and toSearch carry the approach kind as approach_kind, all by default and left out of the URL", () => {
  const base = L.parseState("");
  assert.equal(base.approachKind, "all");
  assert.equal(L.parseState("?approach_kind=path").approachKind, "path");
  assert.equal(L.parseState("?approach_kind=straight").approachKind, "straight");
  assert.equal(L.parseState("?approach_kind=all").approachKind, "all");
  assert.equal(L.parseState("?approach_kind=bogus").approachKind, "all");        // not one of the three: the default
  assert.equal(L.parseState("?approach_kind=toString").approachKind, "all");
  assert.equal(L.toSearch({ ...base, approachKind: "path" }), "?approach_kind=path");
  assert.equal(L.toSearch({ ...base, approachKind: "straight" }), "?approach_kind=straight");
  assert.equal(L.toSearch({ ...base, approachKind: "all" }), "");
  assert.equal(L.toSearch(L.parseState("?approach_kind=all")), "");
  const s = L.parseState("?airport=KSFO&approach_kind=path&window=6h");
  assert.deepEqual(L.parseState(L.toSearch(s)), s);
  assert.equal(L.toSearch(s), "?airport=KSFO&window=6h&approach_kind=path");
});

test("the approach kind sits beside the open drawer's approach=<id>, which keeps its own parameter", () => {
  // `approach` is the drawer's approach id (live cards, bookmarks and the search page link to it), so the filter
  // cannot share it: both are in one URL when a drawer is open over a filtered log.
  const s = L.parseState("?approach_kind=straight&approach=54");
  assert.equal(s.approachKind, "straight");
  assert.equal(s.approach, 54);
  assert.equal(L.toSearch(s), "?approach_kind=straight&approach=54");
  assert.deepEqual(L.parseState(L.toSearch(s)), s);
  const plain = L.parseState("?approach=54");
  assert.deepEqual([plain.approachKind, plain.approach], ["all", 54]);
  const stray = L.parseState("?approach=path");                      // not a drawer id, and not the filter
  assert.deepEqual([stray.approachKind, stray.approach], ["all", null]);
});

test("arrivalsQuery sends the approach kind as approach=, and nothing for all", () => {
  const base = L.parseState("");
  assert.equal(L.arrivalsQuery({ ...base, approachKind: "straight" }, 100000, {}), "since=13600&approach=straight");
  assert.equal(L.arrivalsQuery({ ...base, approachKind: "path" }, 100000, {}), "since=13600&approach=path");
  assert.equal(L.arrivalsQuery({ ...base, approachKind: "all" }, 100000, {}), "since=13600");
  assert.equal(L.arrivalsQuery({ ...base, window: "all", ga: true, approachKind: "path" }, 5, { limit: 50, arrival_id: 7 }),
    "ga=1&approach=path&limit=50&arrival_id=7");
  // the open drawer's approach id is never the filter: it is not sent to /api/arrivals at all
  assert.equal(L.arrivalsQuery({ ...base, approach: 54 }, 100000, {}), "since=13600");
});

test("changing the approach kind reloads the log but not the live cards", () => {
  const view = L.parseState("");
  assert.deepEqual(L.viewChange(view, { ...view, approachKind: "path" }),
    { search: "?approach_kind=path", reloadLog: true, reloadLive: false, drawer: null });
  assert.equal(L.viewChange({ ...view, approachKind: "path" }, { ...view, approachKind: "path", approach: 3 }).reloadLog, false);
});

test("lateralAxisTitle says what the cross-track chart measures from", () => {
  assert.equal(L.lateralAxisTitle({ glide_path_deg: 3, ...QB }), "Cross-track from Quiet Bridge path");
  assert.equal(L.lateralAxisTitle({ glide_path_deg: 3, ...NO_PATH }), "Cross-track from the centreline");
  assert.equal(L.lateralAxisTitle({ glide_path_deg: 3 }), "Cross-track from the centreline");     // an older server
  assert.equal(L.lateralAxisTitle(null), "Cross-track from the centreline");
  assert.equal(L.lateralAxisTitle(undefined), "Cross-track from the centreline");
});

test("lateralZeroLabel names the chart's zero line for what it is drawn against", () => {
  assert.equal(L.lateralZeroLabel(null), "Centreline");
  assert.equal(L.lateralZeroLabel(NO_PATH), "Centreline");
  assert.equal(L.lateralZeroLabel(QB), "Reference");
});

test("cardSubText labels a live card with its path only while its path is set", () => {
  const card = { airport: "KSFO", runway: "28R", attempt_no: 1, path: "quiet-bridge", path_label: "Quiet Bridge" };
  assert.equal(L.cardSubText(card), "KSFO 28R · Quiet Bridge");
  assert.equal(L.cardSubText({ ...card, attempt_no: 2 }), "KSFO 28R · Quiet Bridge · attempt 2");
  // approach.started/updated events carry path_label "ILS" for a straight-in approach: `path` is what gates it
  assert.equal(L.cardSubText({ ...card, path: null, path_label: "ILS" }), "KSFO 28R");
  assert.equal(L.cardSubText({ ...card, path: null, path_label: "straight-in", attempt_no: 3 }), "KSFO 28R · attempt 3");
  assert.equal(L.cardSubText({ airport: "KOAK", runway: "30", attempt_no: null }), "KOAK 30");     // a pre-deploy event
  assert.equal(L.cardSubText({ ...card, path_label: null }), "KSFO 28R · quiet-bridge");
});

test("a live card follows its attempt's path through approach.started and approach.updated", () => {
  const cards = new Map();
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 40, t: 1, payload: { hex: "q1", runway: "28R",
    airport: "KSFO", callsign: "VIR19Z", attempt_no: 1, path: "quiet-bridge", path_label: "Quiet Bridge", along_nm: 9.8 } });
  assert.deepEqual([cards.get("q1").path, cards.get("q1").path_label], ["quiet-bridge", "Quiet Bridge"]);
  assert.equal(L.cardSubText(cards.get("q1")), "KSFO 28R · Quiet Bridge");
  // joined the localizer: the event now names no path, though its label still reads ILS
  L.applyLiveEvent(cards, { kind: "approach.updated", approach_id: 40, t: 2, payload: { hex: "q1", along_nm: 2,
    path: null, path_label: "ILS" } });
  assert.deepEqual([cards.get("q1").path, cards.get("q1").path_label], [null, "ILS"]);
  assert.equal(L.cardSubText(cards.get("q1")), "KSFO 28R");
  // events from before the deploy carry neither
  L.applyLiveEvent(cards, { kind: "approach.started", approach_id: 41, t: 3, payload: { hex: "z9", runway: "28L",
    airport: "KSFO", along_nm: 7 } });
  assert.deepEqual([cards.get("z9").path, cards.get("z9").path_label], [null, null]);
  assert.equal(L.cardSubText(cards.get("z9")), "KSFO 28L");
});

test("an approach.emergency card carries the attempt's path too, and the card fields are the live API's", () => {
  const cards = new Map();
  L.applyLiveEvent(cards, { kind: "approach.emergency", approach_id: 50, t: 100, payload: { hex: "e1", runway: "28R",
    airport: "KSFO", callsign: "VIR19Z", along_nm: 6, hat_ft: 1900, attempt_no: 1, arrival_id: 50, established_at: 90,
    in_progress: true, emergency: ["EMERGENCY_GENERAL"], path: "quiet-bridge", path_label: "Quiet Bridge" } });
  assert.deepEqual([cards.get("e1").path, cards.get("e1").path_label], ["quiet-bridge", "Quiet Bridge"]);
  // the fields a card takes from an event are the ones /api/live sends (web/live.py's CARD_FIELDS, which
  // tests/test_web_page.py checks this list against), so none is dropped between the API and the event stream
  assert.ok(L.CARD_FIELDS.includes("path") && L.CARD_FIELDS.includes("path_label"));
});

// The detail's points as the drawer gets them: oldest first, each naming the reference it was measured against.
const qbThenCentreline = [
  { t: 1, along_nm: 9.0, reference: "quiet-bridge" }, { t: 2, along_nm: 7.0, reference: "quiet-bridge" },
  { t: 3, along_nm: 2.1, reference: null }, { t: 4, along_nm: 1.0, reference: null }];

test("drawerReferences lists the references an approach's points were measured against, in the order first flown", () => {
  assert.deepEqual(L.drawerReferences(qbThenCentreline, QB, false),
    [{ name: "quiet-bridge", label: "Quiet Bridge" }, { name: null, label: "centreline" }]);
  const straight = [{ t: 1, along_nm: 5, reference: null }, { t: 2, along_nm: 4, reference: null }];
  assert.deepEqual(L.drawerReferences(straight, NO_PATH, false), [{ name: null, label: "centreline" }]);
  const pathOnly = qbThenCentreline.slice(0, 2);
  assert.deepEqual(L.drawerReferences(pathOnly, QB, false), [{ name: "quiet-bridge", label: "Quiet Bridge" }]);
  // the same path flown again after the centreline is still listed once
  const twice = [...qbThenCentreline, { t: 5, along_nm: 0.5, reference: "quiet-bridge" }];
  assert.deepEqual(L.drawerReferences(twice, QB, false).map((r) => r.name), ["quiet-bridge", null]);
});

test("the drawer names the centreline as the log does: the detail's centreline_label, else \"centreline\"", () => {
  const ils = { ...QB, centreline_label: "ILS" };
  assert.equal(L.referenceText(L.drawerReferences(qbThenCentreline, ils, false)), "Quiet Bridge → ILS");
  assert.deepEqual(L.drawerReferences(qbThenCentreline, { ...QB, centreline_label: "straight-in" }, false),
    [{ name: "quiet-bridge", label: "Quiet Bridge" }, { name: null, label: "straight-in" }]);
  assert.deepEqual(L.referenceSwitches(qbThenCentreline, ils, false), [{ along_nm: 2.1, label: "ILS" }]);
  const back = [...qbThenCentreline, { t: 5, along_nm: 0.6, reference: "quiet-bridge" }];
  assert.deepEqual(L.referenceSwitches(back, ils, false),
    [{ along_nm: 2.1, label: "ILS" }, { along_nm: 0.6, label: "Quiet Bridge" }]);
  // a straight-in approach lists its centreline by the same label, which referenceText leaves out as the log does
  const straight = [{ t: 1, along_nm: 5, reference: null }, { t: 2, along_nm: 4, reference: null }];
  assert.deepEqual(L.drawerReferences(straight, { ...NO_PATH, centreline_label: "ILS" }, false), [{ name: null, label: "ILS" }]);
  // an older server sends none, and a runway without a reference has no detail reference at all
  assert.deepEqual(L.drawerReferences(straight, NO_PATH, false), [{ name: null, label: "centreline" }]);
  assert.deepEqual(L.drawerReferences(straight, null, false), [{ name: null, label: "centreline" }]);
});

test("drawerReferences puts the path first when it was left before any stored point, and copes with missing data", () => {
  const onlyCentreline = [{ t: 1, along_nm: 9, reference: null }, { t: 2, along_nm: 8, reference: null }];
  assert.deepEqual(L.drawerReferences(onlyCentreline, QB, false).map((r) => r.name), ["quiet-bridge", null]);
  assert.deepEqual(L.drawerReferences([], NO_PATH, false), [{ name: null, label: "centreline" }]);
  assert.deepEqual(L.drawerReferences([], QB, false), [{ name: "quiet-bridge", label: "Quiet Bridge" }]);   // no points: its path
  // a database not yet migrated sends points with no `reference` at all: the centreline
  assert.deepEqual(L.drawerReferences([{ t: 1, along_nm: 5 }], NO_PATH, false), [{ name: null, label: "centreline" }]);
  // a path no longer configured, or a runway without a reference: the name is the label
  const ghost = [{ t: 1, along_nm: 5, reference: "old-path" }];
  assert.deepEqual(L.drawerReferences(ghost, null, false), [{ name: "old-path", label: "old-path" }]);
  assert.deepEqual(L.drawerReferences(ghost, QB, false),
    [{ name: "quiet-bridge", label: "Quiet Bridge" }, { name: "old-path", label: "old-path" }]);
});

test("drawerReferences for an attempt in progress is its path alone: its points name no reference yet", () => {
  const live = [{ t: 1, along_nm: 9, reference: null }, { t: 2, along_nm: 8, reference: null }];
  assert.deepEqual(L.drawerReferences(live, QB, true), [{ name: "quiet-bridge", label: "Quiet Bridge" }]);
  assert.deepEqual(L.drawerReferences(live, NO_PATH, true), [{ name: null, label: "centreline" }]);
  assert.equal(L.referenceText(L.drawerReferences(live, NO_PATH, true)), "");
  assert.equal(L.referenceText(L.drawerReferences(live, QB, true)), "Quiet Bridge");
});

test("referenceSwitches marks each change of reference at the new reference's first point", () => {
  assert.deepEqual(L.referenceSwitches(qbThenCentreline, QB, false), [{ along_nm: 2.1, label: "centreline" }]);
  const back = [...qbThenCentreline, { t: 5, along_nm: 0.6, reference: "quiet-bridge" }];
  assert.deepEqual(L.referenceSwitches(back, QB, false),
    [{ along_nm: 2.1, label: "centreline" }, { along_nm: 0.6, label: "Quiet Bridge" }]);
  assert.deepEqual(L.referenceSwitches(qbThenCentreline.slice(0, 2), QB, false), []);        // no change, no marker
  assert.deepEqual(L.referenceSwitches([{ t: 1, along_nm: 5, reference: null }, { t: 2, along_nm: 4, reference: null }],
    NO_PATH, false), []);
  assert.deepEqual(L.referenceSwitches([], QB, false), []);
  assert.deepEqual(L.referenceSwitches(qbThenCentreline, QB, true), []);                     // in progress: no markers
});

test("referenceSwitches takes the marker's distance from the first point of the run that has one", () => {
  const gap = [{ t: 1, along_nm: 9, reference: "quiet-bridge" }, { t: 2, along_nm: null, reference: null },
    { t: 3, along_nm: 2.4, reference: null }];
  assert.deepEqual(L.referenceSwitches(gap, QB, false), [{ along_nm: 2.4, label: "centreline" }]);
  const unplaced = [{ t: 1, along_nm: 9, reference: "quiet-bridge" }, { t: 2, along_nm: null, reference: null }];
  assert.deepEqual(L.referenceSwitches(unplaced, QB, false), []);                           // nowhere to draw it
});

test("pathNote gives the drawer's note on the path an approach flew: its source", () => {
  assert.equal(L.pathNote({ glide_path_deg: 3, ...QB }), `Path source: ${QB.path_source}`);
  assert.equal(L.pathNote({ glide_path_deg: 3, ...NO_PATH }), null);
  assert.equal(L.pathNote({ glide_path_deg: 3 }), null);
  assert.equal(L.pathNote(null), null);
  // a path no longer configured: the API sends the stored name for its label and its source
  assert.equal(L.pathNote({ path: "old-path", path_label: "old-path", path_source: "old-path" }),
    "Path no longer configured");
});
