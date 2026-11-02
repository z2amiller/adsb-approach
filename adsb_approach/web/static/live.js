// "On final now": one card per aircraft, kept current by the live event stream. Each card links to
// ?approach=<id> (keeping the rest of the URL) and opens the drawer on a plain click, the way a log row does;
// because it is a real link, middle-click and copying its address still work. Built with createElement and
// textContent only, never innerHTML: callsigns and other strings come off the air.
import { applyLiveEvent, cardSubText, fmt, fmtLateral, sortedCards, toSearch } from "./logic.js";

const VALUE_ROWS = [
  ["Distance", (c) => fmt(c.along_nm, 1, " nm"), () => false],
  ["Height", (c) => fmt(c.hat_ft, 0, " ft"), () => false],
  ["Glide path", (c) => `${fmt(c.gp_dev_ft, 0, " ft")} (${fmt(c.gp_dots, 1)})`, (c) => Math.abs(c.gp_dots ?? 0) >= 1],
  ["Localizer", (c) => fmtLateral(c.loc_dev_ft, c.loc_dots), (c) => Math.abs(c.loc_dots ?? 0) >= 1],
  ["Speed", (c) => fmt(c.gs, 0, " kt"), () => false],
  ["Sink", (c) => fmt(c.sink_fpm, 0, " fpm"), () => false],
];

/** `list` is `#live`, `empty` is `#live-empty`. `getState` reads the page's current view, so a card's link keeps its
 * filters, window and attention preset as it adds `?approach=<id>`, and whether the Helicopters and GA checkboxes
 * are on, for live events: /api/live is already filtered server-side, but the event stream is not -- `setConfig`
 * (called once, from /api/config, the way log.js's own setConfig is) supplies the GA type codes that filtering
 * needs. `onOpen` opens the drawer, as a log row's click does. A card's approach_id changes across a runway change
 * (it follows the newer attempt), so its link is refreshed on every update, not just when the card is created. */
export function createLive({ list, empty, getState, onOpen }) {
  const cards = new Map();
  const views = new Map();          // hex -> that card's elements, kept while its aircraft is on final
  let gaTypes = new Set();

  function setConfig(cfg) {
    gaTypes = new Set(cfg.ga_types ?? []);
  }

  function load(initialCards) {
    cards.clear();
    for (const card of initialCards) cards.set(card.hex, card);
    render();
  }

  function apply(event) {
    if (applyLiveEvent(cards, event, getState().rotorcraft, getState().ga, gaTypes)) render();
  }

  /** Cards change about once a second, so each keeps its elements and is updated in place: rebuilding them would take
   * keyboard focus off a card's link and lose a click pressed across an update. A card moves only when its place in
   * the order changes, and a focused card that moves gets its focus back. */
  function render() {
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    for (const [hex, view] of views) {
      if (!cards.has(hex)) {
        view.li.remove();
        views.delete(hex);
      }
    }
    sortedCards(cards).forEach((card, i) => {
      const view = views.get(card.hex) ?? cardView(card.hex);
      fill(view, card);
      if (list.children[i] !== view.li) list.insertBefore(view.li, list.children[i] ?? null);
    });
    if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    empty.hidden = cards.size > 0;
  }

  function cardView(hex) {
    const li = document.createElement("li");
    li.dataset.hex = hex;
    const link = document.createElement("a");
    link.className = "card";
    const heading = document.createElement("h3");
    const sub = document.createElement("div");
    sub.className = "sub";
    const dl = document.createElement("dl");
    const values = VALUE_ROWS.map(([label]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dl.append(dt, dd);
      return dd;
    });
    link.append(heading, sub, dl);
    li.append(link);
    const view = { li, link, heading, sub, values, approachId: null };
    link.addEventListener("click", (event) => {
      // a plain click opens the drawer in place; middle-click, ctrl/cmd/shift/alt-click and "open in new tab" all
      // fall through to the browser's own handling of a real link
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      onOpen(view.approachId);
    });
    views.set(hex, view);
    return view;
  }

  function fill(view, card) {
    view.approachId = card.approach_id;
    view.link.href = toSearch({ ...getState(), approach: card.approach_id });
    view.link.toggleAttribute("data-emergency", (card.emergency ?? []).length > 0);       // a red card
    setText(view.heading, [card.callsign || card.hex, card.type_code].filter(Boolean).join(" · "));
    setText(view.sub, cardSubText(card));
    VALUE_ROWS.forEach(([, value, warn], i) => {
      setText(view.values[i], value(card));
      view.values[i].toggleAttribute("data-warn", warn(card));
    });
  }

  return { load, apply, setConfig };
}

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}
