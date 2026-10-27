//! The tray menu and the app's Server menu follow the server: Start only while it is stopped;
//! Stop, Restart and Open SQL Workspace only while it runs. The tray icon carries the state
//! too: a green dot while the server runs, a red one while it is stopped.
//!
//! On macOS the bull is normally a template image, which the menu bar tints in one colour, so
//! it can't hold a coloured dot. The icon is drawn instead: the bull white on a dark menu bar
//! and black on a light one (redrawn when the appearance changes), with the dot in colour.

use std::sync::Mutex;
use tauri::{image::Image, menu::MenuItem, AppHandle, Manager, Theme, Wry};

pub const TRAY: &str = "hardcharger";
const RUNNING: [u8; 3] = [0x30, 0xd1, 0x58]; // the system green
const STOPPED: [u8; 3] = [0xff, 0x45, 0x3a]; // the system red

/// Whether the menu item `id` can be used while the server is (or isn't) running.
pub fn enabled(id: &str, running: bool) -> bool {
    match id {
        "start" => !running,
        "stop" | "restart" | "workspace" => running,
        _ => true,
    }
}

pub struct ServerMenus {
    items: Vec<MenuItem<Wry>>,
    base: Image<'static>,
    /// The state last shown, and the appearance it was drawn for.
    shown: Mutex<Option<(bool, bool)>>,
}

impl ServerMenus {
    pub fn new(items: Vec<MenuItem<Wry>>, base: Image<'static>) -> Self {
        Self {
            items,
            base,
            shown: Mutex::new(None),
        }
    }
}

fn dark(app: &AppHandle) -> bool {
    app.get_webview_window("main")
        .and_then(|window| window.theme().ok())
        .is_some_and(|theme| theme == Theme::Dark)
}

/// Shows `running` in the menus and the tray icon; does nothing when that is already shown.
pub fn show(app: &AppHandle, running: bool) {
    let Some(menus) = app.try_state::<ServerMenus>() else {
        return;
    };
    let dark = dark(app);
    {
        let Ok(mut shown) = menus.shown.lock() else {
            return;
        };
        if *shown == Some((running, dark)) {
            return;
        }
        *shown = Some((running, dark));
    }
    for item in &menus.items {
        let _ = item.set_enabled(enabled(item.id().as_ref(), running));
    }
    if let Some(tray) = app.tray_by_id(TRAY) {
        let _ = tray.set_icon(Some(status_icon(
            &menus.base,
            running,
            macos_menu_bar(dark),
        )));
        let _ = tray.set_tooltip(Some(if running {
            "Hardcharger — server running"
        } else {
            "Hardcharger — server stopped"
        }));
    }
}

/// Redraws the icon for the current appearance (the system's light or dark mode changed).
pub fn redraw(app: &AppHandle) {
    let shown = app
        .try_state::<ServerMenus>()
        .and_then(|menus| menus.shown.lock().ok().and_then(|shown| *shown));
    if let Some((running, _)) = shown {
        show(app, running);
    }
}

/// Follows a server that stops on its own (an exit, a crash) within a second. It skips a beat
/// while a start or stop holds the server, which updates the menus itself when it finishes.
pub fn watch(app: AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_secs(1));
        let state = app.state::<crate::Shared>();
        let running = match state.try_lock() {
            Ok(mut server) => server.status().map(|s| s.running).ok(),
            Err(_) => None,
        };
        if let Some(running) = running {
            show(&app, running);
        }
    });
}

/// On macOS, the colour to draw the bull in for the menu bar; elsewhere the icon keeps its own.
fn macos_menu_bar(dark: bool) -> Option<[u8; 3]> {
    if !cfg!(target_os = "macos") {
        return None;
    }
    Some(if dark { [255, 255, 255] } else { [0, 0, 0] })
}

/// The icon with a status dot in its lower right corner, separated from the bull by a
/// transparent ring. `foreground` recolours the bull (macOS); `None` keeps its colours.
pub fn status_icon(base: &Image<'_>, running: bool, foreground: Option<[u8; 3]>) -> Image<'static> {
    let (width, height) = (base.width(), base.height());
    let mut rgba = base.rgba().to_vec();
    if let Some([r, g, b]) = foreground {
        for pixel in rgba.chunks_exact_mut(4) {
            pixel[..3].copy_from_slice(&[r, g, b]);
        }
    }
    let size = width.min(height) as f32;
    let radius = size * 0.2;
    let gap = (size * 0.06).max(1.0);
    let (cx, cy) = (width as f32 - radius - 0.5, height as f32 - radius - 0.5);
    let colour = if running { RUNNING } else { STOPPED };
    for y in 0..height {
        for x in 0..width {
            let distance = ((x as f32 + 0.5 - cx).powi(2) + (y as f32 + 0.5 - cy).powi(2)).sqrt();
            // Coverage within half a pixel of an edge, for smooth circles at small sizes.
            let inside = (radius - distance + 0.5).clamp(0.0, 1.0);
            let ring = (radius + gap - distance + 0.5).clamp(0.0, 1.0);
            if ring == 0.0 {
                continue;
            }
            let pixel = &mut rgba[((y * width + x) * 4) as usize..][..4];
            let behind = pixel[3] as f32 * (1.0 - ring);
            let alpha = inside * 255.0 + behind * (1.0 - inside);
            for channel in 0..3 {
                let mixed = colour[channel] as f32 * inside * 255.0
                    + pixel[channel] as f32 * behind * (1.0 - inside);
                pixel[channel] = if alpha > 0.0 {
                    (mixed / alpha).round() as u8
                } else {
                    0
                };
            }
            pixel[3] = alpha.round() as u8;
        }
    }
    Image::new_owned(rgba, width, height)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn start_only_while_stopped_and_the_rest_only_while_running() {
        for (id, running, expected) in [
            ("start", false, true),
            ("start", true, false),
            ("stop", true, true),
            ("stop", false, false),
            ("restart", true, true),
            ("restart", false, false),
            ("workspace", true, true),
            ("workspace", false, false),
            ("show", false, true),
            ("quit", true, true),
        ] {
            assert_eq!(
                enabled(id, running),
                expected,
                "{id} with running={running}"
            );
        }
    }

    fn pixel(image: &Image<'_>, x: u32, y: u32) -> [u8; 4] {
        let at = ((y * image.width() + x) * 4) as usize;
        image.rgba()[at..at + 4].try_into().unwrap()
    }

    #[test]
    fn the_dot_is_green_or_red_and_separated_from_the_bull() {
        // An opaque white 32x32 square stands in for the bull.
        let base = Image::new_owned(vec![255; 32 * 32 * 4], 32, 32);
        let running = status_icon(&base, true, Some([0, 0, 0]));
        let stopped = status_icon(&base, false, None);
        // The dot's centre: (32 - 6.4 - 0.5) = 25.1.
        assert_eq!(pixel(&running, 25, 25), [0x30, 0xd1, 0x58, 255]);
        assert_eq!(pixel(&stopped, 25, 25), [0xff, 0x45, 0x3a, 255]);
        // Just outside the dot, the ring is transparent.
        assert_eq!(pixel(&running, 25, 17)[3], 0, "ring above the dot");
        // Elsewhere, the bull: recoloured for a light menu bar, or left as it was.
        assert_eq!(pixel(&running, 4, 4), [0, 0, 0, 255]);
        assert_eq!(pixel(&stopped, 4, 4), [255, 255, 255, 255]);
    }
}
