//! The remote servers the SQL workspace lists (Settings > Remote servers), and the passwords
//! the workspace may remember for them in the operating system's keychain.
//!
//! A remote server is what the web UI's Add a server dialog saves: a host, a port, TLS on or
//! off, a path prefix, whether its certificate is trusted by fingerprint, a user name and a
//! query timeout. Never a password: those go to the keychain (macOS Keychain, Windows
//! Credential Manager, the Secret Service), keyed by the saved connection's id, and only when
//! the person checks Remember password in the workspace. The first 2.7.0 builds saved a URL and
//! a TLS mode string; those entries are read the same way (`RemoteServer` deserializes both).

use serde::{Deserialize, Serialize};

pub const DEFAULT_PORT: u16 = 31337;
const KEYCHAIN_SERVICE: &str = "com.gizmodata.hardcharger.desktop.preview";

#[derive(Clone, Serialize, Deserialize, PartialEq, Debug)]
#[serde(from = "StoredRemoteServer")]
pub struct RemoteServer {
    pub id: String,
    pub name: String,
    pub host: String,
    pub port: u16,
    /// TLS (HTTPS) to the server; off: plain HTTP.
    pub tls: bool,
    /// Where the server mounts Chargewire; "" for the default (/chargewire).
    pub prefix: String,
    /// The certificate is trusted by its SHA-256 fingerprint (a private or self-signed one).
    pub trust: bool,
    pub fingerprint: String,
    pub username: String,
    /// Seconds before the server cancels a statement; 0: none.
    pub timeout: u64,
}

impl Default for RemoteServer {
    fn default() -> Self {
        Self {
            id: String::new(),
            name: String::new(),
            host: String::new(),
            port: DEFAULT_PORT,
            tls: true,
            prefix: String::new(),
            trust: false,
            fingerprint: String::new(),
            username: String::new(),
            timeout: 0,
        }
    }
}

/// Every shape a remote server was ever stored in: the current one, and the first 2.7.0
/// builds' `url` + `tls: "verified" | "fingerprint" | "plaintext"`.
#[derive(Deserialize, Default)]
#[serde(default)]
struct StoredRemoteServer {
    id: String,
    name: String,
    host: String,
    port: Option<u16>,
    tls: Option<serde_json::Value>,
    prefix: String,
    trust: bool,
    fingerprint: String,
    username: String,
    timeout: u64,
    url: String,
}

impl From<StoredRemoteServer> for RemoteServer {
    fn from(s: StoredRemoteServer) -> Self {
        let mut out = RemoteServer {
            id: s.id,
            name: s.name,
            host: s.host,
            port: s.port.unwrap_or(DEFAULT_PORT),
            tls: true,
            prefix: normal_prefix(&s.prefix),
            trust: s.trust,
            fingerprint: s.fingerprint,
            username: s.username,
            timeout: s.timeout,
        };
        match s.tls {
            Some(serde_json::Value::Bool(tls)) => out.tls = tls,
            Some(serde_json::Value::String(mode)) => {
                out.tls = mode != "plaintext";
                out.trust = mode == "fingerprint";
            }
            _ => {}
        }
        if out.host.is_empty() && !s.url.is_empty() {
            if let Ok(url) = tauri::Url::parse(s.url.trim()) {
                out.host = url
                    .host_str()
                    .unwrap_or_default()
                    .trim_matches(['[', ']'])
                    .to_string();
                let https = url.scheme() != "http";
                out.port = url.port().unwrap_or(if https { 443 } else { 80 });
                out.prefix = normal_prefix(url.path());
                if s.port.is_some() {
                    out.port = s.port.unwrap_or(out.port);
                }
            }
        }
        out.trust = out.tls && out.trust;
        out
    }
}

/// A prefix as stored: "" for the default, else "/path" without a trailing slash.
pub fn normal_prefix(prefix: &str) -> String {
    let p = prefix.trim().trim_end_matches('/');
    if p.is_empty() || p == "/chargewire" {
        String::new()
    } else if p.starts_with('/') {
        p.to_string()
    } else {
        format!("/{p}")
    }
}

/// Whether `id` is an id the UI makes (a UUID, or its fallback): the keychain key is built
/// from it, so nothing else is accepted.
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

