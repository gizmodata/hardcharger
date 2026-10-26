import type { Connection, DiscoveryDocument } from "@gizmodata/warp-sql";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Layout } from "./components/Layout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ConfirmWorkspaceCloseDialog } from "./components/ConfirmWorkspaceCloseDialog";
import { SignInPage } from "./components/SignInPage";
import { type SignedIn, clearSignIn, loadSignIn, saveSignIn } from "./lib/auth";
import { endSessionsOnExit } from "./lib/pageExit";
import { useRoute } from "./lib/route";
import { useTheme } from "./lib/theme";
import { loadModule } from "./lib/stale";
import { type ConnectionOptions, discover, openConnection } from "./lib/warp";
import {
  type AppSession,
  type Identity,
  type QueryPageHandle,
  type QueryRequest,
  SessionContext,
  errorMessage,
  signInLost,
} from "./session";

type Phase =
  | { kind: "signed-out"; notice?: string }
  | { kind: "connecting"; auth: SignedIn }
  | { kind: "ready"; auth: SignedIn; connection: Connection; identity: Identity };

const QueryDetailPage = lazy(loadModule("query-detail", () => import("./components/QueryDetailPage")));

async function whoAmI(connection: Connection): Promise<Identity> {
  const table = await connection.query(
    "SELECT HARDCHARGER_USER() AS u, HARDCHARGER_ROLE() AS r, HARDCHARGER_VERSION() AS v, " +
      "HARDCHARGER_CURRENT_SESSION() AS s, HARDCHARGER_CURRENT_INSTANCE() AS i, " +
      "HARDCHARGER_IS_FULL_ADMIN() AS a, HARDCHARGER_INSTRUMENTATION_ENABLED() AS m",
  );
  const first: unknown = table.get(0)?.toJSON();
  const row = first as Record<string, string | boolean | null> | undefined;
  const text = (key: string): string => (typeof row?.[key] === "string" ? row[key] : "");
  return {
    user: text("u"),
    role: text("r"),
    version: text("v"),
    sessionId: text("s") || connection.sessionId,
    instanceId: text("i") || null,
    fullAdmin: row?.["a"] === true,
    instrumentationEnabled: row?.["m"] === true,
  };
}

function initialPhase(): Phase {
  const saved = loadSignIn();
  return saved ? { kind: "connecting", auth: saved } : { kind: "signed-out" };
}

