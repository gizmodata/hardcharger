// The app's few pages, as paths under its base (/ui/). The server answers every path under
// /ui/ with index.html, so a page's URL can be reloaded or shared.

import { useCallback, useEffect, useState } from "react";

export type Page = "query" | "sessions" | "api" | "monitor" | "query-detail" | "configuration" | "not-found";

const base = import.meta.env.BASE_URL.replace(/\/$/, "");

export function pageOf(pathname: string): Page {
  const rest = pathname.startsWith(base) ? pathname.slice(base.length) : pathname;
  const path = rest.replace(/\/+$/, "");
  if (path === "" || path === "/query") return "query";
  if (path === "/sessions") return "sessions";
  if (path === "/monitor") return "monitor";
  if (path === "/monitor/query") return "query-detail";
  if (path === "/configuration") return "configuration";
  if (path === "/api") return "api";
  return "not-found";
}

export function pathOf(page: Page): string {
  if (page === "query-detail") return `${base}/monitor/query`;
  if (page === "sessions" || page === "api" || page === "monitor" || page === "configuration") return `${base}/${page}`;
  return `${base}/`;
}

export function queryDetailPath(executionId: string): string {
  return `${pathOf("query-detail")}?execution=${encodeURIComponent(executionId)}`;
}

export function useRoute(): [Page, (page: Page) => void] {
  const [page, setPage] = useState<Page>(() => pageOf(window.location.pathname));
  useEffect(() => {
    const onPop = () => setPage(pageOf(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const go = useCallback((next: Page) => {
    const path = pathOf(next);
    if (window.location.pathname !== path) window.history.pushState(null, "", path);
    setPage(next);
  }, []);
  return [page, go];
}
