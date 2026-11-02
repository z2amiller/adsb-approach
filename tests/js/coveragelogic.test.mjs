// Unit tests for coveragelogic.js: the span control, the day axis, grouping coverage rows by runway, the quiet-runway
// floor, aligning a runway with the day axis, the marks drawn on every chart and the rows their labels stack onto, the
// before/after periods either side of each mark and their n-weighted aggregate, one before/after cell's text, which
// sections the page shows, the outcome charts' stacked series, and the norms note's wording. In the style of
// logic.test.mjs.
import assert from "node:assert/strict";
import { test } from "node:test";

import * as C from "../../adsb_approach/web/static/coveragelogic.js";

/** One coverage_daily row, in the shape GET /api/stats sends (web/stats.py's COVERAGE_FIELDS). `p50: null` fakes a
 * day too thin for a quartile: lowest_hat_p25/p75 go null with it, as a real thin day's would. */
function row(day, { airport = "KSFO", runway = "28R", n = 10, p50 = 800, backfillN = 0, gate1000 = 0.6, gate500 = 0.1,
  closest = 2.5 } = {}) {
  return {
    airport, runway, day, n,
    lowest_hat_p25: p50 === null ? null : p50 - 100,
    lowest_hat_p50: p50,
    lowest_hat_p75: p50 === null ? null : p50 + 100,
    closest_nm_p50: closest,
    gate1000_observed_frac: gate1000,
    gate500_observed_frac: gate500,
    backfill_n: backfillN,
    backfill_frac: n > 0 ? backfillN / n : null,
  };
}

/** `count` consecutive "YYYY-MM-DD" days from `start`, by UTC date arithmetic — an independent day-range builder so
 * these tests don't reach into coveragelogic.js's own unexported day helpers. */
function daysFrom(start, count) {
  const [y, m, d] = start.split("-").map(Number);
  return Array.from({ length: count }, (_, i) => new Date(Date.UTC(y, m - 1, d + i)).toISOString().slice(0, 10));
}

test("parseSpan defaults to 30 days and reads 7, 90, 365 and all", () => {
  assert.deepEqual(C.parseSpan(""), { days: 30 });
  assert.deepEqual(C.parseSpan("?days=7"), { days: 7 });
  assert.deepEqual(C.parseSpan("?days=90"), { days: 90 });
  assert.deepEqual(C.parseSpan("?days=365"), { days: 365 });
  assert.deepEqual(C.parseSpan("?days=all"), { days: null });
});

test("parseSpan falls back to 30 for anything else, and spanSearch round-trips it", () => {
  assert.deepEqual(C.parseSpan("?days=abc"), { days: 30 });
  assert.deepEqual(C.parseSpan("?days=15"), { days: 30 });        // not one of SPANS
  assert.deepEqual(C.parseSpan("?days="), { days: 30 });
  assert.deepEqual(C.parseSpan("?days=-7"), { days: 30 });
  for (const days of [...C.SPANS, null]) {
    assert.deepEqual(C.parseSpan(C.spanSearch(days)), { days });
  }
  assert.equal(C.spanSearch(30), "");
  assert.equal(C.spanSearch(90), "?days=90");
  assert.equal(C.spanSearch(null), "?days=all");
});

test("spanDays lists every day of the span, including the ones with no coverage", () => {
  const body = { since_day: "2026-09-10", until_day: "2026-09-13", coverage: [], outcomes: [] };
  assert.deepEqual(C.spanDays(body), ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"]);
  const rollover = { since_day: "2026-09-29", until_day: "2026-10-02", coverage: [], outcomes: [] };
  assert.deepEqual(C.spanDays(rollover), ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]);
  const oneDay = { since_day: "2026-09-13", until_day: "2026-09-13", coverage: [], outcomes: [] };
  assert.deepEqual(C.spanDays(oneDay), ["2026-09-13"]);
});

