//! Text size: zoom every window (the dashboard, the SQL workspace, query details) as Safari
//! does, with one level for the whole app that is remembered across launches (#55).
//!
//! The level is a page zoom (`WebviewWindow::set_zoom`, WKWebView's pageZoom on macOS,
//! WebView2's ZoomFactor on Windows): the layout scales with the text, so no pixel sizes
//! needed converting. It is kept in `view.json` in the app's data folder (a desktop
//! preference, like launch at login; never server YAML) and applied to each window as it opens.
//!
//! On macOS the shortcuts are caught before any window sees them (an NSEvent local monitor,
//! `install_key_monitor`), so they work the same with focus in the SQL editor or any other text
//! field, type nothing there, and follow the keyboard layout: ⌘= and ⌘⇧= (US), ⌘+ on layouts
//! with a + key (Dutch, German), ⌘− and the keypad's + and −, ⌘0. The View menu shows them and
//! does the same from the mouse; elsewhere its accelerators are the shortcuts.
//!
//! macOS's own Text size (System Settings > Accessibility > Display) is not followed: AppKit
//! offers apps no way to read it (it reaches only apps that adopt Dynamic Type through Apple's
//! private frameworks), and WKWebView doesn't apply it to web content. Text size in Settings is
//! the app's own control instead.

use serde::{Deserialize, Serialize};
use std::{
    path::{Path, PathBuf},
    sync::Mutex,
};
use tauri::{AppHandle, Emitter, Manager, Runtime, WebviewWindow};

/// Safari's zoom steps.
pub const LEVELS: [f64; 11] = [0.5, 0.75, 0.85, 1.0, 1.15, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0];
const FILE: &str = "view.json";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    Bigger,
    Smaller,
    ActualSize,
}

#[derive(Serialize, Deserialize, Default)]
#[serde(default)]
struct Stored {
    zoom: Option<f64>,
}

/// What the dashboard's Text size control shows.
#[derive(Serialize, Clone)]
pub struct ZoomState {
    pub level: f64,
    pub levels: Vec<f64>,
}

/// The app's zoom level and where it is kept.
pub struct Zoom {
    file: PathBuf,
    level: Mutex<f64>,
}

/// The step nearest to `level` (a level saved by hand, or by a later version with other steps).
pub fn nearest(level: f64) -> f64 {
    if !level.is_finite() {
        return 1.0;
    }
    LEVELS
        .iter()
        .copied()
        .min_by(|a, b| (a - level).abs().total_cmp(&(b - level).abs()))
        .unwrap_or(1.0)
}

/// The level after `step` from `level`, as Safari steps: the next step up or down (from a
/// level between steps, the nearest one on that side), stopping at the ends; Actual Size is 100%.
pub fn stepped(level: f64, step: Step) -> f64 {
    const EPSILON: f64 = 1e-6;
    let level = if level.is_finite() { level } else { 1.0 };
    match step {
        Step::ActualSize => 1.0,
        Step::Bigger => LEVELS
            .iter()
            .copied()
            .find(|l| *l > level + EPSILON)
            .unwrap_or(LEVELS[LEVELS.len() - 1]),
        Step::Smaller => LEVELS
            .iter()
            .rev()
            .copied()
            .find(|l| *l < level - EPSILON)
            .unwrap_or(LEVELS[0]),
    }
}

impl Zoom {
    pub fn new(directory: &Path) -> Self {
        let file = directory.join(FILE);
        let level = std::fs::read(&file)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Stored>(&bytes).ok())
            .and_then(|stored| stored.zoom)
            .map(nearest)
            .unwrap_or(1.0);
        Self {
            file,
            level: Mutex::new(level),
        }
    }

    pub fn level(&self) -> f64 {
        self.level.lock().map(|l| *l).unwrap_or(1.0)
    }

    pub fn state(&self) -> ZoomState {
        ZoomState {
            level: self.level(),
            levels: LEVELS.to_vec(),
        }
    }

    /// Sets and saves the level (to the nearest step). A failed save keeps the level for this run.
    pub fn set(&self, level: f64) -> Result<f64, String> {
        let level = nearest(level);
        if let Ok(mut current) = self.level.lock() {
            *current = level;
        }
        let bytes =
            serde_json::to_vec_pretty(&Stored { zoom: Some(level) }).map_err(|e| e.to_string())?;
        crate::server::private_write(&self.file, &bytes)?;
        Ok(level)
    }
}

