// A small OpenID Connect identity provider for the browser sign-in tests: discovery, a JWKS,
// an authorization endpoint that approves at once (it redirects the browser back to the
// redirect URI with a code, as a provider does once the person has signed in) and a token
// endpoint that trades the code for an RS256 ID token carrying the request's nonce. It checks
// the client id and secret, the redirect URI, single-use codes and PKCE (S256).

import { createHash, generateKeyPairSync, randomBytes, sign } from "node:crypto";
import http from "node:http";
import type net from "node:net";

export interface Idp {
  issuer: string;
  clientId: string;
  clientSecret: string;
  /** Who the next sign-in is. */
  email: string;
  /** Refuse the next sign-ins at the provider (error=access_denied). */
  deny: boolean;
  stop: () => Promise<void>;
}

const b64url = (data: Buffer | string) => Buffer.from(data).toString("base64url");

export async function startIdp(): Promise<Idp> {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "e2e-1", alg: "RS256", use: "sig" };
  const codes = new Map<string, { challenge: string; nonce: string; redirectUri: string; email: string }>();
  const idp: Idp = {
    issuer: "",
    clientId: "hardcharger-ui-e2e",
    clientSecret: `${randomBytes(12).toString("hex")}+/=`,
    email: "alice@example.com",
    deny: false,
    stop: async () => {},
  };

  const idToken = (email: string, nonce: string) => {
    const now = Math.floor(Date.now() / 1000);
    const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid: jwk.kid }));
    const payload = b64url(
      JSON.stringify({ iss: idp.issuer, aud: idp.clientId, sub: email, email, nonce, iat: now, exp: now + 300 }),
    );
    const signature = sign("RSA-SHA256", Buffer.from(`${header}.${payload}`), privateKey);
    return `${header}.${payload}.${b64url(signature)}`;
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? "/", idp.issuer);
    const json = (status: number, body: unknown) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    if (url.pathname === "/.well-known/openid-configuration") {
      return json(200, {
        issuer: idp.issuer,
        jwks_uri: `${idp.issuer}/jwks`,
        authorization_endpoint: `${idp.issuer}/authorize`,
        token_endpoint: `${idp.issuer}/token`,
      });
    }
    if (url.pathname === "/jwks") return json(200, { keys: [jwk] });
    if (url.pathname === "/authorize") {
      const q = url.searchParams;
      const redirectUri = q.get("redirect_uri") ?? "";
      if (q.get("client_id") !== idp.clientId || !redirectUri) return json(400, { error: "invalid_request" });
      const back = new URL(redirectUri);
      back.searchParams.set("state", q.get("state") ?? "");
      if (idp.deny) {
        back.searchParams.set("error", "access_denied");
        back.searchParams.set("error_description", "The user declined");
      } else {
        if (q.get("code_challenge_method") !== "S256") return json(400, { error: "invalid_request" });
        const code = randomBytes(16).toString("hex");
        codes.set(code, {
          challenge: q.get("code_challenge") ?? "",
          nonce: q.get("nonce") ?? "",
          redirectUri,
          email: idp.email,
        });
        back.searchParams.set("code", code);
      }
      res.writeHead(302, { Location: back.toString() });
      return res.end();
    }
    if (url.pathname === "/token" && req.method === "POST") {
      let raw = "";
      req.on("data", (d: Buffer) => (raw += d.toString()));
      req.on("end", () => {
        const form = new URLSearchParams(raw);
        if (form.get("client_id") !== idp.clientId || form.get("client_secret") !== idp.clientSecret) {
          return json(401, { error: "invalid_client" });
        }
        const grant = codes.get(form.get("code") ?? "");
        codes.delete(form.get("code") ?? "");
        if (!grant || grant.redirectUri !== form.get("redirect_uri")) return json(400, { error: "invalid_grant" });
        const challenge = createHash("sha256")
          .update(form.get("code_verifier") ?? "")
          .digest("base64url");
        if (challenge !== grant.challenge) return json(400, { error: "invalid_grant", error_description: "PKCE" });
        json(200, { access_token: "unused", token_type: "Bearer", id_token: idToken(grant.email, grant.nonce) });
      });
      return;
    }
    json(404, { error: "not_found" });
  });
  const port = await new Promise<number>((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve((server.address() as net.AddressInfo).port)),
  );
  idp.issuer = `http://127.0.0.1:${port}`;
  idp.stop = () => new Promise<void>((resolve) => server.close(() => resolve()));
  return idp;
}
