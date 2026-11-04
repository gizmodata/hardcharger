// The Dock's Quit, an AppleScript "quit", a logout and a shutdown end the app through
// NSApplication's terminate:. tao's application delegate answers it with the default
// (terminate at once) and it never becomes RunEvent::ExitRequested, so the controller's stop
// never ran and the server outlived the app (gizmodata/hardcharger#28). This adds
// applicationShouldTerminate: to that delegate: the server is stopped first, synchronously,
// then the termination goes on. The server's own backstop (its stdin pipe) covers a crash.
use std::sync::OnceLock;

use objc2::runtime::{AnyClass, AnyObject, Sel};
use objc2::{class, msg_send, sel};
use tauri::{AppHandle, Manager};

static APP: OnceLock<AppHandle> = OnceLock::new();

/// NSApplicationTerminateReply: NSTerminateNow.
const NS_TERMINATE_NOW: usize = 1;

extern "C-unwind" fn application_should_terminate(
    _this: *mut AnyObject,
    _command: Sel,
    _sender: *mut AnyObject,
) -> usize {
    if let Some(app) = APP.get() {
        let state = app.state::<crate::Shared>();
        let result = state
            .lock()
            .map_err(|_| "its controller is unavailable".to_string())
            .and_then(|mut server| server.stop().map(|_| ()));
        if let Err(error) = result {
            eprintln!("hardcharger-desktop: quitting with the server still running: {error}");
        }
    }
    NS_TERMINATE_NOW
}

/// Installs the handler on the application delegate tao created. Call once, after the
/// server controller is managed.
pub fn install(app: AppHandle) {
    let _ = APP.set(app);
    // SAFETY: NSApplication and its delegate exist once the Tauri app is built; the method
    // added has the signature AppKit expects for applicationShouldTerminate:
    // (NSApplicationTerminateReply, an NSUInteger, for one object argument).
    unsafe {
        let shared: *mut AnyObject = msg_send![class!(NSApplication), sharedApplication];
        let delegate: *mut AnyObject = msg_send![shared, delegate];
        let Some(delegate) = delegate.as_ref() else {
            eprintln!("hardcharger-desktop: no application delegate; the Dock's Quit won't stop the server first");
            return;
        };
        let class: &AnyClass = delegate.class();
        let imp: unsafe extern "C-unwind" fn() = std::mem::transmute(
            application_should_terminate
                as extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject) -> usize,
        );
        let added = objc2::ffi::class_addMethod(
            class as *const AnyClass as *mut AnyClass,
            sel!(applicationShouldTerminate:),
            imp,
            c"Q@:@".as_ptr(),
        );
        if !added.as_bool() {
            eprintln!("hardcharger-desktop: the application delegate already answers applicationShouldTerminate:; the Dock's Quit may not stop the server first");
        }
    }
}
