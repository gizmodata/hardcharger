// Focus the page moves by itself (a tab shown, an editor ready) must not take it away from
// a field the person is typing in: a search typed while a new tab's editor was still getting
// ready went into the editor instead.

/** Whether the page may move focus to `target` now, given what has it (`active`). */
export function mayTakeFocus(active: Element | null, target: Element | null): boolean {
  if (!active || active === active.ownerDocument.body) return true;
  if (target && target.contains(active)) return true; // already inside: nothing to take
  // Another SQL editor (a tab hidden or closed) keeps the focus in its own text area; moving it
  // to the editor shown is what the page is for, not taking it from the person.
  if (active.closest(".editor-host")) return true;
  const typing =
    active instanceof HTMLInputElement ||
    active instanceof HTMLTextAreaElement ||
    active instanceof HTMLSelectElement ||
    (active instanceof HTMLElement &&
      (active.isContentEditable || active.closest('[contenteditable=""], [contenteditable="true"]') !== null));
  return !typing;
}
