// The page's logic without its DOM: the view in the URL, filter chips, attempt chips, live cards, log rows,
// chart data, the banner and the reconnecting event stream. Times from the API are UNIX seconds.

export const WINDOWS = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, all: null };
export const LIST_KEYS = ["airport", "airport_not", "runway", "runway_not", "outcome", "outcome_not", "flags_any",
  "flags_none"];
export const CARD_FIELDS = ["along_nm", "hat_ft", "gp_dev_ft", "gp_dots", "loc_dev_ft", "loc_dots", "gs", "gs_corr",
  "sink_fpm", "path", "path_label"];
export const APPROACH_KINDS = ["all", "straight", "path"];
// The approach kind's URL parameter. Not `approach`: that is the open drawer's approach id (a live card's link, a
// bookmark and the search page all use it), so both can be in one URL. /api/arrivals does take `approach=`.
const APPROACH_KIND_PARAM = "approach_kind";

const unique = (values) => [...new Set(values)];

/** The view held in the URL: include and exclude lists for airport, runway (AIRPORT/RUNWAY), outcome and flags, the
 * time window, the attention preset, the approach kind (all, straight-in or path: `approach_kind`) and the open
 * approach. Anything missing or malformed takes its default: the
 * window defaults to the last 24 hours, so a bookmarked or shared page without `window` still hides older arrivals. */
export function parseState(search) {
  const q = new URLSearchParams(search);
  const state = { window: "24h", attention: q.get("preset") === "attention", rotorcraft: q.get("rotorcraft") === "1",
    ga: q.get("ga") === "1", approachKind: "all", approach: null };
  for (const key of LIST_KEYS) {
    state[key] = unique((q.get(key) || "").split(",").map((v) => v.trim()).filter(Boolean));
  }
  const span = q.get("window");
  if (span !== null && Object.hasOwn(WINDOWS, span)) state.window = span;
  const kind = q.get(APPROACH_KIND_PARAM);
  if (APPROACH_KINDS.includes(kind)) state.approachKind = kind;
  const approach = Number(q.get("approach"));
  if (Number.isSafeInteger(approach) && approach > 0) state.approach = approach;
  return state;
}

function listParams(state) {
  const q = new URLSearchParams();
  for (const key of LIST_KEYS) {
    if (state[key].length) q.set(key, state[key].join(","));
  }
  return q;
}

/** The URL query for a view: only what differs from the defaults, always in the same order, so one view has one URL.
 * The window defaults to 24h, so `window=24h` is left out and choosing All writes `window=all`. */
export function toSearch(state) {
  const q = listParams(state);
  if (state.window !== "24h") q.set("window", state.window);
  if (state.attention) q.set("preset", "attention");
  if (state.rotorcraft) q.set("rotorcraft", "1");
  if (state.ga) q.set("ga", "1");
  if (state.approachKind !== "all") q.set(APPROACH_KIND_PARAM, state.approachKind);
  if (state.approach !== null) q.set("approach", String(state.approach));
  const text = q.toString();
  return text ? `?${text}` : "";
}

/** What changing the view from `state` to `next` asks of the page: the URL query to show, whether the log reloads
 * (its filters or window changed), whether the live cards reload (the rotorcraft or GA checkbox changed: nothing
 * else /api/live is sent affects it), and "open", "close" or null for the drawer (whether the open approach
 * changed). */
export function viewChange(state, next) {
  const filters = (view) => toSearch({ ...view, approach: null });
  const drawer = state.approach === next.approach ? null : next.approach === null ? "close" : "open";
  return { search: toSearch(next), reloadLog: filters(state) !== filters(next),
    reloadLive: state.rotorcraft !== next.rotorcraft || state.ga !== next.ga, drawer };
}

/** The /api/arrivals query for a view at nowS: its lists, its window as `since`, its preset, whether to include
 * rotorcraft and GA, its approach kind (`approach=straight|path`, left out for all), and each of `extra` ({limit,
 * cursor, arrival_id}) that is not null. */
export function arrivalsQuery(state, nowS, extra = {}) {
  const q = listParams(state);
  const span = WINDOWS[state.window];
  if (span) q.set("since", String(Math.floor(nowS - span)));
  if (state.attention) q.set("preset", "attention");
  if (state.rotorcraft) q.set("rotorcraft", "1");
  if (state.ga) q.set("ga", "1");
  if (state.approachKind !== "all") q.set("approach", state.approachKind);
  for (const [key, value] of Object.entries(extra)) {
    if (value !== null && value !== undefined) q.set(key, String(value));
  }
  return q.toString();
}

/** The /api/live query for a view: `rotorcraft=1` and/or `ga=1` for whichever checkboxes are on, else empty --
 * /api/live has no other filter. */
export function liveQuery(state) {
  const q = new URLSearchParams();
  if (state.rotorcraft) q.set("rotorcraft", "1");
  if (state.ga) q.set("ga", "1");
  return q.toString();
}

const chipKeys = (key) => (key === "flags" ? ["flags_any", "flags_none"] : [key, `${key}_not`]);

/** A filter chip's mode in a view: "include", "exclude" or "off". `key` is airport, runway, outcome or flags. */
export function chipMode(state, key, value) {
  const [include, exclude] = chipKeys(key);
  if (state[include].includes(value)) return "include";
  return state[exclude].includes(value) ? "exclude" : "off";
}

/** The view after a chip is clicked: off becomes include, include becomes exclude, exclude becomes off. */
export function cycleChip(state, key, value) {
  const [include, exclude] = chipKeys(key);
  const mode = chipMode(state, key, value);
  const next = { ...state, [include]: state[include].filter((v) => v !== value),
    [exclude]: state[exclude].filter((v) => v !== value) };
  if (mode === "off") next[include] = [...next[include], value];
  else if (mode === "include") next[exclude] = [...next[exclude], value];
  return next;
}

const ATTENTION_FALLBACK = "Attention only shows go-arounds, touch-and-goes, abandoned approaches, and fast, slow, "
  + "unstable, steep, shallow, late-established, emergency or minimum-fuel approaches.";
