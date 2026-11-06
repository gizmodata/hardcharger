import { ExternalLink } from "lucide-react";

import { queryDetailPath } from "../lib/route";
import { useSession } from "../session";

/** Only the recorded execution is opened; this never executes the SQL again. */
export function QueryDetailsLink({ executionId, server }: { executionId?: string; server: string }) {
  const session = useSession();
  if (
    !executionId ||
    (!server &&
      (!session.isAdmin || session.identity.fullAdmin === false || session.identity.instrumentationEnabled === false))
  )
    return null;
  return (
    <a
      className="btn btn-ghost btn-sm"
      href={queryDetailPath(executionId, server)}
      target={`hardcharger-query-${executionId}`}
      // Same-origin details need the browser's copy of sessionStorage to retain sign-in.
      // The details page immediately detaches its opener once mounted.
      rel="opener"
      title="Open this execution's SQL details in a separate window (requires an unrestricted administrator)"
      onClick={(event) => event.stopPropagation()}
    >
      <ExternalLink size={13} /> SQL details
    </a>
  );
}
