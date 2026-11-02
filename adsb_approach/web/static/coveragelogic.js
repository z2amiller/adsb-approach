// The coverage page's logic without its DOM: the span control, the day axis, grouping the
// coverage rows by runway, the quiet-runway floor, aligning a runway's rows with the day axis, the marks drawn on
// every chart, the before/after periods either side of each mark and their n-weighted aggregate, the outcome charts'
// stacked series, and the norms note's wording. Every function here is pure: no DOM, no fetch, no clock except the
// `nowS` a caller passes in. coverage.js reads these and draws them with uPlot; it owns everything that touches the
// page itself.
import { fmt, fmtDelta } from "./logic.js";

export const SPANS = [7, 30, 90, 365];          // the span control's choices, in days; `all` is the fifth
export const DEFAULT_DAYS = 30;                 // the page opens on the last 30 days
export const TRAFFIC_FLOOR = 25;                // approaches in the span, under which a runway folds away
export const OUTCOMES = ["landed", "lost_on_final", "lost", "go_around", "touch_and_go", "runway_change", "abandoned"];

// --- Local day arithmetic -------------------------------------------------------------------------------------
// A coverage day is a "YYYY-MM-DD" string, already cut in the receiver's own local zone by the server. Every
// function below treats it as an opaque calendar date and steps it with Date.UTC, never by parsing it as local
// time in the viewer's own zone, which would shift the date under DST or a different offset entirely.

