// The tests' fixtures. Every page fails its test on anything unexpected: a console error,
// an uncaught exception, a Content-Security-Policy violation, or an HTTP answer of 400 or
// more. A test that provokes an error on purpose declares it with `expectProblem`, and the
// test then also fails if that error never happened (so a negative test can't pass by
// accident).

import { type Page, test as base, expect } from "@playwright/test";

import { type HarnessState, harnessState } from "./harness.ts";

export interface ExpectedProblem {
  /** An HTTP status the page may receive... */
  status?: number;
  /** ...for a URL matching this. */
  url?: RegExp;
  /** A console error matching this. */
  console?: RegExp;
  /** At least once (default true). */
  required?: boolean;
}

interface Tracked extends ExpectedProblem {
  seen: number;
}

const expectations = new WeakMap<Page, Tracked[]>();

/** Declares an error the test provokes on purpose. */
export function expectProblem(page: Page, problem: ExpectedProblem): void {
  expectations.get(page)!.push({ required: true, ...problem, seen: 0 });
}

function allowedHttp(rules: Tracked[], status: number, url: string): boolean {
  const rule = rules.find((r) => r.status === status && (!r.url || r.url.test(url)));
  if (rule) rule.seen++;
  return rule !== undefined;
}

function allowedConsole(rules: Tracked[], text: string, url: string): boolean {
  // Browsers log every failed HTTP request; it is allowed when its answer was.
  const failed = /Failed to load resource: the server responded with a status of (\d+)/.exec(text);
  if (failed) {
    const status = Number(failed[1]);
    return rules.some((r) => r.status === status && (!r.url || r.url.test(url)));
  }
  const rule = rules.find((r) => r.console?.test(text));
  if (rule) rule.seen++;
  return rule !== undefined;
}

/**
 * Watches a page for anything unexpected (see above); the returned function asserts there
 * was nothing, and that every declared problem happened. The `page` fixture does this for
 * every test; a test that makes pages of its own calls it for them.
 */
export async function watchPage(page: Page): Promise<() => void> {
  const rules: Tracked[] = [];
  expectations.set(page, rules);
  const problems: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    if (!allowedConsole(rules, msg.text(), msg.location().url)) problems.push(`console error: ${msg.text()}`);
  });
  page.on("pageerror", (err) => {
    if (!allowedConsole(rules, err.message, "")) problems.push(`uncaught: ${err.message}`);
  });
  page.on("response", (resp) => {
    if (resp.status() < 400) return;
    if (!allowedHttp(rules, resp.status(), resp.url())) {
      problems.push(`HTTP ${resp.status()} for ${resp.request().method()} ${resp.url()}`);
    }
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      console.error(`CSP violation: ${e.violatedDirective} blocked ${e.blockedURI || "inline code"}`);
    });
  });
  return () => {
    expect(problems, "unexpected errors on the page").toEqual([]);
    const missing = rules.filter((r) => r.required && r.seen === 0 && (r.status !== undefined || r.console));
    expect(
      missing.map((r) => `${r.status ?? ""} ${r.url ?? ""} ${r.console ?? ""}`.trim()),
      "expected errors that never happened",
    ).toEqual([]);
  };
}

export const test = base.extend<{ harness: HarnessState }>({
  harness: async ({}, use) => use(harnessState()),
  baseURL: async ({}, use) => use(harnessState().url),
  page: async ({ page }, use) => {
    const check = await watchPage(page);
    await use(page);
    check();
  },
});

export { expect };

/** Signs in through the form and waits for the app (at `origin`, the shared server by default). */
export async function signIn(page: Page, who: { username: string; password: string }, origin = ""): Promise<void> {
  await page.goto(`${origin}/ui/`);
  await page.getByLabel("User name").fill(who.username);
  await page.getByLabel("Password").fill(who.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("header-user")).toContainText(who.username);
}

/** Replaces the active editor's text (Monaco) and puts the cursor at the end. */
export async function setSql(page: Page, sql: string): Promise<void> {
  const editor = page.locator(".editor-tab:not([hidden]) .monaco-editor").first();
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Delete");
  // insertText puts the text in as one edit (typing would trigger auto-closing quotes).
  await page.keyboard.insertText(sql);
}

export async function runSql(page: Page, sql: string): Promise<void> {
  await setSql(page, sql);
  await page.keyboard.press("ControlOrMeta+Enter");
}

