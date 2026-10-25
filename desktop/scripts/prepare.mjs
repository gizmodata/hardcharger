// Copy the existing brand kit, never redraw it. Native binaries must be supplied from a
// trusted local build or a verified release; this script never downloads executables.
import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(desktop, "..");
const brand = resolve(root, "docs/assets/brand");
await mkdir(resolve(desktop, "public/brand"), { recursive: true });
await mkdir(resolve(desktop, "src-tauri/icons"), { recursive: true });
for (const theme of ["light", "dark"]) {
  await cp(resolve(root, `ui/src/assets/brand/head-horizontal-${theme}.svg`), resolve(desktop, `public/brand/header-${theme}.svg`));
  await cp(resolve(root, `ui/src/assets/brand/mascot-stacked-${theme}.webp`), resolve(desktop, `public/brand/mascot-${theme}.webp`));
}
for (const [source, target] of [
  ["app-icons/Hardcharger-head.icns", "icon.icns"],
  ["app-icons/Hardcharger-head.ico", "icon.ico"],
  ["favicon/favicon-128.png", "icon.png"],
  ["monochrome/white/hardcharger-32.png", "tray.png"],
]) {
  await cp(resolve(brand, source), resolve(desktop, "src-tauri/icons", target));
}
// The desktop and native server report the same version.
const cmake = await readFile(resolve(root, "server/CMakeLists.txt"), "utf8");
const version = cmake.match(/project\(hardcharger VERSION ([\d.]+)/)[1] + cmake.match(/set\(HARDCHARGER_VERSION_SUFFIX "([^"]*)"\)/)[1];
const configPath = resolve(desktop, "src-tauri/tauri.conf.json");
const config = JSON.parse(await readFile(configPath, "utf8"));
config.version = version;
await writeFile(configPath, JSON.stringify(config, null, 2) + "\n");

const server = process.env.HARDCHARGER_DESKTOP_SERVER;
if (server) {
  const target = process.env.HARDCHARGER_DESKTOP_TARGET ?? spawnSync("rustc", ["-vV"], { encoding: "utf8" }).stdout.match(/^host: (.+)$/m)[1];
  const suffix = target.includes("windows") ? ".exe" : "";
  const client = process.env.HARDCHARGER_DESKTOP_CLIENT ?? resolve(dirname(resolve(server)), `hardcharger${suffix}`);
  await mkdir(resolve(desktop, "src-tauri/binaries"), { recursive: true });
  for (const [source, name] of [[server, "hardcharger-server"], [client, "hardcharger"]]) {
    await cp(resolve(source), resolve(desktop, `src-tauri/binaries/${name}-${target}${suffix}`));
  }
  console.log(`Staged server and CLI for ${target}.`);
}
console.log(`Prepared Hardcharger ${version} branding.`);
