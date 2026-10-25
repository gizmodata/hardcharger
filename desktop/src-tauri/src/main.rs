#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod server;

use server::{Server, Settings, Status};
use std::sync::{Arc, Mutex};
use tauri::{
    image::Image,
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_autostart::ManagerExt;

type Shared = Arc<Mutex<Server>>;

// Custom application commands are available only to the bundled controller. A server's
// web UI never receives native privileges, including when its content is compromised.
fn controller(window: &WebviewWindow) -> Result<(), String> {
    if window.label() != "main" {
        return Err("Desktop controls are restricted to the application window.".into());
    }
    let url = window.url().map_err(|e| e.to_string())?;
    if url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost") {
        return Ok(());
    }
    if cfg!(debug_assertions) && url.as_str().starts_with("http://127.0.0.1:1420/") {
        return Ok(());
    }
    Err("Desktop controls are restricted to bundled content.".into())
}

async fn work<T: Send + 'static>(
    state: Shared,
    action: impl FnOnce(&mut Server) -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut server = state
            .lock()
            .map_err(|_| "The server controller is unavailable.")?;
        action(&mut server)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn server_status(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
) -> Result<Status, String> {
    controller(&window)?;
    work(state.inner().clone(), Server::status).await
}

#[tauri::command]
async fn server_control(
    window: WebviewWindow,
    app: AppHandle,
    state: tauri::State<'_, Shared>,
    action: String,
) -> Result<Status, String> {
    controller(&window)?;
    control(&app, state.inner().clone(), &action).await
}

async fn control(app: &AppHandle, state: Shared, action: &str) -> Result<Status, String> {
    // Destroy the query view before stopping its server, ending its sessions. The next
    // workspace receives a fresh token; an old webview never retains a stale credential.
    if action != "start" {
        if let Some(window) = app.get_webview_window("workspace") {
            window.destroy().map_err(|e| e.to_string())?;
        }
    }
    let action = action.to_owned();
    let result = work(state, move |server| match action.as_str() {
        "start" => server.start(),
        "stop" => server.stop(),
        "restart" => {
            server.stop()?;
            server.start()
        }
        _ => Err("Unknown server action.".into()),
    })
    .await;
    let _ = app.emit_to("main", "server-changed", ());
    result
}

fn reveal(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

async fn workspace(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("workspace") {
        window.show().map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        return window.set_focus().map_err(|e| e.to_string());
    }
    let state = app.state::<Shared>().inner().clone();
    let (origin, token) = work(state, |server| {
        if !server.status()?.running {
            return Err("Start the local server to open its workspace.".into());
        }
        Ok((server.endpoint(), server.sign_in()?))
    })
    .await?;
    // Match the web UI's existing iOS sign-in contract, once per access token and only on
    // the precise loopback origin. No password or token appears in a URL or native event.
    let script = format!(
        r#"(() => {{
        if (location.origin !== {origin}) return;
        const applyTheme = () => {{
            const style = document.createElement('style');
            style.textContent = {palette};
            document.head.appendChild(style);
        }};
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyTheme, {{once:true}});
        else applyTheme();
        const signIn = {token};
        const marker = 'hardcharger-desktop:handed-token';
        if (sessionStorage.getItem(marker) !== signIn.token) {{
            sessionStorage.setItem('hardcharger-ui:sign-in', JSON.stringify(signIn));
            sessionStorage.setItem('hardcharger-ui:user-name', signIn.username);
            sessionStorage.setItem(marker, signIn.token);
        }}
    }})();"#,
        origin = serde_json::to_string(&origin).map_err(|e| e.to_string())?,
        token = token,
        palette = serde_json::to_string(include_str!("../../src/palette.css"))
            .map_err(|e| e.to_string())?
    );
    let url = format!("{origin}/ui/")
        .parse::<tauri::Url>()
        .map_err(|e| e.to_string())?;
    let allowed = url.origin();
    WebviewWindowBuilder::new(&app, "workspace", WebviewUrl::External(url))
        .title("Hardcharger — SQL Workspace")
        .inner_size(1440.0, 940.0)
        .min_inner_size(880.0, 600.0)
        .initialization_script(script)
        .on_navigation(move |destination| destination.origin() == allowed)
        // File drops belong to the web UI's existing upload flow.
        .disable_drag_drop_handler()
        .build()
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
async fn open_workspace(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    controller(&window)?;
    workspace(app).await
}

#[tauri::command]
async fn read_logs(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
) -> Result<String, String> {
    controller(&window)?;
    work(state.inner().clone(), |server| server.logs()).await
}

#[tauri::command]
async fn save_settings(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
    settings: Settings,
) -> Result<Status, String> {
    controller(&window)?;
    work(state.inner().clone(), move |server| {
        server.save_settings(settings)
    })
    .await
}

#[tauri::command]
async fn server_options(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
) -> Result<Vec<serde_json::Value>, String> {
    controller(&window)?;
    work(state.inner().clone(), |server| Ok(server.options())).await
}

#[tauri::command]
async fn reveal_data(window: WebviewWindow, state: tauri::State<'_, Shared>) -> Result<(), String> {
    controller(&window)?;
    work(state.inner().clone(), |server| {
        open::that(&server.directory).map_err(|e| e.to_string())
    })
    .await
}

#[tauri::command]
fn launch_at_login(
    window: WebviewWindow,
    app: AppHandle,
    enabled: Option<bool>,
) -> Result<bool, String> {
    controller(&window)?;
    let manager = app.autolaunch();
    if let Some(enabled) = enabled {
        if enabled {
            manager.enable()
        } else {
            manager.disable()
        }
        .map_err(|e| e.to_string())?;
    }
    manager.is_enabled().map_err(|e| e.to_string())
}

