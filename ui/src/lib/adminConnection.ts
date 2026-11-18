import type { Connection } from "@gizmodata/chargewire";
import { useEffect, useState } from "react";

import { errorMessage, useSession } from "../session";
import { observerQueryTag } from "./queryTags";
import { actsAsSystemAdmin, elevateSql, reportingRefusals, useSystemAdminTabs } from "./systemAdmin";

/**
 * Who may use the admin pages on this server: the administrator, or a user while one of this
 * page's editor tabs acts as System Admin (`systemAdmin`: the page's own session then says SET
 * ROLE SYSTEM ADMIN too).
 */
export function useAdminAccess(): { allowed: boolean; systemAdmin: boolean } {
  const session = useSession();
  const elevated = useSystemAdminTabs(session.systemAdmin);
  const administrator = session.isAdmin && session.identity.fullAdmin !== false;
  const systemAdmin = !administrator && session.localServer && elevated;
  return { allowed: administrator || systemAdmin, systemAdmin };
}

/**
 * A separate observer session: never polls inside an editor's transaction or queue. For a
 * user whose tab acts as System Admin, it says SET ROLE SYSTEM ADMIN itself and confirms it
 * before anything else; it is closed (which ends that on the server) once no tab acts as one,
 * and a refusal as System Admin on it has every tab ask its own session again.
 * `systemAdminOnly`: opened only for that (a page the administrator uses through the UI's own
 * session).
 */
export function useAdminConnection(page: string, server = "", systemAdminOnly = false) {
  const session = useSession();
  const access = useAdminAccess();
  const [connection, setConnection] = useState<{ server: string; value: Connection }>();
  const [error, setError] = useState<{ server: string; message: string }>();
  const allowed = !!server || access.allowed;
  const systemAdmin = !server && access.systemAdmin;
  // systemAdminOnly: a page that reads through the UI's own session for the administrator
  // (Sessions) needs a session of its own only to act as System Admin.
  const opens = systemAdminOnly ? systemAdmin : allowed;
  useEffect(() => {
    if (!opens) return;
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
        let used = conn;
        if (systemAdmin) {
          used = reportingRefusals(conn, () => session.systemAdmin.recheck());
          await used.execute(elevateSql);
          if (!(await actsAsSystemAdmin(used)))
            throw new Error("This session could not act as System Admin: the server did not confirm it.");
        }
        // Only an admin can request this. Monitoring must remain usable when every
        // ordinary slot is occupied, even if admin_bypass_queue_default is false.
        await used.execute("SET hardcharger.bypass_queue = true");
        await used.execute("SET hardcharger.capture_query_profile = 'off'");
        if (controller.signal.aborted) return;
        setError(undefined);
        setConnection({ server, value: used });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && (server || !session.checkSignIn(err)))
          setError({ server, message: errorMessage(err) });
      });
    return () => {
      controller.abort();
      setConnection(undefined);
      void owned?.close().catch(() => {});
    };
  }, [session, opens, systemAdmin, page, server]);
  return {
    connection: connection?.server === server ? connection.value : undefined,
    error: error?.server === server ? error.message : undefined,
    allowed,
    systemAdmin,
  };
}
