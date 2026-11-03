// The maps (MapViews.tsx), loaded when first shown. A map that fails to draw says so in its
// place; a map file that is gone (a new build replaced it) goes on to the page's boundary,
// which reloads into the new version.
import { Component, type ComponentProps, type ReactNode, Suspense, lazy } from "react";

import { isChunkLoadError } from "../lib/stale";

const LazyResultsMap = lazy(() => import("./MapViews").then((m) => ({ default: m.ResultsMap })));
const LazyGeometryPreview = lazy(() => import("./MapViews").then((m) => ({ default: m.GeometryPreview })));

class MapBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  override state: { error: unknown } = { error: undefined };
  static getDerivedStateFromError(error: unknown) {
    return { error };
  }
  override render(): ReactNode {
    const { error } = this.state;
    if (error === undefined) return this.props.children;
    if (isChunkLoadError(error)) throw error as Error;
    return (
      <div className="geometry-map-failed" role="alert">
        The map couldn't be drawn: {error instanceof Error ? error.message : JSON.stringify(error)}
      </div>
    );
  }
}

const loading = <div className="geometry-map-loading">Loading the map…</div>;

export function ResultsMap(props: ComponentProps<typeof LazyResultsMap>) {
  return (
    <MapBoundary>
      <Suspense fallback={loading}>
        <LazyResultsMap {...props} />
      </Suspense>
    </MapBoundary>
  );
}

export function GeometryPreview(props: ComponentProps<typeof LazyGeometryPreview>) {
  return (
    <MapBoundary>
      <Suspense fallback={loading}>
        <LazyGeometryPreview {...props} />
      </Suspense>
    </MapBoundary>
  );
}
