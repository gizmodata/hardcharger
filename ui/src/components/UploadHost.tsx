// Where the upload dialog lives: shown when something asks for it (requestUpload), loaded the
// first time it is (the readers, and SheetJS, load only when a file is read), and a file
// dragged onto the page offers to upload it.

import { FileUp } from "lucide-react";
import { Suspense, lazy, useEffect, useRef, useState } from "react";

import { currentActiveTab, useActiveTab } from "../lib/activeTab";
import { useServers } from "../lib/servers";
import { uploadUnavailable } from "../lib/upload/availability";
import { closeUpload, requestUpload, useUploadRequest } from "../lib/upload/request";
import { readOnlyDefault, useSession } from "../session";
import { loadModule } from "../lib/stale";
import "../styles/upload.css";

const UploadDialog = lazy(loadModule("upload", () => import("./UploadDialog")));

const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes("Files") ?? false;

export function UploadHost() {
  const { request, serial } = useUploadRequest();
  const [dragging, setDragging] = useState(false);
  const app = useSession();
  const snapshot = useServers(app.servers);
  const active = useActiveTab();
  // A file dropped on a read-only tab, or on a tab whose connection opens read-only sessions,
  // can't be uploaded: the overlay says so, and the drop opens nothing.
  const off = uploadUnavailable({
    connectionReadOnly: readOnlyDefault(
      app,
      active.server,
      snapshot.servers.find((s) => s.saved.id === active.server)?.saved,
    ),
    tabReadOnly: active.readOnly === true,
  });
  const offRef = useRef(off);
  useEffect(() => {
    offRef.current = off;
  });

  useEffect(() => {
    let depth = 0;
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      setDragging(true);
    };
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = offRef.current ? "none" : "copy";
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      depth = 0;
      setDragging(false);
      const file = e.dataTransfer?.files[0];
      if (!file || offRef.current) return;
      const tab = currentActiveTab();
      requestUpload({
        catalog: tab.catalog,
        schema: tab.schema,
        role: tab.role,
        server: tab.server,
        file,
        ...(tab.readOnly ? { readOnly: true } : {}),
      });
    };
    // Capture: ahead of the editor's own drop handling, which would paste a file's name.
    window.addEventListener("dragenter", enter, true);
    window.addEventListener("dragover", over, true);
    window.addEventListener("dragleave", leave, true);
    window.addEventListener("drop", drop, true);
    return () => {
      window.removeEventListener("dragenter", enter, true);
      window.removeEventListener("dragover", over, true);
      window.removeEventListener("dragleave", leave, true);
      window.removeEventListener("drop", drop, true);
    };
  }, []);

  return (
    <>
      {dragging && !request && (
        <div className="upload-drop" data-testid="upload-drop-overlay" aria-hidden>
          <div className="upload-drop-box" data-off={off ? "" : undefined}>
            <FileUp size={28} />
            {off ? (
              <>
                <strong data-testid="upload-drop-off">{off}</strong>
                <span>Dropping the file does nothing.</span>
              </>
            ) : (
              <>
                <strong>Drop the file to upload it into a table</strong>
                <span>CSV, TSV, Excel, Parquet, JSON or Arrow</span>
              </>
            )}
          </div>
        </div>
      )}
      {request && (
        <Suspense fallback={null}>
          <UploadDialog request={request} serial={serial} onClose={closeUpload} />
        </Suspense>
      )}
    </>
  );
}
