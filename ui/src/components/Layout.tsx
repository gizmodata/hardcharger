import { Braces, Database, Info, LogOut, Moon, PanelLeftClose, PanelLeftOpen, Sun, Users } from "lucide-react";
import { Suspense, lazy, useMemo, useState } from "react";

import { useActiveTab } from "../lib/activeTab";
import { listingsFor } from "../lib/catalogs";
import { useStoredSize } from "../lib/storedSize";
import type { Page } from "../lib/route";
import { loadModule } from "../lib/stale";
import type { Theme } from "../lib/theme";
import { useSession } from "../session";
import { AboutDialog } from "./AboutDialog";
import { ByGizmoData, TrademarkNotice, Wordmark } from "./Brand";
import { ErrorBoundary } from "./ErrorBoundary";
import { DataExplorer } from "./DataExplorer";
import { SavedQueriesPanel } from "./SavedQueriesPanel";
import { ResizeHandle } from "./ResizeHandle";

const panelSize = {
  key: "hardcharger-ui:side-panel-width",
  defaultPx: 288,
  minPx: 220,
  maxPx: () => Math.max(220, Math.round(window.innerWidth * 0.45)),
};
const panelHiddenKey = "hardcharger-ui:side-panel-hidden";

const panelTabKey = "hardcharger-ui:side-panel-tab";

function storedPanelTab(): "saved" | "data" {
  try {
    return window.localStorage.getItem(panelTabKey) === "saved" ? "saved" : "data";
  } catch {
    return "data";
  }
}

function storedHidden(): boolean {
  try {
    return window.localStorage.getItem(panelHiddenKey) === "1";
  } catch {
    return false;
  }
}

// The pages are loaded when first shown (the query page carries the editor, which is most of
// the app's size). A file that is gone after an upgrade becomes a StaleBuildError, which the
// page's error boundary turns into reloads and, failing those, a "new version" notice.
const QueryPage = lazy(loadModule("query", () => import("./QueryPage")));
const SessionsPage = lazy(loadModule("sessions", () => import("./SessionsPage")));
// The API page carries Swagger UI: loaded only when the page is first shown.
const ApiPage = lazy(loadModule("api", () => import("./ApiPage")));

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
  // So does the API page (Swagger UI has no way to unmount it, and keeps what was tried).
  const [visitedApi, setVisitedApi] = useState(page === "api");
  if (page === "api" && !visitedApi) setVisitedApi(true);
  const [about, setAbout] = useState(false);
  const [panelWidth, setPanelWidth, resetPanelWidth] = useStoredSize(panelSize);
  const [panelHidden, setPanelHidden] = useState(storedHidden);
  const togglePanel = () => {
    const next = !panelHidden;
    setPanelHidden(next);
    try {
      window.localStorage.setItem(panelHiddenKey, next ? "1" : "0");
    } catch {
      // not remembered
    }
  };
  const [panelTab, setPanelTab] = useState<"saved" | "data">(storedPanelTab);
  const choosePanelTab = (tab: "saved" | "data") => {
    setPanelTab(tab);
    try {
      window.localStorage.setItem(panelTabKey, tab);
    } catch {
      // not remembered
    }
  };
  // The schema browser lists what the active editor tab's role may see.
  const active = useActiveTab();
  const listings = useMemo(() => listingsFor(session, active.role), [session, active.role]);

  const nav = (target: Page, label: string, icon: React.ReactNode) => (
    <button
      type="button"
      className={`nav-item${page === target ? " nav-item-active" : ""}`}
      aria-current={page === target ? "page" : undefined}
      aria-label={label}
      onClick={() => go(target)}
    >
      {icon}
      <span className="nav-label">{label}</span>
    </button>
  );

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={togglePanel}
            aria-label={panelHidden ? "Show the side panel" : "Hide the side panel"}
            title={panelHidden ? "Show the side panel" : "Hide the side panel"}
            aria-expanded={!panelHidden}
            data-testid="side-panel-toggle"
          >
            {panelHidden ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
          </button>
          <a href="https://gizmodata.com" target="_blank" rel="noopener noreferrer" className="header-brand">
            <Wordmark size="header" />
          </a>
          <nav className="header-nav" aria-label="Pages">
            {nav("query", "Query", <Database size={15} />)}
            {nav("sessions", "Sessions", <Users size={15} />)}
            {nav("api", "API", <Braces size={15} />)}
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
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => session.signOut()}
            title="Sign out"
            aria-label="Sign out"
          >
            <LogOut size={15} /> <span className="nav-label">Sign out</span>
          </button>
        </div>
      </header>
      <div className="app-body">
        {!panelHidden && (
          <>
            <aside className="sidebar" style={{ width: panelWidth }} aria-label="Side panel" data-testid="side-panel">
              <div className="panel-tabs" role="tablist" aria-label="Side panel">
                {(
                  [
                    ["saved", "Saved queries"],
                    ["data", "Data"],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    role="tab"
                    aria-selected={panelTab === key}
                    className={`panel-tab${panelTab === key ? " panel-tab-active" : ""}`}
                    onClick={() => choosePanelTab(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="panel-body" hidden={panelTab !== "saved"}>
                <SavedQueriesPanel />
              </div>
              <div className="panel-body" hidden={panelTab !== "data"}>
                <DataExplorer cache={listings} />
              </div>
              <footer className="sidebar-foot">
                <ByGizmoData />
                <TrademarkNotice />
              </footer>
            </aside>
            <ResizeHandle
              direction="column"
              label="Resize the side panel"
              sizePx={panelWidth}
              minPx={panelSize.minPx}
              maxPx={panelSize.maxPx()}
              onResize={setPanelWidth}
              onReset={resetPanelWidth}
              testId="side-panel-resize"
            />
          </>
        )}
        <main className="app-main">
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
          {visitedApi && (
            <div className="page" hidden={page !== "api"}>
              <ErrorBoundary scope="page" resetKey="api">
                <Suspense fallback={<div className="page-loading">Loading…</div>}>
                  <ApiPage theme={theme.name} />
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
