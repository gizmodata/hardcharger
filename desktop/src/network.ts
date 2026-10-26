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

export function validateNetwork(network: NetworkSettings) {
  const errors = Object.values(networkErrors(network));
  if (errors.length) throw new Error(errors[0]);
}
