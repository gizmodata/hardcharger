import { useEffect, useRef } from "react";

/** Native window closes must ask before ending an open transaction's sessions. */
export function ConfirmWorkspaceCloseDialog({ onAnswer }: { onAnswer: (close: boolean) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-label="Close SQL workspace?"
      onCancel={(event) => {
        event.preventDefault();
        onAnswer(false);
      }}
    >
      <div className="dialog-head">
        <h2>Close SQL workspace?</h2>
      </div>
      <div className="dialog-body">
        <p>An open transaction will be rolled back when this window's sessions end.</p>
        <p>Cancel to keep working, or close the workspace and end its sessions.</p>
      </div>
      <div className="dialog-foot">
        <button type="button" className="btn btn-ghost" autoFocus onClick={() => onAnswer(false)}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={() => onAnswer(true)}>
          Close workspace
        </button>
      </div>
    </dialog>
  );
}
