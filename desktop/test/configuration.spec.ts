import { test, expect } from "@playwright/test";
import { exportConfiguration, parseConfiguration, type ServerOption } from "../src/configuration";

const option = (name: string, type: ServerOption["type"], managed = false, secret = false): ServerOption => ({ name, type, managed, secret, yaml_key: name, group: "Test", description: name, default: "" });
const schema = [option("port", "integer"), option("memory_limit", "string"), option("print_queries", "boolean"), option("token_authorized_emails", "list"), option("oauth_client_secret", "string", false, true), option("host", "string", true)];

test("server configuration YAML round-trips types and omits stored secrets", () => {
  const options = parseConfiguration('port: 31400\nmemory_limit: 2GB\nprint_queries: false\ntoken_authorized_emails:\n  - "*@example.com"\noauth_client_secret: private-value\n', schema);
  const exported = exportConfiguration(31400, options, schema);
  expect(exported).not.toContain("private-value");
  expect(exported).not.toContain("oauth_client_secret");
  const { oauth_client_secret: _, ...publicOptions } = options;
  expect(parseConfiguration(exported, schema)).toEqual(publicOptions);
});

test("invalid YAML and options cannot become a staged configuration", () => {
  for (const text of ['port: 31400\nport: 31401', 'host: 0.0.0.0', 'unknown_option: true', 'port: "31337"', 'print_queries: "false"', 'token_authorized_emails: [1]', 'port: 80', '- memory_limit: 2GB']) {
    expect(() => parseConfiguration(text, schema)).toThrow();
  }
});
