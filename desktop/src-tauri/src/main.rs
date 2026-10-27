#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod migrate;
mod network;
mod server;
mod tray;
mod updates;

use server::{Server, Settings, Status};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc, Mutex,
};
use tauri::{
    image::Image,
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_autostart::ManagerExt;

pub(crate) type Shared = Arc<Mutex<Server>>;
static DETAIL_WINDOW: AtomicU64 = AtomicU64::new(1);
const SQL_CLOSE_PATH: &str = "/ui/__desktop_close_window";

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

pub(crate) async fn work<T: Send + 'static>(
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
    confirmed: Option<bool>,
) -> Result<Status, String> {
    controller(&window)?;
    if matches!(action.as_str(), "stop" | "restart") && confirmed != Some(true) {
        return Err("Confirm stopping or restarting the server first.".into());
    }
    control(&app, state.inner().clone(), &action).await
}

pub(crate) async fn control(
    app: &AppHandle,
    state: Shared,
    action: &str,
) -> Result<Status, String> {
    // Destroy the query view before stopping its server, ending its sessions. The next
    // workspace receives a fresh token; an old webview never retains a stale credential.
    if action != "start" {
        for (label, window) in app.webview_windows() {
            if label.starts_with("query-detail-") {
                window.destroy().map_err(|e| e.to_string())?;
            }
        }
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
    if let Ok(status) = &result {
        tray::show(app, status.running);
    }
    let _ = app.emit_to("main", "server-changed", ());
    result
}

#[tauri::command]
fn update_state(
    window: WebviewWindow,
    updates: tauri::State<'_, updates::Updates>,
) -> Result<updates::UpdateState, String> {
    controller(&window)?;
    Ok(updates.state())
}

#[tauri::command]
async fn update_check(
    window: WebviewWindow,
    app: AppHandle,
) -> Result<updates::UpdateState, String> {
    controller(&window)?;
    updates::check(&app).await
}

#[tauri::command]
fn update_preferences(
    window: WebviewWindow,
    updates: tauri::State<'_, updates::Updates>,
    preferences: updates::Preferences,
) -> Result<updates::UpdateState, String> {
    controller(&window)?;
    updates.set_preferences(preferences)
}

/// Installing stops a running server, so like Stop and Restart it needs the user's
/// confirmation, which the dashboard asks for before calling this.
#[tauri::command]
async fn update_install(
    window: WebviewWindow,
    app: AppHandle,
    state: tauri::State<'_, Shared>,
    confirmed: Option<bool>,
) -> Result<(), String> {
    controller(&window)?;
    let running = work(state.inner().clone(), Server::status).await?.running;
    if running && confirmed != Some(true) {
        return Err("Confirm stopping the server to install the update first.".into());
    }
    updates::install(&app, state.inner().clone()).await
}

fn reveal(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

// Called only by a SQL window's own same-origin close navigation after cleanup.
// destroy() bypasses CloseRequested, which starts the cleanup on native closes.
fn finish_sql_window_close(app: &AppHandle, label: &str) {
    let owner = app.clone();
    let label = label.to_owned();
    let _ = app.run_on_main_thread(move || {
        if let Some(window) = owner.get_webview_window(&label) {
            let _ = window.destroy();
        }
    });
}

// Reached only after the workspace has confirmed any transaction rollback and
// ended its own sessions. Each inspector takes its own awaited cleanup path.
fn finish_workspace_close(app: &AppHandle) {
    let owner = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        for (label, window) in owner.webview_windows() {
            if label.starts_with("query-detail-") {
                let _ = window.close();
            }
        }
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(6);
        loop {
            let children: Vec<_> = owner
                .webview_windows()
                .into_iter()
                .filter(|(label, _)| label.starts_with("query-detail-"))
                .collect();
            if children.is_empty() {
                break;
            }
            // The page's normal cleanup has a five-second disconnected-server
            // deadline. Also allow a close if a webview itself is unresponsive.
            if std::time::Instant::now() >= deadline {
                for (_, window) in children {
                    let _ = window.destroy();
                }
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
        finish_sql_window_close(&owner, "workspace");
    });
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
        server.check_workspace_trust()?;
        Ok((server.endpoint(), server.sign_in()?))
    })
    .await?;
    // Match the web UI's existing iOS sign-in contract, once per access token and only on
    // the precise loopback origin. No password or token appears in a URL or native event.
    let script = format!(
        r#"(() => {{
        window.close = () => {{
            if (location.origin === {origin}) window.location.replace('{close_path}');
        }};
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
        close_path = SQL_CLOSE_PATH,
        token = token,
        palette = serde_json::to_string(include_str!("../../src/palette.css"))
            .map_err(|e| e.to_string())?
    );
    let url = format!("{origin}/ui/")
        .parse::<tauri::Url>()
        .map_err(|e| e.to_string())?;
    let allowed = url.origin();
    let detail_origin = allowed.clone();
    let detail_app = app.clone();
    let detail_script = script.clone();
    let workspace_app = app.clone();
    WebviewWindowBuilder::new(&app, "workspace", WebviewUrl::External(url))
        .title("Hardcharger — SQL Workspace")
        .inner_size(1440.0, 940.0)
        .min_inner_size(880.0, 600.0)
        .initialization_script(script)
        .on_navigation(move |destination| {
            if destination.origin() != allowed {
                return false;
            }
            if destination.path() == SQL_CLOSE_PATH {
                finish_workspace_close(&workspace_app);
                return false;
            }
            true
        })
        .on_new_window(move |destination, features| {
            // Server content can open query inspectors on its own exact origin.
            // These views have no native command capabilities, just like the workspace.
            if destination.origin() != detail_origin || destination.path() != "/ui/monitor/query" {
                return tauri::webview::NewWindowResponse::Deny;
            }
            let allowed = detail_origin.clone();
            let label = format!(
                "query-detail-{}",
                DETAIL_WINDOW.fetch_add(1, Ordering::Relaxed)
            );
            let close_app = detail_app.clone();
            let close_label = label.clone();
            let close_origin = detail_origin.clone();
            let result = WebviewWindowBuilder::new(
                &detail_app,
                label,
                WebviewUrl::External("about:blank".parse().unwrap()),
            )
            // Retains the opener's webview configuration on macOS, environment
            // on Windows, and related view on Linux for window.open support.
            .window_features(features)
            .title("Hardcharger — Query details")
            .inner_size(1100.0, 850.0)
            .min_inner_size(700.0, 500.0)
            .initialization_script(detail_script.clone())
            .on_page_load(move |window, payload| {
                if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
                    && payload.url().origin() == close_origin
                {
                    // A related WebKit window inherits its opener's initial scripts;
                    // install the self-close hook after its actual page has loaded.
                    let _ = window.eval(format!(
                        "window.close = () => window.location.replace('{SQL_CLOSE_PATH}');"
                    ));
                }
            })
            .on_navigation(move |url| {
                if url.origin() == allowed && url.path() == SQL_CLOSE_PATH {
                    // WebKit does not reliably close a Tauri-created window through
                    // JavaScript window.close(). This navigation can close only this
                    // inspector; it grants no IPC or controller capabilities.
                    finish_sql_window_close(&close_app, &close_label);
                    return false;
                }
                url.as_str() == "about:blank" || url.origin() == allowed
            })
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
            .disable_drag_drop_handler()
            .build();
            match result {
                Ok(window) => tauri::webview::NewWindowResponse::Create { window },
                Err(error) => {
                    report(
                        &detail_app,
                        Err(format!("Could not open query details: {error}")),
                    );
                    tauri::webview::NewWindowResponse::Deny
                }
            }
        })
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
async fn settings_pending(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
    pending: bool,
    network: Option<bool>,
) -> Result<(), String> {
    controller(&window)?;
    work(state.inner().clone(), move |server| {
        server.set_settings_pending(pending, network)
    })
    .await
}

#[tauri::command]
async fn network_info(window: WebviewWindow) -> Result<network::NetworkInfo, String> {
    controller(&window)?;
    tauri::async_runtime::spawn_blocking(network::info)
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn generate_certificate(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
    server_name: String,
) -> Result<network::GeneratedCertificate, String> {
    controller(&window)?;
    work(state.inner().clone(), move |server| {
        if server.status()?.running {
            return Err("Stop the server before generating a new certificate.".into());
        }
        network::generate(&server.directory, &server_name)
    })
    .await
}

#[tauri::command]
async fn show_certificate_folder(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
    certificate: String,
) -> Result<(), String> {
    controller(&window)?;
    let folder = work(state.inner().clone(), move |server| {
        network::certificate_folder(&server.directory, &certificate)
    })
    .await?;
    open::that(folder).map_err(|e| e.to_string())
}

#[tauri::command]
async fn workspace_trust(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
) -> Result<network::WorkspaceTrust, String> {
    controller(&window)?;
    work(state.inner().clone(), |server| {
        server.verify_workspace_endpoint()?;
        network::workspace_trust(&server.directory, &server.settings)
    })
    .await
}

#[tauri::command]
async fn trust_workspace_ca(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
    fingerprint: String,
    server_name: String,
) -> Result<(), String> {
    controller(&window)?;
    work(state.inner().clone(), move |server| {
        server.verify_workspace_endpoint()?;
        network::trust_workspace_ca(
            &server.directory,
            &server.settings,
            &fingerprint,
            &server_name,
        )
    })
    .await
}

#[tauri::command]
fn open_marketing_link(window: WebviewWindow, url: String) -> Result<(), String> {
    controller(&window)?;
    if !matches!(
        url.as_str(),
        "https://gizmodata.com"
            | "https://gizmodata.com/hardcharger"
            | "https://docs.hardcharger.gizmodata.com"
            | "https://docs.hardcharger.gizmodata.com/guides/web-ui/"
            | "https://docs.hardcharger.gizmodata.com/clients/python/"
            | "https://docs.hardcharger.gizmodata.com/clients/jdbc/"
            | "https://docs.hardcharger.gizmodata.com/clients/cli/"
            | "https://docs.hardcharger.gizmodata.com/clients/javascript/"
    ) {
        return Err("This desktop link is not available.".into());
    }
    open::that(url).map_err(|e| e.to_string())
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
        "stop" | "restart" => {
            // Both the app menu and tray ask the controller to show its modal first.
            // No server or workspace state changes until the user confirms there.
            reveal(app);
            report(
                app,
                app.emit_to("main", "server-control-requested", id)
                    .map_err(|e| e.to_string()),
            );
        }
        "start" => {
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
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            server_status,
            server_control,
            open_workspace,
            read_logs,
            save_settings,
            server_options,
            settings_pending,
            network_info,
            generate_certificate,
            show_certificate_folder,
            workspace_trust,
            trust_workspace_ca,
            reveal_data,
            open_marketing_link,
            launch_at_login,
            update_state,
            update_check,
            update_preferences,
            update_install
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
            // 2.1.x kept its data under the .preview identifier: move it first (migrate.rs).
            let directory = migrate::data_directory(app.path().app_data_dir()?);
            app.manage(updates::Updates::new(
                &directory,
                app.package_info().version.to_string(),
            ));
            let server = Server::new(binary, directory).map_err(std::io::Error::other)?;
            let start = server.settings.start_on_open;
            app.manage(Arc::new(Mutex::new(server)));

            // Created disabled: tray::show enables them for the server's state below.
            let stateful = |id: &str, label: &str, shortcut: Option<&str>| {
                MenuItem::with_id(app, id, label, false, shortcut)
            };
            let tray_workspace = stateful("workspace", "Open SQL Workspace", None)?;
            let tray_start = stateful("start", "Start Server", None)?;
            let tray_stop = stateful("stop", "Stop Server", None)?;
            let tray_restart = stateful("restart", "Restart Server", None)?;
            let menu = Menu::with_items(
                app,
                &[
                    &MenuItem::with_id(app, "show", "Open Hardcharger", true, None::<&str>)?,
                    &tray_workspace,
                    &PredefinedMenuItem::separator(app)?,
                    &tray_start,
                    &tray_stop,
                    &tray_restart,
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
            // Drawn by tray::show with the status dot: never a template image, which macOS
            // would tint in one colour, dot included.
            TrayIconBuilder::with_id(tray::TRAY)
                .icon(tray::status_icon(&icon, false, None))
                .icon_as_template(false)
                .tooltip("Hardcharger — local DuckDB server")
                .menu(&menu)
                .build(app)?;

            let menu_workspace = stateful("workspace", "Open SQL Workspace", Some("CmdOrCtrl+1"))?;
            let menu_start = stateful("start", "Start Server", None)?;
            let menu_stop = stateful("stop", "Stop Server", None)?;
            let menu_restart = stateful("restart", "Restart Server", None)?;
            let server_menu = Submenu::with_items(
                app,
                "Server",
                true,
                &[
                    &menu_workspace,
                    &MenuItem::with_id(app, "show", "Server Dashboard", true, Some("CmdOrCtrl+2"))?,
                    &PredefinedMenuItem::separator(app)?,
                    &menu_start,
                    &menu_stop,
                    &menu_restart,
                ],
            )?;
            app.manage(tray::ServerMenus::new(
                vec![
                    tray_workspace,
                    tray_start,
                    tray_stop,
                    tray_restart,
                    menu_workspace,
                    menu_start,
                    menu_stop,
                    menu_restart,
                ],
                Image::new_owned(icon.rgba().to_vec(), icon.width(), icon.height()),
            ));
            tray::show(app.handle(), false);
            tray::watch(app.handle().clone());
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
            #[cfg(target_os = "macos")]
            window_menu.set_as_windows_menu_for_nsapp()?;
            app.on_menu_event(|app, event| menu_action(app, event.id.as_ref()));
            if !std::env::args().any(|a| a == "--background") {
                reveal(app.handle());
            }
            updates::schedule(app.handle().clone());
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
            if window.label() == "workspace" || window.label().starts_with("query-detail-") {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    if let Some(view) = window.app_handle().get_webview_window(window.label()) {
                        // Keep the webview alive for session DELETEs. A page that has
                        // not installed React's exit listener cannot have opened any
                        // sessions yet, and can finish closing immediately.
                        let script = format!("if (window.__hardchargerCloseReady) {{ window.dispatchEvent(new Event('hardcharger:close-window')); }} else {{ window.location.replace('{SQL_CLOSE_PATH}'); }}");
                        if view.url().is_ok_and(|url| url.path().starts_with("/ui/"))
                            && view.eval(&script).is_ok()
                        {
                            api.prevent_close();
                        }
                    }
                }
            }
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
                // The menu bar follows the system appearance: redraw the bull for it.
                if let tauri::WindowEvent::ThemeChanged(_) = event {
                    tray::redraw(window.app_handle());
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
