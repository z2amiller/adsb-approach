// Wires the coverage page together: one fetch of GET /api/stats per span, drawn with uPlot into a
// small-multiples chart per runway, a stacked chart per airport and a before/after table per mark. The page opens no
// event stream and reloads nothing on its own — it shows what the last fleet rebuild wrote — so the only
// triggers for a new fetch are the initial load, a span change and pressing Retry. Built with createElement/textContent
// only: runway, airport and mark-label strings come from the database, never through innerHTML.
import { fmt } from "./logic.js";
import { TRAFFIC_FLOOR, beforeAfter, byRunway, cellText, labelRows, markPositions, normsText, outcomeSeries, parseSpan, periods, runwaySeries, spanDays, spanSearch, splitQuiet, visibleSections } from "./coveragelogic.js";

const CHART_HEIGHT_PX = 220;
const MARK_LABEL_ROW_HEIGHT_PX = 13;    // 11px font plus a little leading, so stacked rows don't touch
const MARK_LABEL_MAX_ROWS = 4;          // beyond this, colliding labels share the last row rather than overflow
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const OUTCOME_LABELS = { landed: "Landed", lost_on_final: "Lost on final", lost: "Lost", go_around: "Go-around",
  touch_and_go: "Touch-and-go", runway_change: "Runway change", abandoned: "Abandoned" };
const OUTCOME_TOKENS = { landed: "--outcome-landed", lost_on_final: "--outcome-lost-on-final", lost: "--outcome-lost",
  go_around: "--outcome-go-around", touch_and_go: "--outcome-touch-and-go", runway_change: "--outcome-runway-change",
  abandoned: "--outcome-abandoned" };

// The before/after table's columns beyond "Runway", each a getter of one aggregate() field (already the percentage
// itself for a share, so fmt/fmtDelta's own unit does the rest) plus the digits and unit fmt/fmtDelta format it with.
const TABLE_COLUMNS = [
  { label: "Approaches", value: (agg) => agg?.n ?? null, digits: 0, unit: "" },
  { label: "Median lowest", value: (agg) => agg?.lowestP50 ?? null, digits: 0, unit: " ft" },
  { label: "1,000 ft gate", value: (agg) => (agg?.gate1000Frac != null ? agg.gate1000Frac * 100 : null), digits: 0, unit: "%" },
  { label: "500 ft gate", value: (agg) => (agg?.gate500Frac != null ? agg.gate500Frac * 100 : null), digits: 0, unit: "%" },
  { label: "adsb.lol", value: (agg) => (agg?.backfillShare != null ? agg.backfillShare * 100 : null), digits: 0, unit: "%" },
];

const $ = (id) => document.getElementById(id);
const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const statsNoteEl = $("stats-note");
const spanEl = $("span");
const showQuietEl = $("show-quiet");
const chartsEl = $("coverage-charts");
const quietNoteEl = $("quiet-note");
const marksEl = $("marks");
const outcomeChartsEl = $("outcome-charts");
const emptyEl = $("stats-empty");
const errorEl = $("stats-error");
const errorMessageEl = $("stats-error-message");
const retryEl = $("stats-retry");

let days = parseSpan(location.search).days;
let showQuiet = false;
let lastBody = null;
let loadToken = 0;
let charts = [];               // {chart, el}: every uPlot instance currently on the page, for resize and teardown

// --- Day formatting (DOM-only: coveragelogic.js's own contract stops at data, not display text) -------------------

function dayParts(day) {
  const [y, m, d] = day.split("-").map(Number);
  return { y, m, d };
}

/** "2026-09-12" -> "12 Sep": the day-axis tick and cursor-readout style used throughout this page. */
function dayLabel(day) {
  const { m, d } = dayParts(day);
  return `${d} ${MONTHS[m - 1]}`;
}

/** "2026-10-04" -> "4 Oct 2026": a mark's own heading carries the year, since a mark can be much older than the
 * span it is being compared over. */
