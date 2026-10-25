import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";

// Structured arguments keep paths with spaces intact. The existing Azure signing helper
// is the sole signing implementation; no private key is downloaded to a Windows runner.
await writeFile("src-tauri/signing.generated.json", JSON.stringify({
  bundle: { windows: { signCommand: {
    cmd: "pwsh",
    args: ["-NoProfile", "-File", resolve("../.github/scripts/azure-sign.ps1"), "%1"],
  } } },
}, null, 2));
