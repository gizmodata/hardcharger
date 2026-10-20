import "../styles/api.css";

import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import swaggerCss from "swagger-ui-dist/swagger-ui.css?inline";
import SwaggerUIBundle from "swagger-ui-dist/swagger-ui-es-bundle.js";

import {
  NoOpenApiError,
  type OpenApiDocument,
  authorizeRequests,
  downloadBinaryResponses,
  loadOpenApi,
  sameOriginSpec,
} from "../lib/apiDocs";
import type { ThemeName } from "../lib/theme";
import { errorMessage, useSession } from "../session";
import swaggerFitCss from "../styles/swagger-shadow.css?inline";

// Swagger UI's stylesheet styles generic class names (.main, .info, .btn, .tab, ...), and the
// app's own stylesheets would style Swagger UI's elements. So Swagger UI lives in a shadow
// root with its stylesheet inside: nothing of it reaches the rest of the page (the editor's
// suggestion list included), and nothing of the page's reaches it. Its dark theme, written
// for html.dark-mode, follows the shadow host's class instead.
const shadowCss = `${swaggerCss.replace(/html\.dark-mode/g, ":host(.dark-mode)")}\n${swaggerFitCss}`;

/** The shadow root Swagger UI renders into, made once per host element. */
function swaggerRoot(host: HTMLElement): HTMLElement {
  const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
  let node = shadow.querySelector<HTMLElement>(".api-swagger-root");
  if (!node) {
    const style = document.createElement("style");
    style.textContent = shadowCss;
    node = document.createElement("div");
    node.className = "api-swagger-root";
    shadow.replaceChildren(style, node);
  }
  return node;
}

// Swagger UI without what this page decides itself: the server switcher (every request goes
// to this origin) and the Authorize dialogs (requests carry the user's own sign-in).
const hidden = () => () => null;
const sameOriginOnly = () => ({
  wrapComponents: {
    ServersContainer: hidden,
    authorizeBtn: hidden,
    authorizeOperationBtn: hidden,
  },
});

type State = { kind: "loading" } | { kind: "ready"; doc: OpenApiDocument } | { kind: "none" | "error"; why: string };

/**
 * The server's own API (GET /warp/v1/openapi.json), shown with Swagger UI. "Try it out"
 * sends requests to this server only, with the signed-in user's token added as each request
 * leaves; the token is never handed to Swagger UI, shown, or kept anywhere new.
 */
export default function ApiPage({ theme }: { theme: ThemeName }) {
  const session = useSession();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const host = useRef<HTMLDivElement>(null);
  // Read when a request is sent, so a request always carries the current sign-in.
  const token = useRef(session.auth.token);
  useEffect(() => {
    token.current = session.auth.token;
  }, [session.auth.token]);

  useEffect(() => {
    const controller = new AbortController();
    loadOpenApi(session.server, window.location.origin, controller.signal).then(
      (doc) => setState({ kind: "ready", doc }),
      (err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof NoOpenApiError) setState({ kind: "none", why: err.message });
        else setState({ kind: "error", why: errorMessage(err) });
      },
    );
    return () => controller.abort();
  }, [session.server, attempt]);

  useEffect(() => {
    if (state.kind !== "ready" || !host.current) return;
    const origin = window.location.origin;
    const node = swaggerRoot(host.current);
    SwaggerUIBundle({
      domNode: node,
      spec: sameOriginSpec(state.doc, origin),
      presets: [SwaggerUIBundle.presets.apis],
      plugins: [sameOriginOnly],
      layout: "BaseLayout",
      deepLinking: false,
      // Never configured from the page's URL (?url=, ?configUrl=), never calls out to a
      // validator on the Internet.
      queryConfigEnabled: false,
      validatorUrl: null,
      persistAuthorization: false,
      // The request as shown (Curl, Request URL) is the one before the token was added.
      showMutatedRequest: false,
      displayRequestDuration: true,
      filter: true,
      defaultModelsExpandDepth: 0,
      requestInterceptor: authorizeRequests(() => token.current, origin),
      responseInterceptor: downloadBinaryResponses,
    });
    return () => {
      node.replaceChildren();
    };
  }, [state]);

  // Swagger UI's own dark theme follows the app's (on the shadow host, not on <html>).
  useEffect(() => {
    host.current?.classList.toggle("dark-mode", theme === "dark");
  }, [theme, state]);

  return (
    <div className="api-page" data-testid="api-page">
      <div className="page-head">
        <h2>API</h2>
        <p className="page-sub">
          This server&apos;s Warp SQL API, from <code>/warp/v1/openapi.json</code>. <b>Try it out</b> sends requests to
          this server only, signed in as <b>{session.identity.user}</b>.
        </p>
      </div>
      {state.kind === "loading" && <div className="page-loading">Loading the API description…</div>}
      {(state.kind === "none" || state.kind === "error") && (
        <div className="api-missing" role="status" data-testid="api-missing">
          <p>
            {state.kind === "none"
              ? "This server doesn't publish a description of its API, so there is nothing to show here."
              : "The API description couldn't be loaded."}
          </p>
          <p className="page-sub">{state.why}</p>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              setState({ kind: "loading" });
              setAttempt((n) => n + 1);
            }}
          >
            <RefreshCw size={14} /> Retry
          </button>
        </div>
      )}
      {state.kind === "ready" && <div ref={host} className="api-swagger" data-testid="api-swagger" />}
    </div>
  );
}
