// The upload worker: reads the file the user chose (inspect) and converts it to Arrow, one
// batch of a few megabytes at a time, each sent when the page asks for the next (so at most
// a batch or two is in memory). Parsing here, off the page's thread, keeps a large or
// malformed file from freezing the page; cancelling an upload ends the worker.

import { Table, tableToIPC } from "apache-arrow";

import { type Chunk, type Inspection, type ReadOptions, type Settings, UploadError, convert, inspect } from "./sources";
import type { OutputColumn } from "./types";
import type { ConvertContext } from "./values";

export type WorkerRequest =
  | { op: "inspect"; id: number; file: File; options: ReadOptions }
  | {
      op: "convert";
      id: number;
      file: File;
      settings: Settings;
      columns: OutputColumn[];
      context: ConvertContext;
    }
  | { op: "next"; id: number };

export type WorkerReply =
  | { id: number; type: "inspected"; inspection: Inspection }
  | { id: number; type: "chunk"; ipc: Uint8Array; rows: number; fraction: number }
  | { id: number; type: "done" }
  | { id: number; type: "error"; message: string };

const scope = self as unknown as {
  postMessage: (message: WorkerReply, transfer?: Transferable[]) => void;
  onmessage: ((e: MessageEvent<WorkerRequest>) => void) | null;
};

const running = new Map<number, AsyncGenerator<Chunk>>();

function fail(id: number, err: unknown): void {
  running.delete(id);
  const message =
    err instanceof UploadError
      ? err.message
      : `The file couldn't be read: ${err instanceof Error ? err.message : String(err)}`;
  scope.postMessage({ id, type: "error", message });
}

async function next(id: number): Promise<void> {
  const gen = running.get(id);
  if (!gen) return;
  try {
    const step = await gen.next();
    if (step.done) {
      running.delete(id);
      scope.postMessage({ id, type: "done" });
      return;
    }
    const { batch, fraction } = step.value;
    const ipc = tableToIPC(new Table(batch), "stream");
    scope.postMessage({ id, type: "chunk", ipc, rows: batch.numRows, fraction }, [ipc.buffer]);
  } catch (err) {
    fail(id, err);
  }
}

scope.onmessage = (e) => {
  const req = e.data;
  switch (req.op) {
    case "inspect":
      inspect(req.file, req.options).then(
        (inspection) => scope.postMessage({ id: req.id, type: "inspected", inspection }),
        (err: unknown) => fail(req.id, err),
      );
      return;
    case "convert":
      running.set(req.id, convert(req.file, req.settings, req.columns, req.context));
      void next(req.id);
      return;
    case "next":
      void next(req.id);
      return;
  }
};