function markDateLabel(day) {
  const { y, m, d } = dayParts(day);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

function daySpanCount(first, last) {
  const a = dayParts(first), b = dayParts(last);
  return Math.round((Date.UTC(b.y, b.m - 1, b.d) - Date.UTC(a.y, a.m - 1, a.d)) / 86400000) + 1;
}

/** "1 Sep – 3 Oct (33 days)" for a [firstDay, lastDay] range. */
function periodLabel(range) {
  const [first, last] = range;
  const n = daySpanCount(first, last);
  return `${dayLabel(first)} – ${dayLabel(last)} (${n} day${n === 1 ? "" : "s"})`;
}

// --- Loading ---------------------------------------------------------------------------------------------------

async function fetchJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  const body = await r.json().catch(() => ({}));
  if (r.status !== 200) throw new Error(body.detail || String(r.status));
  return body;
}

async function load() {
  const token = ++loadToken;
  spanEl.value = days === null ? "all" : String(days);
  history.replaceState(null, "", spanSearch(days) || location.pathname);
  errorEl.hidden = true;
  let body;
  try {
    body = await fetchJson(`api/stats${spanSearch(days)}`);
  } catch (err) {
    if (token !== loadToken) return;               // a later load (span change, retry) has already moved on
    errorMessageEl.textContent = err.message;
    errorEl.hidden = false;
    return;
  }
  if (token !== loadToken) return;
  lastBody = body;
  render(body);
}

spanEl.addEventListener("change", () => {
  days = spanEl.value === "all" ? null : Number(spanEl.value);
  load();
});
showQuietEl.addEventListener("change", () => {
  showQuiet = showQuietEl.checked;
  if (lastBody) render(lastBody);                   // no new fetch: the toggle only changes which rows are shown
});
retryEl.addEventListener("click", () => load());

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
darkQuery.addEventListener("change", () => {
  if (lastBody) render(lastBody);                   // rebuild so every chart reads its stroke/fill colours again
});

new ResizeObserver(() => {
  for (const { chart, el } of charts) chart.setSize({ width: el.clientWidth, height: CHART_HEIGHT_PX });
}).observe(document.querySelector("main"));

// --- Rendering ---------------------------------------------------------------------------------------------------

function noteText(body) {
  const zone = body.timezone ? `Local days (${body.timezone})` : "Local days (this receiver's own zone)";
  return `${zone} · ${normsText(body.norms, Date.now() / 1000)}`;
}

function destroyCharts() {
  for (const { chart } of charts) chart.destroy();
  charts = [];
}

/** The Runways, Before/after and How-approaches-ended sections each show or hide on their own signal
 * (visibleSections in coveragelogic.js), not one shared "empty" flag — an empty coverage_daily used to hide the
 * outcome charts too, even with outcome rows to draw, and "Before and after" showed a bare heading whenever no mark
 * was configured, the shipped default. groups/quietKeys/marksPositions are computed unconditionally: they are cheap
 * on an empty `coverage`, and renderMarks wants quietKeys regardless of whether the Runways section itself draws. */
function render(body) {
  statsNoteEl.textContent = noteText(body);
  destroyCharts();

  const spanDaysList = spanDays(body);
  const sections = visibleSections(body, spanDaysList);
  const groups = byRunway(body.coverage);
  const { busy, quiet } = splitQuiet(groups, TRAFFIC_FLOOR);
  const quietKeys = new Set(quiet.map((g) => g.key));
  const shown = showQuiet ? groups : busy;
  const marksPositions = markPositions(body.marks, spanDaysList);

  emptyEl.hidden = sections.coverage;
  chartsEl.hidden = !sections.coverage;
  quietNoteEl.hidden = !sections.coverage || quiet.length === 0 || showQuiet;
  quietNoteEl.textContent = quiet.length ? `${quiet.length} quiet runway${quiet.length === 1 ? "" : "s"} hidden` : "";
  if (sections.coverage) renderCoverageCharts(shown, spanDaysList, marksPositions);
  else chartsEl.replaceChildren();

  const marksSection = marksEl.closest("section");
  if (marksSection) marksSection.hidden = !sections.marks;
  if (sections.marks) renderMarks(body.coverage, body.marks, spanDaysList, quietKeys);
  else marksEl.replaceChildren();

  const outcomeSection = outcomeChartsEl.closest("section");
  if (outcomeSection) outcomeSection.hidden = !sections.outcomes;
  if (sections.outcomes) renderOutcomeCharts(body.outcomes, spanDaysList);
  else outcomeChartsEl.replaceChildren();
}

