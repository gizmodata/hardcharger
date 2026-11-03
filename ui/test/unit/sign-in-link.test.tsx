// A link to the web UI with ?sign_in=browser (GizmoData Cloud's Connect) starts the browser
// sign-in on its own, as if its button had been clicked: once, only when the server offers
// it, nobody is signed in and the page isn't the way back from a flow. The parameter leaves
// the address bar before anything starts. Each test loads the modules afresh, as a page load
// does (the parameter and the return fragment are read once per load).
import type { DiscoveryDocument } from "@gizmodata/chargewire";
import { act, cleanup, render, screen } from "@testing-library/react";
import { StrictMode, type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type SignInPageModule = typeof import("../../src/components/SignInPage");

async function freshPage(): Promise<SignInPageModule["SignInPage"]> {
  vi.resetModules();
  return (await import("../../src/components/SignInPage")).SignInPage;
}

const offering = {
  server: { name: "Hardcharger", version: "test" },
  capabilities: ["oauth_browser"],
  auth: { grant_types: ["password"], oauth_browser: { provider: "GizmoData Cloud" } },
} as unknown as DiscoveryDocument;

const notOffering = {
  server: { name: "Hardcharger", version: "test" },
  capabilities: [],
  auth: { grant_types: ["password"] },
} as unknown as DiscoveryDocument;

// The identity provider's address: this page with another fragment, which jsdom can navigate to.
const idpFragment = "#at-the-identity-provider";

let fetch: ReturnType<typeof vi.fn>;

function answerFlows(answer: () => Response): void {
  fetch = vi.fn(async () => answer());
  vi.stubGlobal("fetch", fetch);
}

function flowCreated(): Response {
  return Response.json(
    {
      flow_id: "f_abc",
      poll_secret: "s3cret",
      authorize_url: window.location.origin + window.location.pathname + window.location.search + idpFragment,
      poll_interval_s: 2,
      expires_at: "2026-11-03T21:00:00Z",
    },
    { status: 201 },
  );
}

function props(
  overrides: Partial<ComponentProps<SignInPageModule["SignInPage"]>>,
): ComponentProps<SignInPageModule["SignInPage"]> {
  return {
    server: offering,
    serverError: undefined,
    busy: false,
    notice: undefined,
    theme: { name: "light", toggle: () => {} },
    onSignedIn: () => {},
    ...overrides,
  };
}

/** Lets the page's effects and the (stubbed) flow request run to their end. */
async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await act(async () => {});
}

function flowRequests(): unknown[] {
  return fetch.mock.calls.filter(([url]) => String(url).endsWith("/v1/auth/oauth/flows"));
}

function browserButton(): HTMLButtonElement {
  return screen.getByTestId<HTMLButtonElement>("signin-browser");
}

