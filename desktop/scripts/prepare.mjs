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
// The bull's navy outline needs contrast in the Dock and taskbar. Render the existing
// artwork on a white tile for every desktop format, rather than using the kit's navy
// ICNS/ICO. Keep this composition in SVG so the background and padding are deterministic.
const iconDirectory = resolve(desktop, "src-tauri/icons");
const head = await readFile(resolve(brand, "standalone/head/hardcharger-head-1024.png"));
const iconSource = resolve(iconDirectory, "app-icon.svg");
await writeFile(iconSource, `<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" rx="224" fill="#ffffff"/>
  <image x="55" y="55" width="914" height="914" href="data:image/png;base64,${head.toString("base64")}"/>
</svg>\n`);
const icons = spawnSync(process.execPath, [
  resolve(desktop, "node_modules/@tauri-apps/cli/tauri.js"), "icon", iconSource, "--output", iconDirectory,
], { encoding: "utf8" });
if (icons.status !== 0) throw new Error(`Could not generate desktop icons: ${icons.stderr || icons.error || icons.stdout}`);
await cp(resolve(brand, "monochrome/white/hardcharger-32.png"), resolve(iconDirectory, "tray.png"));
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
