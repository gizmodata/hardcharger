import type { Connection } from "@gizmodata/chargewire";
import { useEffect, useState } from "react";

import { errorMessage, useSession } from "../session";
import { observerQueryTag } from "./queryTags";

/** A separate observer session: never polls inside an editor's transaction or queue. */
export function useAdminConnection(page: string, server = "") {
  const session = useSession();
  const [connection, setConnection] = useState<{ server: string; value: Connection }>();
  const [error, setError] = useState<{ server: string; message: string }>();
  const allowed = !!server || (session.isAdmin && session.identity.fullAdmin !== false);
  useEffect(() => {
    if (!allowed) return;
    const controller = new AbortController();
    let owned: Connection | undefined;
    session
      .openConnection({ server, signal: controller.signal, queryTag: observerQueryTag(page) })
      .then(async (conn) => {
        owned = conn;
        if (controller.signal.aborted) {
          await conn.close();
          return;
        }
        if (server) {
          const permission = await conn.query("SELECT HARDCHARGER_IS_FULL_ADMIN() AS full_admin");
          if (permission.get(0)?.toJSON()["full_admin"] !== true)
            throw new Error("Only unrestricted administrators can view query details on this server.");
        }
        // Only an admin can request this. Monitoring must remain usable when every
        // ordinary slot is occupied, even if admin_bypass_queue_default is false.
        await conn.execute("SET hardcharger.bypass_queue = true");
        await conn.execute("SET hardcharger.capture_query_profile = 'off'");
        if (!controller.signal.aborted) setConnection({ server, value: conn });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && (server || !session.checkSignIn(err)))
          setError({ server, message: errorMessage(err) });
      });
    return () => {
      controller.abort();
      void owned?.close().catch(() => {});
    };
  }, [session, allowed, page, server]);
  return {
    connection: connection?.server === server ? connection.value : undefined,
    error: error?.server === server ? error.message : undefined,
    allowed,
  };
}