// --- The runway charts -------------------------------------------------------------------------------------------

function renderCoverageCharts(groups, spanDaysList, marksPositions) {
  chartsEl.replaceChildren();
  for (const group of groups) {
    const figure = document.createElement("figure");
    const figcaption = document.createElement("figcaption");
    figcaption.textContent = `${group.airport} ${group.runway} · ${group.total} approach${group.total === 1 ? "" : "es"}`;
    const chartEl = document.createElement("div");
    chartEl.className = "chart";
    const readoutEl = document.createElement("p");
    readoutEl.className = "chart-readout";
    figure.append(figcaption, chartEl, readoutEl);
    chartsEl.append(figure);
    const series = runwaySeries(group, spanDaysList);
    charts.push({ chart: buildRunwayChart(chartEl, readoutEl, spanDaysList, series, marksPositions), el: chartEl });
  }
}

/** ~8 evenly spaced day labels regardless of the span's length, so a year-long axis doesn't try to letter 365 ticks. */
function thinDayTicks(splits, spanDaysList) {
  const step = Math.max(1, Math.round(spanDaysList.length / 8));
  return splits.map((v) => {
    const idx = Math.round(v);
    if (idx < 0 || idx >= spanDaysList.length || Math.abs(v - idx) > 1e-9) return "";
    return idx % step === 0 ? dayLabel(spanDaysList[idx]) : "";
  });
}

/** A draw hook: a dashed vertical line at each mark's x, in `muted`, with its label near the top of the plot,
 * clipped to the plot area so a mark near either edge never overwrites the axes. Two marks close enough that their
 * labels would collide stack onto separate rows instead (labelRows in coveragelogic.js): each label is
 * measured with the canvas's own `measureText`, so the row assignment matches the font actually drawn with. */
function drawMarksHook(marksPositions, muted) {
  return (u) => {
    if (marksPositions.length === 0) return;
    const { ctx } = u;
    ctx.save();
    ctx.beginPath();
    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
    ctx.clip();
    ctx.strokeStyle = muted;
    ctx.fillStyle = muted;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "top";
    const lefts = marksPositions.map((mark) => u.valToPos(mark.x, "x", true) - u.bbox.left + 3);
    const widths = marksPositions.map((mark) => ctx.measureText(mark.label).width);
    const rows = labelRows(lefts, widths, u.bbox.width, MARK_LABEL_MAX_ROWS);
    marksPositions.forEach((mark, i) => {
      const cx = u.valToPos(mark.x, "x", true);
      ctx.beginPath();
      ctx.moveTo(cx, u.bbox.top);
      ctx.lineTo(cx, u.bbox.top + u.bbox.height);
      ctx.stroke();
      const left = Math.min(lefts[i], u.bbox.width - widths[i]);
      ctx.fillText(mark.label, u.bbox.left + left, u.bbox.top + 2 + rows[i] * MARK_LABEL_ROW_HEIGHT_PX);
    });
    ctx.restore();
  };
}

/** A setCursor hook: "12 Sep · 880 ft · 266 approaches · 95% adsb.lol" in `readoutEl`, replacing uPlot's own legend
 * (disabled below) with the one combined line. */
