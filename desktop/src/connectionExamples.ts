/** Shareable client examples. Never accept or serialize a saved password/private key. */
export interface ConnectionExample {
  id: string;
  label: string;
  setup: string;
  code: string;
  docs: string;
}

export function connectionExamples(endpoint: string, username: string): { uri: string; examples: ConnectionExample[] } {
  const url = new URL(endpoint);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Invalid server endpoint");
  const tls = url.protocol === "https:";
  const address = `${url.hostname}:${url.port || (tls ? "443" : "80")}`;
  const uri = `hardcharger://${address}${tls ? "" : "?tls=false"}`;
  const jdbc = `jdbc:hardcharger://${address}${tls ? "" : "?useEncryption=false"}`;
  const user = JSON.stringify(username);
  const quote = (text: string) => `'${text.replaceAll("'", `'"'"'`)}'`;
  const docs = "https://docs.hardcharger.gizmodata.com/clients/";
  return { uri, examples: [
    { id: "web", label: "Web UI", setup: "Open this URL in a browser and sign in with your server account.",
      code: `${url.origin}/ui/`, docs: "https://docs.hardcharger.gizmodata.com/guides/web-ui/" },
    { id: "python", label: "Python / ADBC", setup: "pip install adbc-driver-hardcharger",
      code: `import os
from adbc_driver_hardcharger import dbapi as hardcharger

with hardcharger.connect(
    uri=${JSON.stringify(uri)},
    username=os.environ.get("HARDCHARGER_USER", ${user}),
    password=os.environ["HARDCHARGER_PASSWORD"],${tls ? '\n    tls_root_certs=os.environ.get("HARDCHARGER_TLS_ROOTS"),' : ""}
) as connection:
    with connection.cursor() as cursor:
        cursor.execute(operation="SELECT 42 AS answer")
        print(cursor.fetch_arrow_table())`, docs: `${docs}python/` },
    { id: "jdbc", label: "JDBC", setup: "Java 17+; add the Hardcharger JDBC driver to your classpath.",
      code: `import java.sql.*;
import java.util.Properties;

public class HardchargerExample {
    public static void main(String[] args) throws Exception {
        Properties properties = new Properties();
        properties.setProperty("user", System.getenv().getOrDefault("HARDCHARGER_USER", ${user}));
        properties.setProperty("password", System.getenv("HARDCHARGER_PASSWORD"));${tls ? `
        String roots = System.getenv("HARDCHARGER_TLS_ROOTS");
        if (roots != null && !roots.isBlank()) properties.setProperty("tlsRootCerts", roots);` : ""}
        try (Connection connection = DriverManager.getConnection(${JSON.stringify(jdbc)}, properties);
             Statement statement = connection.createStatement();
             ResultSet rows = statement.executeQuery("SELECT 42 AS answer")) {
            rows.next();
            System.out.println(rows.getInt("answer"));
        }
    }
}`, docs: `${docs}jdbc/` },
    { id: "cli", label: "CLI", setup: "Install the Hardcharger command-line tools. Set HARDCHARGER_USER and HARDCHARGER_PASSWORD on the client, or let the CLI prompt for a password.",
      code: `hardcharger ${quote(uri)} --username ${quote(username)}${tls ? '\n\n# For a private CA, add: --tls-roots /path/to/Hardcharger-local-CA.crt' : ""}`,
      docs: `${docs}cli/` },
    { id: "javascript", label: "JavaScript", setup: "Node.js 22.19+. Install: npm install @gizmodata/hardcharger apache-arrow",
      code: `${tls ? 'import { readFileSync } from "node:fs";\n' : ""}import { connect } from "@gizmodata/hardcharger";

${tls ? 'const roots = process.env.HARDCHARGER_TLS_ROOTS;\n' : ""}const connection = await connect({
  url: ${JSON.stringify(uri)},
  username: process.env.HARDCHARGER_USER ?? ${user},
  password: process.env.HARDCHARGER_PASSWORD,${tls ? '\n  ...(roots ? { tls: { ca: readFileSync(roots, "utf8") } } : {}),' : ""}
});
try {
  const rows = await connection.query("SELECT 42 AS answer");
  console.log(rows.toArray().map(row => row.toJSON()));
} finally {
  await connection.close();
}`, docs: `${docs}javascript/` },
  ] };
}
