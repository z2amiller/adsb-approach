// DOM pieces the Approaches log (log.js) and the Find a flight page (search.js) both build rows from. Built with
// createElement/textContent only: flag names and path labels are API strings and never go through innerHTML.
import { attemptReferenceText, isEmergencyFlag } from "./logic.js";

/** A flag chip, <span class="flag">NAME</span>, marked data-emergency when `name` is an EMERGENCY_ flag (the
 * prefix is the whole rule, so MINIMUM_FUEL is a flag and not red). */
export function flagChip(name) {
  const span = document.createElement("span");
  span.className = "flag";
  span.textContent = name;
  span.toggleAttribute("data-emergency", isEmergencyFlag(name));
  return span;
}

/** An attempt's path chip, <span class="path-chip">Quiet Bridge → ILS</span> (attemptReferenceText), hidden while the
 * attempt has nothing to show, as a straight-in one does not. It carries the attempt's id like the attempt's button,
 * so that a click on it opens that attempt. */
export function pathChip(attempt) {
  const span = document.createElement("span");
  span.className = "path-chip";
  span.dataset.approach = String(attempt.approach_id);
  const text = attemptReferenceText(attempt);
  span.textContent = text;
  span.hidden = text === "";
  return span;
}