function updateReadout(readoutEl, spanDaysList, series) {
  return (u) => {
    const idx = u.cursor.idx;
    if (idx == null) {
      readoutEl.textContent = "";
      return;
    }
    const n = series.counts[idx];
    const nText = n == null ? "—" : `${n} approach${n === 1 ? "" : "es"}`;
    const bf = series.backfill[idx];
    const bfText = bf == null ? "—" : `${Math.round(bf * 100)}% adsb.lol`;
    readoutEl.textContent = `${dayLabel(spanDaysList[idx])} · ${fmt(series.lowest[idx], 0, " ft")} · ${nText} · ${bfText}`;
  };
}

function buildRunwayChart(el, readoutEl, spanDaysList, series, marksPositions) {
  const xs = spanDaysList.map((_, i) => i);
  const backfillPct = series.backfill.map((v) => (v == null ? null : v * 100));
  const fg = color("--fg"), grid = color("--grid"), muted = color("--muted");
  const opts = {
    width: el.clientWidth,
    height: CHART_HEIGHT_PX,
    scales: {
      x: { time: false },
      y: {},
      pct: { range: [0, 100] },
      // The count bars' own scale, ranged so they sit low and unobtrusive behind the two lines rather than filling
      // the chart: 4x headroom over the busiest day in view.
      n: { range: (u, min, max) => [0, (max || 1) * 4] },
    },
    axes: [
      { stroke: fg, grid: { stroke: grid }, values: (u, splits) => thinDayTicks(splits, spanDaysList) },
      { scale: "y", label: "ft", stroke: fg, grid: { stroke: grid } },
      { scale: "pct", side: 1, label: "%", stroke: fg, grid: { show: false },
        values: (u, splits) => splits.map((v) => (v == null ? "" : `${Math.round(v)}%`)) },
    ],
    series: [
      {},
      // The day's approaches, behind both lines: added first so the lines below draw over them.
      { scale: "n", paths: window.uPlot.paths.bars({ size: [0.7, 20], align: 0 }), fill: color("--series-count"),
        stroke: "transparent", width: 0, points: { show: false }, label: "Approaches" },
      { scale: "y", stroke: color("--series-track"), label: "Median lowest", value: (u, v) => fmt(v, 0, " ft") },
      { scale: "pct", stroke: color("--series-2"), dash: [6, 4], label: "adsb.lol",
        value: (u, v) => (v == null ? "—" : `${Math.round(v)}%`) },
    ],
    legend: { show: false },                          // replaced by the combined readout line below the chart
    cursor: { drag: { x: false, y: false } },
    hooks: { draw: [drawMarksHook(marksPositions, muted)], setCursor: [updateReadout(readoutEl, spanDaysList, series)] },
  };
  return new window.uPlot(opts, [xs, series.counts, series.lowest, backfillPct], el);
}

// --- The before/after tables --------------------------------------------------------------------------------------
// cellText lives in coveragelogic.js now: it was already pure, just untestable here without a DOM.

function markRow(row) {
  const tr = document.createElement("tr");
  const runwayTd = document.createElement("td");
  runwayTd.textContent = `${row.airport} ${row.runway}`;
  tr.append(runwayTd);
  for (const col of TABLE_COLUMNS) {
    const td = document.createElement("td");
    td.textContent = cellText(row.before, row.after, col);
    tr.append(td);
  }
  return tr;
}

function periodSummary(range) {
  const before = range.before === null ? "nothing before this mark in this span" : periodLabel(range.before);
  return `${before} against ${periodLabel(range.after)}`;
}