const ATTENTION_OUTCOME_WORDS = { go_around: "go-arounds", touch_and_go: "touch-and-goes", abandoned: "abandoned approaches" };
const ATTENTION_FLAG_WORDS = { FAST: "fast", SLOW: "slow", UNSTABLE_1000: "unstable", UNSTABLE_500: "unstable",
  STEEP: "steep", SHALLOW: "shallow", LATE_ESTABLISHED: "late-established", EMERGENCY_GENERAL: "emergency",
  EMERGENCY_NORDO: "emergency", EMERGENCY_UNLAWFUL: "emergency", EMERGENCY_DOWNED: "emergency",
  MINIMUM_FUEL: "minimum-fuel" };

/** The "Attention only" checkbox's tooltip: what the filter shows, worded from /api/config's `attention` (its
 * `outcomes` and `flags`, as sent by ATTENTION_OUTCOMES/ATTENTION_FLAGS in web/arrivals.py), or ATTENTION_FALLBACK
 * when a config lacks it (an older server, or none loaded yet) — which reads the same as today's real config. */
export function attentionTitle(attention) {
  const outcomes = attention?.outcomes?.length ? unique(attention.outcomes.map((o) => ATTENTION_OUTCOME_WORDS[o] ?? o)) : null;
  const flags = attention?.flags?.length ? unique(attention.flags.map((f) => ATTENTION_FLAG_WORDS[f] ?? f)) : null;
  if (!outcomes || !flags) return ATTENTION_FALLBACK;
  const flagsPhrase = flags.length > 1 ? `${flags.slice(0, -1).join(", ")} or ${flags[flags.length - 1]}` : flags[0];
  return `Attention only shows ${outcomes.join(", ")}, and ${flagsPhrase} approaches.`;
}

const OUTCOME_WORDS = { landed: "landed", lost_on_final: "lost on final", lost: "lost", go_around: "GA",
  touch_and_go: "touch-and-go", runway_change: "runway change", abandoned: "abandoned" };

/** One attempt's chip: "GA @ 800 ft" for a go-around or touch-and-go, at the height it began when known and otherwise
 * the lowest tracked; "landed 28L" and the like for the rest; "on final 28L" while the attempt is open. An attempt
 * whose `flags` include EXTERNAL_OUTCOME (an adsb.lol tail decided it) gains " · adsb.lol",
 * for example "landed 28L · adsb.lol" or "GA @ 400 ft · adsb.lol" — EXTERNAL_TAIL alone, or landed_confirmed_by alone,
 * does not: only the tail deciding the outcome is worth calling out here. */
export function attemptChip(attempt) {
  if (attempt.outcome === null) return `on final ${attempt.runway}`;
  const words = OUTCOME_WORDS[attempt.outcome] ?? attempt.outcome;
  const external = attempt.flags?.includes("EXTERNAL_OUTCOME") ? " · adsb.lol" : "";
  if (attempt.outcome === "go_around" || attempt.outcome === "touch_and_go") {
    const height = attempt.ga_initiated_hat_ft ?? attempt.lowest_hat_ft;
    if (typeof height === "number") return `${words} @ ${Math.round(height)} ft${external}`;
  }
  return `${words} ${attempt.runway}${external}`;
}

/** An arrival's attempt chips in order: "GA @ 800 ft → landed 28L". */
export function attemptChain(arrival) {
  return arrival.attempts.map(attemptChip).join(" → ");
}

/** The references an attempt flew, as a log row's chip reads them: their labels in the order first flown, joined with
 * an arrow ("Quiet Bridge → ILS"). `references` is /api/arrivals' list of {name, label}, with name null for the
 * runway's centreline. Empty when the only reference is the centreline: a straight-in approach needs no chip, and
 * "ILS" on every row would drown the ones that flew a procedure path. */
export function referenceText(references) {
  if (!Array.isArray(references) || references.length === 0) return "";
  if (references.length === 1 && references[0].name === null) return "";
  return references.map((r) => r.label).join(" → ");
}

/** An attempt's chip beside its runway in the log: referenceText of its `references`, or, only when it lists none, its
 * path's label while it has a path (the name when the label is missing). Never from `path_label` alone: approach
 * events label a straight-in approach "ILS" too, so `path` is what says an attempt flew a path. Empty for an
 * attempt from before procedure paths, which has neither field. */
export function attemptReferenceText(attempt) {
  const text = referenceText(attempt.references);
  if (text !== "") return text;
  const none = !Array.isArray(attempt.references) || attempt.references.length === 0;
  return none && attempt.path != null ? (attempt.path_label ?? attempt.path) : "";
}

/** The drawer's attempt-switcher button text: "#2 28L go around", or "#1 28R on final" while that attempt is still
 * open. Gains " · adsb.lol" on the same EXTERNAL_OUTCOME rule as attemptChip, so the drawer's attempt buttons can
 * show which attempt's outcome adsb.lol decided, matching the drawer title's own " · adsb.lol" for the open one. */
export function attemptButtonText(attempt) {
  const outcome = attempt.outcome === null ? "on final" : attempt.outcome.replace(/_/g, " ");
  const external = attempt.flags?.includes("EXTERNAL_OUTCOME") ? " · adsb.lol" : "";
  return `#${attempt.attempt_no} ${attempt.runway} ${outcome}${external}`;
}

const cardValues = (payload) => Object.fromEntries(CARD_FIELDS.map((key) => [key, payload[key] ?? null]));

/** Whether a flag name is one of the four EMERGENCY_MASK flags: its name
 * starts with "EMERGENCY_" -- the prefix is the page's whole rule, so this needs no list of names. MINIMUM_FUEL does
 * not match, so it is drawn like any other attention flag, never red. */
export function isEmergencyFlag(name) {
  return typeof name === "string" && name.startsWith("EMERGENCY_");
}

// Order matters: emergencyNote joins in this order regardless of the order flagNames lists them in.
const EMERGENCY_WORDS = { EMERGENCY_GENERAL: "General emergency · 7700", EMERGENCY_NORDO: "Radio failure · 7600",
  EMERGENCY_UNLAWFUL: "Unlawful interference · 7500", EMERGENCY_DOWNED: "Downed" };