test("spanDays falls back to the days the rows carry when the span is all", () => {
  const body = { since_day: null, until_day: "2026-09-13",
    coverage: [row("2026-09-11")], outcomes: [{ airport: "KSFO", day: "2026-09-10", outcome: "landed", n: 1 }] };
  assert.deepEqual(C.spanDays(body), ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"]);
  const empty = { since_day: null, until_day: "2026-09-13", coverage: [], outcomes: [] };
  assert.deepEqual(C.spanDays(empty), ["2026-09-13"]);
});

test("byRunway groups the coverage rows by runway, busiest first", () => {
  const coverage = [row("2026-09-10", { runway: "28R", n: 10 }), row("2026-09-11", { runway: "28R", n: 12 }),
    row("2026-09-10", { runway: "28L", n: 30 }), row("2026-09-10", { airport: "KOAK", runway: "30", n: 30 })];
  const groups = C.byRunway(coverage);
  assert.deepEqual(groups.map((g) => g.key), ["KOAK/30", "KSFO/28L", "KSFO/28R"]);     // 30 and 30 tie on AIRPORT/RUNWAY
  assert.deepEqual(groups.map((g) => g.total), [30, 30, 22]);
  assert.deepEqual(groups.map((g) => g.airport), ["KOAK", "KSFO", "KSFO"]);
  assert.equal(groups[2].rows.length, 2);
});

test("splitQuiet folds away the runways under the traffic floor", () => {
  const groups = [{ key: "KSFO/28R", total: 30 }, { key: "KSFO/28L", total: 25 }, { key: "KOAK/30", total: 24 }];
  const { busy, quiet } = C.splitQuiet(groups, C.TRAFFIC_FLOOR);
  assert.deepEqual(busy.map((g) => g.key), ["KSFO/28R", "KSFO/28L"]);    // 25 meets the floor
  assert.deepEqual(quiet.map((g) => g.key), ["KOAK/30"]);                // 24 is under it
});

test("runwaySeries aligns a runway with the day axis and leaves a missing day as a gap", () => {
  const group = { rows: [row("2026-09-10", { n: 10, p50: 800, backfillN: 2 }),
    row("2026-09-12", { n: 12, p50: 700, backfillN: 3 })] };
  const days = ["2026-09-10", "2026-09-11", "2026-09-12"];
  const series = C.runwaySeries(group, days);
  assert.deepEqual(series.lowest, [800, null, 700]);
  assert.deepEqual(series.counts, [10, null, 12]);
  assert.deepEqual(series.backfill, [0.2, null, 0.25]);

  // a row that exists but is too thin for a quartile: the median gaps, but the count and the backfill share do not
  const thin = { rows: [row("2026-09-10", { n: 2, p50: null, backfillN: 0 })] };
  const thinSeries = C.runwaySeries(thin, ["2026-09-10"]);
  assert.deepEqual(thinSeries.lowest, [null]);
  assert.deepEqual(thinSeries.counts, [2]);
  assert.deepEqual(thinSeries.backfill, [0]);
});

test("markPositions puts a mark on its own day, between two days when that day is missing, and drops one outside the span", () => {
  const days = ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"];
  assert.deepEqual(C.markPositions([{ at: "2026-09-12", label: "antenna" }], days),
    [{ at: "2026-09-12", label: "antenna", x: 2 }]);

  const gappy = ["2026-09-10", "2026-09-12", "2026-09-14"];      // every other day missing from the axis
  assert.deepEqual(C.markPositions([{ at: "2026-09-11", label: "mid" }], gappy),
    [{ at: "2026-09-11", label: "mid", x: 0.5 }]);               // halfway between index 0 and index 1

  assert.deepEqual(
    C.markPositions([{ at: "2026-09-09", label: "too early" }, { at: "2026-09-15", label: "too late" }], days), []);
});

test("labelRows puts a mark alone on row 0", () => {
  assert.deepEqual(C.labelRows([50], [40], 800, 4), [0]);
});

test("labelRows keeps two far-apart marks on row 0", () => {
  assert.deepEqual(C.labelRows([0, 300], [40, 40], 800, 4), [0, 0]);
});

test("labelRows drops an overlapping second mark to row 1", () => {
  assert.deepEqual(C.labelRows([0, 20], [40, 40], 800, 4), [0, 1]);
});

test("labelRows gives a third mark that overlaps both of the first two its own row", () => {
  // 0-40, 5-45 and 10-50 all overlap pairwise, in the same order as the input arrays (x order == input order here).
  assert.deepEqual(C.labelRows([0, 5, 10], [40, 40, 40], 800, 4), [0, 1, 2]);
});

test("labelRows falls back to the last row instead of dropping a label once maxRows is reached", () => {
  assert.deepEqual(C.labelRows([0, 5, 10], [40, 40, 40], 800, 2), [0, 1, 1]);
});

test("labelRows pulls a label left to stay inside the plot, which can make it collide with an earlier one", () => {
  // Unclamped, "B" (55-65) wouldn't reach "A" (90-130), so both would fit on row 0. Clamped to plotWidth=100, A
  // becomes 60-100 and now does reach B's row, so A is pushed to row 1 instead of overlapping it there.
  assert.deepEqual(C.labelRows([90, 55], [40, 10], 100, 4), [1, 0]);
});

test("periods run from one mark to the next, and a mark on the first day has nothing before it", () => {
  const days = daysFrom("2026-09-01", 20);
  const marks = [{ at: "2026-09-01", label: "first" }, { at: "2026-09-08", label: "second" },
    { at: "2026-09-15", label: "third" }];
  assert.deepEqual(C.periods(marks, days), [
    { at: "2026-09-01", label: "first", before: null, after: ["2026-09-01", "2026-09-07"] },
    { at: "2026-09-08", label: "second", before: ["2026-09-01", "2026-09-07"], after: ["2026-09-08", "2026-09-14"] },
    { at: "2026-09-15", label: "third", before: ["2026-09-08", "2026-09-14"], after: ["2026-09-15", "2026-09-20"] },
  ]);
});

test("aggregate weights each day by its approaches", () => {
  const rows = [row("2026-09-10", { n: 10, p50: 700 }), row("2026-09-11", { n: 30, p50: 900 })];
  const agg = C.aggregate(rows);
  assert.equal(agg.n, 40);
  assert.equal(agg.lowestP50, (700 * 10 + 900 * 30) / 40);        // 850: the busier day counts three times as much
});

test("aggregate is exact for the gate and adsb.lol shares", () => {
  const rows = [row("2026-09-10", { n: 10, backfillN: 2, gate1000: 0.5, gate500: 0.1 }),
    row("2026-09-11", { n: 30, backfillN: 9, gate1000: 0.9, gate500: 0.3 })];
  const agg = C.aggregate(rows);
  assert.equal(agg.backfillShare, 11 / 40);                        // a straight count over a count
  assert.equal(agg.gate1000Frac, (0.5 * 10 + 0.9 * 30) / 40);
  assert.equal(agg.gate500Frac, (0.1 * 10 + 0.3 * 30) / 40);
});

test("aggregate ignores a day whose median is null, and is null when no day has one", () => {
  const rows = [row("2026-09-10", { n: 10, p50: 700 }), row("2026-09-11", { n: 90, p50: null })];
  const agg = C.aggregate(rows);
  assert.equal(agg.n, 100);                 // the thin day still counts in n...
  assert.equal(agg.lowestP50, 700);         // ...but is left out of the median's own weighting entirely
  assert.equal(C.aggregate([row("2026-09-10", { n: 10, p50: null })]).lowestP50, null);
  assert.equal(C.aggregate([]), null);
});

test("beforeAfter compares the periods either side of each mark, per runway", () => {
  const coverage = [
    row("2026-09-01", { runway: "28R", n: 10, p50: 900 }), row("2026-09-05", { runway: "28R", n: 10, p50: 900 }),
    row("2026-09-08", { runway: "28R", n: 10, p50: 700 }), row("2026-09-12", { runway: "28R", n: 10, p50: 700 }),
  ];
  const days = daysFrom("2026-09-01", 15);
  const [entry] = C.beforeAfter(coverage, [{ at: "2026-09-08", label: "antenna" }], days);
  assert.equal(entry.at, "2026-09-08");
  assert.equal(entry.label, "antenna");
  const r = entry.rows.find((x) => x.key === "KSFO/28R");
  assert.equal(r.before.n, 20);
  assert.equal(r.before.lowestP50, 900);
  assert.equal(r.after.n, 20);
  assert.equal(r.after.lowestP50, 700);
});

test("beforeAfter leaves a runway with no data on one side with a null after", () => {
  const coverage = [row("2026-09-01", { n: 10, p50: 900 })];         // nothing flew after the mark
  const days = daysFrom("2026-09-01", 10);
  const [entry] = C.beforeAfter(coverage, [{ at: "2026-09-08", label: "antenna" }], days);
  assert.equal(entry.rows[0].after, null);
});

test("beforeAfter gives the first mark no before period when the span begins at it", () => {
  const coverage = [row("2026-09-01", { n: 10, p50: 900 })];
  const days = daysFrom("2026-09-01", 10);
  const [entry] = C.beforeAfter(coverage, [{ at: "2026-09-01", label: "install" }], days);
  assert.equal(entry.rows[0].before, null);
});

test("displayDelta is the change between the two numbers as the table prints them", () => {
  assert.equal(C.displayDelta(75.5, 75.4, 0), -1);          // "76% → 75%": a fall, never "+0%"
  assert.equal(C.displayDelta(76.4, 75.5, 0), 0);           // "76% → 76%": no change shown, so none reported
  assert.equal(C.displayDelta(800.2, 812.7, 0), 13);
  assert.ok(Math.abs(C.displayDelta(0.24, 0.26, 1) - 0.1) < 1e-9);    // rounded at the digits the cell shows
  assert.equal(C.displayDelta(null, 75, 0), null);
  assert.equal(C.displayDelta(75, undefined, 0), null);
  assert.equal(C.displayDelta(75, NaN, 0), null);
});

// cellText's `col`, in the shape of coverage.js's TABLE_COLUMNS: a getter over an aggregate() result (or null), plus
// the digits and unit fmt/fmtDelta format it with.
const FT_COL = { value: (agg) => agg?.lowestP50 ?? null, digits: 0, unit: " ft" };
const PCT_COL = { value: (agg) => (agg?.gate1000Frac != null ? agg.gate1000Frac * 100 : null), digits: 0, unit: "%" };
const N_COL = { value: (agg) => agg?.n ?? null, digits: 0, unit: "" };

test("cellText prints both sides and a parenthesised delta for a normal cell", () => {
  assert.equal(C.cellText({ lowestP50: 800 }, { lowestP50: 700 }, FT_COL), "800 ft → 700 ft (−100 ft)");
});

test("cellText's delta is null, and left out, when the column's own value is missing on one side", () => {
  // Both periods flew (neither `before` nor `after` is null) but this thin `after` has no quartile of its own —
  // aggregate()'s own kind of gap, distinct from a wholly missing period.
  assert.equal(C.cellText({ lowestP50: 800 }, { lowestP50: null }, FT_COL), "800 ft → —");
});

test("cellText shows an em dash and no delta for a missing before", () => {
  assert.equal(C.cellText(null, { n: 42 }, N_COL), "— → 42");
});

test("cellText shows an em dash and no delta for a missing after", () => {
  assert.equal(C.cellText({ n: 42 }, null, N_COL), "42 → —");
});

test("cellText shows an em dash on both sides when before and after are both missing", () => {
  assert.equal(C.cellText(null, null, N_COL), "— → —");
});

test("cellText rounds each side before subtracting, so 76% -> 75% never reads (+0%)", () => {
  assert.equal(C.cellText({ gate1000Frac: 0.755 }, { gate1000Frac: 0.754 }, PCT_COL), "76% → 75% (−1%)");
});

test("outcomeSeries stacks each airport's outcomes in a fixed order, with zero for a quiet day", () => {
  const days = ["2026-09-10", "2026-09-11"];
  const outcomes = [
    { airport: "KSFO", day: "2026-09-10", outcome: "landed", n: 5 },
    { airport: "KSFO", day: "2026-09-10", outcome: "go_around", n: 1 },
    { airport: "KOAK", day: "2026-09-10", outcome: "landed", n: 20 },
  ];
  const series = C.outcomeSeries(outcomes, days);
  assert.deepEqual(series.map((s) => s.airport), ["KOAK", "KSFO"]);      // busiest first
  assert.deepEqual(series.map((s) => s.total), [20, 6]);
  const ksfo = series[1];
  assert.deepEqual(ksfo.series.map((s) => s.outcome), C.OUTCOMES);       // the fixed order, even for outcomes absent here
  assert.deepEqual(ksfo.series.find((s) => s.outcome === "landed").values, [5, 0]);
  assert.deepEqual(ksfo.series.find((s) => s.outcome === "go_around").values, [1, 0]);
  assert.deepEqual(ksfo.series.find((s) => s.outcome === "lost").values, [0, 0]);   // no rows at all: still zero
});

test("visibleSections shows the runway charts only when coverage has rows", () => {
  const days = daysFrom("2026-09-01", 5);
  assert.equal(C.visibleSections({ coverage: [], outcomes: [], marks: [] }, days).coverage, false);
  assert.equal(C.visibleSections({ coverage: [row("2026-09-01")], outcomes: [], marks: [] }, days).coverage, true);
});

test("visibleSections shows the outcome charts from outcomes alone, even with no coverage", () => {
  const days = daysFrom("2026-09-01", 5);
  const body = { coverage: [], marks: [],
    outcomes: [{ airport: "KSFO", day: "2026-09-01", outcome: "landed", n: 73 }] };
  assert.deepEqual(C.visibleSections(body, days), { coverage: false, marks: false, outcomes: true });
});

test("visibleSections hides Before and after when there are no marks in the span, shows it once one lands", () => {
  const days = daysFrom("2026-09-01", 5);
  const cov = [row("2026-09-01")];
  assert.equal(C.visibleSections({ coverage: cov, outcomes: [], marks: [] }, days).marks, false);
  const withMark = { coverage: cov, outcomes: [], marks: [{ at: "2026-09-02", label: "antenna" }] };
  assert.equal(C.visibleSections(withMark, days).marks, true);
});

test("visibleSections hides Before and after when a mark has no coverage to compare across it", () => {
  const days = daysFrom("2026-09-01", 5);            // the state every deploy is in until the next rebuild:
  const body = { coverage: [], marks: [{ at: "2026-09-02", label: "antenna" }],   // marks configured, 0005 has
    outcomes: [{ airport: "KSFO", day: "2026-09-01", outcome: "landed", n: 73 }] };  // emptied coverage_daily
  assert.deepEqual(C.visibleSections(body, days), { coverage: false, marks: false, outcomes: true });
});

test("normsText names the keys and their age, and says when there are none", () => {
  const keys = { type: 14, class: 10, "*": 7 };
  const nowS = new Date(2026, 8, 13, 12, 0, 0).getTime() / 1000;         // 2026-09-13, noon local
  assert.equal(C.normsText({ built_at: null, keys }, nowS), "no norms yet");
  assert.equal(C.normsText({ built_at: nowS - 60, keys: { type: 0, class: 0, "*": 0 } }, nowS), "no norms yet");
  assert.equal(C.normsText({ built_at: nowS - 10, keys }, nowS),
    "norms: 14 type, 10 class, 7 all-aircraft keys, built just now");
  assert.equal(C.normsText({ built_at: nowS - 720, keys }, nowS),
    "norms: 14 type, 10 class, 7 all-aircraft keys, built 12 min ago");
  assert.equal(C.normsText({ built_at: nowS - 3 * 3600, keys }, nowS),
    "norms: 14 type, 10 class, 7 all-aircraft keys, built 3 h ago");
  const yesterday = new Date(2026, 8, 12, 9, 0, 0).getTime() / 1000;     // a different local day: falls back to a date
  assert.equal(C.normsText({ built_at: yesterday, keys }, nowS),
    "norms: 14 type, 10 class, 7 all-aircraft keys, built Sep 12");
});
