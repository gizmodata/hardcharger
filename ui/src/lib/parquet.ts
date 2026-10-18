// Parquet export through parquet-wasm (Arrow -> Parquet in WebAssembly, every Arrow type
// the Rust writer knows). Loaded only when someone exports Parquet. WebAssembly needs
// 'wasm-unsafe-eval' in the page's script-src; nothing else does.

import { type Table, tableToIPC } from "apache-arrow";
import init, { Compression, Table as WasmTable, WriterPropertiesBuilder, writeParquet } from "parquet-wasm/esm";
import wasmUrl from "parquet-wasm/esm/parquet_wasm_bg.wasm?url";

let ready: Promise<unknown> | undefined;

export async function tableToParquet(table: Table): Promise<Uint8Array<ArrayBuffer>> {
  ready ??= init({ module_or_path: wasmUrl }).catch((err: unknown) => {
    ready = undefined;
    throw err;
  });
  await ready;
  const wasmTable = WasmTable.fromIPCStream(tableToIPC(table, "stream"));
  const properties = new WriterPropertiesBuilder().setCompression(Compression.ZSTD).build();
  const bytes = writeParquet(wasmTable, properties);
  // A copy on an ordinary ArrayBuffer (the wasm memory's view can't go into a Blob as is).
  const out = new Uint8Array(bytes.length);
  out.set(bytes);
  return out;
}
