// Where a wait for a resuming server stands (Chargewire §11.2): a countdown to the next
// attempt and the time spent so far, redrawn every second.
import { useEffect, useState } from "react";

import { describeResume, type ResumeProgress } from "../lib/resuming";

/** The time, re-rendering every second. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** The full sentence, for the Add a server dialog. */
export function ResumeStatus({ progress }: { progress: ResumeProgress }) {
  const now = useNow();
  return (
    <p className="resume-status" role="status" aria-live="polite" data-testid="resume-status">
      {describeResume(progress, now).text}
    </p>
  );
}

/** A short form, for a server's row in the tree. */
export function ResumeBadge({ progress }: { progress: ResumeProgress }) {
  const now = useNow();
  const d = describeResume(progress, now);
  return (
    <span
      className="tree-type tree-server-status"
      data-status="resuming"
      title={d.text}
      data-testid="tree-server-resuming"
    >
      resuming… {d.sofar}
    </span>
  );
}