/// Applies the app's level to one window (called as each window opens).
pub fn apply<R: Runtime>(window: &WebviewWindow<R>) {
    let level = window.state::<Zoom>().level();
    let _ = window.set_zoom(level);
}

/// Sets the app's level, applies it to every open window and tells the dashboard.
pub fn set_level<R: Runtime>(app: &AppHandle<R>, level: f64) -> Result<ZoomState, String> {
    let zoom = app.state::<Zoom>();
    let result = zoom.set(level);
    for window in app.webview_windows().values() {
        let _ = window.set_zoom(zoom.level());
    }
    let state = zoom.state();
    let _ = app.emit_to("main", "zoom-changed", state.clone());
    result.map(|_| state)
}

/// Bigger, Smaller or Actual Size, from a shortcut or the View menu.
pub fn step<R: Runtime>(app: &AppHandle<R>, step: Step) {
    let level = stepped(app.state::<Zoom>().level(), step);
    if let Err(error) = set_level(app, level) {
        eprintln!("hardcharger-desktop: the text size could not be saved: {error}");
    }
}

/// What a key press with ⌘ means for zoom, by the character it types (layout-aware, Shift
/// included, as AppKit reports `charactersIgnoringModifiers`) or the keypad key it is.
pub fn shortcut(
    characters: &str,
    key_code: u16,
    command: bool,
    control: bool,
    option: bool,
) -> Option<Step> {
    // Virtual key codes (Carbon's kVK_*): the keypad's + and −, and the 0 key's position.
    const KEYPAD_PLUS: u16 = 0x45;
    const KEYPAD_MINUS: u16 = 0x4E;
    const ANSI_0: u16 = 0x1D;
    if !command || control || option {
        return None;
    }
    match (characters, key_code) {
        (_, KEYPAD_PLUS) | ("=", _) | ("+", _) => Some(Step::Bigger),
        (_, KEYPAD_MINUS) | ("-", _) | ("\u{2212}", _) => Some(Step::Smaller),
        ("0", _) | (_, ANSI_0) => Some(Step::ActualSize),
        _ => None,
    }
}