function markSection(entry, range, quietKeys) {
  const section = document.createElement("section");
  const h3 = document.createElement("h3");
  h3.textContent = `${entry.label} · ${markDateLabel(entry.at)}`;
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Runway", ...TABLE_COLUMNS.map((c) => c.label)]) {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = document.createElement("tbody");
  for (const row of entry.rows) {
    if (showQuiet || !quietKeys.has(row.key)) tbody.append(markRow(row));
  }
  table.append(thead, tbody);
  const period = document.createElement("p");
  period.className = "period";
  period.textContent = periodSummary(range);
  section.append(h3, table, period);
  return section;
}

function renderMarks(coverage, marks, spanDaysList, quietKeys) {
  marksEl.replaceChildren();
  const ranges = periods(marks, spanDaysList);
  const entries = beforeAfter(coverage, marks, spanDaysList);
  entries.forEach((entry, i) => marksEl.append(markSection(entry, ranges[i], quietKeys)));
}

// --- The outcome charts --------------------------------------------------------------------------------------------

function renderOutcomeCharts(outcomes, spanDaysList) {
  outcomeChartsEl.replaceChildren();
  for (const entry of outcomeSeries(outcomes, spanDaysList)) {
    const figure = document.createElement("figure");
    const figcaption = document.createElement("figcaption");
    figcaption.textContent = `${entry.airport} · ${entry.total} approach${entry.total === 1 ? "" : "es"}`;
    const chartEl = document.createElement("div");
    chartEl.className = "chart";
    figure.append(figcaption, chartEl);
    outcomeChartsEl.append(figure);
    charts.push({ chart: buildOutcomeChart(chartEl, spanDaysList, entry.series), el: chartEl });
  }
}

/** A stacked bar chart drawn without uPlot's native support for one: each series holds the *cumulative* sum through
 * its own place in OUTCOMES order, and they are drawn largest-cumulative-first (reversed), each later, smaller bar
 * painting over the bottom of the one before it. What is left showing through, at every point, is exactly that
 * outcome's own count — so the series drawn last (`landed`, the smallest cumulative sum: itself alone) sits flush on
 * the baseline, and the one drawn first (the full day's total, in `abandoned`'s colour) is left showing only at the
 * very top of the stack. */
function buildOutcomeChart(el, spanDaysList, series) {
  const xs = spanDaysList.map((_, i) => i);
  const own = new Map(series.map((s) => [s.outcome, s.values]));    // each outcome's own count, for the legend
  let running = xs.map(() => 0);
  const cumulative = series.map((s) => {
    running = running.map((v, i) => v + s.values[i]);
    return { outcome: s.outcome, values: [...running] };
  });
  const drawOrder = [...cumulative].reverse();
  const fg = color("--fg"), grid = color("--grid");
  const opts = {
    width: el.clientWidth,
    height: CHART_HEIGHT_PX,
    scales: { x: { time: false }, y: {} },
    axes: [
      { stroke: fg, grid: { stroke: grid }, values: (u, splits) => thinDayTicks(splits, spanDaysList) },
      { scale: "y", label: "approaches", stroke: fg, grid: { stroke: grid } },
    ],
    series: [
      // The legend is this chart's only colour key, so it stays — but every series carries the running total
      // through its own place in the stack, and uPlot would read those straight out as the outcome's own count.
      // Both readouts come from the day under the cursor instead, as the runway charts' own readout does.
      { label: "Day", value: (u) => (u.cursor.idx == null ? "—" : dayLabel(spanDaysList[u.cursor.idx])) },
      ...drawOrder.map((s) => ({
        scale: "y", paths: window.uPlot.paths.bars({ size: [0.7, 20], align: 0 }), fill: color(OUTCOME_TOKENS[s.outcome]),
        stroke: "transparent", width: 0, points: { show: false }, label: OUTCOME_LABELS[s.outcome],
        value: (u) => (u.cursor.idx == null ? "—" : own.get(s.outcome)[u.cursor.idx]),
      })),
    ],
    cursor: { drag: { x: false, y: false } },
  };
  return new window.uPlot(opts, [xs, ...drawOrder.map((s) => s.values)], el);
}

load();
