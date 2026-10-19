import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { SavedItem } from "../lib/savedQueries";

interface Props {
  title: string;
  initialName: string;
  initialFolder: string | null;
  items: readonly SavedItem[];
  onSave: (name: string, folder: string | null) => Promise<void>;
  onClose: () => void;
}

/** Folders as select options, indented by depth, in tree order. */
function folderOptions(items: readonly SavedItem[]): { id: string; label: string }[] {
  const out: { id: string; label: string }[] = [];
  const walk = (parent: string | null, depth: number) => {
    const folders = items
      .filter((i) => i.kind === "folder" && i.parentId === parent)
      .sort((a, b) => a.name.localeCompare(b.name));
    for (const f of folders) {
      out.push({ id: f.id, label: `${"  ".repeat(depth)}${f.name}` });
      walk(f.id, depth + 1);
    }
  };
  walk(null, 1);
  return out;
}

/** Asks for a saved query's name and folder (Save the first time, and Save as…). */
export function SaveQueryDialog({ title, initialName, initialFolder, items, onSave, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(initialName);
  const [folder, setFolder] = useState<string>(initialFolder ?? "");
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const submit = async () => {
    setBusy(true);
    setError(undefined);
    try {
      await onSave(name, folder || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };
  return (
    <dialog ref={dialog} className="dialog" onClose={onClose} aria-label={title} data-testid="save-query-dialog">
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="dialog-body">
          <label className="field">
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} autoFocus aria-label="Name" />
          </label>
          <label className="field">
            <span>Folder</span>
            <select value={folder} onChange={(e) => setFolder(e.target.value)} aria-label="Folder">
              <option value="">Top level</option>
              {folderOptions(items).map((f) => (
                <option key={f.id} value={f.id}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
          <p className="inspector-meta">Saved queries are kept in this browser.</p>
        </div>
        <div className="dialog-foot">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            Save
          </button>
        </div>
      </form>
    </dialog>
  );
}
