// "Start sessions read-only" on the sign-in page, and the ?read_only=1 link that ticks it (#61).
// The choice rides on the sign-in (this tab's sessionStorage, like the token), survives the
// browser sign-in's round trip through the identity provider in the flow this tab kept, and is
// never applied without the box showing it ticked. The parameter is read once per page load and
// leaves the address bar at once. Each test loads the modules afresh, as a page load does.
import type { DiscoveryDocument } from "@gizmodata/chargewire";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { lastReadOnly, loadSignIn, parseReadOnlyParam, rememberReadOnly, saveSignIn } from "../../src/lib/auth";

type SignInPageModule = typeof import("../../src/components/SignInPage");
type AuthModule = typeof import("../../src/lib/auth");

async function freshPage(): Promise<SignInPageModule["SignInPage"]> {
  vi.resetModules();
  return (await import("../../src/components/SignInPage")).SignInPage;
}

async function freshAuth(): Promise<AuthModule> {
  vi.resetModules();
  return import("../../src/lib/auth");
}

const server = {
  server: { name: "Hardcharger", version: "test" },
  capabilities: ["oauth_browser"],
  auth: { grant_types: ["password", "client_certificate"], oauth_browser: { provider: "Mock IdP" } },
} as unknown as DiscoveryDocument;

const idpFragment = "#at-the-identity-provider";

let fetch: ReturnType<typeof vi.fn>;

function answer(respond: (url: string, init: RequestInit) => Response): void {
  fetch = vi.fn(async (url: string, init: RequestInit) => respond(url, init));
  vi.stubGlobal("fetch", fetch);
}

function tokenOrFlow(url: string): Response {
  if (url.endsWith("/v1/auth/token"))
    return Response.json({ access_token: "tok", token_type: "Bearer", expires_in: 60 });
  if (url.endsWith("/v1/auth/oauth/flows")) {
    return Response.json(
      {
        flow_id: "f_ro",
        poll_secret: "s3cret",
        authorize_url: window.location.origin + window.location.pathname + window.location.search + idpFragment,
      },
      { status: 201 },
    );
  }
  return Response.json({ state: "complete", access_token: "tok", token_type: "Bearer", expires_in: 60 });
}

function props(
  overrides: Partial<ComponentProps<SignInPageModule["SignInPage"]>>,
): ComponentProps<SignInPageModule["SignInPage"]> {
  return {
    server,
    serverError: undefined,
    busy: false,
    notice: undefined,
    theme: { name: "light", toggle: () => {} },
    onSignedIn: () => {},
    ...overrides,
  };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await act(async () => {});
}

function box(): HTMLInputElement {
  return screen.getByTestId<HTMLInputElement>("signin-read-only");
}

function address(): string {
  return window.location.pathname + window.location.search + window.location.hash;
}

async function signInWithPassword(): Promise<void> {
  fireEvent.change(screen.getByLabelText("User name"), { target: { value: "joe" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: /^Sign in$/ }));
  await settle();
}

