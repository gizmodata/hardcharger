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

export function validateNetwork(network: NetworkSettings) {
  if (!network.enabled) return;
  if (!network.listen_address.trim()) throw new Error("Enter a listen IP address, for example 0.0.0.0.");
  const host = network.server_name.replace(/^\[|\]$/g, "");
  if (!host || /[\s/\\@?#]/.test(host) || host === "0.0.0.0" || host === "::")
    throw new Error("Enter this computer's DNS name or IP address, without a scheme, port or path.");
  try {
    const url = new URL(`https://${host.includes(":") ? `[${host}]` : host}:31337`);
    if (!url.hostname || url.username || url.password) throw new Error();
  } catch { throw new Error("Enter a valid server DNS name or IP address, without a port."); }
  if (!network.username.trim()) throw new Error("Network access requires a user name.");
  if (!network.password) throw new Error("Set a password before allowing network connections.");
  if (network.tls && (!network.tls_cert || !network.tls_key))
    throw new Error("TLS requires both a PEM certificate chain and private key path.");
}
