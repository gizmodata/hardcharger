// Exercise the app's real WebView over its debug-only DevTools socket.
// Start the app first. No credentials or tokens are read or printed.
import { execFileSync } from 'node:child_process';
import { homedir } from 'node:os';
import assert from 'node:assert/strict';
const adb = `${process.env.ANDROID_HOME || `${homedir()}/Library/Android/sdk`}/platform-tools/adb`;
const serial = process.argv[2] || 'emulator-5554';
const port = process.argv[3] || '9223';
const pid = execFileSync(adb, ['-s', serial, 'shell', 'pidof', 'com.gizmodata.hardcharger'], { encoding: 'utf8' }).trim();
execFileSync(adb, ['-s', serial, 'forward', `tcp:${port}`, `localabstract:webview_devtools_remote_${pid}`]);
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = targets.find(t => t.url.includes('/ui/'));
assert(target, 'The signed-in web workspace must be open');
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
let sequence = 0;
const pending = new Map();
socket.onmessage = e => {
  const message = JSON.parse(e.data);
  if (message.id) {
    const callback = pending.get(message.id); pending.delete(message.id);
    if (message.error) callback.reject(new Error(JSON.stringify(message.error))); else callback.resolve(message.result);
  }
};
function send(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
try {
  assert(await evaluate('document.querySelector(".app-main") !== null'), 'Automatic sign-in loads the workspace');
  assert(await evaluate('parseFloat(getComputedStyle(document.documentElement).height) > 200'), 'WebView CSS viewport has a real height');
  await evaluate('document.querySelector("[aria-label=\\"Hide the side panel\\"]")?.click(); document.querySelector("[aria-label=\\"Hide this note\\"]")?.click(); document.querySelector("[aria-label=\\"SQL editor\\"]").focus(); true');
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2 });
  await send('Input.insertText', { text: 'SELECT 42 AS android_answer, \'Android workspace\' AS source;' });
  await evaluate('[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="Run").click(); true');
  let passed = false;
  for (let i = 0; i < 100; i++) {
    passed = await evaluate('[...document.querySelectorAll("[role=grid]")].some(g => g.innerText.includes("android_answer") && g.innerText.includes("42") && g.innerText.includes("Android workspace"))');
    if (passed) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert(passed, 'SQL result renders through the actual server and Arrow client');
  console.log(`${serial}: automatic sign-in, CSS viewport, editor input and SQL results passed`);

} finally { socket.close(); }