/** The drawer's red note for an approach holding an EMERGENCY_MASK flag: the drawer draws no
 * flag chips of its own, so without this nothing in it shows an emergency at all,
 * and its immediate refresh on approach.emergency changes nothing visible. `flagNames` is the approach's own `flags`
 * list, as the detail response and attemptChip's `attempt.flags` both give it; every EMERGENCY_MASK name present
 * becomes its own phrase, joined in EMERGENCY_WORDS' fixed order rather than flagNames' -- several at once is rare
 * (a squawk and an ADS-B state can each latch their own flag) but the order must not depend on latch timing.
 * MINIMUM_FUEL and every other flag are ignored: this is only ever the red note, never a yellow attention one. `null`
 * with nothing to say, so the drawer can hide the element the same way it hides externalNote's and normLabel's. */
export function emergencyNote(flagNames) {
  if (!Array.isArray(flagNames)) return null;
  const words = Object.keys(EMERGENCY_WORDS).filter((name) => flagNames.includes(name)).map((name) => EMERGENCY_WORDS[name]);
  return words.length ? words.join(" · ") : null;
}

/** A live card's full fields from an approach.started or approach.emergency payload, which share the same shape
 * apart from established_at: approach.started has none (its event.t is the establishment moment), while
 * approach.emergency, which can fire well after establishment, carries its own. */
const cardFrom = (event, p, establishedAt) => ({
  approach_id: event.approach_id, arrival_id: p.arrival_id ?? null, attempt_no: p.attempt_no ?? null, hex: p.hex,
  callsign: p.callsign ?? null, type_code: p.type_code ?? null, airport: p.airport, runway: p.runway,
  established_at: establishedAt, t: event.t, emergency: p.emergency ?? [], ...cardValues(p),
});

/** A live card's sub-line: "KSFO 28R", with " · Quiet Bridge" after the runway while the attempt is on a procedure
 * path and " · attempt 2" for a later attempt. Gated on `path`, not `path_label`: approach.started and
 * approach.updated carry path_label "ILS" (the runway's centreline label) for a straight-in approach, and an event
 * from before procedure paths carries neither. */
export function cardSubText(card) {
  const parts = [`${card.airport} ${card.runway}`];
  if (card.path != null) parts.push(card.path_label ?? card.path);
  if (card.attempt_no > 1) parts.push(`attempt ${card.attempt_no}`);
  return parts.join(" · ");
}

/** Apply one event to the live cards, a Map from hex to card. A card shows its aircraft's newest attempt: an event for
 * an older attempt than the card's is ignored; approach.started for a newer attempt replaces the card, so during a
 * runway change the card moves to the new runway; approach.updated refreshes its values; approach.completed for the
 * card's own attempt removes it. approach.started for a rotorcraft attempt creates no card unless `showRotorcraft`,
 * and for a GA attempt (its type_code in `gaTypes`, from /api/config's ga_types) unless `showGa` -- a missing
 * type_code is never treated as GA, the same NULL-safe rule the server's own filter uses: the event
 * stream, unlike /api/live, is not filtered server-side. Either bypass is itself bypassed when the payload's
 * `emergency` is non-empty, and the card keeps that field.
 *
 * approach.emergency needs no rotorcraft/GA check of its own: the collector only ever publishes it with
 * `emergency` non-empty, so a card it builds is always the bypass case. With `in_progress` true it builds a card for
 * an aircraft the two filters above had hidden -- approach.updated cannot, since the client ignores updates for a
 * card it never created -- or refreshes one already showing; with `in_progress` false (the just-after-close path)
 * it changes no card, since the log row is the only thing left to refresh for an attempt that has already closed. A
 * stale event for an attempt older than the card's current one is ignored, the same as approach.started. */
export function applyLiveEvent(cards, event, showRotorcraft = false, showGa = false, gaTypes = new Set()) {
  const p = event.payload ?? {};
  if (!p.hex) return false;
  const card = cards.get(p.hex);
  if (event.kind === "approach.started") {
    const bypass = (p.emergency ?? []).length > 0;
    if (p.traffic_class === "rotorcraft" && !showRotorcraft && !bypass) return false;
    if (!showGa && p.type_code && gaTypes.has(p.type_code) && !bypass) return false;
    if (card && card.approach_id >= event.approach_id) return false;
    cards.set(p.hex, cardFrom(event, p, event.t));
    return true;
  }
  if (event.kind === "approach.emergency") {
    if (!p.in_progress) return false;
    if (card && card.approach_id > event.approach_id) return false;
    if (card && card.approach_id === event.approach_id) {
      // A card already showing this attempt keeps the values approach.updated filled in: this payload carries only
      // along_nm and hat_ft of them, so building the card afresh would blank its glide path, localizer, speed and
      // sink until the next update.
      card.emergency = p.emergency ?? [];
      return true;
    }
    cards.set(p.hex, cardFrom(event, p, p.established_at));
    return true;
  }
  if (!card || card.approach_id !== event.approach_id) return false;
  if (event.kind === "approach.updated") {
    Object.assign(card, cardValues(p), { t: event.t });
    return true;
  }
  if (event.kind === "approach.completed") {
    cards.delete(p.hex);
    return true;
  }
  return false;
}

/** The cards in display order: nearest the threshold first, cards without a distance last. */
export function sortedCards(cards) {
  return [...cards.values()].sort((a, b) => (a.along_nm === null) - (b.along_nm === null)
    || (a.along_nm ?? 0) - (b.along_nm ?? 0) || a.approach_id - b.approach_id);
}

/** The arrival an event may change in the log, or null. The log lists arrivals with a closed attempt, so a row
 * changes when an attempt closes, when a closed attempt is reclassified or its landing confirmed, when a listed
 * arrival starts another attempt, and when EMERGENCY_MASK flags are newly stamped on one of its attempts
 * (approach.emergency) -- whether that attempt is still open (its "emergency" bypass may now apply) or was just
 * closed (the collector stamps the flight's latched flags onto its most recent attempt just after a close), so this is
 * unconditional on `in_progress`. */