/** Runs the statement (or the keys' command) in the shown tab and waits until the run is over. */
export async function runDone(page: Page, sql: string, keys = "ControlOrMeta+Enter"): Promise<void> {
  const tab = activeTab(page);
  const before = Number(await tab.getAttribute("data-runs"));
  await setSql(page, sql);
  await page.keyboard.press(keys);
  await expect.poll(async () => Number(await tab.getAttribute("data-runs"))).toBeGreaterThan(before);
}

/** The editor tabs' bar (its tabs are role="tab"; so are the side panel's and the result sets'). */
export function editorTabs(page: Page) {
  return page.getByRole("tablist", { name: "Editor tabs" });
}

/** The editor tab on show (each tab keeps its own editor, toolbar and results). */
export function activeTab(page: Page) {
  return page.locator(".editor-tab:not([hidden])");
}

/** Waits until the cursor is in the shown tab's editor (each tab focuses its editor when shown). */
export async function editorFocused(page: Page): Promise<void> {
  await expect(activeTab(page).locator(".monaco-editor.focused")).toBeVisible();
}

/** The grid's text: header names, then each row's cells. */
export async function gridRows(page: Page): Promise<string[][]> {
  const grid = activeTab(page).getByTestId("results-grid");
  await expect(grid).toBeVisible();
  return grid.evaluate((el) =>
    [...el.querySelectorAll('[role="row"]')]
      .sort((a, b) => Number(a.getAttribute("aria-rowindex")) - Number(b.getAttribute("aria-rowindex")))
      .map((row) =>
        [...row.querySelectorAll('[role="gridcell"], [role="columnheader"]:not(.grid-corner)')].map((c) => {
          const name = c.querySelector(".grid-head-name");
          return (name ?? c).textContent ?? "";
        }),
      ),
  );
}

// What the clipboard holds, read the way a person would: paste into a text box. (Granting
// clipboard permissions would test a path Safari never takes.)
export async function pasted(page: Page): Promise<string> {
  await page.evaluate(() => {
    document.getElementById("paste-target")?.remove();
    const t = document.createElement("textarea");
    t.id = "paste-target";
    t.setAttribute("aria-label", "paste target");
    document.body.appendChild(t);
  });
  const target = page.getByLabel("paste target");
  await target.focus();
  await page.keyboard.press("ControlOrMeta+V");
  await expect(target).not.toHaveValue("");
  const value = await target.inputValue();
  await page.evaluate(() => document.getElementById("paste-target")?.remove());
  return value;
}

/**
 * What a paste receives, as text and as HTML (what a spreadsheet takes the cells from): pasted
 * into a scratch element, so no clipboard permission is involved (Safari's path).
 */
export async function pastedBoth(page: Page): Promise<{ text: string; html: string }> {
  await page.evaluate(() => {
    const sink = document.createElement("div");
    sink.id = "paste-sink";
    sink.contentEditable = "true";
    sink.setAttribute("aria-label", "paste sink");
    sink.style.cssText = "position:fixed;right:0;bottom:0;width:20px;height:20px;opacity:0.01;z-index:2147483647";
    sink.addEventListener("paste", (e) => {
      (window as unknown as { pastedBoth: unknown }).pastedBoth = {
        text: e.clipboardData?.getData("text/plain") ?? "",
        html: e.clipboardData?.getData("text/html") ?? "",
      };
      e.preventDefault();
    });
    document.body.appendChild(sink);
    sink.focus();
  });
  await page.keyboard.press("ControlOrMeta+V");
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { pastedBoth?: unknown }).pastedBoth !== undefined))
    .toBe(true);
  const got = await page.evaluate(() => {
    document.getElementById("paste-sink")?.remove();
    const w = window as unknown as { pastedBoth?: { text: string; html: string } };
    const value = w.pastedBoth!;
    delete w.pastedBoth;
    return value;
  });
  return got;
}

/** The shown tab's editor text, as drawn (Monaco draws spaces as no-break spaces). */
export async function editorText(page: Page): Promise<string> {
  const lines = await activeTab(page)
    .locator(".view-line")
    .evaluateAll((els) =>
      els
        .map((el) => ({ top: parseFloat((el as HTMLElement).style.top || "0"), text: el.textContent ?? "" }))
        .sort((a, b) => a.top - b.top)
        .map((l) => l.text),
    );
  return lines.join("\n").replaceAll("\u00a0", " ");
}
