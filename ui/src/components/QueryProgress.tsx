import type { StatementDocument } from "@gizmodata/chargewire";
import { Server } from "lucide-react";

export function QueryProgress({
  document,
  rows,
  receiving,
  cancelling,
}: {
  document?: StatementDocument;
  rows: number;
  receiving?: boolean;
  cancelling?: boolean;
}) {
  const estimate = document?.progress?.fraction;
  const fraction =
    typeof estimate === "number" && Number.isFinite(estimate) && estimate >= 0 && estimate <= 1 ? estimate : undefined;
  const phase = cancelling
    ? "Cancelling…"
    : document?.state === "queued"
      ? "Queued…"
      : receiving || document?.state === "succeeded"
        ? "Receiving results…"
        : "Executing query…";
  return (
    <div className="query-progress" data-testid="query-progress">
      <div className="query-progress-heading">
        <Server size={18} aria-hidden="true" />
        <strong>{phase}</strong>
        {fraction !== undefined && (
          <span>
            {Math.floor(fraction * 100)}% <small>(estimated)</small>
          </span>
        )}
      </div>
      <progress aria-label="Estimated query progress" max={1} value={fraction} />
      {rows > 0 && <p>{rows.toLocaleString()} rows received</p>}
    </div>
  );
}
