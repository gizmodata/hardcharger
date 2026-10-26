import { useEffect, useRef } from "react";

/** An in-page dialog also works in native webviews without window.confirm support. */
export function ConfirmSessionDialog({
  sessionId,
  username,
  end,
  onConfirm,
  onCancel,
}: {
  sessionId: string;
  username: string;
  end: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = end ? "End session?" : "Cancel session work?";
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
      </div>
      <div className="dialog-body">
        <p>
          <strong>{username}</strong> · <code>{sessionId}</code>
        </p>
        <p>
          {end
            ? "This disconnects the session and cancels all of its queued and in-flight work."
            : "This cancels all queued and in-flight work in this session. The session stays connected."}
        </p>
      </div>
      <div className="dialog-foot">
        <button type="button" className="btn btn-ghost" autoFocus onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={onConfirm}>
          {end ? "End session" : "Cancel work"}
        </button>
      </div>
    </dialog>
  );
}
