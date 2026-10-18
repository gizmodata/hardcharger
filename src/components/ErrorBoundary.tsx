import { Component, type ReactNode, useEffect, useRef, useState } from "react";

import { type Recovery, isChunkLoadError, recover, recovered } from "../lib/stale";

interface Props {
  /** "page": inside the layout (the header and sidebar stay usable); "root": the whole app. */
  scope: "page" | "root";
  children: ReactNode;
  /** Changing it clears the error (e.g. the route), so navigating away recovers. */
  resetKey?: string;
}

interface State {
  error: unknown;
  key: string | undefined;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: undefined, key: undefined };

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey !== state.key) return { key: props.resetKey, error: undefined };
    return null;
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error === undefined) return this.props.children;
    if (isChunkLoadError(error)) return <StaleBuild scope={this.props.scope} />;
    return (
      <div className={`error-page error-page-${this.props.scope}`} role="alert">
        <h2>Something went wrong</h2>
        <p className="error-detail">{error instanceof Error ? error.message : JSON.stringify(error)}</p>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            if (this.props.scope === "root") window.location.reload();
            else this.setState({ error: undefined });
          }}
        >
          {this.props.scope === "root" ? "Reload" : "Try again"}
        </button>
      </div>
    );
  }
}

/** A lazily loaded file is gone: reload (0, 3, 10 s), then say a new version is out. */
function StaleBuild({ scope }: { scope: "page" | "root" }) {
  const [state, setState] = useState<Recovery | undefined>(undefined);
  const [seconds, setSeconds] = useState(0);
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const next = recover();
    setState(next);
    setSeconds(next.kind === "reloading" ? Math.ceil(next.delayMs / 1000) : 0);
  }, []);
  useEffect(() => {
    if (state?.kind !== "reloading" || seconds <= 0) return;
    const t = window.setTimeout(() => setSeconds((s) => s - 1), 1000);
    return () => window.clearTimeout(t);
  }, [state, seconds]);
  if (state === undefined) return null;
  return (
    <div className={`error-page error-page-${scope}`} role="alert" data-testid="stale-build">
      {state.kind === "reloading" ? (
        <>
          <h2>Loading the latest version…</h2>
          <p>
            Part of this page could not be loaded; the server may be upgrading. Reloading
            {seconds > 0 ? ` in ${seconds} s` : ""} (attempt {state.attempt} of 3).
          </p>
        </>
      ) : (
        <>
          <h2>A new version of Hardcharger UI is available</h2>
          <p>This page's files changed on the server. Reload to use the new version.</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              recovered();
              window.location.reload();
            }}
          >
            Reload
          </button>
        </>
      )}
    </div>
  );
}
