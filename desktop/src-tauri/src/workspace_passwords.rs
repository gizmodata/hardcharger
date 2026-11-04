//! What the SQL workspace may ask the app: its remembered passwords for saved connections (the
//! Add a server dialog's Remember password, kept in the OS keychain by remote_servers.rs), and
//! removing or renaming a server in Settings > Remote servers, so the app's list follows the
//! workspace's.
//!
//! The workspace is server content (the local server's /ui, or `hardcharger ui`'s), which gets
//! no native privileges at all, except these commands of two app-defined plugins
//! (remote-passwords: get, set, delete; remote-servers: remove, rename). They are
//! granted at run time to the window labelled "workspace" on the exact origin the app opened it
//! on (Tauri refuses them anywhere else, and every command checks both again), and they read
//! and write only `remote-server:<id>` entries of the app's own keychain service.

use std::sync::Mutex;

use tauri::{
    ipc::CapabilityBuilder,
    plugin::{Builder, TauriPlugin},
    AppHandle, Emitter, Manager, Runtime, WebviewWindow,
};

use crate::remote_servers::{self, Keychain, ServerRef};

pub const PLUGIN: &str = "remote-passwords";
pub const SERVERS_PLUGIN: &str = "remote-servers";

/// The origin the workspace window was last opened on, and the origins granted so far.
#[derive(Default)]
pub struct WorkspaceOrigin(
    pub Mutex<Option<String>>,
    pub Mutex<std::collections::BTreeSet<String>>,
);

fn check<R: Runtime>(window: &WebviewWindow<R>) -> Result<(), String> {
    if window.label() != "workspace" {
        return Err("Remembered passwords are for the SQL workspace only.".into());
    }
    let origin = window
        .url()
        .map_err(|e| e.to_string())?
        .origin()
        .ascii_serialization();
    let expected = window
        .state::<WorkspaceOrigin>()
        .0
        .lock()
        .map_err(|_| "The workspace state is unavailable.")?
        .clone();
    if expected.as_deref() != Some(origin.as_str()) {
        return Err("Remembered passwords are for the SQL workspace only.".into());
    }
    Ok(())
}

