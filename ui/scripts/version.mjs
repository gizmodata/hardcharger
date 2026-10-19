// The UI's version: the server's (server/CMakeLists.txt), since the UI ships inside it and is
// built from this repository in the same CI run. Used by the build (__UI_VERSION__) and the
// release tarball's name. The full version, as the server reports it: the numbers of
// `project(hardcharger VERSION X.Y.Z ...)` and `set(HARDCHARGER_VERSION_SUFFIX "...")` after
// them ("" or "-rc.N"), so 0.3.0 or 2.0.0-rc.1 (.github/scripts/release.sh reads it the same way).
import { readFileSync } from "node:fs";

const RELEASE_VERSION = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.[1-9][0-9]*)?$/;

export function uiVersion(cmakeLists = new URL("../../server/CMakeLists.txt", import.meta.url)) {
  const cmake = readFileSync(cmakeLists, "utf8");
  const numbers = /^project\(hardcharger VERSION ([0-9.]+)/m.exec(cmake)?.[1];
  if (!numbers) throw new Error("no `project(hardcharger VERSION …)` in server/CMakeLists.txt");
  const suffix = /^set\(HARDCHARGER_VERSION_SUFFIX "([^"]*)"\)/m.exec(cmake)?.[1];
  if (suffix === undefined) throw new Error('no `set(HARDCHARGER_VERSION_SUFFIX "…")` in server/CMakeLists.txt');
  const version = numbers + suffix;
  if (!RELEASE_VERSION.test(version)) throw new Error(`"${version}" is not a release version (X.Y.Z or X.Y.Z-rc.N)`);
  return version;
}