export function App() {
  const [phase, setPhase] = useState<Phase>(initialPhase);
  const [server, setServer] = useState<DiscoveryDocument | undefined>();
  const [serverError, setServerError] = useState<string | undefined>();
  const [page, go] = useRoute();
  const theme = useTheme();
  // The query page while it is mounted, and requests made before it was.
  const queryPage = useRef<QueryPageHandle | undefined>(undefined);
  const pending = useRef<QueryRequest[]>([]);
  const extra = useRef(new Set<Connection>());
  const opening = useRef(new Set<Promise<Connection>>());
  const closingWindow = useRef(false);
  const [closeConfirmation, setCloseConfirmation] = useState<{ resolve: (close: boolean) => void }>();
  const current = useRef(phase);
  useEffect(() => {
    current.current = phase;
  });

  // Track the base connection too, including opens that are still in progress when a
  // native inspector is closed. Its webview must stay alive until those sessions end.
  const openExtraConnection = useCallback(async (auth: SignedIn, options?: ConnectionOptions) => {
    if (closingWindow.current) throw new Error("This window is closing.");
    const pending = openConnection(auth, options).then((c) => {
      extra.current.add(c);
      return c;
    });
    opening.current.add(pending);
    try {
      return await pending;
    } finally {
      opening.current.delete(pending);
    }
  }, []);

  // Closing or leaving the page ends its sessions on the server now, not at their idle timeout.
  useEffect(
    () =>
      endSessionsOnExit(
        () => {
          const now = current.current;
          if (now.kind !== "ready") return undefined;
          const open = [...new Set([now.connection, ...extra.current])].filter((c) => !c.closed);
          return { token: now.auth.token, ids: open.map((c) => c.sessionId) };
        },
        undefined,
        async () => {
          closingWindow.current = true;
          await Promise.allSettled([...opening.current]);
          await Promise.allSettled([...extra.current].map((c) => c.close()));
        },
        () => new Promise<boolean>((resolve) => setCloseConfirmation({ resolve })),
      ),
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    discover(controller.signal).then(setServer, (err: unknown) => {
      if (!controller.signal.aborted) setServerError(errorMessage(err));
    });
    return () => controller.abort();
  }, []);

  const signOut = useCallback((notice?: string) => {
    clearSignIn();
    setPhase((current) => {
      if (current.kind === "ready") void current.connection.close().catch(() => {});
      return notice === undefined ? { kind: "signed-out" } : { kind: "signed-out", notice };
    });
    for (const c of extra.current) void c.close().catch(() => {});
    extra.current.clear();
  }, []);

  // Connect with the saved (or just issued) token, and learn who we are.
  useEffect(() => {
    if (phase.kind !== "connecting") return;
    const controller = new AbortController();
    const { auth } = phase;
    void (async () => {
      let connection: Connection | undefined;
      try {
        connection = await openExtraConnection(auth, { signal: controller.signal });
        const identity = await whoAmI(connection);
        if (controller.signal.aborted) {
          void connection.close();
          return;
        }
        setPhase({ kind: "ready", auth, connection, identity });
      } catch (err) {
        void connection?.close().catch(() => {});
        if (controller.signal.aborted) return;
        clearSignIn();
        setPhase({ kind: "signed-out", notice: signInLost(err) ?? errorMessage(err) });
      }
    })();
    return () => controller.abort();
  }, [phase, openExtraConnection]);

  // The token's end: back to the sign-in page, before requests start failing.
  useEffect(() => {
    if (phase.kind !== "ready") return;
    const left = phase.auth.expiresAt - Date.now();
    const t = window.setTimeout(() => signOut("Your sign-in has expired. Sign in again."), Math.max(0, left));
    return () => window.clearTimeout(t);
  }, [phase, signOut]);

  const requestQuery = useCallback(
    (request: QueryRequest) => {
      go("query");
      if (queryPage.current) queryPage.current.open(request);
      else pending.current.push(request);
    },
    [go],
  );

  const insertText = useCallback((text: string) => queryPage.current?.insert(text), []);
  const pointTabAt = useCallback(
    (catalog: string, schema?: string) =>
      queryPage.current ? queryPage.current.pointTabAt(catalog, schema) : "Open the query page first.",
    [],
  );

  const registerQueryPage = useCallback((handle: QueryPageHandle) => {
    queryPage.current = handle;
    for (const r of pending.current.splice(0)) handle.open(r);
    return () => {
      if (queryPage.current === handle) queryPage.current = undefined;
    };
  }, []);

  const session = useMemo<AppSession | undefined>(() => {
    if (phase.kind !== "ready" || !server) return undefined;
    const { auth, connection, identity } = phase;
    return {
      auth,
      server,
      connection,
      identity,
      isAdmin: identity.role === "admin",
      openConnection: (options) => openExtraConnection(auth, options),
      signOut,
      checkSignIn: (err) => {
        const why = signInLost(err);
        if (why === undefined) return false;
        signOut(why);
        return true;
      },
      requestQuery,
      insertText,
      pointTabAt,
      registerQueryPage,
    };
  }, [phase, server, signOut, openExtraConnection, requestQuery, insertText, pointTabAt, registerQueryPage]);

  const closeDialog = closeConfirmation && (
    <ConfirmWorkspaceCloseDialog
      onAnswer={(close) => {
        closeConfirmation.resolve(close);
        setCloseConfirmation(undefined);
      }}
    />
  );

  // The session's functions read refs only when called (in handlers and effects), never while
  // rendering; the compiler's check can't tell them apart from a read of the ref itself.
  // eslint-disable-next-line react-hooks/refs
  if (session) {
    return (
      <SessionContext value={session}>
        {page === "query-detail" ? (
          <ErrorBoundary scope="page">
            <Suspense fallback={<p className="loading-line">Loading query details…</p>}>
              <QueryDetailPage theme={theme} />
            </Suspense>
          </ErrorBoundary>
        ) : (
          <Layout theme={theme} page={page} go={go} />
        )}
        {closeDialog}
      </SessionContext>
    );
  }
  return (
    <>
      <SignInPage
        server={server}
        serverError={serverError}
        busy={phase.kind !== "signed-out"}
        notice={phase.kind === "signed-out" ? phase.notice : undefined}
        theme={theme}
        onSignedIn={(auth) => {
          saveSignIn(auth);
          setPhase({ kind: "connecting", auth });
        }}
      />
      {closeDialog}
    </>
  );
}
