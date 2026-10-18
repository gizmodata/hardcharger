// Certificates for the mutual-TLS tests: a CA, a server certificate for 127.0.0.1, and client
// certificates with e-mail identities, as PEM files (EC P-256, made with WebCrypto).
import { webcrypto } from "node:crypto";
import { writeFileSync } from "node:fs";
import path from "node:path";

// @peculiar/x509 (tsyringe) needs the Reflect metadata API before it loads.
import "reflect-metadata";
import * as x509 from "@peculiar/x509";

x509.cryptoProvider.set(webcrypto as unknown as Crypto);

const algorithm = { name: "ECDSA", namedCurve: "P-256", hash: "SHA-256" };

async function pem(key: CryptoKey): Promise<string> {
  const der = Buffer.from(await webcrypto.subtle.exportKey("pkcs8", key));
  const lines = der
    .toString("base64")
    .match(/.{1,64}/g)!
    .join("\n");
  return `-----BEGIN PRIVATE KEY-----\n${lines}\n-----END PRIVATE KEY-----\n`;
}

export interface Pki {
  ca: string;
  server: { cert: string; key: string };
  client: (email: string) => Promise<{ cert: string; key: string }>;
}

export async function makePki(dir: string): Promise<Pki> {
  const notBefore = new Date(Date.now() - 5 * 60_000);
  const notAfter = new Date(Date.now() + 24 * 3600_000);
  const caKeys = (await webcrypto.subtle.generateKey(algorithm, true, ["sign", "verify"])) as CryptoKeyPair;
  const ca = await x509.X509CertificateGenerator.createSelfSigned({
    serialNumber: "01",
    name: "CN=hardcharger-ui-test-ca",
    notBefore,
    notAfter,
    keys: caKeys,
    signingAlgorithm: algorithm,
    extensions: [
      new x509.BasicConstraintsExtension(true, undefined, true),
      new x509.KeyUsagesExtension(x509.KeyUsageFlags.keyCertSign | x509.KeyUsageFlags.cRLSign, true),
      await x509.SubjectKeyIdentifierExtension.create(caKeys.publicKey),
    ],
  });
  let serial = 2;
  const issue = async (name: string, san: x509.JsonGeneralName, usage: string, file: string) => {
    const keys = (await webcrypto.subtle.generateKey(algorithm, true, ["sign", "verify"])) as CryptoKeyPair;
    const cert = await x509.X509CertificateGenerator.create({
      serialNumber: (serial++).toString(16).padStart(2, "0"),
      subject: `CN=${name}`,
      issuer: ca.subject,
      notBefore,
      notAfter,
      publicKey: keys.publicKey,
      signingKey: caKeys.privateKey,
      signingAlgorithm: algorithm,
      extensions: [
        new x509.BasicConstraintsExtension(false),
        new x509.ExtendedKeyUsageExtension([usage]),
        new x509.SubjectAlternativeNameExtension([san]),
        await x509.AuthorityKeyIdentifierExtension.create(ca),
      ],
    });
    const certPath = path.join(dir, `${file}.pem`);
    const keyPath = path.join(dir, `${file}.key`);
    writeFileSync(certPath, cert.toString("pem"));
    writeFileSync(keyPath, await pem(keys.privateKey));
    return { cert: certPath, key: keyPath };
  };
  const caPath = path.join(dir, "ca.pem");
  writeFileSync(caPath, ca.toString("pem"));
  const server = await issue("server", { type: "ip", value: "127.0.0.1" }, x509.ExtendedKeyUsage.serverAuth, "server");
  return {
    ca: caPath,
    server,
    client: (email) =>
      issue(email, { type: "email", value: email }, x509.ExtendedKeyUsage.clientAuth, email.replace("@", "_at_")),
  };
}