impl RemoteServer {
    /// Why this entry can't be saved, or Ok.
    pub fn validate(&self) -> Result<(), String> {
        let label = if self.name.trim().is_empty() {
            self.host.as_str()
        } else {
            self.name.as_str()
        };
        if self.name.trim().is_empty() {
            return Err("Every remote server needs a name.".into());
        }
        if !valid_id(&self.id) {
            return Err(format!("Remote server {label}: invalid id."));
        }
        let host = self.host.trim();
        if host.is_empty()
            || host
                .chars()
                .any(|c| c.is_whitespace() || matches!(c, '/' | '\\' | '@' | '?' | '#'))
        {
            return Err(format!(
                "Remote server {label}: the host is a name or an address, without a scheme, path or spaces."
            ));
        }
        if self.port == 0 {
            return Err(format!(
                "Remote server {label}: the port is a number from 1 to 65535."
            ));
        }
        if self.trust && !self.tls {
            return Err(format!(
                "Remote server {label}: a certificate can be trusted only with TLS on."
            ));
        }
        if self.trust && !self.fingerprint.is_empty() {
            let hex: String = self
                .fingerprint
                .chars()
                .filter(|c| !matches!(c, ':' | ' ' | '-'))
                .collect();
            if hex.len() != 64 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err(format!(
                    "Remote server {label}: the fingerprint must be the certificate's SHA-256 (64 hex digits)."
                ));
            }
        }
        Ok(())
    }
}

/// A saved connection as the workspace names it when removing or renaming one: its id, and its
/// address (the workspace may hold the same server under an id of its own).
#[derive(Deserialize, Debug, Clone)]
pub struct ServerRef {
    pub id: String,
    pub host: String,
    pub port: u16,
    #[serde(default)]
    pub prefix: String,
}

impl RemoteServer {
    /// Whether this entry is the server `r` names: the same id, or the same address.
    pub fn is(&self, r: &ServerRef) -> bool {
        self.id == r.id
            || (self.host.eq_ignore_ascii_case(r.host.trim())
                && self.port == r.port
                && self.prefix == normal_prefix(&r.prefix))
    }
}

/// Removes the entries `r` names; returns their ids.
pub fn remove_from(list: &mut Vec<RemoteServer>, r: &ServerRef) -> Vec<String> {
    let removed: Vec<String> = list
        .iter()
        .filter(|s| s.is(r))
        .map(|s| s.id.clone())
        .collect();
    list.retain(|s| !s.is(r));
    removed
}

/// Renames the entries `r` names; returns whether any was.
pub fn rename_in(list: &mut [RemoteServer], r: &ServerRef, name: &str) -> bool {
    let mut any = false;
    for s in list.iter_mut().filter(|s| s.is(r)) {
        s.name = name.to_string();
        any = true;
    }
    any
}

/// A name a person may give a server: not empty, one line, not absurdly long.
pub fn valid_name(name: &str) -> bool {
    let n = name.trim();
    !n.is_empty() && n.len() <= 200 && !n.chars().any(char::is_control)
}

// ---- Remembered passwords ----

/// Where remembered passwords live: the OS keychain, or (in tests) memory.
pub trait PasswordStore: Send + Sync {
    fn get(&self, account: &str) -> Result<Option<String>, String>;
    fn set(&self, account: &str, password: &str) -> Result<(), String>;
    fn delete(&self, account: &str) -> Result<(), String>;
}

/// The operating system's keychain, through the keyring crate.
pub struct Keychain;

impl PasswordStore for Keychain {
    fn get(&self, account: &str) -> Result<Option<String>, String> {
        let entry = keyring::Entry::new(KEYCHAIN_SERVICE, account).map_err(|e| e.to_string())?;
        match entry.get_password() {
            Ok(password) => Ok(Some(password)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(format!("Could not read the keychain: {e}")),
        }
    }
    fn set(&self, account: &str, password: &str) -> Result<(), String> {
        keyring::Entry::new(KEYCHAIN_SERVICE, account)
            .and_then(|entry| entry.set_password(password))
            .map_err(|e| format!("Could not write to the keychain: {e}"))
    }
    fn delete(&self, account: &str) -> Result<(), String> {
        let entry = keyring::Entry::new(KEYCHAIN_SERVICE, account).map_err(|e| e.to_string())?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(format!("Could not remove it from the keychain: {e}")),
        }
    }
}