beforeEach(() => {
  window.history.replaceState(null, "", "/ui/?sign_in=browser");
  answerFlows(flowCreated);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

describe("?sign_in=browser", () => {
  it("starts the browser sign-in once, after taking the parameter out of the address bar", async () => {
    window.history.replaceState(null, "", "/ui/sessions?x=1&sign_in=browser");
    const SignInPage = await freshPage();
    render(
      <StrictMode>
        <SignInPage {...props({})} />
      </StrictMode>,
    );
    await settle();
    expect(flowRequests()).toHaveLength(1);
    const [, init] = flowRequests()[0] as [string, RequestInit];
    // Started from the address without the parameter: the way back can't start it again.
    expect(JSON.parse(init.body as string)).toEqual({ return_to: "/ui/sessions?x=1" });
    expect(window.location.hash).toBe(idpFragment);
    expect(window.location.search).toBe("?x=1");
  });

  it("waits for the server's discovery document before deciding", async () => {
    const SignInPage = await freshPage();
    const view = render(<SignInPage {...props({ server: undefined })} />);
    await settle();
    expect(flowRequests()).toHaveLength(0);
    expect(window.location.search).toBe("");
    view.rerender(<SignInPage {...props({ server: offering })} />);
    await settle();
    expect(flowRequests()).toHaveLength(1);
    expect(window.location.hash).toBe(idpFragment);
  });

  it("is ignored when the server doesn't offer browser sign-in", async () => {
    const SignInPage = await freshPage();
    render(<SignInPage {...props({ server: notOffering })} />);
    await settle();
    expect(flowRequests()).toHaveLength(0);
    expect(screen.queryByTestId("signin-browser")).toBeNull();
    expect(screen.getByRole("button", { name: /^Sign in$/ })).toBeTruthy();
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/ui/");
  });

  it("is ignored, and removed silently, when someone is signed in already", async () => {
    const SignInPage = await freshPage();
    const view = render(<SignInPage {...props({ busy: true })} />);
    await settle();
    expect(flowRequests()).toHaveLength(0);
    expect(window.location.search).toBe("");
    // The saved sign-in turns out to be stale: the page offers sign-in, and waits for a click.
    view.rerender(<SignInPage {...props({ busy: false, notice: "Your sign-in has expired." })} />);
    await settle();
    expect(flowRequests()).toHaveLength(0);
    expect(browserButton().disabled).toBe(false);
    expect(window.location.hash).toBe("");
  });

  it("is not started again by a reload or a later sign-in page", async () => {
    const SignInPage = await freshPage();
    const first = render(<SignInPage {...props({})} />);
    await settle();
    expect(flowRequests()).toHaveLength(1);
    // Signed out again in the same page load: the sign-in page is back, without starting.
    first.unmount();
    window.history.replaceState(null, "", "/ui/");
    render(<SignInPage {...props({})} />);
    await settle();
    expect(flowRequests()).toHaveLength(1);
    cleanup();
    // A reload loads the address as the first page left it.
    expect(window.location.search).toBe("");
    const Reloaded = await freshPage();
    render(<Reloaded {...props({})} />);
    await settle();
    expect(flowRequests()).toHaveLength(1);
    expect(browserButton().disabled).toBe(false);
  });

  it("is not started on the way back from a flow", async () => {
    window.history.replaceState(null, "", "/ui/?sign_in=browser#chargewire_oauth_flow=f_abc");
    sessionStorage.setItem("hardcharger-ui:browser-sign-in", JSON.stringify({ flowId: "f_abc", pollSecret: "pss" }));
    answerFlows(() => Response.json({ state: "complete", access_token: "tok", token_type: "Bearer", expires_in: 60 }));
    const onSignedIn = vi.fn();
    const SignInPage = await freshPage();
    render(<SignInPage {...props({ onSignedIn })} />);
    await settle();
    expect(flowRequests()).toHaveLength(0);
    // The page collected the flow it came back with instead.
    expect(fetch.mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringMatching(/\/v1\/auth\/oauth\/flows\/f_abc$/),
    ]);
    expect(onSignedIn).toHaveBeenCalledWith(expect.objectContaining({ token: "tok" }));
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/ui/");
  });

  it("is not started on the way back from a flow, even when discovery answers after it", async () => {
    // Someone else's flow: this tab holds no poll secret for it, so the page ignores it and
    // is ready for a click again before the server's discovery document arrives.
    window.history.replaceState(null, "", "/ui/?sign_in=browser#chargewire_oauth_flow=f_someone_elses");
    const SignInPage = await freshPage();
    const view = render(<SignInPage {...props({ server: undefined })} />);
    await settle();
    view.rerender(<SignInPage {...props({ server: offering })} />);
    await settle();
    expect(fetch).not.toHaveBeenCalled();
    expect(browserButton().disabled).toBe(false);
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/ui/");
  });

  it("shows why it couldn't start, as a failed click does", async () => {
    answerFlows(() =>
      Response.json({ code: "not_implemented", detail: "browser sign-in is not configured" }, { status: 501 }),
    );
    const SignInPage = await freshPage();
    render(<SignInPage {...props({})} />);
    await settle();
    expect(flowRequests()).toHaveLength(1);
    expect(screen.getByTestId("signin-error").textContent).toBe("browser sign-in is not configured");
    expect(browserButton().disabled).toBe(false);
    expect(window.location.hash).toBe("");
    expect(window.location.search).toBe("");
  });

  it("is ignored with a value it doesn't know, which leaves the address bar too", async () => {
    window.history.replaceState(null, "", "/ui/?sign_in=okta&tab=2");
    const SignInPage = await freshPage();
    render(<SignInPage {...props({})} />);
    await settle();
    expect(flowRequests()).toHaveLength(0);
    expect(browserButton().disabled).toBe(false);
    expect(window.location.search).toBe("?tab=2");
  });
});
