// The UI's version: the server's (server/CMakeLists.txt), since the UI ships inside it and is
// built from this repository in the same CI run. Used by the build (__UI_VERSION__) and the
// release tarball's name.
import { readFileSync } from "node:fs";

export function uiVersion() {
  const cmake = readFileSync(new URL("../../server/CMakeLists.txt", import.meta.url), "utf8");
  const version = /^project\(hardcharger VERSION ([0-9.]+)/m.exec(cmake)?.[1];
  if (!version) throw new Error("no `project(hardcharger VERSION …)` in server/CMakeLists.txt");
  return version;
}
