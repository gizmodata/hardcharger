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

export const test = base.extend<{ harness: HarnessState }>({
  harness: async ({}, use) => use(harnessState()),
  baseURL: async ({}, use) => use(harnessState().url),
  page: async ({ page }, use) => {
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
    await use(page);
    expect(problems, "unexpected errors on the page").toEqual([]);
    const missing = rules.filter((r) => r.required && r.seen === 0 && (r.status !== undefined || r.console));
    expect(
      missing.map((r) => `${r.status ?? ""} ${r.url ?? ""} ${r.console ?? ""}`.trim()),
      "expected errors that never happened",
    ).toEqual([]);
  },
});

export { expect };

/** Signs in through the form and waits for the app. */
export async function signIn(page: Page, who: { username: string; password: string }): Promise<void> {
  await page.goto("/ui/");
  await page.getByLabel("User name").fill(who.username);
  await page.getByLabel("Password").fill(who.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("header-user")).toContainText(who.username);
}

/** Replaces the active editor's text (Monaco) and puts the cursor at the end. */
export async function setSql(page: Page, sql: string): Promise<void> {
  const editor = page.locator(".editor-slot:not([hidden]) .monaco-editor").first();
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

/** The grid's text: header names, then each row's cells. */
export async function gridRows(page: Page): Promise<string[][]> {
  const grid = page.getByTestId("results-grid");
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
