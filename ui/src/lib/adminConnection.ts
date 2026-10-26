import type { Connection } from "@gizmodata/warp-sql";
import { useEffect, useState } from "react";

import { errorMessage, useSession } from "../session";

/** A separate observer session: never polls inside an editor's transaction or queue. */
export function useAdminConnection(page: string) {
  const session = useSession();
  const [connection, setConnection] = useState<Connection>();
  const [error, setError] = useState<string>();
  const allowed = session.isAdmin && session.identity.fullAdmin !== false;
  useEffect(() => {
    if (!allowed) return;
    const controller = new AbortController();
    let owned: Connection | undefined;
    session
      .openConnection({ signal: controller.signal, queryTag: { page } })
      .then(async (conn) => {
        owned = conn;
        if (controller.signal.aborted) {
          await conn.close();
          return;
        }
        // Only an admin can request this. Monitoring must remain usable when every
        // ordinary slot is occupied, even if admin_bypass_queue_default is false.
        await conn.execute("SET hardcharger.bypass_queue = true");
        if (!controller.signal.aborted) setConnection(conn);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && !session.checkSignIn(err)) setError(errorMessage(err));
      });
    return () => {
      controller.abort();
      void owned?.close().catch(() => {});
    };
  }, [session, allowed, page]);
  return { connection, error, allowed };
}
