// Where the upload dialog lives: shown when something asks for it (requestUpload), loaded the
// first time it is (the readers, and SheetJS, load only when a file is read), and a file
// dragged onto the page offers to upload it.

import { FileUp } from "lucide-react";
import { Suspense, lazy, useEffect, useState } from "react";

import { currentActiveTab } from "../lib/activeTab";
import { closeUpload, requestUpload, useUploadRequest } from "../lib/upload/request";
import { loadModule } from "../lib/stale";
import "../styles/upload.css";

const UploadDialog = lazy(loadModule("upload", () => import("./UploadDialog")));

const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes("Files") ?? false;

export function UploadHost() {
  const { request, serial } = useUploadRequest();
  const [dragging, setDragging] = useState(false);

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
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
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
      if (!file) return;
      const tab = currentActiveTab();
      requestUpload({ catalog: tab.catalog, schema: tab.schema, role: tab.role, file });
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
          <div className="upload-drop-box">
            <FileUp size={28} />
            <strong>Drop the file to upload it into a table</strong>
            <span>CSV, TSV, Excel, Parquet, JSON or Arrow</span>
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
