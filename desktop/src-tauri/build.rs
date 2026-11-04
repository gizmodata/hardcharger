fn main() {
    // The SQL workspace's remembered passwords (workspace_passwords.rs): an app-defined plugin,
    // so its three commands get permissions of their own (remote-passwords:allow-get, ...) that
    // the app grants the workspace window at run time. The app's own commands keep working
    // without an ACL manifest, as before.
    // remote-servers: removing or renaming a server in Settings > Remote servers from it.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .plugin(
                "remote-passwords",
                tauri_build::InlinedPlugin::new().commands(&["get", "set", "delete"]),
            )
            .plugin(
                "remote-servers",
                tauri_build::InlinedPlugin::new().commands(&["remove", "rename"]),
            ),
    )
    .expect("failed to run the Tauri build script");
}
