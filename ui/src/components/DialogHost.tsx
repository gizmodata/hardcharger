// Shows the page's confirm, prompt and notice dialogs (lib/dialogs.ts), one at a time, in the
// app's own dialog style. Enter confirms (the OK button has the focus, or the prompt's field),
// Esc cancels.
import { useEffect, useRef, useState } from "react";

import { type DialogRequest, settleDialog, useCurrentDialog } from "../lib/dialogs";

export function DialogHost() {
  const request = useCurrentDialog();
  if (!request) return null;
  return <OneDialog key={request.id} request={request} />;
}

function OneDialog({ request }: { request: DialogRequest }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const okButton = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState(request.kind === "prompt" ? request.initial : "");
  // The focus goes back where it was when the dialog closes (the editor, after Alt+W), as it
  // does after a native dialog. Taken while rendering: by the time effects run, the dialog's own
  // button has it.
  const [before] = useState(() => (document.activeElement instanceof HTMLElement ? document.activeElement : null));
  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    // showModal focuses the dialog's first control (Cancel): Enter must confirm, so the field
    // (a prompt) or the confirming button takes the focus.
    if (input.current) {
      input.current.focus();
      input.current.select();
    } else {
      okButton.current?.focus();
    }
    return () => {
      if (before?.isConnected) before.focus();
    };
  }, [before]);
  const cancel = () => settleDialog(request.id, request.kind === "confirm" ? false : undefined);
  const ok = () => settleDialog(request.id, request.kind === "prompt" ? value : true);
  const title =
    request.kind === "confirm"
      ? (request.options.title ?? "Are you sure?")
      : request.kind === "prompt"
        ? (request.options.title ?? request.label)
        : (request.title ?? "Notice");
  const okLabel =
    request.kind === "notify" ? "OK" : (request.options.okLabel ?? (request.kind === "prompt" ? "Save" : "OK"));
  const danger = request.kind === "confirm" && request.options.danger === true;
  return (
    <dialog
      ref={dialog}
      className="dialog dialog-app"
      aria-label={title}
      data-testid="app-dialog"
      data-kind={request.kind}
      onCancel={(e) => {
        e.preventDefault();
        cancel();
      }}
    >
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          ok();
        }}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
        </div>
        <div className="dialog-body">
          {request.kind === "prompt" ? (
            <label className="field">
              <span>{request.label}</span>
              <input
                value={value}
                ref={input}
                onChange={(e) => setValue(e.target.value)}
                aria-label={request.label}
                data-testid="app-dialog-input"
              />
            </label>
          ) : (
            <p className="app-dialog-message">{request.message}</p>
          )}
        </div>
        <div className="dialog-foot dialog-actions">
          {request.kind !== "notify" && (
            <button type="button" className="btn btn-ghost" onClick={cancel} data-testid="app-dialog-cancel">
              Cancel
            </button>
          )}
          <button
            type="submit"
            className={danger ? "btn btn-danger" : "btn btn-primary"}
            ref={okButton}
            data-testid="app-dialog-ok"
          >
            {okLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
