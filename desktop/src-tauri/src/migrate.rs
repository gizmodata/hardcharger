//! The app's identifier was com.gizmodata.hardcharger.desktop.preview through 2.1.x. In-app
//! updates fix it for good (an update must match it), so the release that adds them takes the
//! final com.gizmodata.hardcharger.desktop, and with it a new app-data folder. On the first
//! launch the old folder (database, settings, certificates, logs) moves to the new name.
//!
//! The move is one rename within the same parent folder. The paths the app itself wrote into
//! its files (the generated TLS certificate and key) are rewritten, and the old name is left as
//! a link to the new folder (a symlink, or a junction on Windows, which needs no privileges)
//! so that paths in the user's own settings (an init SQL file, an attached DuckDB file) keep
//! working. Saved credentials stay where they are: the credential store's service name never
//! changed (server.rs's credential_store).

use std::fs;
use std::path::{Path, PathBuf};

pub const PREVIOUS: &str = "com.gizmodata.hardcharger.desktop.preview";

/// The data folder to use: `current`, after moving the 2.1.x folder there when only the old
/// one exists. If the move fails (a file held open by an old copy still running, on Windows),
/// the old folder is used where it is, and the move is tried again at the next launch.
pub fn data_directory(current: PathBuf) -> PathBuf {
    let Some(parent) = current.parent() else {
        return current;
    };
    let previous = parent.join(PREVIOUS);
    let is_link = fs::symlink_metadata(&previous).is_ok_and(|m| m.file_type().is_symlink());
    if current.exists() || is_link || !previous.is_dir() || is_junction(&previous) {
        return current;
    }
    if fs::rename(&previous, &current).is_err() {
        return previous;
    }
    rewrite_paths(&current, &previous);
    let _ = link(&current, &previous);
    current
}

/// Every JSON string in the app's own files that names a path in the old folder.
fn rewrite_paths(current: &Path, previous: &Path) {
    let mut files = vec![current.join("settings.json")];
    if let Ok(entries) = fs::read_dir(current.join("certificates")) {
        files.extend(
            entries
                .flatten()
                .map(|e| e.path().join("certificate-info.json")),
        );
    }
    let (from, to) = (previous.to_string_lossy(), current.to_string_lossy());
    for file in files {
        let Ok(bytes) = fs::read(&file) else { continue };
        let Ok(mut value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
            continue;
        };
        if replace_prefix(&mut value, &from, &to) {
            if let Ok(updated) = serde_json::to_vec_pretty(&value) {
                // Same permissions as the original: certificate records are private.
                let _ = crate::server::private_write(&file, &updated);
            }
        }
    }
}

fn replace_prefix(value: &mut serde_json::Value, from: &str, to: &str) -> bool {
    match value {
        serde_json::Value::String(text) => {
            let Some(rest) = text.strip_prefix(from) else {
                return false;
            };
            // A whole path component only: ".preview-old" is not inside ".preview".
            if !(rest.is_empty() || rest.starts_with(['/', '\\'])) {
                return false;
            }
            *text = format!("{to}{rest}");
            true
        }
        // Every element, not just up to the first match (which any() would stop at).
        serde_json::Value::Array(items) => {
            let mut replaced = false;
            for item in items {
                replaced |= replace_prefix(item, from, to);
            }
            replaced
        }
        serde_json::Value::Object(map) => {
            let mut replaced = false;
            for item in map.values_mut() {
                replaced |= replace_prefix(item, from, to);
            }
            replaced
        }
        _ => false,
    }
}

#[cfg(unix)]
fn link(current: &Path, previous: &Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(current, previous)
}

#[cfg(windows)]
fn link(current: &Path, previous: &Path) -> std::io::Result<()> {
    junction::create(current, previous)
}

#[cfg(unix)]
fn is_junction(_: &Path) -> bool {
    false
}

#[cfg(windows)]
fn is_junction(path: &Path) -> bool {
    junction::exists(path).unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn parent(name: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("hc-migrate-{name}-{nanos}"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn moves_the_preview_folder_rewrites_its_own_paths_and_leaves_a_link() {
        let root = parent("move");
        let previous = root.join(PREVIOUS);
        let current = root.join("com.gizmodata.hardcharger.desktop");
        let certificates = previous.join("certificates/abc");
        fs::create_dir_all(&certificates).unwrap();
        fs::write(previous.join("Hardcharger.duckdb"), b"database").unwrap();
        let old = previous.to_string_lossy().to_string();
        let sep = std::path::MAIN_SEPARATOR;
        fs::write(
            previous.join("settings.json"),
            serde_json::to_vec(&json!({
                "port": 31337,
                "server_options": {"init_sql_commands_file": format!("{old}{sep}init.sql"), "other": format!("{old}-old{sep}x")},
                "network": {"tls_cert": format!("{old}{sep}certificates{sep}abc{sep}server.pem")},
            }))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            certificates.join("certificate-info.json"),
            serde_json::to_vec(
                &json!({"certificate": format!("{old}{sep}certificates{sep}abc{sep}server.pem")}),
            )
            .unwrap(),
        )
        .unwrap();

        assert_eq!(data_directory(current.clone()), current);
        assert_eq!(
            fs::read(current.join("Hardcharger.duckdb")).unwrap(),
            b"database"
        );
        let new = current.to_string_lossy().to_string();
        let settings: serde_json::Value =
            serde_json::from_slice(&fs::read(current.join("settings.json")).unwrap()).unwrap();
        assert_eq!(
            settings["network"]["tls_cert"],
            format!("{new}{sep}certificates{sep}abc{sep}server.pem")
        );
        assert_eq!(
            settings["server_options"]["init_sql_commands_file"],
            format!("{new}{sep}init.sql")
        );
        assert_eq!(
            settings["server_options"]["other"],
            format!("{old}-old{sep}x"),
            "only whole path components"
        );
        let info: serde_json::Value = serde_json::from_slice(
            &fs::read(current.join("certificates/abc/certificate-info.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            info["certificate"],
            format!("{new}{sep}certificates{sep}abc{sep}server.pem")
        );
        // The old name still reaches the data: paths the app can't know about keep working.
        assert_eq!(
            fs::read(previous.join("Hardcharger.duckdb")).unwrap(),
            b"database"
        );

        // A second launch changes nothing, and never moves the link.
        assert_eq!(data_directory(current.clone()), current);
        assert_eq!(
            fs::read(current.join("Hardcharger.duckdb")).unwrap(),
            b"database"
        );
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_fresh_install_or_an_existing_new_folder_is_left_alone() {
        let root = parent("fresh");
        let current = root.join("com.gizmodata.hardcharger.desktop");
        assert_eq!(data_directory(current.clone()), current);
        assert!(!current.exists(), "Server::new creates it");

        let previous = root.join(PREVIOUS);
        fs::create_dir_all(&previous).unwrap();
        fs::write(previous.join("marker"), b"old").unwrap();
        fs::create_dir_all(&current).unwrap();
        assert_eq!(data_directory(current.clone()), current);
        assert!(
            previous.join("marker").exists(),
            "an existing new folder wins; the old one stays"
        );
        fs::remove_dir_all(&root).ok();
    }
}