fn report(app: &AppHandle, result: Result<(), String>) {
    if let Err(error) = result {
        reveal(app);
        let _ = app.emit_to("main", "desktop-error", error);
    }
}

fn menu_action(app: &AppHandle, id: &str) {
    match id {
        "show" => reveal(app),
        "workspace" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                report(&app, workspace(app.clone()).await);
            });
        }
        "start" | "stop" | "restart" => {
            let app = app.clone();
            let action = id.to_owned();
            tauri::async_runtime::spawn(async move {
                let state = app.state::<Shared>().inner().clone();
                report(&app, control(&app, state, &action).await.map(|_| ()));
            });
        }
        "quit" => app.exit(0),
        _ => (),
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| reveal(app)))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .args(["--background"])
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            server_status,
            server_control,
            open_workspace,
            read_logs,
            save_settings,
            server_options,
            reveal_data,
            launch_at_login
        ])
        .setup(|app| {
            let binary_name = if cfg!(windows) {
                "hardcharger-server.exe"
            } else {
                "hardcharger-server"
            };
            let binary = std::env::current_exe()?
                .parent()
                .ok_or("Missing app executable directory")?
                .join(binary_name);
            let directory = app.path().app_data_dir()?;
            let server = Server::new(binary, directory).map_err(std::io::Error::other)?;
            let start = server.settings.start_on_open;
            app.manage(Arc::new(Mutex::new(server)));

            let menu = Menu::with_items(
                app,
                &[
                    &MenuItem::with_id(app, "show", "Open Hardcharger", true, None::<&str>)?,
                    &MenuItem::with_id(app, "workspace", "Open SQL Workspace", true, None::<&str>)?,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "start", "Start Server", true, None::<&str>)?,
                    &MenuItem::with_id(app, "stop", "Stop Server", true, None::<&str>)?,
                    &MenuItem::with_id(app, "restart", "Restart Server", true, None::<&str>)?,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "quit", "Quit Hardcharger", true, None::<&str>)?,
                ],
            )?;
            let icon = if cfg!(target_os = "macos") {
                Image::from_bytes(include_bytes!("../icons/tray.png"))?
            } else {
                app.default_window_icon()
                    .ok_or("Missing bull head icon")?
                    .clone()
            };
            TrayIconBuilder::with_id("hardcharger")
                .icon(icon)
                .icon_as_template(cfg!(target_os = "macos"))
                .tooltip("Hardcharger — local DuckDB server")
                .menu(&menu)
                .build(app)?;

            let server_menu = Submenu::with_items(
                app,
                "Server",
                true,
                &[
                    &MenuItem::with_id(
                        app,
                        "workspace",
                        "Open SQL Workspace",
                        true,
                        Some("CmdOrCtrl+1"),
                    )?,
                    &MenuItem::with_id(app, "show", "Server Dashboard", true, Some("CmdOrCtrl+2"))?,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "start", "Start Server", true, None::<&str>)?,
                    &MenuItem::with_id(app, "stop", "Stop Server", true, None::<&str>)?,
                    &MenuItem::with_id(app, "restart", "Restart Server", true, None::<&str>)?,
                ],
            )?;
            let application = Submenu::with_items(
                app,
                "Hardcharger",
                true,
                &[
                    &PredefinedMenuItem::about(app, Some("About Hardcharger"), None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::hide(app, None)?,
                    &MenuItem::with_id(app, "quit", "Quit Hardcharger", true, Some("CmdOrCtrl+Q"))?,
                ],
            )?;
            let edit = Submenu::with_items(
                app,
                "Edit",
                true,
                &[
                    &PredefinedMenuItem::undo(app, None)?,
                    &PredefinedMenuItem::redo(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::cut(app, None)?,
                    &PredefinedMenuItem::copy(app, None)?,
                    &PredefinedMenuItem::paste(app, None)?,
                    &PredefinedMenuItem::select_all(app, None)?,
                ],
            )?;
            let window_menu = Submenu::with_items(
                app,
                "Window",
                true,
                &[
                    &PredefinedMenuItem::minimize(app, None)?,
                    &PredefinedMenuItem::fullscreen(app, None)?,
                    &PredefinedMenuItem::close_window(app, None)?,
                ],
            )?;
            app.set_menu(Menu::with_items(
                app,
                &[&application, &server_menu, &edit, &window_menu],
            )?)?;
            app.on_menu_event(|app, event| menu_action(app, event.id.as_ref()));
            if !std::env::args().any(|a| a == "--background") {
                reveal(app.handle());
            }
            if start {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    let state = handle.state::<Shared>().inner().clone();
                    report(&handle, control(&handle, state, "start").await.map(|_| ()));
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("Could not build Hardcharger desktop");

    app.run(|app, event| match event {
        tauri::RunEvent::ExitRequested { api, .. } => {
            // Stop synchronously before allowing the event loop to exit. The server drains
            // queries and closes DuckDB; never silently leave the child process behind.
            let state = app.state::<Shared>();
            let result = state
                .lock()
                .map_err(|_| "Server controller unavailable".to_string())
                .and_then(|mut server| server.stop());
            if let Err(error) = result {
                api.prevent_exit();
                report(app, Err(error));
            }
        }
        #[cfg(target_os = "macos")]
        tauri::RunEvent::Reopen { .. } => reveal(app),
        _ => (),
    });
}
