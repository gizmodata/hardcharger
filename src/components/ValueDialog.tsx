import { Copy, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { copyText } from "../lib/clipboard";

/** One value in full (a long string, a nested value), with a copy button. */
export function ValueDialog({ name, value, onClose }: { name: string; value: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog ref={dialog} className="dialog dialog-wide" onClose={onClose} aria-label={`Value of ${name}`}>
      <div className="dialog-head">
        <h2>{name}</h2>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => void copyText(value).then((ok) => setCopied(ok))}
        >
          <Copy size={14} /> {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <pre className="value-text">{value}</pre>
    </dialog>
  );
}