fn account(id: &str) -> Result<String, String> {
    if !valid_id(id) {
        return Err("Not a saved connection's id.".into());
    }
    Ok(format!("remote-server:{id}"))
}

/// The password remembered for saved connection `id`.
pub fn recall(store: &dyn PasswordStore, id: &str) -> Result<Option<String>, String> {
    store.get(&account(id)?)
}

/// Remembers `password` for saved connection `id` (an empty one forgets it).
pub fn remember(store: &dyn PasswordStore, id: &str, password: &str) -> Result<(), String> {
    if password.is_empty() {
        return forget(store, id);
    }
    if password.len() > 64 * 1024 {
        return Err("That password is too long to remember.".into());
    }
    store.set(&account(id)?, password)
}

/// Forgets the password remembered for saved connection `id` (nothing to do when none is).
pub fn forget(store: &dyn PasswordStore, id: &str) -> Result<(), String> {
    store.delete(&account(id)?)
}

/// Forgets the passwords of the remote servers in `before` that `after` no longer lists
/// (removed in Settings). Returns the first error, after trying them all.
pub fn forget_removed(
    store: &dyn PasswordStore,
    before: &[RemoteServer],
    after: &[RemoteServer],
) -> Result<(), String> {
    let mut first = Ok(());
    for gone in before
        .iter()
        .filter(|b| !after.iter().any(|a| a.id == b.id))
    {
        if valid_id(&gone.id) {
            if let Err(e) = forget(store, &gone.id) {
                if first.is_ok() {
                    first = Err(e);
                }
            }
        }
    }
    first
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    /// A keychain in memory, for tests.
    #[derive(Default)]
    pub struct MemoryStore(pub Mutex<BTreeMap<String, String>>);

    impl PasswordStore for MemoryStore {
        fn get(&self, account: &str) -> Result<Option<String>, String> {
            Ok(self.0.lock().unwrap().get(account).cloned())
        }
        fn set(&self, account: &str, password: &str) -> Result<(), String> {
            self.0
                .lock()
                .unwrap()
                .insert(account.into(), password.into());
            Ok(())
        }
        fn delete(&self, account: &str) -> Result<(), String> {
            self.0.lock().unwrap().remove(account);
            Ok(())
        }
    }

    fn server(id: &str) -> RemoteServer {
        RemoteServer {
            id: id.into(),
            name: format!("Server {id}"),
            host: "db.example.com".into(),
            ..RemoteServer::default()
        }
    }

    #[test]
    fn stores_fills_and_forgets_a_password_by_connection_id() {
        let store = MemoryStore::default();
        let synthetic = "synthetic-test-value-1";
        assert_eq!(recall(&store, "a1").unwrap(), None);
        remember(&store, "a1", synthetic).unwrap();
        // Keyed by the connection's id, nothing else.
        assert!(store.0.lock().unwrap().contains_key("remote-server:a1"));
        assert!(recall(&store, "a1").unwrap().as_deref() == Some(synthetic));
        assert_eq!(recall(&store, "b2").unwrap(), None);
        // An empty password (Remember password unchecked) forgets it, as forget does.
        remember(&store, "a1", "").unwrap();
        assert_eq!(recall(&store, "a1").unwrap(), None);
        remember(&store, "a1", synthetic).unwrap();
        forget(&store, "a1").unwrap();
        assert_eq!(recall(&store, "a1").unwrap(), None);
        forget(&store, "a1").unwrap(); // forgetting nothing is fine
    }

    #[test]
    fn refuses_ids_that_are_not_a_connections() {
        let store = MemoryStore::default();
        for id in [
            "",
            "../x",
            "a b",
            "remote-server:x",
            &"x".repeat(65),
            "a/b",
            "ä",
        ] {
            assert!(remember(&store, id, "v").is_err(), "{id:?}");
            assert!(recall(&store, id).is_err(), "{id:?}");
        }
        assert!(store.0.lock().unwrap().is_empty());
        assert!(remember(&store, "550e8400-e29b-41d4-a716-446655440000", "v").is_ok());
    }

    #[test]
    fn removing_a_server_in_settings_forgets_its_password() {
        let store = MemoryStore::default();
        remember(&store, "a1", "one").unwrap();
        remember(&store, "b2", "two").unwrap();
        forget_removed(&store, &[server("a1"), server("b2")], &[server("b2")]).unwrap();
        assert_eq!(recall(&store, "a1").unwrap(), None);
        assert!(recall(&store, "b2").unwrap().is_some());
    }

    #[test]
    fn reads_the_current_shape_and_the_first_builds_url_shape() {
        let current: RemoteServer = serde_json::from_str(
            r#"{"id":"a","name":"A","host":"db.example.com","port":4433,"tls":true,"prefix":"/cw","trust":true,"fingerprint":"aa","username":"ann","timeout":30}"#,
        )
        .unwrap();
        assert_eq!(
            (
                current.host.as_str(),
                current.port,
                current.tls,
                current.prefix.as_str(),
                current.trust,
                current.timeout
            ),
            ("db.example.com", 4433, true, "/cw", true, 30)
        );
        let old: RemoteServer = serde_json::from_str(
            r#"{"id":"b","name":"B","url":"https://[::1]:31400/chargewire","tls":"fingerprint","fingerprint":"cc"}"#,
        )
        .unwrap();
        assert_eq!(
            (
                old.host.as_str(),
                old.port,
                old.tls,
                old.prefix.as_str(),
                old.trust
            ),
            ("::1", 31400, true, "", true)
        );
        let plain: RemoteServer =
            serde_json::from_str(r#"{"id":"c","name":"C","url":"http://h","tls":"plaintext"}"#)
                .unwrap();
        assert_eq!((plain.port, plain.tls, plain.trust), (80, false, false));
        let https_default: RemoteServer =
            serde_json::from_str(r#"{"id":"d","name":"D","url":"https://h","tls":"verified"}"#)
                .unwrap();
        assert_eq!((https_default.port, https_default.trust), (443, false));
        // Written back in the current shape only.
        let written = serde_json::to_value(&old).unwrap();
        assert!(written.get("url").is_none());
        assert_eq!(written["host"], "::1");
        assert_eq!(written["tls"], true);
    }

    #[test]
    fn the_workspace_removes_and_renames_by_id_or_by_address() {
        let mut list = vec![server("a1"), {
            let mut s = server("b2");
            s.host = "Lab.local".into();
            s.prefix = "/cw".into();
            s
        }];
        // By address, the workspace holding it under an id of its own.
        let by_address = ServerRef {
            id: "ui-own-id".into(),
            host: "lab.local".into(),
            port: DEFAULT_PORT,
            prefix: "/cw/".into(),
        };
        assert!(rename_in(&mut list, &by_address, "Lab 2"));
        assert_eq!(list[1].name, "Lab 2");
        assert_eq!(remove_from(&mut list, &by_address), vec!["b2".to_string()]);
        // By id.
        let by_id = ServerRef {
            id: "a1".into(),
            host: "elsewhere".into(),
            port: 1,
            prefix: String::new(),
        };
        assert_eq!(remove_from(&mut list, &by_id), vec!["a1".to_string()]);
        assert!(list.is_empty());
        assert!(!rename_in(&mut list, &by_id, "x"));
        assert!(
            valid_name("Warehouse")
                && !valid_name(" ")
                && !valid_name("a\nb")
                && !valid_name(&"x".repeat(201))
        );
    }

    #[test]
    fn validates_host_port_and_trust() {
        assert!(server("a1").validate().is_ok());
        let bad = |f: &dyn Fn(&mut RemoteServer)| {
            let mut s = server("a1");
            f(&mut s);
            s.validate().unwrap_err()
        };
        assert!(bad(&|s| s.host = "https://h".into()).contains("host"));
        assert!(bad(&|s| s.host = String::new()).contains("host"));
        assert!(bad(&|s| s.port = 0).contains("port"));
        assert!(bad(&|s| s.name = " ".into()).contains("name"));
        assert!(bad(&|s| {
            s.tls = false;
            s.trust = true
        })
        .contains("TLS"));
        assert!(bad(&|s| {
            s.trust = true;
            s.fingerprint = "zz".into()
        })
        .contains("SHA-256"));
        assert!(bad(&|s| s.id = "../x".into()).contains("id"));
    }
}
