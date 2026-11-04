//! The SQL workspace's remembered passwords for saved connections (its Add a server dialog's
//! Remember password), kept in the OS keychain by remote_servers.rs.
//!
//! The workspace is server content (the local server's /ui, or `hardcharger ui`'s), which gets
//! no native privileges at all, except these three commands of an app-defined plugin. They are
//! granted at run time to the window labelled "workspace" on the exact origin the app opened it
//! on (Tauri refuses them anywhere else, and every command checks both again), and they read
//! and write only `remote-server:<id>` entries of the app's own keychain service.

use std::sync::Mutex;

use tauri::{
    ipc::CapabilityBuilder,
    plugin::{Builder, TauriPlugin},
    AppHandle, Manager, Runtime, WebviewWindow,
};

use crate::remote_servers::{self, Keychain};

pub const PLUGIN: &str = "remote-passwords";

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
        .permission(format!("{PLUGIN}:allow-delete"));
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
            get_ipc_response(
                &window,
                InvokeRequest {
                    cmd: cmd.into(),
                    callback: CallbackFn(0),
                    error: CallbackFn(1),
                    url: url.parse().unwrap(),
                    body: InvokeBody::Json(serde_json::json!({"id": "../not-an-id"})),
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
        grant(app.handle(), origin).unwrap();
        grant(app.handle(), origin).unwrap(); // reopening on the same origin
        for cmd in ["get", "delete"] {
            assert!(
                call(&page, &format!("plugin:remote-passwords|{cmd}")).contains(ran),
                "{cmd}"
            );
        }
        // Another origin in the same window, and an app command from the page: refused.
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