beforeEach(() => {
  window.history.replaceState(null, "", "/ui/");
  answer(tokenOrFlow);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

describe("parseReadOnlyParam", () => {
  it("takes 1, true, yes and on (any case) as a request, and nothing else", () => {
    for (const yes of ["1", "true", "TRUE", "yes", "on", " 1 "]) expect(parseReadOnlyParam(yes)).toBe(true);
    for (const no of [null, "", "0", "false", "no", "off", "2", "read_only"])
      expect(parseReadOnlyParam(no)).toBe(false);
  });
});

describe("?read_only=1", () => {
  it("is read once per page load and leaves the address bar at once, keeping the rest", async () => {
    window.history.replaceState(null, "", "/ui/sessions?x=1&read_only=1#frag");
    const auth = await freshAuth();
    expect(auth.readOnlyRequested()).toBe(true);
    expect(address()).toBe("/ui/sessions?x=1#frag");
    // Asked again in the same load (React may ask twice): the same answer, from memory.
    expect(auth.readOnlyRequested()).toBe(true);
    // Remembered for this tab, so the box shows it ticked on the next sign-in page too.
    expect(auth.lastReadOnly()).toBe(true);
    // A reload loads the address as it was left: nothing asks any more, though the tab's
    // remembered choice still ticks the box.
    const reloaded = await freshAuth();
    expect(reloaded.readOnlyRequested()).toBe(false);
    expect(reloaded.lastReadOnly()).toBe(true);
  });

  it("is removed without effect with a value that isn't a request", async () => {
    window.history.replaceState(null, "", "/ui/?read_only=0&tab=2");
    const auth = await freshAuth();
    expect(auth.readOnlyRequested()).toBe(false);
    expect(address()).toBe("/ui/?tab=2");
    expect(auth.lastReadOnly()).toBe(false);
  });

  it("ticks the box, and a password sign-in is then read-only", async () => {
    window.history.replaceState(null, "", "/ui/?read_only=1");
    const onSignedIn = vi.fn();
    const SignInPage = await freshPage();
    render(<SignInPage {...props({ onSignedIn })} />);
    expect(box().checked).toBe(true);
    expect(address()).toBe("/ui/");
    await signInWithPassword();
    expect(onSignedIn).toHaveBeenCalledWith(expect.objectContaining({ token: "tok", readOnly: true }));
  });

  it("with ?sign_in=browser: both leave the address bar, and the choice rides with the flow", async () => {
    window.history.replaceState(null, "", "/ui/?sign_in=browser&read_only=1&x=1");
    const SignInPage = await freshPage();
    render(
      <StrictMode>
        <SignInPage {...props({})} />
      </StrictMode>,
    );
    await settle();
    const flows = fetch.mock.calls.filter(([url]) => String(url).endsWith("/v1/auth/oauth/flows"));
    expect(flows).toHaveLength(1);
    const [, init] = flows[0] as [string, RequestInit];
    // The way back carries neither parameter: the choice is in this tab's storage instead.
    expect(JSON.parse(init.body as string)).toEqual({ return_to: "/ui/?x=1" });
    expect(window.location.hash).toBe(idpFragment);
    expect(JSON.parse(sessionStorage.getItem("hardcharger-ui:browser-sign-in")!)).toEqual({
      flowId: "f_ro",
      pollSecret: "s3cret",
      readOnly: true,
    });
  });
});

describe("Start sessions read-only", () => {
  it("is unticked by default, and an unticked sign-in carries no read-only", async () => {
    const onSignedIn = vi.fn();
    const SignInPage = await freshPage();
    render(<SignInPage {...props({ onSignedIn })} />);
    expect(box().checked).toBe(false);
    await signInWithPassword();
    const auth = onSignedIn.mock.calls[0]![0] as { readOnly?: boolean };
    expect(auth.readOnly).toBeUndefined();
    expect(lastReadOnly()).toBe(false);
  });

  it("ticked, makes a password sign-in read-only and is remembered for this tab", async () => {
    const onSignedIn = vi.fn();
    const SignInPage = await freshPage();
    render(<SignInPage {...props({ onSignedIn })} />);
    fireEvent.click(box());
    await signInWithPassword();
    expect(onSignedIn).toHaveBeenCalledWith(expect.objectContaining({ readOnly: true }));
    expect(lastReadOnly()).toBe(true);
    // The next sign-in page in this tab shows it ticked (never applied unseen).
    cleanup();
    const Again = await freshPage();
    render(<Again {...props({})} />);
    expect(box().checked).toBe(true);
  });

  it("ticked, makes a certificate sign-in read-only", async () => {
    const onSignedIn = vi.fn();
    const SignInPage = await freshPage();
    render(<SignInPage {...props({ onSignedIn })} />);
    fireEvent.click(box());
    fireEvent.click(screen.getByRole("button", { name: /Sign in with my certificate/ }));
    await settle();
    const [, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ grant_type: "client_certificate" });
    expect(onSignedIn).toHaveBeenCalledWith(expect.objectContaining({ readOnly: true }));
  });

  it("survives the identity provider's round trip, and the box shows it on the way back", async () => {
    const SignInPage = await freshPage();
    render(<SignInPage {...props({})} />);
    fireEvent.click(box());
    fireEvent.click(screen.getByTestId("signin-browser"));
    await settle();
    expect(window.location.hash).toBe(idpFragment);
    cleanup();
    // Back from the provider: a new page load, with the flow id in the fragment.
    window.history.replaceState(null, "", "/ui/#chargewire_oauth_flow=f_ro");
    const onSignedIn = vi.fn();
    const Back = await freshPage();
    render(<Back {...props({ onSignedIn })} />);
    expect(box().checked).toBe(true);
    await settle();
    expect(onSignedIn).toHaveBeenCalledWith(expect.objectContaining({ token: "tok", readOnly: true }));
    expect(address()).toBe("/ui/");
  });

  it("unticked before the provider, a sign-in coming back is read-write", async () => {
    rememberReadOnly(true); // an earlier choice in this tab, shown ticked...
    const SignInPage = await freshPage();
    render(<SignInPage {...props({})} />);
    expect(box().checked).toBe(true);
    fireEvent.click(box()); // ...and cleared
    fireEvent.click(screen.getByTestId("signin-browser"));
    await settle();
    cleanup();
    window.history.replaceState(null, "", "/ui/#chargewire_oauth_flow=f_ro");
    const onSignedIn = vi.fn();
    const Back = await freshPage();
    render(<Back {...props({ onSignedIn })} />);
    await settle();
    const auth = onSignedIn.mock.calls[0]![0] as { readOnly?: boolean };
    expect(auth.readOnly).toBeUndefined();
    expect(box().checked).toBe(false);
  });
});

describe("the saved sign-in", () => {
  it("keeps read-only with the token, and reads an older entry without it as read-write", () => {
    saveSignIn({ username: "joe", token: "t", expiresAt: Date.now() + 60_000, readOnly: true });
    expect(loadSignIn()?.readOnly).toBe(true);
    // As the iOS app writes it (WebConsole.swift's signInScript): no readOnly.
    sessionStorage.setItem(
      "hardcharger-ui:sign-in",
      JSON.stringify({ username: "joe", token: "t", expiresAt: Date.now() + 60_000 }),
    );
    expect(loadSignIn()).toEqual({ username: "joe", token: "t", expiresAt: expect.any(Number) });
  });
});