function parseDayUTC(day) {
  const [y, m, d] = day.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

function formatDayUTC(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function addDays(day, n) {
  return formatDayUTC(parseDayUTC(day) + n * 86400000);
}

/** Whole days from `a` to `b` (b - a), both "YYYY-MM-DD". */
function dayDiff(a, b) {
  return Math.round((parseDayUTC(b) - parseDayUTC(a)) / 86400000);
}

// --- The span control ------------------------------------------------------------------------------------------

/** The span from a `location.search`-style query string: `{days: N}` for `?days=N` when N is one of SPANS,
 * `{days: null}` for `?days=all`, and `{days: DEFAULT_DAYS}` for anything absent or unrecognised. */
export function parseSpan(search) {
  const raw = new URLSearchParams(search).get("days");
  if (raw === "all") return { days: null };
  const n = raw === null ? NaN : Number(raw);
  return { days: SPANS.includes(n) ? n : DEFAULT_DAYS };
}

/** The query string for a span: `""` for the default (30), `"?days=all"` for every day stored, else `"?days=N"`.
 * Round-trips with parseSpan, so the URL a viewer shares reopens on the same span. */
export function spanSearch(days) {
  if (days === DEFAULT_DAYS) return "";
  return days === null ? "?days=all" : `?days=${days}`;
}

// --- The day axis ------------------------------------------------------------------------------------------------

/** Every day of the span, `body.since_day` to `body.until_day` inclusive, one day per step. `since_day` is null for
 * `days=all`: the axis then starts at the earliest day `body.coverage` or `body.outcomes` carries, or is just
 * `[body.until_day]` when neither holds a row. */
export function spanDays(body) {
  let since = body.since_day;
  if (since === null || since === undefined) {
    const seen = [...body.coverage.map((r) => r.day), ...body.outcomes.map((r) => r.day)];
    since = seen.length ? seen.reduce((a, b) => (a < b ? a : b)) : body.until_day;
  }
  const days = [];
  for (let day = since; day <= body.until_day; day = addDays(day, 1)) days.push(day);
  return days;
}

// --- Grouping by runway -----------------------------------------------------------------------------------------

/** The coverage rows grouped by runway, busiest first (ties broken by AIRPORT/RUNWAY): `[{key, airport, runway,
 * total, rows}]`, `key` the "AIRPORT/RUNWAY" the rest of the page keys a runway by, `total` its approaches over the
 * whole span (every row passed in, not just one day) and `rows` its own coverage_daily rows, untouched. */
export function byRunway(coverage) {
  const groups = new Map();
  for (const row of coverage) {
    const key = `${row.airport}/${row.runway}`;
    let g = groups.get(key);
    if (!g) {
      g = { key, airport: row.airport, runway: row.runway, total: 0, rows: [] };
      groups.set(key, g);
    }
    g.total += row.n;
    g.rows.push(row);
  }
  return [...groups.values()].sort((a, b) => b.total - a.total || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

/** byRunway's groups split at TRAFFIC_FLOOR: `{busy, quiet}`, each keeping the busiest-first order it arrived in. A
 * runway with fewer than `floor` approaches over the span is quiet: its daily medians rest on too few approaches to
 * read, so the page folds it behind #show-quiet. */
export function splitQuiet(groups, floor) {
  return { busy: groups.filter((g) => g.total >= floor), quiet: groups.filter((g) => g.total < floor) };
}

/** One runway group's rows aligned with the day axis: `{lowest, counts, backfill}`, each as long as `days`. A day
 * with no coverage_daily row is a gap (null) in all three; a day with a row but no quartile (too few approaches)
 * is a gap only in `lowest`, since `counts` still knows `n` and `backfill` is still an exact share of it. */
export function runwaySeries(group, days) {
  const byDay = new Map(group.rows.map((r) => [r.day, r]));
  const lowest = [], counts = [], backfill = [];
  for (const day of days) {
    const row = byDay.get(day);
    lowest.push(row ? (row.lowest_hat_p50 ?? null) : null);
    counts.push(row ? row.n : null);
    backfill.push(row ? (row.backfill_frac ?? null) : null);
  }
  return { lowest, counts, backfill };
}

// --- Marks -----------------------------------------------------------------------------------------------------

/** The configured marks positioned on the day axis: `[{at, label, x}]`, `x` the mark's day as a (possibly
 * fractional) index into `days` — an uPlot chart's own x scale, since the chart's x is the day's index, never a
 * timestamp. A mark whose day is missing from `days` (the axis skipped it) sits between the two days that bracket
 * it, in proportion to how many calendar days separate them. A mark outside `[days[0], days[days.length-1]]` is
 * dropped: it draws on no chart of this span. */
export function markPositions(marks, days) {
  if (days.length === 0) return [];
  const first = days[0], last = days[days.length - 1];
  const out = [];
  for (const mark of marks) {
    if (mark.at < first || mark.at > last) continue;
    const idx = days.indexOf(mark.at);
    if (idx !== -1) {
      out.push({ at: mark.at, label: mark.label, x: idx });
      continue;
    }
    let hi = 0;
    while (hi < days.length && days[hi] < mark.at) hi += 1;
    const lo = hi - 1;
    const x = lo + dayDiff(days[lo], mark.at) / dayDiff(days[lo], days[hi]);
    out.push({ at: mark.at, label: mark.label, x });
  }
  return out;
}

// --- Mark label rows (without them every label drew at the same y, so two marks a few days apart overlapped) ----

/** A row (0-based) for each mark's label, so labels that would collide stack instead of overlapping: one entry per
 * position/width pair, aligned with `positions`/`widths` in the order they arrived in — not the x order this walks
 * internally. A label's extent is `[x, x + width]`, pulled left to end at `plotWidth` when it would run past the
 * plot's right edge, so the rightmost mark's label still reads inside the chart; that pull is applied before the
 * row search, so it can also change which labels are judged to collide.
 *
 * Marks are walked left to right by x. Each is placed on the lowest row whose last label so far ends at or before
 * this one's left edge — a row with nothing on it yet always qualifies. A label that collides with every row
 * already open, with `maxRows` reached, falls back to the last row rather than disappearing or growing the chart
 * without bound; a second collision there is accepted, not resolved further. */
export function labelRows(positions, widths, plotWidth, maxRows) {
  const order = positions.map((_, i) => i).sort((a, b) => positions[a] - positions[b]);
  const rowEnds = [];        // the right edge of the last label placed on each row so far, in walk order
  const rows = new Array(positions.length);
  for (const i of order) {
    const width = widths[i];
    const left = Math.min(positions[i], plotWidth - width);
    const right = left + width;
    let row = rowEnds.findIndex((end) => end <= left);
    if (row === -1) row = rowEnds.length < maxRows ? rowEnds.length : maxRows - 1;
    rowEnds[row] = right;
    rows[i] = row;
  }
  return rows;
}

/** The before/after day ranges either side of each mark inside the span, oldest mark first: `[{at, label, before,
 * after}]`, each of `before`/`after` a `[firstDay, lastDay]` pair or null. `before` runs from the previous mark's
 * own day (or the span's first day, for the earliest mark) to the day before this mark; `after` runs from this
 * mark's own day to the day before the next mark (or the span's last day, for the latest). A mark that falls on the
 * span's first day has nothing before it in this span, so its `before` is null. */
export function periods(marks, days) {
  if (days.length === 0) return [];
  const first = days[0], last = days[days.length - 1];
  const inSpan = marks.filter((m) => m.at >= first && m.at <= last)
    .slice().sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
  return inSpan.map((mark, i) => {
    const before = mark.at === first ? null : [i === 0 ? first : inSpan[i - 1].at, addDays(mark.at, -1)];
    const after = [mark.at, i === inSpan.length - 1 ? last : addDays(inSpan[i + 1].at, -1)];
    return { at: mark.at, label: mark.label, before, after };
  });
}

// --- Aggregation -------------------------------------------------------------------------------------------------

/** The n-weighted aggregate of a period's coverage rows (the one thing on this page worth arguing about): `{n,
 * backfillShare, gate1000Frac, gate500Frac, lowestP50, closestP50}`, or null for no rows at all.
 *
 * `n` is the period's total approaches (Σ row.n) and `backfillShare` is Σ row.backfill_n / n: both exact, a
 * straight count over a count. `gate1000Frac` and `gate500Frac` are Σ(frac × n) / Σn over the days that have a
 * fraction: also exact, since each day's fraction is already a count over that day's own n, so the weighted mean is
 * the period's own count over its own approaches.
 *
 * `lowestP50` and `closestP50` are Σ(p50 × n) / Σn over the days that have a median: an *estimate*, since a
 * period's true median cannot be recovered from daily medians. It is used anyway because it gives a busy day its
 * weight, where the median of the daily medians would treat a slow day and a busy one alike. A day whose value is
 * null (too thin for a quartile) is left out of that column's own weight, though it still counts in `n`; a column
 * with no value on any day is null. */
export function aggregate(rows) {
  if (rows.length === 0) return null;
  let n = 0, backfillN = 0;
  const gate1000 = { sum: 0, weight: 0 }, gate500 = { sum: 0, weight: 0 };
  const lowest = { sum: 0, weight: 0 }, closest = { sum: 0, weight: 0 };
  const add = (acc, value, weight) => {
    if (typeof value === "number") {
      acc.sum += value * weight;
      acc.weight += weight;
    }
  };
  for (const r of rows) {
    n += r.n;
    backfillN += r.backfill_n;
    add(gate1000, r.gate1000_observed_frac, r.n);
    add(gate500, r.gate500_observed_frac, r.n);
    add(lowest, r.lowest_hat_p50, r.n);
    add(closest, r.closest_nm_p50, r.n);
  }
  const mean = (acc) => (acc.weight > 0 ? acc.sum / acc.weight : null);
  return {
    n, backfillShare: n > 0 ? backfillN / n : null,
    gate1000Frac: mean(gate1000), gate500Frac: mean(gate500),
    lowestP50: mean(lowest), closestP50: mean(closest),
  };
}

/** The before/after tables: one entry per mark inside the span, oldest
 * first, `[{at, label, rows: [{key, airport, runway, before, after}]}]`. `rows` covers every runway of the whole
 * span, busiest first (coverage.js decides which of them a viewer sees, via #show-quiet); `before` and `after` are
 * each that runway's aggregate() over its rows in the mark's before/after range, or null when the range itself is
 * null (nothing before the first mark) or held no rows for this runway. There is no aggregate Δ here (one
 * used to sit on `lowestP50` alone, and nothing read it) — each cell's own delta is cellText's job below, at
 * that column's own digits, so it can never disagree with the two numbers it sits beside. */
export function beforeAfter(coverage, marks, days) {
  const groups = byRunway(coverage);
  const inRange = (rows, range) => (range === null ? [] : rows.filter((r) => r.day >= range[0] && r.day <= range[1]));
  return periods(marks, days).map(({ at, label, before, after }) => ({
    at,
    label,
    rows: groups.map((g) => {
      const beforeAgg = before === null ? null : aggregate(inRange(g.rows, before));
      const afterAgg = after === null ? null : aggregate(inRange(g.rows, after));
      return { key: g.key, airport: g.airport, runway: g.runway, before: beforeAgg, after: afterAgg };
    }),
  }));
}

/** The change from `before` to `after` as the table prints the two of them: each side is rounded to the `digits`
 * its own cell shows before they are subtracted, so the Δ can never contradict the numbers beside it. Computed
 * from the raw values instead, a 0.1-point fall reads "+0%" next to "76% → 75%" — and the 1,000 ft gate column is
 * the one a mark is judged by. null when either side is missing. */
export function displayDelta(before, after, digits) {
  if (typeof before !== "number" || typeof after !== "number"
      || !Number.isFinite(before) || !Number.isFinite(after)) return null;
  return Number(after.toFixed(digits)) - Number(before.toFixed(digits));
}

/** One before/after table cell's text (coverage.js's markRow, moved here so it can be tested without a DOM):
 * `"800 ft → 700 ft (−100 ft)"`, the two values `col.value` reads off `before`/`after` (each a beforeAfter()
 * aggregate or null; `col` one of coverage.js's TABLE_COLUMNS), formatted by `fmt` at the column's own digits and
 * unit. The parenthesised change is displayDelta's own, rounded on both sides before subtracting so it can never
 * disagree with the two numbers beside it, and is left out entirely — no empty parens — when displayDelta is null:
 * either side missing, or this column has nothing for it on one side (aggregate()'s own gaps: a thin period's
 * lowestP50, say, with n and the rest still present). */
export function cellText(before, after, col) {
  const b = col.value(before), a = col.value(after);
  const bText = fmt(b, col.digits, col.unit);
  const aText = fmt(a, col.digits, col.unit);
  const change = displayDelta(b, a, col.digits);        // of the two numbers as printed, so the three agree
  const delta = change === null ? "" : ` (${fmtDelta(change, col.unit, col.digits)})`;
  return `${bText} → ${aText}${delta}`;
}

// --- Outcome charts ----------------------------------------------------------------------------------------------

/** The daily outcome counts stacked per airport, busiest first: `[{airport, total, series: [{outcome, values}]}]`,
 * `series` always in OUTCOMES order with one `values` entry per day of `days`, zero for a day with no such outcome
 * (never a gap: a stacked bar chart has nothing to leave a hole for). `total` is the airport's approaches over the
 * whole span, every outcome included. */
export function outcomeSeries(outcomes, days) {
  const airports = new Map();
  for (const row of outcomes) {
    let a = airports.get(row.airport);
    if (!a) {
      a = { total: 0, counts: new Map() };
      airports.set(row.airport, a);
    }
    a.total += row.n;
    const key = `${row.day} ${row.outcome}`;
    a.counts.set(key, (a.counts.get(key) ?? 0) + row.n);
  }
  return [...airports.entries()]
    .sort((a, b) => b[1].total - a[1].total || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([airport, a]) => ({
      airport,
      total: a.total,
      series: OUTCOMES.map((outcome) => ({
        outcome,
        values: days.map((day) => a.counts.get(`${day} ${outcome}`) ?? 0),
      })),
    }));
}

// --- What the page shows (two empty-state gaps) ---------------------------------------------------

/** Which of the coverage page's three sections have anything to draw, from one GET /api/stats body and the span's
 * day axis (spanDays' own return): `{coverage, marks, outcomes}`. Each is its own read of the body, not derived
 * from the others — the bug this replaced hid the outcome charts and the empty-coverage message together whenever
 * `coverage` was empty, even on a freshly migrated database that already had outcome rows. `coverage` and
 * `outcomes` are just whether those rows exist at all; `marks` needs both a mark inside this span (periods()) and
 * coverage rows to compare across it, since a mark's table is built from byRunway(coverage) and has no row without
 * them. Either half alone leaves a bare heading over a header-only table: with no mark configured, which is the
 * shipped default, and with marks configured on a freshly migrated database, which is every deploy until the next
 * rebuild. */
export function visibleSections(body, days) {
  return {
    coverage: body.coverage.length > 0,
    marks: body.coverage.length > 0 && periods(body.marks, days).length > 0,
    outcomes: body.outcomes.length > 0,
  };
}

// --- The norms note ------------------------------------------------------------------------------------------

/** `built <age>`'s wording: "just now" under a minute, "N min ago" under an hour, "N h ago" while `builtAt` falls on
 * the same local calendar day as `nowS`, and otherwise the date it was built — the same same-day/not-today split
 * logic.js's own clockText uses for a health timestamp, since an age in hours stops being a useful readout once it
 * would have to count past midnight. */
function ageText(builtAt, nowS) {
  const diffS = Math.max(0, nowS - builtAt);
  if (diffS < 60) return "just now";
  const minutes = Math.round(diffS / 60);
  if (minutes < 60) return `${fmt(minutes, 0)} min ago`;
  const built = new Date(builtAt * 1000), now = new Date(nowS * 1000);
  const sameDay = built.getFullYear() === now.getFullYear() && built.getMonth() === now.getMonth()
    && built.getDate() === now.getDate();
  if (sameDay) return `${fmt(Math.round(diffS / 3600), 0)} h ago`;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[built.getMonth()]} ${built.getDate()}`;
}

/** The stats note's norms clause: "norms: 14 type, 10 class, 7 all-aircraft keys, built 12 min ago", or "no norms
 * yet" when nothing has been built (`built_at` null) or every kind's count is zero (an empty database's norms,
 * which still carries a `built_at` of null, and so already reads "no norms yet" by that rule alone). */
export function normsText(norms, nowS) {
  const keys = norms.keys;
  if (norms.built_at === null || (keys.type === 0 && keys.class === 0 && keys["*"] === 0)) return "no norms yet";
  return `norms: ${fmt(keys.type, 0)} type, ${fmt(keys.class, 0)} class, ${fmt(keys["*"], 0)} all-aircraft keys, `
    + `built ${ageText(norms.built_at, nowS)}`;
}
