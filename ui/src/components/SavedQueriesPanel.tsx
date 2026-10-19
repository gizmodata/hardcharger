import { ChevronDown, ChevronRight, FileCode2, FileUp, Folder, FolderPlus, MoreHorizontal } from "lucide-react";
import { type DragEvent, useCallback, useEffect, useRef, useState } from "react";

import { saveFile } from "../lib/download";
import {
  type SavedItem,
  SavedQueryError,
  descendants,
  fileName,
  freeName,
  getSelectedFolder,
  onSavedQueriesChanged,
  savedQueriesChanged,
  savedQueryStore,
  setSelectedFolder,
} from "../lib/savedQueries";
import { zip } from "../lib/zip";
import { useSession } from "../session";
import { FloatingMenu, type MenuItem } from "./FloatingMenu";

const dragType = "application/x-hardcharger-saved-item";

/**
 * Saved queries, in folders, kept in this browser. Click a query to open it (or go to the tab
 * that has it); click a folder to select and open it. New folder, Import .sql files, and the
 * library's Export / Import (a file, to move them to another browser). Drag a query or folder
 * onto a folder, or onto Top level. Each row's … renames, downloads (.sql, or a folder's .zip)
 * or deletes.
 */
export function SavedQueriesPanel() {
  const session = useSession();
  const store = savedQueryStore(session.identity.user);
  const [items, setItems] = useState<SavedItem[] | undefined>();
  const [unavailable, setUnavailable] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(getSelectedFolder);
  const [renaming, setRenaming] = useState<string | undefined>();
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const [notice, setNotice] = useState<string | undefined>();
  const sqlFiles = useRef<HTMLInputElement>(null);
  const libraryFile = useRef<HTMLInputElement>(null);
  const dragged = useRef<string | undefined>(undefined);

  const reload = useCallback(() => {
    store.list().then(
      (list) => {
        setItems(list);
        setUnavailable(false);
      },
      () => setUnavailable(true),
    );
  }, [store]);
  useEffect(() => {
    reload();
    return onSavedQueriesChanged(reload);
  }, [reload]);

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(undefined), 3000);
    return () => window.clearTimeout(t);
  }, [notice]);

  const act = async (work: () => Promise<unknown>, done?: string) => {
    try {
      await work();
      savedQueriesChanged();
      if (done) setNotice(done);
    } catch (err) {
      setNotice(err instanceof SavedQueryError || err instanceof Error ? err.message : String(err));
      reload();
    }
  };

  const select = (id: string | null) => {
    setSelected(id);
    setSelectedFolder(id);
  };

  const newFolder = () =>
    void act(async () => {
      const list = items ?? [];
      const folder = await store.createFolder(freeName(list, "New folder", selected), selected);
      if (selected) setOpen((o) => new Set(o).add(selected));
      setRenaming(folder.id);
    });

  const importSql = async (files: FileList | null) => {
    if (!files?.length) return;
    const read = await Promise.all([...files].map(async (f) => ({ name: f.name, sql: await f.text() })));
    await act(
      () => store.importFiles(read, selected),
      `Imported ${read.length.toLocaleString()} ${read.length === 1 ? "query" : "queries"}.`,
    );
    if (selected) setOpen((o) => new Set(o).add(selected));
  };

  const exportLibrary = () =>
    void act(async () => {
      const text = await store.exportLibrary();
      saveFile(new Blob([text], { type: "application/json" }), "hardcharger-saved-queries.json");
    }, "Exported the saved queries.");

  const importLibrary = async (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    const text = await file.text();
    await act(async () => {
      const n = await store.importLibrary(text);
      setNotice(`Imported ${n.toLocaleString()} saved ${n === 1 ? "item" : "items"}.`);
    });
  };

  const download = (item: SavedItem) =>
    void act(async () => {
      if (item.kind === "query") {
        saveFile(new Blob([item.sql], { type: "application/sql" }), `${fileName(item.name)}.sql`);
        return;
      }
      const list = items ?? [];
      const pathOf = (id: string | null): string[] => {
        const parts: string[] = [];
        for (let p = id; p && p !== item.parentId; p = list.find((i) => i.id === p)?.parentId ?? null) {
          parts.unshift(fileName(list.find((i) => i.id === p)?.name ?? ""));
        }
        return parts;
      };
      const enc = new TextEncoder();
      const files = descendants(list, item.id)
        .map((id) => list.find((i) => i.id === id)!)
        .filter((i): i is Extract<SavedItem, { kind: "query" }> => i.kind === "query")
        .map((q) => ({ path: [...pathOf(q.parentId), `${fileName(q.name)}.sql`].join("/"), data: enc.encode(q.sql) }));
      saveFile(new Blob([(await zip(files)) as BlobPart], { type: "application/zip" }), `${fileName(item.name)}.zip`);
    });

  const remove = (item: SavedItem) => {
    const list = items ?? [];
    const inside = descendants(list, item.id).length - 1;
    const question =
      item.kind === "folder"
        ? `Delete the folder "${item.name}"${inside ? ` and the ${inside.toLocaleString()} ${inside === 1 ? "item" : "items"} in it` : ""}?`
        : `Delete the saved query "${item.name}"?`;
    if (!window.confirm(question)) return;
    void act(() => store.remove(item.id), "Deleted.");
  };

  const openQuery = (q: Extract<SavedItem, { kind: "query" }>) =>
    session.requestQuery({
      sql: q.sql,
      run: false,
      newTab: true,
      title: q.name,
      saved: { id: q.id, sql: q.sql },
      ...(q.catalog ? { catalog: q.catalog, schema: q.schema } : {}),
    });

  // Drag and drop: onto a folder, or onto Top level.
  const onDragStart = (e: DragEvent, id: string) => {
    dragged.current = id;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData(dragType, id);
  };
  const dropOn = (folder: string | null) => ({
    onDragOver: (e: DragEvent) => {
      if (dragged.current) e.preventDefault();
    },
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const id = dragged.current;
      dragged.current = undefined;
      if (id && id !== folder) {
        void act(() => store.move(id, folder));
        if (folder) setOpen((o) => new Set(o).add(folder));
      }
    },
  });

  const rows = (parent: string | null, depth: number) => {
    const list = (items ?? [])
      .filter((i) => i.parentId === parent)
      .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "folder" ? -1 : 1));
    return list.map((item) => {
      const isOpen = open.has(item.id);
      const label = item.name;
      return (
        <li
          key={item.id}
          role="treeitem"
          aria-label={label}
          aria-expanded={item.kind === "folder" ? isOpen : undefined}
          aria-selected={item.kind === "folder" ? selected === item.id : undefined}
        >
          <div
            className={`tree-row-wrap${selected === item.id ? " tree-found" : ""}`}
            {...(item.kind === "folder" ? dropOn(item.id) : {})}
            onContextMenu={(e) => {
              e.preventDefault();
              setMenu({ x: e.clientX, y: e.clientY, items: itemMenu(item) });
            }}
          >
            {renaming === item.id ? (
              <input
                className="tree-rename"
                aria-label="New name"
                defaultValue={item.name}
                autoFocus
                onFocus={(e) => e.currentTarget.select()}
                onBlur={(e) => {
                  const name = e.currentTarget.value;
                  setRenaming(undefined);
                  if (name.trim() && name !== item.name) void act(() => store.rename(item.id, name));
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                  if (e.key === "Escape") setRenaming(undefined);
                }}
              />
            ) : (
              <button
                type="button"
                className={`tree-row depth-${depth}`}
                draggable
                onDragStart={(e) => onDragStart(e, item.id)}
                onDragEnd={() => (dragged.current = undefined)}
                onClick={() => {
                  if (item.kind === "folder") {
                    select(item.id);
                    setOpen((o) => {
                      const next = new Set(o);
                      if (next.has(item.id) && selected === item.id) next.delete(item.id);
                      else next.add(item.id);
                      return next;
                    });
                  } else openQuery(item);
                }}
                title={item.kind === "query" ? `Open ${item.name}` : undefined}
              >
                {item.kind === "folder" ? (
                  isOpen ? (
                    <ChevronDown size={14} />
                  ) : (
                    <ChevronRight size={14} />
                  )
                ) : (
                  <span className="tree-spacer" />
                )}
                {item.kind === "folder" ? (
                  <Folder size={14} className="tree-icon" />
                ) : (
                  <FileCode2 size={14} className="tree-icon" />
                )}
                <span className="tree-name">{label}</span>
              </button>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm tree-action"
              aria-label={`More for ${label}`}
              onClick={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                setMenu({ x: r.left, y: r.bottom, items: itemMenu(item) });
              }}
            >
              <MoreHorizontal size={14} />
            </button>
          </div>
          {item.kind === "folder" && isOpen && <ul role="group">{rows(item.id, depth + 1)}</ul>}
        </li>
      );
    });
  };

  const itemMenu = (item: SavedItem): MenuItem[] => [
    { label: "Rename", run: () => setRenaming(item.id) },
    { label: item.kind === "folder" ? "Download as .zip" : "Download .sql", run: () => download(item) },
    { label: "Delete", run: () => remove(item) },
  ];

  return (
    <div className="saved-queries" data-testid="saved-queries">
      <div className="sidebar-head">
        <span>Saved queries</span>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={newFolder}
          disabled={unavailable}
          title="New folder (in the selected folder)"
          aria-label="New folder"
        >
          <FolderPlus size={14} />
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={() => sqlFiles.current?.click()}
          disabled={unavailable}
          title="Import .sql files (into the selected folder)"
          aria-label="Import .sql files"
        >
          <FileUp size={14} />
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          disabled={unavailable}
          aria-label="More for saved queries"
          onClick={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            setMenu({
              x: r.left,
              y: r.bottom,
              items: [
                { label: "Export all to a file", run: exportLibrary },
                { label: "Import from a file", run: () => libraryFile.current?.click() },
              ],
            });
          }}
        >
          <MoreHorizontal size={14} />
        </button>
        <input
          ref={sqlFiles}
          type="file"
          accept=".sql,text/plain,application/sql"
          multiple
          hidden
          data-testid="import-sql"
          onChange={(e) => {
            void importSql(e.currentTarget.files);
            e.currentTarget.value = "";
          }}
        />
        <input
          ref={libraryFile}
          type="file"
          accept=".json,application/json"
          hidden
          data-testid="import-library"
          onChange={(e) => {
            void importLibrary(e.currentTarget.files);
            e.currentTarget.value = "";
          }}
        />
      </div>
      {notice && (
        <p className="tree-notice" role="status" data-testid="saved-notice">
          {notice}
        </p>
      )}
      {unavailable ? (
        <p className="tree-note">
          This browser doesn&apos;t let the page keep saved queries (a private window, or blocked site data). Everything
          else works.
        </p>
      ) : (
        <ul className="tree" role="tree" aria-label="Saved queries">
          <li role="treeitem" aria-label="Top level" aria-selected={selected === null} aria-expanded>
            <div className={`tree-row-wrap${selected === null ? " tree-found" : ""}`} {...dropOn(null)}>
              <button type="button" className="tree-row depth-0 tree-root" onClick={() => select(null)}>
                <span className="tree-name">Top level</span>
              </button>
            </div>
            <ul role="group">{rows(null, 0)}</ul>
          </li>
          {items?.length === 0 && (
            <li className="tree-note">
              No saved queries yet. Save a tab&apos;s SQL with Ctrl/⌘+S. They are kept in this browser.
            </li>
          )}
        </ul>
      )}
      {!unavailable && (
        <p className="saved-hint" data-testid="saved-hint">
          Kept in this browser, not on the server. To use them in another browser, export them to a file (the ⋯ menu)
          and import it there.
        </p>
      )}
      {menu && <FloatingMenu x={menu.x} y={menu.y} items={menu.items} onClose={closeMenu} />}
    </div>
  );
}
