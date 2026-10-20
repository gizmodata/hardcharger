// The page's side of the upload worker: inspect a file, then read its batches one at a time,
// the next one converted while the last one uploads. Stopping (cancel, or closing the dialog)
// ends the worker, and with it whatever it was reading.

import { type Table, tableFromIPC } from "apache-arrow";

import type { Inspection, ReadOptions, Settings } from "./sources";
import type { OutputColumn } from "./types";
import type { WorkerReply, WorkerRequest } from "./upload.worker";
import type { ConvertContext } from "./values";

export interface ReadChunk {
  table: Table;
  rows: number;
  /** IPC bytes of the batch. */
  bytes: number;
  fraction: number;
}

const crashed =
  "The browser stopped reading the file (it may be too large for this browser's memory). " +
  "For a large file, CSV or Parquet uploads in pieces.";

export class UploadReader {
  #worker: Worker | undefined;
  #nextId = 1;
  #handlers = new Map<number, (reply: WorkerReply) => void>();

  #ensure(): Worker {
    if (this.#worker) return this.#worker;
    const worker = new Worker(new URL("./upload.worker.ts", import.meta.url), { type: "module", name: "upload" });
    worker.onmessage = (e: MessageEvent<WorkerReply>) => this.#handlers.get(e.data.id)?.(e.data);
    worker.onerror = (e) => {
      e.preventDefault();
      this.#crash();
    };
    worker.onmessageerror = () => this.#crash();
    this.#worker = worker;
    return worker;
  }

  #crash(): void {
    for (const [id, handler] of this.#handlers) handler({ id, type: "error", message: crashed });
    this.#handlers.clear();
    this.stop();
  }

  #post(message: WorkerRequest): void {
    this.#ensure().postMessage(message);
  }

  /** Ends the worker (and what it was doing); the next call starts a new one. */
  stop(): void {
    this.#worker?.terminate();
    this.#worker = undefined;
    const pending = [...this.#handlers.entries()];
    this.#handlers.clear();
    for (const [id, handler] of pending) handler({ id, type: "error", message: "stopped" });
  }

  inspect(file: File, options: ReadOptions): Promise<Inspection> {
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      this.#handlers.set(id, (reply) => {
        this.#handlers.delete(id);
        if (reply.type === "inspected") resolve(reply.inspection);
        else if (reply.type === "error") reject(new Error(reply.message));
      });
      this.#post({ op: "inspect", id, file, options });
    });
  }

  /** The file's batches; one is converted ahead while the caller uploads the last. */
  async *chunks(
    file: File,
    settings: Settings,
    columns: OutputColumn[],
    context: ConvertContext,
  ): AsyncGenerator<ReadChunk> {
    const id = this.#nextId++;
    const ready: ReadChunk[] = [];
    let finished = false;
    let failure: Error | undefined;
    let wake: (() => void) | undefined;
    let requested = true;
    this.#handlers.set(id, (reply) => {
      if (reply.type === "chunk") {
        requested = false;
        ready.push({
          table: tableFromIPC(reply.ipc),
          rows: reply.rows,
          bytes: reply.ipc.byteLength,
          fraction: reply.fraction,
        });
      } else if (reply.type === "done") {
        finished = true;
        this.#handlers.delete(id);
      } else if (reply.type === "error") {
        failure = new Error(reply.message);
        this.#handlers.delete(id);
      }
      wake?.();
    });
    this.#post({ op: "convert", id, file, settings, columns, context });
    try {
      for (;;) {
        const chunk = ready.shift();
        if (chunk) {
          if (!requested && !finished) {
            requested = true;
            this.#post({ op: "next", id });
          }
          yield chunk;
          continue;
        }
        if (failure) throw failure;
        if (finished) return;
        await new Promise<void>((resolve) => (wake = resolve));
        wake = undefined;
      }
    } finally {
      this.#handlers.delete(id);
    }
  }
}
