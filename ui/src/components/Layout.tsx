import { Database, Info, LogOut, Moon, Sun, Users } from "lucide-react";
import { Suspense, lazy, useCallback, useState } from "react";

import type { Page } from "../lib/route";
import { loadModule } from "../lib/stale";
import type { Theme } from "../lib/theme";
import { useSession } from "../session";
import { AboutDialog } from "./AboutDialog";
import { ByGizmoData, Wordmark } from "./Brand";
import { ErrorBoundary } from "./ErrorBoundary";
import { SchemaBrowser } from "./SchemaBrowser";

// The pages are loaded when first shown (the query page carries the editor, which is most of
// the app's size). A file that is gone after an upgrade becomes a StaleBuildError, which the
// page's error boundary turns into reloads and, failing those, a "new version" notice.
const QueryPage = lazy(loadModule("query", () => import("./QueryPage")));
const SessionsPage = lazy(loadModule("sessions", () => import("./SessionsPage")));

interface Props {
  theme: Theme;
  page: Page;
  go: (page: Page) => void;
}

export function Layout({ theme, page, go }: Props) {
  const session = useSession();
  // The query page stays mounted once shown (its tabs, editors and results live there).
  const [visitedQuery, setVisitedQuery] = useState(page === "query");
  if (page === "query" && !visitedQuery) setVisitedQuery(true);
  const [about, setAbout] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(280);

  const startResize = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = sidebarWidth;
      const move = (ev: PointerEvent) =>
        setSidebarWidth(Math.max(200, Math.min(560, startWidth + ev.clientX - startX)));
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    [sidebarWidth],
  );

  const nav = (target: Page, label: string, icon: React.ReactNode) => (
    <button
      type="button"
      className={`nav-item${page === target ? " nav-item-active" : ""}`}
      aria-current={page === target ? "page" : undefined}
      onClick={() => go(target)}
    >
      {icon}
      {label}
    </button>
  );

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <a href="https://gizmodata.com" target="_blank" rel="noopener noreferrer" className="header-brand">
            <Wordmark size="header" />
          </a>
          <nav className="header-nav" aria-label="Pages">
            {nav("query", "Query", <Database size={15} />)}
            {nav("sessions", "Sessions", <Users size={15} />)}
          </nav>
        </div>
        <div className="header-right">
          <span className="chip" title="Server version" data-testid="header-version">
            {session.identity.version}
          </span>
          <span className="chip chip-user" title="Signed in as" data-testid="header-user">
            {session.identity.user}
            <span className="chip-role">{session.identity.role}</span>
          </span>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={theme.toggle}
            title={theme.name === "light" ? "Dark theme" : "Light theme"}
            aria-label={theme.name === "light" ? "Dark theme" : "Light theme"}
          >
            {theme.name === "light" ? <Moon size={16} /> : <Sun size={16} />}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={() => setAbout(true)}
            title="About this server"
            aria-label="About this server"
          >
            <Info size={16} />
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => session.signOut()} title="Sign out">
            <LogOut size={15} /> Sign out
          </button>
        </div>
      </header>
      <div className="app-body">
        <aside className="sidebar" style={{ width: sidebarWidth }} aria-label="Schema browser">
          <SchemaBrowser />
          <div className="sidebar-foot">
            <ByGizmoData />
          </div>
          <div
            className="sidebar-resizer"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize the schema browser"
            onPointerDown={startResize}
          />
        </aside>
        <main className="main">
          {visitedQuery && (
            <div className="page" hidden={page !== "query"}>
              <ErrorBoundary scope="page">
                <Suspense fallback={<div className="page-loading">Loading the editor…</div>}>
                  <QueryPage theme={theme.name} />
                </Suspense>
              </ErrorBoundary>
            </div>
          )}
          {page === "sessions" && (
            <div className="page">
              <ErrorBoundary scope="page" resetKey="sessions">
                <Suspense fallback={<div className="page-loading">Loading…</div>}>
                  <SessionsPage />
                </Suspense>
              </ErrorBoundary>
            </div>
          )}
          {page === "not-found" && (
            <div className="page error-page error-page-page">
              <h2>No such page</h2>
              <button type="button" className="btn btn-primary" onClick={() => go("query")}>
                Go to the query page
              </button>
            </div>
          )}
        </main>
      </div>
      {about && <AboutDialog onClose={() => setAbout(false)} />}
    </div>
  );
}
