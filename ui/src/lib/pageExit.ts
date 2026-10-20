// Leaving the page (closing the tab, reloading, navigating away) ends its Warp SQL sessions
// on the server at once, instead of leaving them open until their idle timeout. The DELETEs
// (Warp SQL §5.4) are keepalive fetches, which the browser finishes after the page is gone;
// sendBeacon can't send the Authorization header they need.
//
// A page kept in the back/forward cache comes back with its sessions ended: it loads again,
// which starts new ones (and restores the tabs, kept in this browser).

import { warpBase } from "./warp";

export interface OpenSessions {
  token: string;
  /** The Warp SQL session ids the page holds open. */
  ids: string[];
}

/** Sends the DELETE of each session; returns how many it sent. */
export function endSessions(open: OpenSessions | undefined): number {
  if (!open) return 0;
  for (const id of open.ids) {
    fetch(`${warpBase()}/v1/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
      keepalive: true,
      headers: { Authorization: `Bearer ${open.token}` },
    }).catch(() => {});
  }
  return open.ids.length;
}

/** How many reasons the page has now to ask before it is left (warnBeforeLeaving). */
let warnings = 0;

/**
 * Has the browser ask before the page is closed or left (an open transaction would be lost),
 * until the returned function is called. While it asks, beforeunload leaves the sessions alone
 * (the user may stay); pagehide ends them if the user leaves.
 */
export function warnBeforeLeaving(): () => void {
  const ask = (e: BeforeUnloadEvent) => e.preventDefault();
  window.addEventListener("beforeunload", ask);
  warnings++;
  let on = true;
  return () => {
    if (!on) return;
    on = false;
    warnings--;
    window.removeEventListener("beforeunload", ask);
  };
}

/**
 * Ends the page's sessions when it is closed, reloaded or left (or put in the back/forward
 * cache), and loads the page again when it comes back from that cache. Returns the way to stop.
 *
 * pagehide is the event for this, but a browser may close a tab without it (WebKit closing a
 * page from automation does), so beforeunload ends them too, unless the page asks the user
 * first (warnBeforeLeaving): the user may stay, and then the sessions must still be there.
 */
export function endSessionsOnExit(open: () => OpenSessions | undefined, reload = () => location.reload()): () => void {
  // The sessions already ended (a navigation beforeunload saw may not happen after all, and
  // the page then goes on with new ones, which a later exit ends).
  const ended = new Set<string>();
  const exit = () => {
    const now = open();
    if (!now) return;
    const ids = now.ids.filter((id) => !ended.has(id));
    endSessions({ token: now.token, ids });
    for (const id of ids) ended.add(id);
  };
  const beforeUnload = () => {
    if (warnings === 0) exit();
  };
  const show = (e: PageTransitionEvent) => {
    if (e.persisted && ended.size > 0) reload();
  };
  window.addEventListener("beforeunload", beforeUnload);
  window.addEventListener("pagehide", exit);
  window.addEventListener("pageshow", show);
  return () => {
    window.removeEventListener("beforeunload", beforeUnload);
    window.removeEventListener("pagehide", exit);
    window.removeEventListener("pageshow", show);
  };
}
