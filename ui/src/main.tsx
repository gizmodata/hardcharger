import { createRoot } from "react-dom/client";

import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { isChunkLoadError } from "./lib/stale";
import "./styles/app.css";

createRoot(document.getElementById("root")!, {
  // A missing lazily loaded file is expected after an upgrade and handled (reloads, then a
  // notice); anything else an error boundary catches is a bug worth a console line.
  onCaughtError: (error, info) => {
    if (!isChunkLoadError(error)) console.error("Hardcharger UI:", error, info.componentStack);
  },
}).render(
  <ErrorBoundary scope="root">
    <App />
  </ErrorBoundary>,
);
