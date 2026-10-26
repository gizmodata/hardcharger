import {
  type LucideIcon,
  Ban,
  CheckCircle2,
  Clock3,
  Download,
  HelpCircle,
  LoaderCircle,
  TimerOff,
  XCircle,
} from "lucide-react";
import { statusLabel, type MonitoredQuery, type StatusDisplay } from "../lib/sqlMonitor";

const icons: Record<string, LucideIcon> = {
  queued: Clock3,
  executing: LoaderCircle,
  fetching: Download,
  success: CheckCircle2,
  error: XCircle,
  cancelled: Ban,
};

export function QueryStatus({
  query,
  display,
}: {
  query: Pick<MonitoredQuery, "status" | "cancel_reason">;
  display: StatusDisplay;
}) {
  const label = statusLabel(query);
  const timeout =
    query.status === "timeout" || query.cancel_reason === "timeout" || query.cancel_reason === "queue_timeout";
  const Icon = timeout ? TimerOff : (icons[query.status] ?? HelpCircle);
  return (
    <span
      className={`monitor-status monitor-status-${timeout ? "timeout" : query.status}${display === "icons" ? " monitor-status-icon" : ""}`}
      title={label}
    >
      {display === "icons" ? (
        <>
          <Icon size={17} role="img" aria-label={label} />
          <span className="monitor-status-description" aria-hidden="true">
            {label}
          </span>
        </>
      ) : (
        <>
          {query.status === "executing" && <LoaderCircle size={14} aria-hidden="true" />}
          {label}
        </>
      )}
    </span>
  );
}
