import { X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { CopyrightNotice, TrademarkNotice, Wordmark } from "./Brand";
import { remainingServerInfo } from "../lib/serverInfo";
import { errorMessage, useSession } from "../session";

/**
 * About: the logo and the title stay at the top and the notices at the bottom; the details
 * between them scroll inside the dialog when the window is too short for all of it.
 */
export function AboutDialog({ onClose }: { onClose: () => void }) {
  const session = useSession();
  const dialog = useRef<HTMLDialogElement>(null);
  const [info, setInfo] = useState<Record<string, unknown> | undefined>();
  const [error, setError] = useState<string | undefined>();

  useEffect(() => {
    dialog.current?.showModal();
    const controller = new AbortController();
    session.connection.metadata.serverInfo({ signal: controller.signal }).then(setInfo, (err: unknown) => {
      if (controller.signal.aborted || session.checkSignIn(err)) return;
      setError(errorMessage(err));
    });
    return () => controller.abort();
  }, [session]);

  const { server, identity, auth } = session;
  const capabilities = server.capabilities ?? [];
  const rows: [string, ReactNode][] = [
    ["Server", `${server.server?.name ?? ""} ${server.server?.version ?? ""}`.trim()],
    ["Protocol", `Chargewire ${server.protocol_versions.join(", ")}`],
    [
      "Capabilities",
      capabilities.length ? (
        <ul className="about-caps" data-testid="about-capabilities">
          {capabilities.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      ) : (
        "none"
      ),
    ],
    ["Signed in as", `${identity.user} (${identity.role})`],
    ["Session", identity.sessionId],
    ["Instance", identity.instanceId ?? ""],
    ["Sign-in expires", new Date(auth.expiresAt).toLocaleString()],
    ["Web UI", __UI_VERSION__],
  ];
  const more = info ? remainingServerInfo(info) : [];

  return (
    <dialog ref={dialog} className="dialog dialog-about" onClose={onClose} aria-labelledby="about-title">
      <div className="about-top">
        <div className="about-brand" data-testid="about-brand">
          <Wordmark size="about" />
          <button type="button" className="btn btn-ghost btn-icon about-close" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="dialog-head">
          <h2 id="about-title">About this server</h2>
        </div>
      </div>
      <div className="about-body" data-testid="about-body">
        <table className="kv" data-testid="about-table">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}>
                <th scope="row">{k}</th>
                <td>{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {more.length > 0 && (
          <details className="about-more" data-testid="about-server-info">
            <summary>All server info</summary>
            <table className="kv">
              <tbody>
                {more.map(([k, v]) => (
                  <tr key={k}>
                    <th scope="row">{k}</th>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
        {error && <p className="error-detail">{error}</p>}
      </div>
      <div className="dialog-foot about-foot">
        <p>
          Hardcharger™ and its web UI are built by{" "}
          <a href="https://gizmodata.com" target="_blank" rel="noopener noreferrer">
            GizmoData™
          </a>
          .
        </p>
        <p>
          It includes open-source software under its own licences:{" "}
          <a
            href={`${import.meta.env.BASE_URL}third-party-notices.txt`}
            target="_blank"
            rel="noopener noreferrer"
            data-testid="about-licenses"
          >
            Open-source licences
          </a>{" "}
          (this web UI&rsquo;s; the server&rsquo;s are in its THIRD_PARTY_NOTICES.md, and{" "}
          <code>hardcharger-server --licenses</code> prints both).
        </p>
        <CopyrightNotice />
        <TrademarkNotice />
      </div>
    </dialog>
  );
}
