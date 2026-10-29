export interface NetworkSettings {
  enabled: boolean;
  listen_address: string;
  server_name: string;
  tls: boolean;
  tls_cert: string;
  tls_key: string;
  username: string;
  password: string;
}

export const defaultNetwork: NetworkSettings = {
  enabled: false, listen_address: "0.0.0.0", server_name: "", tls: true,
  tls_cert: "", tls_key: "", username: "hardcharger", password: "",
};

export interface NetworkInfo { hostname: string; addresses: { address: string; interface: string }[] }
export interface GeneratedCertificate {
  certificate: string; privateKey: string; trustCertificate: string;
  fingerprint: string; expires: string; names: string[];
}
export interface WorkspaceTrust { certificate: GeneratedCertificate; serverName: string; platform: string }

export function networkErrors(network: NetworkSettings): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!network.enabled) return errors;
  if (!network.listen_address.trim()) errors["network-listen"] = "Enter a listen IP address, for example 0.0.0.0.";
  const host = network.server_name.replace(/^\[|\]$/g, "");
  if (!host || /[\s/\\@?#]/.test(host) || host === "0.0.0.0" || host === "::")
    errors["network-name"] = "Enter this computer's DNS name or IP address, without a scheme, port or path.";
  else try {
    const url = new URL(`https://${host.includes(":") ? `[${host}]` : host}:31337`);
    if (!url.hostname || url.username || url.password) throw new Error();
  } catch { errors["network-name"] = "Enter a valid server DNS name or IP address, without a port."; }
  if (!network.username.trim()) errors["network-user"] = "Network access requires a user name.";
  if (!network.password) errors["network-password"] = "Set a password before allowing network connections.";
  if (network.tls && !network.tls_cert) errors["network-cert"] = "TLS requires both a PEM certificate chain and private key path. Generate them below or supply your own.";
  if (network.tls && !network.tls_key) errors["network-key"] = "TLS requires both a PEM certificate chain and private key path. Generate them below or supply your own.";
  return errors;
}

/** A host the way a URL compares it: brackets and case removed, IPv6 compressed. */
export function normalHost(name: string): string {
  const host = name.trim().replace(/^\[|\]$/g, "");
  try { return new URL(`https://${host.includes(":") ? `[${host}]` : host}`).hostname.replace(/^\[|\]$/g, ""); }
  catch { return host.toLowerCase(); }
}

const isIp = (host: string) => host.includes(":") || /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
const isLoopback = (host: string) => host === "::1" || host.startsWith("127.");

/** Whether a certificate this app generated lists the name clients use. */
export function certificateCovers(certificate: GeneratedCertificate, serverName: string): boolean {
  const host = normalHost(serverName);
  return !!host && certificate.names.some(name => normalHost(name) === host);
}

/**
 * The saved server address when it is an IP this computer no longer has (DHCP handed it a new one,
 * or it moved networks), with the detected address of the same family to use instead.
 */
export function lostAddress(network: NetworkSettings | undefined, detected: NetworkInfo | null) {
  if (!network?.enabled || !detected?.addresses.length) return null;
  const saved = normalHost(network.server_name);
  if (!saved || !isIp(saved) || isLoopback(saved) || detected.addresses.some(a => normalHost(a.address) === saved)) return null;
  const replacement = detected.addresses.find(a => a.address.includes(":") === saved.includes(":"));
  return replacement ? { saved, replacement } : null;
}

export function validateNetwork(network: NetworkSettings) {
  const errors = Object.values(networkErrors(network));
  if (errors.length) throw new Error(errors[0]);
}
