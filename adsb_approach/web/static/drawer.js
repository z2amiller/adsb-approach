// The detail drawer and its charts: height above threshold with the published glide path, cross-track
// and speed against distance to the threshold, the gates table and the METAR. While the drawer shows an attempt in
// progress, it follows the live event stream (onEvent) and refetches its detail — throttled for approach.updated,
// at once for the events that close it — updating the charts in place instead of rebuilding them. Charts are
// vendored uPlot, a global from static/vendor/uPlot.iife.min.js loaded before this module. Built with
// createElement/textContent only: callsigns and other API strings never go through innerHTML.
import { attemptButtonText, attemptChip, chartData, crossTickFilter, crossTickLabel, drawerReferences, emergencyNote, externalGlobeLabel, externalNote, fleetBandSeries, fmt, fmtDelta, fmtLateral, lateralAxisTitle, lateralZeroLabel, mergedPointCount, normLabel, openApproachRefresh, pathNote, referenceSwitches, referenceText, rowTime, translucent } from "./logic.js";

const CHART_HEIGHT_PX = 200;
const FOLLOW_THROTTLE_MS = 2000;

const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** `elements`: drawer, title, emergency, attempts, tar1090, globe, external, norm, path (the note on the procedure
 * path, with its chip and source), height, cross, crossTitle (the cross-track chart's caption), speed, gates and metar
 * (the ids in index.html). `clock` formats epoch seconds the way app.js's banner does (its own wrapper around
 * logic.js's clockText), for the drawer note's lookup-due time (externalNote). */
export function createDrawer(elements, { onOpen, fetchJson, clock }) {
  let openId = null;
  let openerEl = null;             // focus goes back here on close, if it is still in the document
  let charts = [];                 // [{chart, el, key}]: el is the chart's own container, for ResizeObserver
  let chartsExternal = false;      // whether `charts` was built with adsb.lol twin series (see buildCharts, render)
  let chartsBand = false;          // whether `charts` was built with a fleet norm band (see buildCharts, render)
  let chartsZeroLabel = null;      // the cross-track chart's zero line's name it was built with (lateralZeroLabel)
  let lastPoints = null;
  let lastReference = null;
  let lastFleet = null;
  let lastSwitches = [];           // the cross-track chart's markers, read by its draw hook on every redraw
  let followTimer = null;          // the pending throttled refresh of an open attempt in progress, or null

  const darkQuery = matchMedia("(prefers-color-scheme: dark)");
  darkQuery.addEventListener("change", () => {
    if (lastPoints !== null) buildCharts(lastPoints, lastReference, lastFleet, lastSwitches);
  });

  new ResizeObserver(() => {
    for (const { chart, el } of charts) chart.setSize({ width: el.clientWidth, height: CHART_HEIGHT_PX });
  }).observe(elements.drawer);

  /** `opener` is the element focus returns to on close: the one focused now unless the caller names another, as a page
   * does that closes a different drawer first, whose closing would otherwise leave that one focused. */
  async function open(approachId, opener = document.activeElement) {
    stopFollow();
    if (openId === null) openerEl = opener;                    // remember only the drawer's original opener
    openId = approachId;
    elements.drawer.hidden = false;
    elements.title.textContent = "Loading…";
    elements.title.focus();
    clearDetail();                  // the previous approach's charts, gates, METAR and links don't linger while this loads
    let body;
    try {
      body = await fetchJson(`api/approaches/${approachId}`);
    } catch (err) {
      if (openId !== approachId) return;             // superseded by a later open()
      elements.title.textContent = err.status === 404 ? `Approach ${approachId} not found`
        : `Could not load approach ${approachId}: ${err.message}`;
      return;
    }
    if (openId !== approachId) return;
    render(body);
  }

  /** `restoreFocus: false` leaves focus where it is, for a page that closes this drawer to open another. */
  function close({ restoreFocus = true } = {}) {
    stopFollow();
    openId = null;
    elements.drawer.hidden = true;
    destroyCharts();
    lastPoints = null;
    lastReference = null;
    lastFleet = null;
    lastSwitches = [];
    if (restoreFocus && openerEl && document.contains(openerEl)) openerEl.focus();
    openerEl = null;
  }

  function stopFollow() {
    if (followTimer !== null) clearTimeout(followTimer);
    followTimer = null;
  }

  /** Refetch the open approach's detail and render it in place: used both for an immediate refresh and when a
   * throttled one's timer fires. Silently gives up on a failure — a later event, or the next throttled tick while
   * the attempt is still open, tries again — so a blip doesn't blank a drawer that already shows good data. */
  async function refreshOpen() {
    const id = openId;
    if (id === null) return;
    let body;
    try {
      body = await fetchJson(`api/approaches/${id}`);
    } catch {
      return;
    }
    if (openId !== id) return;      // the drawer moved on while this was in flight
    render(body);
  }

  /** Follows an approach while the drawer shows it in progress (item 4): an approach.updated for it schedules
   * refreshOpen at most once every FOLLOW_THROTTLE_MS; approach.completed, approach.go_around or
   * approach.runway_change for it refreshes at once, so the stored points and gates replace the in-progress view
   * without waiting on the throttle. */
  function onEvent(event) {
    const refresh = openApproachRefresh(openId, event);
    if (refresh === "immediate") {
      stopFollow();
      refreshOpen();
    } else if (refresh === "throttled" && followTimer === null) {
      followTimer = setTimeout(() => { followTimer = null; refreshOpen(); }, FOLLOW_THROTTLE_MS);
    }
  }

  /** Renders a detail body, whether from open() or a follow-up refreshOpen(): charts are rebuilt on a fresh open
   * (charts is empty just after clearDetail()), and on a follow-up refresh of the same open approach whose adsb.lol
   * series or fleet norm band joined or left (an approach.extended splice, see openApproachRefresh; a reflag never
   * refreshes an open approach, since it only touches stored ones) since setData cannot add or remove a series, or
   * where the cross-track chart's zero line changed its name (the attempt moved onto a procedure path while watched)
   * — otherwise updated in place, uPlot's setData, so it doesn't flicker or lose its zoom while watched. */
  function render(body) {
    const a = body.approach;
    elements.title.textContent = titleText(a);
    const emergency = emergencyNote(a.flags);
    elements.emergency.textContent = emergency ?? "";
    elements.emergency.hidden = emergency === null;
    elements.attempts.replaceChildren(...body.arrival.map((attempt) => attemptButton(attempt, a.id)));
    elements.tar1090.href = body.tar1090_url;
    elements.globe.hidden = body.external_globe_url === null;
    if (body.external_globe_url === null) elements.globe.removeAttribute("href");
    else {
      elements.globe.href = body.external_globe_url;
      elements.globe.textContent = `Open on ${externalGlobeLabel(body.external_globe_url)}`;
    }
    const note = externalNote(body.external, clock, mergedPointCount(body.points));
    elements.external.textContent = note ?? "";
    elements.external.hidden = note === null;
    const norm = normLabel(body.fleet, a.runway);
    elements.norm.textContent = norm ?? "";
    elements.norm.hidden = norm === null;
    const data = chartData(body.points, body.reference);
    const external = Boolean(data.external);
    const band = fleetBandSeries(data.height[0], body.fleet, body.reference);
    const hasBand = Boolean(band.height || band.speed);
    const switches = referenceSwitches(body.points, body.reference, body.in_progress);
    elements.crossTitle.textContent = `${lateralAxisTitle(body.reference)} (ft)`;
    const chain = referenceText(drawerReferences(body.points, body.reference, body.in_progress));
    const source = pathNote(body.reference);
    elements.pathChip.textContent = chain;
    elements.pathChip.hidden = chain === "";
    elements.pathSource.textContent = source ?? "";
    elements.path.hidden = chain === "" && source === null;
    if (charts.length && external === chartsExternal && hasBand === chartsBand
        && lateralZeroLabel(body.reference) === chartsZeroLabel) updateCharts(body.points, body.reference, body.fleet, switches);
    else buildCharts(body.points, body.reference, body.fleet, switches);
    elements.gates.tBodies[0].replaceChildren(...(body.in_progress ? [gatesPendingRow()] : body.gates.map(gateRow)));
    elements.metar.textContent = body.weather ? body.weather.raw : "No METAR was used for this approach.";
  }

  function titleText(a) {
    return `${a.callsign || a.hex} · ${a.airport} ${a.runway} · ${attemptChip(a)} · ${rowTime(a.established_at)}`;
  }

  function attemptButton(attempt, currentApproachId) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = attemptButtonText(attempt);
    if (attempt.outcome === null) button.dataset.open = "";      // in progress, the way the log marks an open attempt
    if (attempt.approach_id === currentApproachId) {
      button.disabled = true;
      button.setAttribute("aria-current", "true");
    } else {
      button.addEventListener("click", () => onOpen(attempt.approach_id));
    }
    return button;
  }

  function clearDetail() {
    elements.attempts.replaceChildren();
    elements.tar1090.removeAttribute("href");
    elements.globe.hidden = true;
    elements.globe.removeAttribute("href");
    elements.globe.textContent = "";
    elements.external.textContent = "";
    elements.external.hidden = true;
    elements.norm.textContent = "";
    elements.norm.hidden = true;
    elements.emergency.textContent = "";
    elements.emergency.hidden = true;
    elements.path.hidden = true;
    elements.pathChip.textContent = "";
    elements.pathSource.textContent = "";
    elements.crossTitle.textContent = `${lateralAxisTitle(null)} (ft)`;
    destroyCharts();
    lastPoints = null;
    lastReference = null;
    lastFleet = null;
    lastSwitches = [];
    elements.gates.tBodies[0].replaceChildren();
    elements.metar.textContent = "";
  }

  /** A chart series followed by its adsb.lol twin, in chartData's column order, while `hasExternal`: solid, in the
   * local series' own colour at half strength (translucent), labelled "<label> · adsb.lol", and carrying whatever
   * else the local spec sets (e.g. the cross-track chart's `value` legend readout), so both read alike apart from
   * colour. Without adsb.lol points, just the one spec, unchanged. */
  function withExternalTwin(spec, hasExternal) {
    if (!hasExternal) return [spec];
    const { label, stroke, ...rest } = spec;
    return [spec, { label: `${label} · adsb.lol`, stroke: translucent(stroke, 0.5), ...rest }];
  }

  /** A chart's own series and data, with a fleet norm's [low, high] band (fleetBandSeries) appended when there is
   * one: two more series after every one the chart already has, unstroked and pointless ("Norm 25th", "Norm 75th")
   * so the legend names them without drawing them twice, and a `bands` option that fills between them in
   * --series-band — drawn behind the track, since uPlot fills a band under the series already drawn. `band` null
   * (no norm for this chart's series) leaves the chart exactly as it was. */
  function withBand(seriesSpecs, data, band) {
    if (!band) return { seriesSpecs, data, options: {} };
    const lowIdx = seriesSpecs.length + 1;      // +1 for uPlot's own leading x pseudo-series
    const highIdx = lowIdx + 1;
    return {
      seriesSpecs: [...seriesSpecs, { label: "Norm 25th", stroke: "transparent", points: { show: false } },
        { label: "Norm 75th", stroke: "transparent", points: { show: false } }],
      data: [...data, band[0], band[1]],
      options: { bands: [{ series: [highIdx, lowIdx], fill: color("--series-band") }] },
    };
  }

  function buildCharts(points, reference, fleet, switches) {
    destroyCharts();
    lastPoints = points;
    lastReference = reference;
    lastFleet = fleet;
    lastSwitches = switches;
    const data = chartData(points, reference);
    chartsExternal = Boolean(data.external);
    const band = fleetBandSeries(data.height[0], fleet, reference);
    chartsBand = Boolean(band.height || band.speed);
    chartsZeroLabel = lateralZeroLabel(reference);
    const trackStroke = color("--series-track");
    const pathStroke = color("--series-path");
    const height = withBand([
      ...withExternalTwin({ label: "Height", stroke: trackStroke }, chartsExternal),
      { label: "Glide path", stroke: pathStroke, dash: [6, 4] },
    ], data.height, band.height);
    const speed = withBand([
      ...withExternalTwin({ label: "Ground speed", stroke: trackStroke }, chartsExternal),
      ...withExternalTwin({ label: "Wind-corrected", stroke: color("--series-2") }, chartsExternal),
    ], data.speed, band.speed);
    charts = [
      { el: elements.height, key: "height",
        chart: buildChart(elements.height, height.data, "ft", height.seriesSpecs, height.options) },
      // Drawn as seen from above, the aircraft flying left to right towards the threshold (the x axis is reversed), so
      // the y axis is reversed too: left of the course is above the centreline, right of it below.
      { el: elements.cross, key: "cross", chart: buildChart(elements.cross, data.cross, "ft", [
        ...withExternalTwin({ label: "Cross-track", stroke: trackStroke, value: (u, v) => fmtLateral(v) }, chartsExternal),
        { label: chartsZeroLabel, stroke: pathStroke, dash: [6, 4] },
      ], { yScale: { distr: 4, asinh: 100, dir: -1 },
           yAxis: { filter: (u, splits) => crossTickFilter(splits), values: (u, ticks) => ticks.map(crossTickLabel) },
           hooks: { draw: [(u) => drawSwitchMarkers(u, lastSwitches)] } }) },
      { el: elements.speed, key: "speed",
        chart: buildChart(elements.speed, speed.data, "kt", speed.seriesSpecs, speed.options) },
    ];
  }

  /** Updates the charts' data in place (uPlot's setData, which also rescales), for a follow-up refresh of the same
   * open approach: cheaper than a rebuild, and doesn't discard the chart's own state (e.g. a legend hover). The
   * band is recomputed the same way buildCharts placed it, appended after the chart's own series, so a chart built
   * with one keeps receiving it in the same series positions. */
  function updateCharts(points, reference, fleet, switches) {
    lastPoints = points;
    lastReference = reference;
    lastFleet = fleet;
    lastSwitches = switches;
    const data = chartData(points, reference);
    const band = fleetBandSeries(data.height[0], fleet, reference);
    for (const { chart, key } of charts) {
      const series = band[key] ? [...data[key], band[key][0], band[key][1]] : data[key];
      chart.setData(series);
    }
  }

  /** `yScale` extends the y scale (the cross-track chart's asinh distr); `yAxis` extends the y axis (its L/R tick
   * labels); `bands` fills between two series (the fleet norm band, withBand); `hooks` are uPlot's own (the
   * cross-track chart's switch markers draw in one). All default to nothing extra. */
  function buildChart(el, data, unit, seriesSpecs, { yScale = {}, yAxis = {}, bands, hooks } = {}) {
    el.replaceChildren();
    const options = {
      width: el.clientWidth,
      height: CHART_HEIGHT_PX,
      scales: { x: { time: false, dir: -1 }, y: { ...yScale } },
      axes: [
        { label: "nm to threshold", stroke: color("--fg"), grid: { stroke: color("--grid") } },
        { label: unit, stroke: color("--fg"), grid: { stroke: color("--grid") }, ...yAxis },
      ],
      cursor: { drag: { x: false, y: false } },
      series: [{}, ...seriesSpecs],
    };
    if (bands) options.bands = bands;
    if (hooks) options.hooks = hooks;
    return new window.uPlot(options, data, el);
  }

  /** The cross-track chart's reference switch markers, drawn over the plot after uPlot's
   * own drawing: a dashed vertical line at each switch's distance and, just right of it (the side the aircraft flies
   * on, the x axis being reversed), the new reference's label, kept inside the plot. A marker outside the scale's
   * current range is skipped. Canvas pixels are device pixels, so every size is scaled by the pixel ratio. */
  function drawSwitchMarkers(u, switches) {
    if (switches.length === 0) return;
    const { ctx, bbox } = u;
    const ratio = window.devicePixelRatio || 1;
    const { min, max } = u.scales.x;
    ctx.save();
    ctx.beginPath();
    ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
    ctx.clip();
    ctx.lineWidth = ratio;
    ctx.strokeStyle = color("--series-2");
    ctx.setLineDash([4 * ratio, 3 * ratio]);
    ctx.fillStyle = color("--fg");
    ctx.font = `${11 * ratio}px sans-serif`;
    ctx.textBaseline = "top";
    for (const { along_nm: along, label } of switches) {
      if (along < min || along > max) continue;
      const x = Math.round(u.valToPos(along, "x", true));
      ctx.beginPath();
      ctx.moveTo(x, bbox.top);
      ctx.lineTo(x, bbox.top + bbox.height);
      ctx.stroke();
      const pad = 4 * ratio;
      const fits = x + pad + ctx.measureText(label).width <= bbox.left + bbox.width;
      ctx.textAlign = fits ? "left" : "right";
      ctx.fillText(label, fits ? x + pad : x - pad, bbox.top + 3 * ratio);
    }
    ctx.restore();
  }

  function destroyCharts() {
    for (const { chart } of charts) chart.destroy();
    charts = [];
    chartsExternal = false;
    chartsBand = false;
    chartsZeroLabel = null;
  }

  function gatesPendingRow() {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 9;
    td.textContent = "Gates are scored when the approach ends.";
    tr.append(td);
    return tr;
  }

  function gateRow(gate) {
    const tr = document.createElement("tr");
    const label = document.createElement("td");
    label.textContent = `${gate.gate_ft.toLocaleString()} ft`;
    tr.append(label);
    if (!gate.observed) {
      const cell = document.createElement("td");
      cell.colSpan = 8;
      cell.textContent = "not observed";
      tr.append(cell);
      return tr;
    }
    const values = [
      fmt(gate.along_nm, 2, " nm"),
      `${fmt(gate.gp_dev_ft, 0, " ft")} (${fmt(gate.gp_dots, 1)})`,
      fmtLateral(gate.loc_dev_ft, gate.loc_dots),
      fmt(gate.gs, 0, " kt"),
      fmt(gate.gs_corr, 0, " kt"),
      fmt(gate.sink_fpm, 0, " fpm"),
      fmtDelta(gate.fleet_gp_delta_ft, " ft"),
      fmtDelta(gate.fleet_gs_delta_kt, " kt"),
    ];
    for (const text of values) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    return tr;
  }

  return { open, close, onEvent };
}