#[tauri::command]
async fn get<R: Runtime>(window: WebviewWindow<R>, id: String) -> Result<Option<String>, String> {
    check(&window)?;
    tauri::async_runtime::spawn_blocking(move || remote_servers::recall(&Keychain, &id))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn set<R: Runtime>(
    window: WebviewWindow<R>,
    id: String,
    password: String,
) -> Result<(), String> {
    check(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        remote_servers::remember(&Keychain, &id, &password)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn delete<R: Runtime>(window: WebviewWindow<R>, id: String) -> Result<(), String> {
    check(&window)?;
    tauri::async_runtime::spawn_blocking(move || remote_servers::forget(&Keychain, &id))
        .await
        .map_err(|e| e.to_string())?
}

/// Removes the server from Settings > Remote servers (by id or address), with its password.
#[tauri::command]
async fn remove<R: Runtime>(window: WebviewWindow<R>, server: ServerRef) -> Result<bool, String> {
    check(&window)?;
    if !remote_servers::valid_id(&server.id) {
        return Err("Not a saved connection's id.".into());
    }
    let state = window.state::<crate::Shared>().inner().clone();
    let removed = crate::work(state, move |s| {
        let mut gone = Vec::new();
        s.edit_remote_servers(|list| {
            gone = remote_servers::remove_from(list, &server);
            !gone.is_empty()
        })?;
        for id in &gone {
            let _ = remote_servers::forget(&Keychain, id);
        }
        Ok(!gone.is_empty())
    })
    .await?;
    if removed {
        let _ = window.app_handle().emit_to("main", "server-changed", ());
    }
    Ok(removed)
}

/// Renames the server in Settings > Remote servers (by id or address).
#[tauri::command]
async fn rename<R: Runtime>(
    window: WebviewWindow<R>,
    server: ServerRef,
    name: String,
) -> Result<bool, String> {
    check(&window)?;
    if !remote_servers::valid_id(&server.id) {
        return Err("Not a saved connection's id.".into());
    }
    if !remote_servers::valid_name(&name) {
        return Err("A server's name is one line of at most 200 characters.".into());
    }
    let state = window.state::<crate::Shared>().inner().clone();
    let name = name.trim().to_string();
    let renamed = crate::work(state, move |s| {
        s.edit_remote_servers(|list| remote_servers::rename_in(list, &server, &name))
    })
    .await?;
    if renamed {
        let _ = window.app_handle().emit_to("main", "server-changed", ());
    }
    Ok(renamed)
}

pub fn init_servers<R: Runtime>() -> TauriPlugin<R> {
    Builder::new(SERVERS_PLUGIN)
        .invoke_handler(tauri::generate_handler![remove, rename])
        .build()
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new(PLUGIN)
        .invoke_handler(tauri::generate_handler![get, set, delete])
        .build()
}

/// The capability identifier for a workspace origin (one per origin the app opens it on).
pub fn capability_id(origin: &str) -> String {
    let safe: String = origin
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    format!("workspace-passwords-{safe}")
}

/// Lets the workspace window on `origin` call this plugin's commands, and records the origin
/// the commands accept.
pub fn grant<R: Runtime>(app: &AppHandle<R>, origin: &str) -> Result<(), String> {
    *app.state::<WorkspaceOrigin>()
        .0
        .lock()
        .map_err(|_| "The workspace state is unavailable.")? = Some(origin.to_string());
    if !app
        .state::<WorkspaceOrigin>()
        .1
        .lock()
        .map_err(|_| "The workspace state is unavailable.")?
        .insert(origin.to_string())
    {
        return Ok(()); // granted before: the workspace reopened on the same origin
    }
    let capability = CapabilityBuilder::new(capability_id(origin))
        .window("workspace")
        .local(false)
        .remote(format!("{origin}/*"))
        .permission(format!("{PLUGIN}:allow-get"))
        .permission(format!("{PLUGIN}:allow-set"))
        .permission(format!("{PLUGIN}:allow-delete"))
        .permission(format!("{SERVERS_PLUGIN}:allow-remove"))
        .permission(format!("{SERVERS_PLUGIN}:allow-rename"));
    app.add_capability(capability)
        .map_err(|e| format!("Could not set up the workspace: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::ipc::{CallbackFn, InvokeBody};
    use tauri::test::{get_ipc_response, mock_builder, INVOKE_KEY};
    use tauri::webview::InvokeRequest;

    /// The ACL, through Tauri's own runtime: nothing reaches the commands until the app grants
    /// them, then only the workspace window on its origin does, and the app's own commands
    /// stay out of the page's reach. Each call names an invalid id, so a command that runs
    /// answers with its own refusal before touching any keychain.
    #[test]
    fn only_the_workspace_on_its_origin_reaches_the_commands() {
        let app = mock_builder()
            .plugin(init())
            .plugin(init_servers())
            .manage(WorkspaceOrigin::default())
            .build(tauri::generate_context!(test = true))
            .unwrap();
        let origin = "http://127.0.0.1:5555";
        let window = tauri::WebviewWindowBuilder::new(
            &app,
            "workspace",
            tauri::WebviewUrl::External(format!("{origin}/ui/").parse().unwrap()),
        )
        .build()
        .unwrap();
        let call = |url: &str, cmd: &str| {
            let body = if cmd.starts_with("plugin:remote-servers|") {
                serde_json::json!({"server": {"id": "../not-an-id", "host": "h", "port": 1, "prefix": ""}, "name": "N"})
            } else {
                serde_json::json!({"id": "../not-an-id"})
            };
            get_ipc_response(
                &window,
                InvokeRequest {
                    cmd: cmd.into(),
                    callback: CallbackFn(0),
                    error: CallbackFn(1),
                    url: url.parse().unwrap(),
                    body: InvokeBody::Json(body),
                    headers: Default::default(),
                    invoke_key: INVOKE_KEY.into(),
                },
            )
            .unwrap_err()
            .to_string()
        };
        let page = format!("{origin}/ui/");
        let ran = "Not a saved connection's id.";
        let before = call(&page, "plugin:remote-passwords|delete");
        assert!(!before.contains(ran));
        assert!(call(&page, "plugin:remote-servers|remove").contains("not allowed"));
        grant(app.handle(), origin).unwrap();
        grant(app.handle(), origin).unwrap(); // reopening on the same origin
        for cmd in [
            "remote-passwords|get",
            "remote-passwords|delete",
            "remote-servers|remove",
            "remote-servers|rename",
        ] {
            assert!(call(&page, &format!("plugin:{cmd}")).contains(ran), "{cmd}");
        }
        // Another origin in the same window, and an app command from the page: refused.
        assert!(
            call("http://127.0.0.1:6666/ui/", "plugin:remote-servers|rename")
                .contains("not allowed on window")
        );
        let other = call(
            "http://127.0.0.1:6666/ui/",
            "plugin:remote-passwords|delete",
        );
        assert!(!other.contains(ran));
        assert!(before.contains("not allowed"), "{before}");
        assert!(other.contains("not allowed on window"), "{other}");
        // Granted for the remote origin only: never as a local (bundled) page.
        assert!(!other.contains("URL: local"), "{other}");
    }

    #[test]
    fn one_capability_per_origin_with_a_safe_identifier() {
        assert_eq!(
            capability_id("http://127.0.0.1:53211"),
            "workspace-passwords-http---127-0-0-1-53211"
        );
        assert_ne!(
            capability_id("http://127.0.0.1:1"),
            capability_id("http://127.0.0.1:2")
        );
    }
}
