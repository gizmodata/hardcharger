import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import logo from "../assets/gizmodata-logo.png";
import { TrademarkNotice } from "./Brand";
import { errorMessage, useSession } from "../session";

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
  const rows: [string, string][] = [
    ["Server", `${server.server?.name ?? ""} ${server.server?.version ?? ""}`.trim()],
    ["Protocol", `Warp SQL ${server.protocol_versions.join(", ")}`],
    ["Capabilities", (server.capabilities ?? []).join(", ") || "none"],
    ["Signed in as", `${identity.user} (${identity.role})`],
    ["Session", identity.sessionId],
    ["Instance", identity.instanceId ?? ""],
    ["Sign-in expires", new Date(auth.expiresAt).toLocaleString()],
    ["Web UI", __UI_VERSION__],
  ];
  if (info) {
    for (const [k, v] of Object.entries(info)) {
      if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") rows.push([k, String(v)]);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose} aria-labelledby="about-title">
      <div className="dialog-head">
        <img src={logo} alt="" width={28} height={30} />
        <h2 id="about-title">About this server</h2>
        <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
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
      {error && <p className="error-detail">{error}</p>}
      <div className="dialog-foot">
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
        <TrademarkNotice />
      </div>
    </dialog>
  );
}