export function arrivalToRefresh(event) {
  const p = event.payload ?? {};
  const id = p.arrival_id ?? null;
  if (id === null) return null;
  if (["approach.completed", "approach.reclassified", "approach.confirmed", "approach.extended",
       "approach.emergency"].includes(event.kind)) {
    return id;
  }
  if (event.kind === "approach.started" && (p.attempt_no ?? 1) > 1) return id;
  return null;
}

/** Whether an event demands refetching the drawer's open approach (`openId`, null when no drawer is open), and how
 * urgently: "immediate" for approach.completed, approach.go_around, approach.runway_change, approach.reclassified,
 * approach.confirmed, approach.extended or approach.emergency (newly stamped EMERGENCY_MASK flags,
 * however `in_progress` reads) — the attempt just closed, or a backfill splice just
 * reclassified it, confirmed its landing, or merely added adsb.lol points, so the stored points, gates and flags
 * must replace the in-progress or now-stale view without waiting — or "throttled" for approach.updated, which
 * the collector writes at most once a second and so does not need following that eagerly. null when the event is for
 * a different approach, or is a kind that never changes what the drawer shows for its own approach
 * (approach.started). */
export function openApproachRefresh(openId, event) {
  if (openId === null || event.approach_id !== openId) return null;
  if (event.kind === "approach.updated") return "throttled";
  if (["approach.completed", "approach.go_around", "approach.runway_change", "approach.reclassified",
       "approach.confirmed", "approach.extended", "approach.emergency"].includes(event.kind)) {
    return "immediate";
  }
  return null;
}

const newer = (a, b) => a.started_at > b.started_at || (a.started_at === b.started_at && a.arrival_id > b.arrival_id);

/** The log's rows after one arrival was fetched again with the view's filters: `row` is what /api/arrivals returned
 * for it, or null when it no longer passes them. A listed row is replaced where it is, or removed. A new row goes
 * where the API's order puts it (newest first by started_at, then arrival_id); when that is past the last row while
 * more pages remain, it is left for the page that holds it, so loading more never repeats a row. */
export function upsertRow(rows, arrivalId, row, morePages) {
  const index = rows.findIndex((r) => r.arrival_id === arrivalId);
  if (index !== -1) {
    return row === null ? rows.filter((_, i) => i !== index) : rows.map((r, i) => (i === index ? row : r));
  }
  if (row === null) return rows;
  const at = rows.findIndex((r) => newer(row, r));
  if (at === -1) return morePages ? rows : [...rows, row];
  return [...rows.slice(0, at), row, ...rows.slice(at)];
}

/** The log's rows still inside the view's time window at nowS. The API lists an arrival while one of its closed
 * attempts ended at or after `since`, so a row whose closed attempts all ended earlier has left the window; All keeps
 * every row. The log fetches only rows that change, so a page left open needs this as time passes, or it keeps rows
 * from days ago under "24 hours" and grows with every arrival. */
export function rowsInWindow(rows, state, nowS) {
  const span = WINDOWS[state.window];
  if (!span) return rows;
  const since = Math.floor(nowS - span);
  return rows.filter((row) => row.attempts.some((a) => a.outcome !== null && a.ended_at >= since));
}

/** A CSS custom property's `#rrggbb` colour (as `getComputedStyle().getPropertyValue` returns it — surrounding
 * whitespace included, which this trims before matching) at partial opacity: `rgba(r, g, b, alpha)` with decimal
 * channels. Used to draw an adsb.lol series in its local twin's colour at half strength (drawer.js's buildCharts).
 * Anything that isn't a bare hex colour is returned unchanged, so a scale or unresolved `var(...)` passes through. */
