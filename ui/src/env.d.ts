/** The web UI's version (the server's, from server/CMakeLists.txt), set by vite.config.ts. */
declare const __UI_VERSION__: string;
/** The year the UI was built (the copyright notice in About), set by vite.config.ts. */
declare const __BUILD_YEAR__: string;
/** A file Vite emits as an asset of its own (hashed, under /ui/assets/): its URL. */
declare module "*.json?url" {
  const url: string;
  export default url;
}