/// macOS: catches the zoom shortcuts for every window of the app before the window (and the
/// web page in it, the SQL editor included) sees them.
#[cfg(target_os = "macos")]
pub fn install_key_monitor(app: AppHandle) {
    use block2::RcBlock;
    use objc2::runtime::AnyObject;
    use objc2::{class, msg_send};
    use std::ffi::CStr;
    use std::os::raw::c_char;

    const KEY_DOWN_MASK: u64 = 1 << 10;
    const SHIFT: usize = 1 << 17;
    const CONTROL: usize = 1 << 18;
    const OPTION: usize = 1 << 19;
    const COMMAND: usize = 1 << 20;
    let _ = SHIFT;

    let handler = RcBlock::new(move |event: *mut AnyObject| -> *mut AnyObject {
        // SAFETY: AppKit hands the block a valid NSEvent of type keyDown for the mask below;
        // the selectors used exist on every NSEvent of that type.
        let step = unsafe {
            let Some(e) = event.as_ref() else {
                return event;
            };
            let flags: usize = msg_send![e, modifierFlags];
            let key_code: u16 = msg_send![e, keyCode];
            let characters: *mut AnyObject = msg_send![e, charactersIgnoringModifiers];
            let text = characters
                .as_ref()
                .map(|s| {
                    let utf8: *const c_char = msg_send![s, UTF8String];
                    if utf8.is_null() {
                        String::new()
                    } else {
                        CStr::from_ptr(utf8).to_string_lossy().into_owned()
                    }
                })
                .unwrap_or_default();
            let window: *mut AnyObject = msg_send![e, window];
            if window.is_null() {
                return event;
            }
            shortcut(
                &text,
                key_code,
                flags & COMMAND != 0,
                flags & CONTROL != 0,
                flags & OPTION != 0,
            )
        };
        match step {
            Some(step) => {
                self::step(&app, step);
                std::ptr::null_mut() // handled: no window, page or menu sees it
            }
            None => event,
        }
    });
    // SAFETY: called on the main thread once the app is built; the monitor (and its block,
    // which AppKit copies) lives for the rest of the process.
    unsafe {
        let monitor: *mut AnyObject = msg_send![
            class!(NSEvent),
            addLocalMonitorForEventsMatchingMask: KEY_DOWN_MASK,
            handler: &*handler
        ];
        if monitor.is_null() {
            eprintln!("hardcharger-desktop: no key monitor; zoom works from the View menu only");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn steps_like_safari_and_stop_at_the_ends() {
        assert_eq!(stepped(1.0, Step::Bigger), 1.15);
        assert_eq!(stepped(1.15, Step::Bigger), 1.25);
        assert_eq!(stepped(1.0, Step::Smaller), 0.85);
        assert_eq!(stepped(3.0, Step::Bigger), 3.0);
        assert_eq!(stepped(0.5, Step::Smaller), 0.5);
        assert_eq!(stepped(2.0, Step::ActualSize), 1.0);
        // A level between steps moves to the nearest step on that side.
        assert_eq!(stepped(1.2, Step::Bigger), 1.25);
        assert_eq!(stepped(1.2, Step::Smaller), 1.15);
        assert_eq!(nearest(f64::NAN), 1.0);
        assert_eq!(nearest(9.0), 3.0);
    }

    #[test]
    fn shortcuts_follow_the_typed_character_on_every_layout() {
        let cmd = |chars: &str, code: u16| shortcut(chars, code, true, false, false);
        // US: ⌘= and ⌘⇧= (which types +) zoom in, ⌘- out, ⌘0 to actual size.
        assert_eq!(cmd("=", 0x18), Some(Step::Bigger));
        assert_eq!(cmd("+", 0x18), Some(Step::Bigger));
        assert_eq!(cmd("-", 0x1B), Some(Step::Smaller));
        assert_eq!(cmd("0", 0x1D), Some(Step::ActualSize));
        // Dutch and German: a + key of its own (another position), - and 0 where they are.
        assert_eq!(cmd("+", 0x1E), Some(Step::Bigger));
        assert_eq!(cmd("+", 0x2C), Some(Step::Bigger));
        assert_eq!(cmd("-", 0x2C), Some(Step::Smaller));
        // French: the 0 key types à without Shift; ⌘ on that key is still Actual Size.
        assert_eq!(cmd("à", 0x1D), Some(Step::ActualSize));
        // The numeric keypad's + and −, whatever the layout says they type.
        assert_eq!(cmd("+", 0x45), Some(Step::Bigger));
        assert_eq!(cmd("-", 0x4E), Some(Step::Smaller));
        // Not zoom: no ⌘, ⌃ or ⌥ with it, or another key.
        assert_eq!(shortcut("=", 0x18, false, false, false), None);
        assert_eq!(shortcut("=", 0x18, true, true, false), None);
        assert_eq!(shortcut("=", 0x18, true, false, true), None);
        assert_eq!(cmd("s", 0x01), None);
        assert_eq!(cmd("1", 0x12), None);
    }

    #[test]
    fn the_level_is_remembered_across_a_relaunch() {
        let directory = std::env::temp_dir().join(format!(
            "hardcharger-zoom-test-{}",
            crate::server::random_secret().unwrap()
        ));
        std::fs::create_dir_all(&directory).unwrap();
        assert_eq!(Zoom::new(&directory).level(), 1.0);
        let zoom = Zoom::new(&directory);
        assert_eq!(zoom.set(1.5).unwrap(), 1.5);
        assert_eq!(Zoom::new(&directory).level(), 1.5);
        assert_eq!(zoom.set(1.49).unwrap(), 1.5, "kept to a step");
        std::fs::write(directory.join(FILE), b"{not json").unwrap();
        assert_eq!(
            Zoom::new(&directory).level(),
            1.0,
            "unreadable: actual size"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }
}