export function translucent(color, alpha) {
  const trimmed = typeof color === "string" ? color.trim() : color;
  const m = typeof trimmed === "string" ? /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(trimmed) : null;
  if (!m) return color;
  const [r, g, b] = m.slice(1).map((h) => parseInt(h, 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** uPlot data for the drawer's three charts, each against distance to the threshold in nm. uPlot wants x ascending,
 * so points are sorted by distance, and a point at the same distance as the one kept before it is dropped. The height
 * chart has the published glide path beside the track: tch_ft + distance × ft_per_nm × tan(glide path), the line the
 * scorer measures deviations from, drawn from the threshold (0 nm is added when no point reaches it) and not past it.
 * The cross-track chart gets a constant zero series beside it, for the centreline reference line. A missing value
 * stays null, which uPlot draws as a gap.
 *
 * Once any point's `origin` is "adsb.lol" (a point without `origin` counts as local),
 * each measured series (height, cross, gs, gsCorr) splits into a local and an adsb.lol twin, placed right after it,
 * and the result gains `external: true`; without such a point the shape is exactly today's. A local point that is
 * the x-neighbour, before or after, of an adsb.lol point keeps its value in the adsb.lol twin too, so the drawn line
 * meets the solid one at the seam instead of leaving a gap. */
export function chartData(points, reference) {
  const kept = [];
  for (const p of points.filter((q) => typeof q.along_nm === "number").sort((a, b) => a.along_nm - b.along_nm)) {
    if (kept.length && kept[kept.length - 1].along_nm === p.along_nm) continue;
    kept.push(p);
  }
  const isExternal = kept.map((p) => p.origin === "adsb.lol");
  const external = isExternal.some(Boolean);
  const x = kept.map((p) => p.along_nm);
  const column = (key) => kept.map((p) => p[key] ?? null);
  const height = column("hat_ft");
  const cross = column("cross_ft");
  const gs = column("gs");
  const gsCorr = column("gs_corr");
  let path = x.map(() => null);
  if (reference) {
    const slope = reference.ft_per_nm * Math.tan((reference.glide_path_deg * Math.PI) / 180);
    path = x.map((d) => (d >= 0 ? reference.tch_ft + d * slope : null));
    if (x.length === 0 || x[0] > 0) {
      x.unshift(0);
      path.unshift(reference.tch_ft);
      isExternal.unshift(false);
      for (const series of [height, cross, gs, gsCorr]) series.unshift(null);
    }
  }
  const zero = x.map(() => 0);
  if (!external) return { height: [x, height, path], cross: [x, cross, zero], speed: [x, gs, gsCorr] };
  const split = (series) => {
    const local = series.map((v, i) => (isExternal[i] ? null : v));
    const twin = series.map((v, i) => {
      if (isExternal[i]) return v;
      const neighboursExternal = (i > 0 && isExternal[i - 1]) || (i < isExternal.length - 1 && isExternal[i + 1]);
      return neighboursExternal ? v : null;
    });
    return [local, twin];
  };
  const [heightLocal, heightExternal] = split(height);
  const [crossLocal, crossExternal] = split(cross);
  const [gsLocal, gsExternal] = split(gs);
  const [gsCorrLocal, gsCorrExternal] = split(gsCorr);
  return {
    external: true,
    height: [x, heightLocal, heightExternal, path],
    cross: [x, crossLocal, crossExternal, zero],
    speed: [x, gsLocal, gsExternal, gsCorrLocal, gsCorrExternal],
  };
}

const CENTRELINE = "centreline";

/** The label of the reference `name` in the drawer, for an approach whose detail `reference` is given: the path's own
 * label for its last path, the detail's `centreline_label` ("ILS" or "straight-in", as the log says) for null, and the
 * stored name for any other path, which only an earlier stretch of an approach that flew several can name. A detail
 * with no `centreline_label` (an older server, or a runway with no reference) calls the centreline "centreline". */
function referenceLabel(name, reference) {
  if (name === null) return reference?.centreline_label ?? CENTRELINE;
  return name === reference?.path ? (reference.path_label ?? name) : name;
}

/** The detail's points as runs of one reference each, oldest first: {name, along_nm}, the distance of the run's first
 * point that has one. A point from a database not yet migrated has no `reference`: the centreline. */
function referenceRuns(points) {
  const runs = [];
  for (const p of points) {
    const name = p.reference ?? null;
    let run = runs[runs.length - 1];
    if (!run || run.name !== name) {
      run = { name, along_nm: null };
      runs.push(run);
    }
    if (run.along_nm === null && typeof p.along_nm === "number") run.along_nm = p.along_nm;
  }
  return runs;
}

/** The references an approach flew, as the drawer knows them, each {name, label}: the distinct references of its
 * points in the order first flown, with the approach's last path (`reference.path`) first when it was left before any
 * stored point (only points within the scoring range are stored), the same rule /api/arrivals' `references` follows.
 * An approach still in progress has points that name no reference yet (they come from its events), so it is its
 * path alone, or the centreline. Feed it to referenceText for the drawer's chip. */
export function drawerReferences(points, reference, inProgress) {
  const path = reference?.path ?? null;
  const names = [];
  if (!inProgress) {
    for (const run of referenceRuns(points)) {
      if (!names.includes(run.name)) names.push(run.name);
    }
  }
  if (path !== null && !names.includes(path)) names.unshift(path);
  if (names.length === 0) names.push(null);
  return names.map((name) => ({ name, label: referenceLabel(name, reference) }));
}

/** Where the drawer's cross-track chart marks a change of reference: one {along_nm, label} per change in the
 * consecutive points' `reference`, at the first point measured against the new one, labelled with it. None for an
 * attempt in progress (its points name no reference, so a change would be invented), and none for a new reference
 * whose points have no distance to put it at. */
export function referenceSwitches(points, reference, inProgress) {
  if (inProgress) return [];
  return referenceRuns(points).slice(1)
    .filter((run) => run.along_nm !== null)
    .map((run) => ({ along_nm: run.along_nm, label: referenceLabel(run.name, reference) }));
}

/** The title of the drawer's cross-track chart: what the lateral numbers are measured from. A path approach measures
 * from its path ("Cross-track from Quiet Bridge path"), any other from the runway's centreline. The drawer adds the
 * unit. `reference` is the detail's, null for a runway with none. */
export function lateralAxisTitle(reference) {
  return reference?.path_label ? `Cross-track from ${reference.path_label} path` : "Cross-track from the centreline";
}

/** The name of the cross-track chart's zero line: "Centreline" normally, "Reference" for a path approach, whose zero
 * is the path and then, after a switch, the centreline. */
export function lateralZeroLabel(reference) {
  return reference?.path_label ? "Reference" : "Centreline";
}

/** The drawer's note on the procedure path an approach flew, beside its chip: "Path source: SFO VOR R-095 inbound ..."
 * with the source the configuration gives for it, or null for an approach that flew none. A path no longer configured
 * comes with its stored name for its label and its source alike, which is how this tells. */
export function pathNote(reference) {
  const path = reference?.path ?? null;
  if (path === null) return null;
  const source = reference.path_source ?? path;
  return source === path ? "Path no longer configured" : `Path source: ${source}`;
}

/** The fleet norm's band behind the height and speed charts, against the same `x` (distance
 * to the threshold, nm) chartData returns: `{height, speed}`, each either `[low, high]` — two arrays as long as `x`
 * — or `null`. With no `fleet`, or a `fleet` with no bins, both are null.
 *
 * The bin for a distance is its lower edge, the same rule the scorer's bin_of uses: Math.round(Math.floor(d / w) *
 * w * 1000) / 1000 with w = fleet.bin_width_nm. A distance the key has no bin for is null in every series at that
 * index, and so is a negative distance.
 *
 * A bin's value sits at its centre, bin_nm + w / 2. Between the centres of two adjacent bins the band ramps
 * linearly; past the nearer edge of its own span — when there is no neighbouring bin, or it lacks the percentile
 * pair this series needs — it stays flat at its own bin's value, so the band ends level at the first and last bins
 * rather than sloping off into nothing -- deliberately, instead of a step
 * across each bin. A bin without both of a series' percentiles is unusable for that series: null where it holds
 * `d`, and skipped as a neighbour; height and speed decide this independently.
 *
 * `speed` is the bin's gs_p25 and gs_p75. `height` is null altogether without a `reference`; otherwise it is the
 * published path at `d`, `reference.tch_ft + d * reference.ft_per_nm * Math.tan(reference.glide_path_deg * Math.PI /
 * 180)` — the same line chartData draws as "Glide path" — plus the bin's (ramped) gp_p25 and gp_p75. */
export function fleetBandSeries(x, fleet, reference) {
  if (!fleet?.bins?.length) return { height: null, speed: null };
  const w = fleet.bin_width_nm;
  const round3 = (v) => Math.round(v * 1000) / 1000;
  const byBin = new Map(fleet.bins.map((b) => [round3(b.bin_nm), b]));
  const binAt = (d) => (d < 0 ? null : (byBin.get(round3(Math.floor(d / w) * w)) ?? null));
  const hasPair = (bin, lowKey, highKey) => bin !== null && typeof bin[lowKey] === "number" && typeof bin[highKey] === "number";

  // The ramped [low, high] for one series (a percentile pair) at every distance in x.
  const series = (lowKey, highKey) => {
    const low = [], high = [];
    for (const d of x) {
      const bin = binAt(d);
      if (!hasPair(bin, lowKey, highKey)) {
        low.push(null);
        high.push(null);
        continue;
      }
      const c = bin.bin_nm + w / 2;
      let neighbour = null;
      if (d < c) neighbour = byBin.get(round3(bin.bin_nm - w)) ?? null;
      else if (d > c) neighbour = byBin.get(round3(bin.bin_nm + w)) ?? null;
      const ramping = hasPair(neighbour, lowKey, highKey);
      const weight = ramping ? Math.abs(d - c) / w : 0;
      const at = (key) => bin[key] + weight * ((ramping ? neighbour[key] : bin[key]) - bin[key]);
      low.push(at(lowKey));
      high.push(at(highKey));
    }
    return [low, high];
  };

  const speed = series("gs_p25", "gs_p75");
  let height = null;
  if (reference) {
    const [gpLow, gpHigh] = series("gp_p25", "gp_p75");
    const slope = reference.ft_per_nm * Math.tan((reference.glide_path_deg * Math.PI) / 180);
    const path = (d) => reference.tch_ft + d * slope;
    height = [gpLow.map((v, i) => (v === null ? null : path(x[i]) + v)),
      gpHigh.map((v, i) => (v === null ? null : path(x[i]) + v))];
  }
  return { height, speed };
}

/** A number to show, or an em dash when it is missing: fmt(-48.2, 0, " ft") is "-48 ft", and -0.2 is "0". */
export function fmt(value, digits = 0, unit = "") {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const text = value.toFixed(digits);
  return `${/^-0(\.0+)?$/.test(text) ? text.slice(1) : text}${unit}`;
}

/** A lateral deviation (cross-track or localizer) as "45 ft L (0.1)": positive is to the right of the inbound course
 * as the pilot sees it, so positive reads R and negative reads L, with the magnitude and
 * the dots both unsigned. A value that rounds to 0 ft has no side (and no dots). `dots` is optional: the cross-track
 * chart's points carry cross_ft but no dots, so its cursor and legend call this with one argument. A missing `ft`
 * gives the em dash fmt gives. */
export function fmtLateral(ft, dots) {
  if (typeof ft !== "number" || !Number.isFinite(ft)) return fmt(ft);
  const rounded = Math.round(ft);
  if (rounded === 0) return "0 ft";
  const side = rounded > 0 ? "R" : "L";
  const dotsText = typeof dots === "number" && Number.isFinite(dots) ? ` (${fmt(Math.abs(dots), 1)})` : "";
  return `${Math.abs(rounded)} ft ${side}${dotsText}`;
}

/** An arrival's 1,000 ft gate as the log and the search results show it: the gate of its last closed attempt, "-1 ft
 * (0.0) · 3 ft L (0.0) · 137 kt · 768 fpm" (glide-path deviation with its dots, localizer deviation, ground speed,
 * sink rate), or an em dash while no closed attempt has an observed gate. */
export function gate1000Text(arrival) {
  const closed = arrival.attempts.filter((a) => a.outcome !== null);
  const gate = closed[closed.length - 1]?.gate1000;
  if (!gate || !gate.observed) return "—";
  return `${fmt(gate.gp_dev_ft, 0, " ft")} (${fmt(gate.gp_dots, 1)}) · ${fmtLateral(gate.loc_dev_ft, gate.loc_dots)} `
    + `· ${fmt(gate.gs, 0, " kt")} · ${fmt(gate.sink_fpm, 0, " fpm")}`;
}

/** A gate's delta from its fleet norm, signed: fmtDelta(6.2, " ft") is "+6 ft"; an em dash when there is nothing to
 * show — fleet_gp_delta_ft and fleet_gs_delta_kt are null before the first rebuild (or reflag), and without a norm.
 * The gates table's delta formatter (gateRow in drawer.js). `digits` matches whatever fmt printed the two numbers
 * the delta sits beside, so the three agree; the gates table prints whole units, which is the default. */
export function fmtDelta(value, unit, digits = 0) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${signed(value, digits)}${unit}`;
}

/** The cross-track chart's y-axis tick labels, in the same L/R sense as fmtLateral but unitless and without dots:
 * "1000 L", "100 R", "0". uPlot's axis ticks array carries null at positions it wants gridlines but no label (an
 * asinh scale's minor ticks, mostly): blank, not "0" — Math.round(null) is 0, so this must be checked first. */
export function crossTickLabel(ft) {
  if (typeof ft !== "number" || !Number.isFinite(ft)) return "";
  const rounded = Math.round(ft);
  if (rounded === 0) return "0";
  return `${Math.abs(rounded)} ${rounded > 0 ? "R" : "L"}`;
}

/** Which of the cross-track chart's y-axis splits get a label: 0 and each power of ten from 100 outwards, on both
 * sides of the centreline; the rest become null, gridlines without text. It replaces uPlot's own filter for an
 * asinh scale, which labels only positive decades and so left every left-of-course tick blank. */
export function crossTickFilter(splits) {
  return splits.map((ft) => {
    if (typeof ft !== "number" || !Number.isFinite(ft)) return null;
    if (ft === 0) return 0;
    const size = Math.abs(ft);
    if (size < 100) return null;
    return Math.abs(size - 10 ** Math.round(Math.log10(size))) < 1e-6 ? ft : null;
  });
}

/** `seconds` (UNIX) as the viewer's local time, "05:03 PM"; with its date prefixed, "Sep 12, 05:03 PM" (the same style
 * as the drawer's title and the log's Time column), when it falls on a different local calendar day than `nowS`
 * (also UNIX seconds) — so a banner read days later still says when, not just what time it was. */
export function clockText(seconds, nowS) {
  return localClock(seconds, nowS, { hour: "2-digit", minute: "2-digit" });
}

/** clockText with the seconds, "05:03:41 PM" or "Sep 12, 05:03:41 PM", for a span that can fit inside one minute (the
 * search drawer's emergency note: the 30 s latch is the whole test for red, so its start and end can share a minute). */
export function clockSecondsText(seconds, nowS) {
  return localClock(seconds, nowS, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** `seconds` formatted with `options` (the time fields) as the viewer's local time, with the date added when it is not
 * `nowS`'s local calendar day. */
function localClock(seconds, nowS, options) {
  const d = new Date(seconds * 1000);
  const now = new Date(nowS * 1000);
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (!sameDay) Object.assign(options, { month: "short", day: "numeric" });
  return d.toLocaleString([], options);
}

/** `seconds` (UNIX) as a row's Time cell reads it, "Sep 12, 05:03 PM": the date and clock in the viewer's local time,
 * always with the date (clockText leaves it off for today). The arrivals log, the search results and the drawer's
 * title all use it. */
export function rowTime(seconds) {
  return new Date(seconds * 1000)
    .toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** `value` with a leading sign: a real minus sign (U+2212, not a hyphen) before a negative one, and a leading + before
 * zero and positive ones, for the drawer note's seam and applied offsets (externalNote) — so a table of them reads
 * without a column of bare digits looking unsigned. Rounds first, then picks the sign from the rounded value, not the
 * raw one: a value that rounds to zero (e.g. −0.004 at 1 digit) reads +0.0, never −0.0. */
function signed(value, digits) {
  const abs = Math.abs(value).toFixed(digits);
  return `${value < 0 && Number(abs) !== 0 ? "−" : "+"}${abs}`;
}

/** The drawer's second trace link's label host, from `url` (the detail's `external_globe_url`, already the same
 * timeframe as `tar1090_url` — see approach_detail in web/detail.py): its hostname with a leading "globe." removed,
 * so the shipped `https://globe.adsb.lol/` reads "adsb.lol" for the drawer's "Open on adsb.lol" link, and pointing
 * `[external] globe_url` at another tar1090-based aggregator still reads right, whether or not that host happens to
 * start with "globe." too. Pure, so it needs no DOM to test (drawer.js calls it; the "Open on " wording is its own). */
export function externalGlobeLabel(url) {
  const host = new URL(url).hostname;
  return host.startsWith("globe.") ? host.slice("globe.".length) : host;
}

/** How many of an approach's `points` came from adsb.lol (their `origin`; a point without one is the receiver's), for
 * externalNote's `merged`. */
export function mergedPointCount(points) {
  return points.filter((p) => p.origin === "adsb.lol").length;
}

/** The drawer's one-line note on what adsb.lol added to an approach, from the detail's `external` (null without a
 * lookup row), `clock`, the page's local-time
 * formatter (app.js's own wrapper around clockText, the same one passed to bannerFor), and `merged`, how many of the
 * approach's points came from the live poller. Without a lookup row, an approach with merged
 * points reads "adsb.lol: 12 positions merged live" (singular for one) and any other gives null; a lookup row, which
 * only an approach from before the live poller has, is read as follows. A pending or retrying lookup gives its
 * due time. A finished one says what was added: the tail's points and the gaps filled with their points, singular
 * where each count is 1, "nothing to add" when neither added anything; then, once `offset_source` is set, where the
 * applied offset came from, signed at one decimal — "matches" names the exact matches, "along_track" gives the
 * offset and the seam's height offset as today, "default" adds nothing — appended even to "nothing to add". A lookup
 * finished before gap filling (no `offset_source`, migration 0003) reads exactly as it always did: the points added
 * and, once both seam offsets were measured, them, signed. A failed one says why, by its HTTP status when it has one
 * (a 404's own lookup, or another 4xx or 5xx that paused every lookup); an expired one says so. */
export function externalNote(external, clock, merged = 0) {
  if (external === null || external === undefined) {
    return merged > 0 ? `adsb.lol: ${merged} position${merged === 1 ? "" : "s"} merged live` : null;
  }
  const { status, due_at, http_status, points_added, gaps_filled, gap_points_added, offset_source, match_n,
    applied_offset_s, offset_s, offset_ft } = external;
  if (status === "pending" || status === "retry") return `adsb.lol: lookup at ${clock(due_at)}`;
  if (status === "done") {
    if (offset_source === null || offset_source === undefined) {
      if (points_added === 0) return "adsb.lol: nothing to add";
      let text = `adsb.lol: added ${points_added} point${points_added === 1 ? "" : "s"}`;
      if (typeof offset_s === "number" && typeof offset_ft === "number") {
        text += ` · ${signed(offset_s, 1)} s, ${signed(offset_ft, 0)} ft against your receiver`;
      }
      return text;
    }
    const p = points_added || 0, g = gaps_filled || 0, q = gap_points_added || 0;
    let added;
    if (p > 0 && q > 0) added = `added ${p} point${p === 1 ? "" : "s"}, filled ${g} gap${g === 1 ? "" : "s"} (+${q})`;
    else if (p > 0) added = `added ${p} point${p === 1 ? "" : "s"}`;
    else if (q > 0) added = `filled ${g} gap${g === 1 ? "" : "s"} (+${q} point${q === 1 ? "" : "s"})`;
    else added = "nothing to add";
    let offset = "";
    if (offset_source === "matches" && typeof applied_offset_s === "number" && typeof match_n === "number") {
      offset = ` · ${signed(applied_offset_s, 1)} s from ${match_n} matching points`;
    } else if (offset_source === "along_track" && typeof applied_offset_s === "number" && typeof offset_ft === "number") {
      offset = ` · ${signed(applied_offset_s, 1)} s, ${signed(offset_ft, 0)} ft against your receiver`;
    }
    return `adsb.lol: ${added}${offset}`;
  }
  if (status === "failed") {
    if (http_status === 404) return "adsb.lol: no trace";
    if (typeof http_status === "number" && http_status >= 400) return `adsb.lol: failed (HTTP ${http_status})`;
    return "adsb.lol: failed";
  }
  if (status === "expired") return "adsb.lol: expired unfetched";
  return null;
}

/** The drawer's norm line, naming what the open approach's gates were judged against: "norm:
 * B738 on 28R, 142 approaches" for a type key, "norm: airline on 28R, 96 approaches" for a class key, and "norm: all
 * aircraft on 30, 310 approaches" for `*` — singular for one approach. `null` without a `fleet` (the detail's
 * `fleet`, null for an approach with no key or one the latest rebuild no longer writes), so the drawer can hide the
 * line the same way it hides externalNote's. */
export function normLabel(fleet, runway) {
  if (!fleet) return null;
  const who = fleet.key_kind === "*" ? "all aircraft" : fleet.key;
  return `norm: ${who} on ${runway}, ${fleet.n} approach${fleet.n === 1 ? "" : "es"}`;
}

/** The banner for the latest health report (null when the server did not answer) and the stream's state, or null
 * when all is well: {level: "error" | "warning" | "info", text}. `clock` shows UNIX seconds as a local time. Every
 * other case takes precedence over the last one, an enabled poller paused after a refused poll:
 * a single failure doesn't show here, only a live pause. It names the HTTP status that started the pause, or, for a
 * pause without one, says so when the key file couldn't be read (the error starts "api_key_file"). */
export function bannerFor(health, streamState, clock) {
  if (health === null) return { level: "error", text: "The server is not answering." };
  if (!health.db.ok) return { level: "error", text: `The database cannot be read: ${health.db.error}` };
  const c = health.collector;
  if (c.state === "failed") {
    return { level: "error", text: `The collector stopped with an error at ${clock(c.ended_at)}: ${c.note}` };
  }
  if (c.state === "stopped") return { level: "warning", text: `The collector stopped at ${clock(c.ended_at)}.` };
  if (c.state === "none") return { level: "info", text: "No live collector has written to this database." };
  if (health.feed.state === "silent") {
    return { level: "warning",
      text: `Nothing recorded for ${Math.round(health.feed.quiet_s / 60)} min: the feed may be down, or no aircraft are landing.` };
  }
  if (streamState === "retrying") return { level: "warning", text: "Live updates lost: reconnecting." };
  if (health.external?.enabled && typeof health.external.paused_until === "number") {
    const { paused_status: status, paused_error: error } = health.external;
    const suffix = typeof status === "number" ? ` after HTTP ${status}`
      : typeof error === "string" && error.startsWith("api_key_file") ? ": the key file can't be read" : "";
    return { level: "warning", text: `adsb.lol polling paused until ${clock(health.external.paused_until)}${suffix}.` };
  }
  return null;
}

/** A health report's collector run id, or null when there isn't one to compare (no live run has written to this
 * database, or the report itself is unavailable). */
export function liveRunId(collector) {
  return typeof collector?.run_id === "number" ? collector.run_id : null;
}

/** Whether the live cards must be refetched: the collector has moved on to a run other than `lastRunId`, the one last
 * seen (null before the first health report). The collector's start-up sweep (store/db.py) closes a run's open
 * approaches without writing events, so a page left open through a restart keeps their cards until it notices the new
 * run. The very first run observed is not a change: start() already loaded its cards. */
export function liveRunChanged(lastRunId, runId) {
  return lastRunId !== null && runId !== null && runId !== lastRunId;
}

/** Milliseconds before reconnecting after `failures` failures in a row: 1 s doubling to 30 s, plus up to 20 % so that
 * pages cut off together do not all come back at once. */
export function backoffMs(failures, random = Math.random) {
  return Math.round(Math.min(30000, 1000 * 2 ** Math.max(0, failures)) * (1 + 0.2 * random()));
}

/** The page's live events. It opens api/events?after=<the last event id the page has>. On an error it closes the
 * EventSource, whose own retries neither back off nor survive a restarted server, and opens a new one after backoffMs,
 * again from the last event received, so nothing is missed or repeated. The server's `reload` event (the page is
 * ahead of the database or too far behind it) stops the stream and calls onReload. onState gets "connecting", "open"
 * or "retrying" (with the delay). The browser's globals are defaults, so tests can pass their own. */
export class EventStream {
  constructor(afterId, { onEvent, onReload, onState, EventSourceImpl = globalThis.EventSource,
    setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (timer) => clearTimeout(timer),
    random = Math.random } = {}) {
    Object.assign(this, { onEvent, onReload, onState, EventSourceImpl, setTimer, clearTimer, random });
    this.lastId = afterId;
    this.failures = 0;
    this.source = null;
    this.timer = null;
    this.stopped = true;
  }

  start() {
    this.stopped = false;
    this.open();
  }

  open() {
    this.timer = null;
    const source = new this.EventSourceImpl(this.lastId === null ? "api/events" : `api/events?after=${this.lastId}`);
    this.source = source;
    this.onState?.("connecting");
    source.onopen = () => {
      if (this.source !== source) return;
      this.failures = 0;
      this.onState?.("open");
    };
    source.onmessage = (message) => {
      if (this.source !== source) return;
      const event = JSON.parse(message.data);
      if (this.lastId !== null && event.id <= this.lastId) return;
      this.lastId = event.id;
      this.onEvent?.(event);
    };
    source.addEventListener("reload", () => {
      if (this.source !== source) return;
      this.stop();
      this.onReload?.();
    });
    source.onerror = () => {
      if (this.source !== source) return;
      source.close();
      this.source = null;
      if (this.stopped) return;
      const delay = backoffMs(this.failures, this.random);
      this.failures += 1;
      this.onState?.("retrying", delay);
      this.timer = this.setTimer(() => this.open(), delay);
    };
  }

  stop() {
    this.stopped = true;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    this.source?.close();
    this.source = null;
  }
}
